"""RED-first checks for the inactive source-local command-runner harness."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from compute import models, runner


PINNED_DIGEST = "sha256:" + "a" * 64
NOW = int(time.time())


def _spec(*, script: str, profile=models.SMALL):
    return models.build_job_spec(
        job_id="job_" + "a" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={"mode": "SCRIPT", "value": script},
        input_files=[],
        profile=profile,
        now=NOW,
    )


def test_bound_runner_executes_script_under_exact_resource_limits(tmp_path):
    script = """
import json
from pathlib import Path
import resource

limits = {
    "cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
    "memory": list(resource.getrlimit(resource.RLIMIT_AS)),
    "pids": list(resource.getrlimit(resource.RLIMIT_NPROC)),
    "fileSize": list(resource.getrlimit(resource.RLIMIT_FSIZE)),
}
Path("result.json").write_text(json.dumps(limits), encoding="utf-8")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    limits = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert limits["cpu"] == [models.SMALL.cpu_seconds, models.SMALL.cpu_seconds]
    assert limits["pids"] == [models.SMALL.pids_limit, models.SMALL.pids_limit]
    assert limits["fileSize"] == [
        models.SMALL.file_size_bytes,
        models.SMALL.file_size_bytes,
    ]
    if sys.platform == "darwin":
        assert limits["memory"] != [
            models.SMALL.memory_bytes,
            models.SMALL.memory_bytes,
        ]
    else:
        assert limits["memory"] == [
            models.SMALL.memory_bytes,
            models.SMALL.memory_bytes,
        ]


def test_bound_runner_fences_network_apis_inside_the_executed_script(tmp_path):
    script = """
from pathlib import Path
import socket

try:
    socket.create_connection(("93.184.216.34", 443), timeout=0.1)
except Exception as error:
    Path("network.txt").write_text(type(error).__name__ + ":" + str(error))
else:
    raise AssertionError("network fence was bypassed")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    assert "network" in (output_dir / "network.txt").read_text().casefold()


def test_bound_runner_refuses_fresh_child_interpreters(tmp_path):
    script = """
from pathlib import Path
import subprocess
import sys

try:
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", "raise SystemExit(0)"],
        check=False,
    )
except Exception as error:
    if type(error).__name__ != "NetworklessViolation":
        raise AssertionError(type(error).__name__) from error
    Path("process.txt").write_text(type(error).__name__, encoding="utf-8")
else:
    raise AssertionError("fresh child interpreter was allowed")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    assert (output_dir / "process.txt").read_text() == "NetworklessViolation"


def test_bound_runner_refuses_low_level_posix_process_creation(tmp_path):
    script = """
from pathlib import Path
import os
import posix
import sys

try:
    child = posix.posix_spawn(
        sys.executable,
        [sys.executable, "-I", "-S", "-c", "raise SystemExit(0)"],
        {},
    )
except Exception as error:
    if type(error).__name__ != "NetworklessViolation":
        raise AssertionError(type(error).__name__) from error
    Path("posix.txt").write_text(type(error).__name__, encoding="utf-8")
else:
    os.waitpid(child, 0)
    raise AssertionError("low-level POSIX process creation was allowed")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    assert (output_dir / "posix.txt").read_text() == "NetworklessViolation"


def test_bound_runner_refuses_local_sockets_and_descriptor_adoption(tmp_path):
    script = """
from pathlib import Path
import socket

attempts = {
    "unix": lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
    "socketpair": socket.socketpair,
    "descriptor": lambda: socket.socket(fileno=1),
}
blocked = []
for label, attempt in attempts.items():
    try:
        result = attempt()
    except Exception as error:
        if type(error).__name__ != "NetworklessViolation":
            raise AssertionError(label + ":" + type(error).__name__) from error
        blocked.append(label)
    else:
        if isinstance(result, tuple):
            for item in result:
                item.close()
        else:
            result.close()
        raise AssertionError(label + " socket creation was allowed")
Path("sockets.txt").write_text(",".join(blocked), encoding="utf-8")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    assert (output_dir / "sockets.txt").read_text() == (
        "unix,socketpair,descriptor"
    )


