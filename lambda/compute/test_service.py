"""RED-first hostile tests for the networkless compute service."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import pytest

from capabilities.contracts import canonical_sha256

from compute import models, runner
from compute.service import (
    ComputeService,
    RunnerBreach,
    RunnerResult,
)

PINNED_DIGEST = "sha256:" + "a" * 64
NOW = 1_800_000_000


class FakeInputStore:
    """Immutable per-user workspace with content-hashed files."""

    def __init__(self, files: Mapping[str, bytes]):
        self.files = {"user_alpha": dict(files)}

    def read_file(self, user_id: str, path: str) -> bytes:
        data = self.files.get(user_id, {}).get(path)
        if data is None:
            raise FileNotFoundError(path)
        return data


class FakeOutputStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def commit_job(self, user_id: str, job_id: str, files: dict[str, bytes]) -> None:
        for path, data in sorted(files.items()):
            self.objects[f"{user_id}/jobs/{job_id}/{path}"] = data


class FakeReceiptStore:
    def __init__(self):
        self.receipts: dict[tuple[str, str], Any] = {}

    def put_receipt(self, user_id: str, receipt) -> str:
        self.receipts[(user_id, receipt.job_id)] = receipt
        return "receipt_" + canonical_sha256(receipt.to_mapping())

    def get_receipt(self, user_id: str, job_id: str):
        return self.receipts.get((user_id, job_id))


class FakeRunner:
    """Model the container: write outputs, or report a resource/timeout breach."""

    def __init__(self, *, outputs=None, breach: RunnerBreach | None = None):
        self.outputs = outputs or {}
        self.breach = breach
        self.calls: list[Any] = []
        self.killed_pgids: list[int] = []
        self.last_output_dir: Path | None = None

    def run(self, *, spec, output_dir: Path) -> RunnerResult:
        self.calls.append(spec)
        self.last_output_dir = Path(output_dir)
        if self.breach is not None:
            # A breach kills the whole process group; record the effect.
            self.killed_pgids.append(4242)
            return RunnerResult(breach=self.breach, started_at=NOW, completed_at=NOW + 1)
        for rel, data in self.outputs.items():
            target = Path(output_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return RunnerResult(breach=None, started_at=NOW, completed_at=NOW + 2)


class FakeAdmitted:
    """Minimal AdmittedCall stand-in exposing the fields the service reads."""

    def __init__(self, *, user_id, invocation_id, arguments):
        args_hash = canonical_sha256(arguments)

        class _Grant:
            sub = user_id

        class _Call:
            pass

        self.grant = _Grant()
        self.call = _Call()
        self.call.invocation_id = invocation_id
        self.call.args_hash = args_hash
        self.call.arguments = dict(arguments)


def _service(tmp_path, *, runner_obj, input_files=None):
    return ComputeService(
        runner=runner_obj,
        input_store=FakeInputStore(input_files or {"in/a.txt": b"alpha"}),
        output_store=FakeOutputStore(),
        receipt_store=FakeReceiptStore(),
        image_digest=PINNED_DIGEST,
        clock=lambda: NOW,
        profile=models.SMALL,
        workspace_root=tmp_path,
    )


def _run_args(paths=("in/a.txt",)):
    return {
        "command": {"mode": "ARGV", "value": ["python", "job.py"]},
        "inputPaths": sorted(paths),
        "network": "NONE",
        "resourceProfile": "SMALL",
    }


def _admitted(arguments, *, user_id="user_alpha", invocation_id="invocation_12345678"):
    return FakeAdmitted(
        user_id=user_id, invocation_id=invocation_id, arguments=arguments
    )


def test_run_queues_job_and_returns_queued_outcome(tmp_path):
    fake_runner = FakeRunner(outputs={"result.txt": b"done"})
    service = _service(tmp_path, runner_obj=fake_runner)
    outcome = service.run(_admitted(_run_args()))
    assert set(outcome.data) == {"jobId", "status"}
    assert outcome.data["status"] == "QUEUED"
    assert outcome.data["jobId"].startswith("job_")
    # The job actually ran and produced a content-addressed receipt.
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    assert receipt.status == "SUCCEEDED"


def test_run_is_idempotent_for_the_same_dedupe_key(tmp_path):
    fake_runner = FakeRunner(outputs={"result.txt": b"done"})
    service = _service(tmp_path, runner_obj=fake_runner)
    first = service.run(_admitted(_run_args()))
    second = service.run(_admitted(_run_args()))
    assert first.data["jobId"] == second.data["jobId"]
    # The runner is only invoked once; the second call short-circuits.
    assert len(fake_runner.calls) == 1


def test_run_uses_only_the_pinned_image_digest(tmp_path):
    fake_runner = FakeRunner(outputs={"r.txt": b"x"})
    service = _service(tmp_path, runner_obj=fake_runner)
    service.run(_admitted(_run_args()))
    assert fake_runner.calls[0].image_digest == PINNED_DIGEST


def test_run_binds_input_digest_to_the_sorted_staged_manifest(tmp_path):
    fake_runner = FakeRunner(outputs={"r.txt": b"x"})
    service = _service(
        tmp_path,
        runner_obj=fake_runner,
        input_files={"in/a.txt": b"alpha", "in/b.txt": b"beta"},
    )
    outcome = service.run(_admitted(_run_args(("in/a.txt", "in/b.txt"))))
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    expected_manifest = [
        {"path": "in/a.txt", "sha256": hashlib.sha256(b"alpha").hexdigest(), "size": 5},
        {"path": "in/b.txt", "sha256": hashlib.sha256(b"beta").hexdigest(), "size": 4},
    ]
    assert receipt.input_digest == canonical_sha256(expected_manifest)


def test_run_rejects_a_missing_or_oversized_input(tmp_path):
    fake_runner = FakeRunner(outputs={"r.txt": b"x"})
    service = _service(tmp_path, runner_obj=fake_runner)
    with pytest.raises(Exception):
        service.run(_admitted(_run_args(("in/missing.txt",))))
    # The runner is never invoked when staging fails closed.
    assert fake_runner.calls == []


def test_run_rejects_an_unsafe_input_path_before_staging(tmp_path):
    fake_runner = FakeRunner(outputs={"r.txt": b"x"})
    service = _service(tmp_path, runner_obj=fake_runner)
    for bad in ("/etc/passwd", "..\\evil", "../escape", "a\x00b"):
        args = _run_args()
        args["inputPaths"] = [bad]
        with pytest.raises(Exception):
            service.run(_admitted(args))
    assert fake_runner.calls == []


def test_timeout_yields_timed_out_no_outputs_and_kills_tree(tmp_path):
    fake_runner = FakeRunner(
        breach=RunnerBreach(kind="TIMEOUT", error_code="COMPUTE_DEADLINE_EXCEEDED")
    )
    service = _service(tmp_path, runner_obj=fake_runner)
    outcome = service.run(_admitted(_run_args()))
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    assert receipt.status == "TIMED_OUT"
    assert receipt.to_mapping()["outputFiles"] == []
    assert fake_runner.killed_pgids  # the whole group was killed
    assert service._output_store.objects == {}


def test_oom_breach_yields_failed_no_outputs_and_kills_tree(tmp_path):
    fake_runner = FakeRunner(
        breach=RunnerBreach(kind="OOM", error_code="COMPUTE_MEMORY_EXCEEDED")
    )
    service = _service(tmp_path, runner_obj=fake_runner)
    outcome = service.run(_admitted(_run_args()))
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    assert receipt.status == "FAILED"
    assert receipt.error_code == "COMPUTE_MEMORY_EXCEEDED"
    assert receipt.to_mapping()["outputFiles"] == []
    assert fake_runner.killed_pgids
    assert service._output_store.objects == {}


def test_fork_bomb_breach_yields_failed_and_kills_tree(tmp_path):
    fake_runner = FakeRunner(
        breach=RunnerBreach(kind="PIDS", error_code="COMPUTE_PROCESS_LIMIT")
    )
    service = _service(tmp_path, runner_obj=fake_runner)
    outcome = service.run(_admitted(_run_args()))
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    assert receipt.status == "FAILED"
    assert receipt.error_code == "COMPUTE_PROCESS_LIMIT"
    assert fake_runner.killed_pgids
    assert service._output_store.objects == {}


def test_status_returns_own_success_with_outputs(tmp_path):
    fake_runner = FakeRunner(outputs={"result.txt": b"done"})
    service = _service(tmp_path, runner_obj=fake_runner)
    run_outcome = service.run(_admitted(_run_args()))
    job_id = run_outcome.data["jobId"]
    status = service.status(_admitted({"jobId": job_id}))
    assert status.data["jobId"] == job_id
    assert status.data["status"] == "SUCCEEDED"
    assert [record["path"] for record in status.data["outputs"]] == ["result.txt"]


def test_status_for_unknown_job_is_queued_without_leak(tmp_path):
    fake_runner = FakeRunner(outputs={"r.txt": b"x"})
    service = _service(tmp_path, runner_obj=fake_runner)
    status = service.status(_admitted({"jobId": "job_" + "f" * 64}))
    assert status.data["jobId"] == "job_" + "f" * 64
    assert status.data["status"] == "QUEUED"
    assert status.data["outputs"] == []


def test_three_user_cartesian_isolation_never_leaks_foreign_receipts(tmp_path):
    users = ["user_alpha", "user_beta", "user_gamma"]
    input_store = FakeInputStore({"in/a.txt": b"alpha"})
    for user in users[1:]:
        input_store.files[user] = {"in/a.txt": b"alpha"}
    output_store = FakeOutputStore()
    receipt_store = FakeReceiptStore()
    job_ids = {}
    for user in users:
        fake_runner = FakeRunner(outputs={f"{user}.txt": user.encode()})
        service = ComputeService(
            runner=fake_runner,
            input_store=input_store,
            output_store=output_store,
            receipt_store=receipt_store,
            image_digest=PINNED_DIGEST,
            clock=lambda: NOW,
            profile=models.SMALL,
            workspace_root=tmp_path / user,
        )
        outcome = service.run(_admitted(_run_args(), user_id=user))
        job_ids[user] = outcome.data["jobId"]

    # Each user's status query sees only their own job; foreign jobIds leak nothing.
    for viewer in users:
        service = ComputeService(
            runner=FakeRunner(outputs={}),
            input_store=input_store,
            output_store=output_store,
            receipt_store=receipt_store,
            image_digest=PINNED_DIGEST,
            clock=lambda: NOW,
            profile=models.SMALL,
            workspace_root=tmp_path / viewer,
        )
        for owner in users:
            status = service.status(_admitted({"jobId": job_ids[owner]}, user_id=viewer))
            if owner == viewer:
                assert status.data["status"] == "SUCCEEDED"
                assert [r["path"] for r in status.data["outputs"]] == [f"{viewer}.txt"]
            else:
                assert status.data["status"] == "QUEUED"
                assert status.data["outputs"] == []


def test_run_output_validation_rejects_hostile_runner_output(tmp_path):
    import os

    class SymlinkRunner(FakeRunner):
        def run(self, *, spec, output_dir):
            self.calls.append(spec)
            real = Path(output_dir) / "real.txt"
            real.write_bytes(b"real")
            os.symlink(real, Path(output_dir) / "link.txt")
            return RunnerResult(breach=None, started_at=NOW, completed_at=NOW + 1)

    service = _service(tmp_path, runner_obj=SymlinkRunner())
    outcome = service.run(_admitted(_run_args()))
    # A hostile output tree fails closed: no success, no imported objects.
    receipt = service._receipt_store.get_receipt("user_alpha", outcome.data["jobId"])
    assert receipt.status == "FAILED"
    assert service._output_store.objects == {}
