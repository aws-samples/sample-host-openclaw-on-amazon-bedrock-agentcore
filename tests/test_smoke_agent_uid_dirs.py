"""Offline tests for the host preflight in scripts/smoke-agent-uid-dirs.sh.

The script runs against a fake docker executable that records each container
run and answers the preflight with a canned kernel version and openat2 state,
so no image, Docker daemon or network is needed. The real image checks are
exercised by running the script itself against a built image.

    pytest tests/test_smoke_agent_uid_dirs.py -v
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "smoke-agent-uid-dirs.sh"

_FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "-i" in args else ""
mode = args[args.index("--") + 1] if "--" in args else ""
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps({"args": args, "mode": mode, "stdin": stdin}) + "\n")
if mode == "preflight":
    code = int(os.environ.get("FAKE_PREFLIGHT_RC", "0"))
    if code:
        sys.exit(code)
    print(os.environ["FAKE_PREFLIGHT"])
    sys.exit(0)
if mode in os.environ.get("FAKE_FAIL_MODES", "negative").split(","):
    print(f"FAIL[{mode}]: fake run")
    sys.exit(1)
print(f"PASS[{mode}]: fake run")
'''


@pytest.fixture
def env(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(f"#!{sys.executable}\n" + _FAKE_DOCKER)
    docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CALL_LOG": str(tmp_path / "calls.jsonl"),
    }
    return tmp_path, environment


def _run(environment, preflight, **extra):
    environment = {**environment, "FAKE_PREFLIGHT": preflight, **extra}
    return subprocess.run(["bash", str(_SCRIPT), "example.invalid/bridge:test"], env=environment,
                          capture_output=True, text=True, timeout=60)


def _modes(root):
    path = root / "calls.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line)["mode"] for line in lines]


@pytest.mark.parametrize("preflight, reason", [
    # CodeBuild's arm64 build hosts: kernel 4.14, no openat2.
    ("kernel=4.14.355-287.753.amzn2.aarch64\nopenat2=missing", "openat2(2) is not available"),
    # A new kernel whose seccomp profile does not allow openat2.
    ("kernel=6.12.111-136.204.amzn2023.x86_64\nopenat2=missing", "openat2(2) is not available"),
    # The probe could not run (no python3 in the image): go by the kernel version.
    ("kernel=4.14.355-287.753.amzn2.aarch64\nopenat2=unknown", "older than Linux 5.6"),
    ("kernel=5.4.0-1103-aws\nopenat2=unknown", "older than Linux 5.6"),
])
def test_unsupported_host_skips_with_a_message_and_exits_zero(env, preflight, reason):
    root, environment = env
    result = _run(environment, preflight)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("SKIP: ")
    assert reason in result.stdout
    assert "Linux 5.6 or newer" in result.stdout
    assert "PASS" not in result.stdout
    assert _modes(root) == ["preflight"]


@pytest.mark.parametrize("preflight", [
    "kernel=6.12.111-136.204.amzn2023.x86_64\nopenat2=present",
    # A kernel older than 5.6 with openat2 backported: the probe decides.
    "kernel=4.18.0-553.el8_10.x86_64\nopenat2=present",
    "kernel=5.6.0\nopenat2=unknown",
    # A version the script cannot parse does not skip.
    "kernel=custom\nopenat2=unknown",
])
def test_supported_host_runs_all_three_runs(env, preflight):
    root, environment = env
    result = _run(environment, preflight)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP" not in result.stdout
    assert _modes(root) == ["preflight", "root", "agent", "negative"]
    assert result.stdout.rstrip().endswith("PASS: all smoke runs behaved")


def test_preflight_ships_the_openat2_probe(env):
    root, environment = env
    _run(environment, "kernel=6.12.111\nopenat2=present")
    preflight = json.loads((root / "calls.jsonl").read_text().splitlines()[0])
    assert preflight["args"][:2] == ["run", "--rm"]
    assert "--network" in preflight["args"] and "none" in preflight["args"]
    assert "uname -r" in preflight["stdin"]
    assert "libc.syscall(ctypes.c_long(437)" in preflight["stdin"]


def test_failing_agent_run_still_fails_on_a_supported_host(env):
    root, environment = env
    result = _run(environment, "kernel=6.12.111\nopenat2=present", FAKE_FAIL_MODES="agent,negative")
    assert result.returncode != 0
    assert _modes(root) == ["preflight", "root", "agent"]


def test_passing_negative_control_still_fails_on_a_supported_host(env):
    root, environment = env
    result = _run(environment, "kernel=6.12.111\nopenat2=present", FAKE_FAIL_MODES="")
    assert result.returncode == 1
    assert "the negative control passed" in result.stdout
    assert _modes(root) == ["preflight", "root", "agent", "negative"]


def test_preflight_container_failure_fails_instead_of_skipping(env):
    root, environment = env
    result = _run(environment, "", FAKE_PREFLIGHT_RC="125")
    assert result.returncode != 0
    assert "SKIP" not in result.stdout
    assert _modes(root) == ["preflight"]