def test_job_visible_runner_module_retains_no_escape_handles(tmp_path):
    script = """
from pathlib import Path
import __main__ as runner

retained = sorted(name for name in vars(runner) if name.startswith("_ORIGINAL_"))
if retained:
    raise AssertionError("retained authority:" + ",".join(retained))
Path("globals.txt").write_text("none", encoding="utf-8")
"""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is None
    assert (output_dir / "globals.txt").read_text() == "none"


def test_timeout_kills_job_process_and_commits_no_candidate_output(tmp_path):
    script = """
from pathlib import Path
import time

Path("partial.txt").write_text("must never publish")
time.sleep(5)
"""
    one_second = models.ResourceProfile(
        name="SMALL",
        deadline_seconds=1,
        cpu_seconds=models.SMALL.cpu_seconds,
        memory_bytes=models.SMALL.memory_bytes,
        pids_limit=1024,
        file_size_bytes=models.SMALL.file_size_bytes,
        max_output_files=models.SMALL.max_output_files,
        max_output_file_bytes=models.SMALL.max_output_file_bytes,
        max_output_total_bytes=models.SMALL.max_output_total_bytes,
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script, profile=one_second),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
        profile=one_second,
    )

    assert result.breach is not None
    assert result.breach.kind == "TIMEOUT"
    assert list(output_dir.iterdir()) == []


def test_nonzero_exit_discards_every_partial_output(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    script = """
from pathlib import Path
Path("partial.txt").write_text("must never publish")
raise RuntimeError("synthetic failure")
"""

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=_spec(script=script),
        input_dir=tmp_path / "input",
        output_dir=output_dir,
    )

    assert result.breach is not None
    assert result.breach.kind == "FAILED"
    assert list(output_dir.iterdir()) == []


def test_argv_mode_executes_only_a_python_file_from_the_bound_input_tree(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    job = input_dir / "job.py"
    job.write_text(
        "from pathlib import Path\nPath('argv.txt').write_text('ran')\n",
        encoding="utf-8",
    )
    payload = job.read_bytes()
    import hashlib

    spec = models.build_job_spec(
        job_id="job_" + "b" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={"mode": "ARGV", "value": ["python", "job.py"]},
        input_files=[
            {
                "path": "job.py",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        ],
        profile=models.SMALL,
        now=NOW,
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = runner.LocalProcessRunner(clock=lambda: NOW).run(
        spec=spec,
        input_dir=input_dir,
        output_dir=output_dir,
    )

    assert result.breach is None
    assert (output_dir / "argv.txt").read_text(encoding="utf-8") == "ran"
    assert job.read_bytes() == payload


def test_runner_never_mutates_the_read_only_bound_input_tree(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    input_dir.chmod(0o555)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    before = sorted(path.name for path in input_dir.iterdir())

    try:
        result = runner.LocalProcessRunner(clock=lambda: NOW).run(
            spec=_spec(
                script=(
                    "from pathlib import Path; "
                    "Path('read-only-input.txt').write_text('ran')"
                )
            ),
            input_dir=input_dir,
            output_dir=output_dir,
        )
    finally:
        input_dir.chmod(0o755)

    assert result.breach is None
    assert sorted(path.name for path in input_dir.iterdir()) == before
    assert (output_dir / "read-only-input.txt").read_text() == "ran"


def test_container_entrypoint_executes_the_bound_synthetic_command(tmp_path):
    now = int(time.time())
    spec = models.build_job_spec(
        job_id="job_" + "c" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={
            "mode": "SCRIPT",
            "value": (
                "from pathlib import Path; "
                "Path('entrypoint.txt').write_text('executed')"
            ),
        },
        input_files=[],
        profile=models.SMALL,
        now=now,
    )
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "spec.json").write_bytes(spec.to_bytes())
    output_dir = tmp_path / "output"
    root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, str(root / "compute" / "runner.py"), str(input_dir), str(output_dir)],
        check=False,
        capture_output=True,
        timeout=models.SMALL.deadline_seconds,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert (output_dir / "entrypoint.txt").read_text() == "executed"
