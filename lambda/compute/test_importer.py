"""RED-first hostile tests for validated, atomic compute output import."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from capabilities.contracts import ComputeReceiptV1

from compute import importer, models

PINNED_DIGEST = "sha256:" + "a" * 64


def _write(root: Path, rel: str, data: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


class FakeOutputStore:
    """All-or-nothing content namespace under jobs/<jobId>/ with fault injection."""

    def __init__(self, *, fail_after: int | None = None):
        self.objects: dict[str, bytes] = {}
        self.fail_after = fail_after

    def commit_job(self, user_id: str, job_id: str, files: dict[str, bytes]) -> None:
        staged: dict[str, bytes] = {}
        written = 0
        for path, data in sorted(files.items()):
            if self.fail_after is not None and written >= self.fail_after:
                # A mid-import failure must leave NO partial objects behind.
                raise OSError("synthetic output-store loss")
            staged[f"{user_id}/jobs/{job_id}/{path}"] = data
            written += 1
        self.objects.update(staged)


class FakeReceiptStore:
    def __init__(self):
        self.receipts: dict[tuple[str, str], ComputeReceiptV1] = {}

    def put_receipt(self, user_id: str, receipt: ComputeReceiptV1) -> str:
        self.receipts[(user_id, receipt.job_id)] = receipt
        from capabilities.contracts import canonical_sha256

        return "receipt_" + canonical_sha256(receipt.to_mapping())

    def get_receipt(self, user_id: str, job_id: str):
        return self.receipts.get((user_id, job_id))


def test_collect_rejects_a_symlink_output_entry(tmp_path):
    out = tmp_path / "out"
    _write(out, "real.txt", b"real")
    os.symlink(out / "real.txt", out / "link.txt")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_a_hardlinked_output_entry(tmp_path):
    out = tmp_path / "out"
    original = _write(out, "a.txt", b"data")
    os.link(original, out / "b.txt")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_device_fifo_and_socket_nodes(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    os.mkfifo(out / "pipe")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)

    # A bound unix socket node is a non-regular file and must also be refused.
    # The AF_UNIX path is length bounded, so bind from a short cwd-relative name.
    sock_dir = tempfile.mkdtemp(prefix="s")
    prior = os.getcwd()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.chdir(sock_dir)
        server.bind("s.sock")
        with pytest.raises(Exception):
            importer.collect_outputs(sock_dir, models.SMALL)
    finally:
        os.chdir(prior)
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_collect_rejects_a_control_char_output_path(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    # A control character in a name is not a safe relative path; _safe_path
    # rejects it before any content is imported.
    hostile = out / "bad\nname.txt"
    hostile.write_bytes(b"x")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_a_file_resolving_outside_the_root(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    # A directory symlink lets a regular file resolve outside the fresh root.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leak.txt").write_bytes(b"secret")
    os.symlink(outside, out / "escape")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_single_file_over_the_profile_cap(tmp_path):
    out = tmp_path / "out"
    _write(out, "big.bin", b"x" * (models.SMALL.max_output_file_bytes + 1))
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_total_bytes_over_the_profile_cap(tmp_path):
    out = tmp_path / "out"
    chunk = b"y" * models.SMALL.max_output_file_bytes
    count = (models.SMALL.max_output_total_bytes // models.SMALL.max_output_file_bytes) + 1
    for index in range(count + 1):
        _write(out, f"f{index}.bin", chunk)
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_more_files_than_the_profile_cap(tmp_path):
    out = tmp_path / "out"
    for index in range(models.SMALL.max_output_files + 1):
        _write(out, f"f{index}.txt", b"z")
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_rejects_output_mutated_between_read_and_restat(tmp_path, monkeypatch):
    out = tmp_path / "out"
    victim = _write(out, "c.txt", b"stable")

    original = importer._read_bytes

    def mutating_read(path):
        data = original(path)
        if Path(path) == victim:
            # A concurrent writer appends after the trusted read.
            with open(path, "ab") as handle:
                handle.write(b"tampered")
        return data

    monkeypatch.setattr(importer, "_read_bytes", mutating_read)
    with pytest.raises(Exception):
        importer.collect_outputs(out, models.SMALL)


def test_collect_returns_sorted_unique_content_addressed_records(tmp_path):
    out = tmp_path / "out"
    _write(out, "b.txt", b"beta")
    _write(out, "a.txt", b"alpha")
    records, blobs = importer.collect_outputs(out, models.SMALL)
    paths = [record["path"] for record in records]
    assert paths == sorted(paths) == ["a.txt", "b.txt"]
    import hashlib

    assert records[0]["sha256"] == hashlib.sha256(b"alpha").hexdigest()
    assert records[0]["size"] == len(b"alpha")
    assert blobs["a.txt"] == b"alpha"


def test_import_writes_only_under_job_prefix_and_issues_receipt(tmp_path):
    out = tmp_path / "out"
    _write(out, "result.txt", b"ok")
    records, blobs = importer.collect_outputs(out, models.SMALL)
    output_store = FakeOutputStore()
    receipt_store = FakeReceiptStore()
    spec = models.build_job_spec(
        job_id="job_" + "a" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={"mode": "ARGV", "value": ["python", "job.py"]},
        input_files=[{"path": "in/x", "sha256": "1" * 64, "size": 1}],
        profile=models.SMALL,
        now=1_800_000_000,
    )
    receipt_ref = importer.import_success(
        output_store=output_store,
        receipt_store=receipt_store,
        spec=spec,
        records=records,
        blobs=blobs,
        input_digest="1" * 64,
        started_at=1_800_000_000,
        completed_at=1_800_000_005,
    )
    assert receipt_ref.startswith("receipt_")
    assert set(output_store.objects) == {"user_alpha/jobs/job_" + "a" * 64 + "/result.txt"}
    stored = receipt_store.get_receipt("user_alpha", "job_" + "a" * 64)
    assert stored.status == "SUCCEEDED"
    assert stored.error_code is None
    assert stored.to_mapping()["outputFiles"] == records
    assert stored.image_digest == PINNED_DIGEST


def test_import_failure_leaves_no_partial_objects_or_receipt(tmp_path):
    out = tmp_path / "out"
    _write(out, "a.txt", b"one")
    _write(out, "b.txt", b"two")
    records, blobs = importer.collect_outputs(out, models.SMALL)
    output_store = FakeOutputStore(fail_after=1)
    receipt_store = FakeReceiptStore()
    spec = models.build_job_spec(
        job_id="job_" + "b" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={"mode": "ARGV", "value": ["python", "job.py"]},
        input_files=[{"path": "in/x", "sha256": "1" * 64, "size": 1}],
        profile=models.SMALL,
        now=1_800_000_000,
    )
    with pytest.raises(Exception):
        importer.import_success(
            output_store=output_store,
            receipt_store=receipt_store,
            spec=spec,
            records=records,
            blobs=blobs,
            input_digest="1" * 64,
            started_at=1_800_000_000,
            completed_at=1_800_000_005,
        )
    assert output_store.objects == {}
    assert receipt_store.get_receipt("user_alpha", "job_" + "b" * 64) is None
