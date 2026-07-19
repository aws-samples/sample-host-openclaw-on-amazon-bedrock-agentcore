"""Networkless compute service: stage, dispatch, fence, import, and report.

The service derives a content-addressed job identity (the ``DEDUPE_KEY_REQUIRED``
key), stages immutable content-hashed inputs from the requesting user's
workspace into a fresh per-job namespace, builds a validated job spec bound to
the single pinned image digest, invokes an injected runner that models the
disposable networkless container, and on success validates and atomically
imports outputs under ``jobs/<jobId>/`` while issuing a content receipt. On a
deadline or resource breach the runner kills the whole process tree and the
service records a non-success receipt with no published outputs. Status reads
are strictly scoped to the requesting user's namespace and never leak a foreign
job's data.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from capabilities.contracts import ContractValidationError, _safe_path
from capabilities.gateway import AdapterOutcome

from . import importer, models
from .models import ResourceProfile


@dataclass(frozen=True, slots=True)
class RunnerBreach:
    """A terminal resource or deadline breach reported by the runner."""

    kind: str
    error_code: str

    def __post_init__(self) -> None:
        if self.kind not in {"TIMEOUT", "OOM", "PIDS", "FSIZE", "FAILED"}:
            raise ValueError("unsupported runner breach kind")
        if not isinstance(self.error_code, str) or not self.error_code:
            raise ValueError("runner breach requires an error code")


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """The outcome of one runner invocation modeling the container."""

    breach: RunnerBreach | None
    started_at: int
    completed_at: int


class ComputeRunner(Protocol):
    def run(self, *, spec, output_dir: Path, input_dir: Path) -> RunnerResult: ...


class ComputeServiceError(RuntimeError):
    """A compute submission failed closed before dispatch."""


_MAX_INPUT_FILE_BYTES = 1024 * 1024


class ComputeService:
    """Submit-only compute authority behind the capability gateway adapter."""

    def __init__(
        self,
        *,
        runner: ComputeRunner,
        input_store: Any,
        output_store: Any,
        receipt_store: Any,
        image_digest: str,
        clock: Callable[[], int],
        profile: ResourceProfile,
        workspace_root: Any,
    ) -> None:
        if not callable(clock):
            raise TypeError("compute service requires a trusted clock")
        if not isinstance(profile, ResourceProfile):
            raise TypeError("compute service requires a resource profile")
        self._runner = runner
        self._input_store = input_store
        self._output_store = output_store
        self._receipt_store = receipt_store
        self._image_digest = image_digest
        self._clock = clock
        self._profile = profile
        self._workspace_root = Path(workspace_root)

    # -- compute.run --------------------------------------------------------

    def run(self, admitted: Any) -> AdapterOutcome:
        user_id = admitted.grant.sub
        arguments = admitted.call.arguments
        job_id = models.derive_job_id(
            user_id=user_id,
            invocation_id=admitted.call.invocation_id,
            args_hash=admitted.call.args_hash,
        )

        # Idempotent short-circuit: an existing receipt means the job was
        # already accepted; re-derivation is byte-stable so we return QUEUED.
        if self._receipt_store.get_receipt(user_id, job_id) is not None:
            return self._queued(job_id)

        input_files, staged_blobs = self._stage_inputs(user_id, arguments["inputPaths"])
        input_digest = models.derive_input_digest(input_files)
        spec = models.build_job_spec(
            job_id=job_id,
            user_id=user_id,
            image_digest=self._image_digest,
            command=arguments["command"],
            input_files=input_files,
            profile=self._profile,
            now=self._clock(),
        )

        job_root = self._fresh_job_root(user_id, job_id)
        input_dir = job_root / "input"
        output_dir = job_root / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Materialize the immutable, content-hashed inputs into the fresh
        # per-job input directory so the networkless runner receives exactly the
        # staged bytes (and nothing from the live workspace).
        self._materialize_inputs(input_dir, staged_blobs)
        try:
            result = self._runner.run(
                spec=spec, output_dir=output_dir, input_dir=input_dir
            )
            self._finalize(spec, result, output_dir, input_digest)
        finally:
            shutil.rmtree(job_root, ignore_errors=True)
        return self._queued(job_id)

    @staticmethod
    def _materialize_inputs(input_dir: Path, staged_blobs: dict[str, bytes]) -> None:
        for relative, data in staged_blobs.items():
            # relative was already validated by _safe_path during staging.
            target = input_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o400)

    def _finalize(self, spec, result, output_dir, input_digest) -> None:
        if not isinstance(result, RunnerResult):
            raise ComputeServiceError("runner returned an untyped result")
        if result.breach is not None:
            status = "TIMED_OUT" if result.breach.kind == "TIMEOUT" else "FAILED"
            importer.issue_failure_receipt(
                receipt_store=self._receipt_store,
                spec=spec,
                status=status,
                input_digest=input_digest,
                started_at=result.started_at,
                completed_at=result.completed_at,
                error_code=result.breach.error_code,
            )
            return
        try:
            records, blobs = importer.collect_outputs(output_dir, self._profile)
            importer.import_success(
                output_store=self._output_store,
                receipt_store=self._receipt_store,
                spec=spec,
                records=records,
                blobs=blobs,
                input_digest=input_digest,
                started_at=result.started_at,
                completed_at=result.completed_at,
            )
        except Exception:
            # A hostile or mutated output tree fails closed with no objects.
            importer.issue_failure_receipt(
                receipt_store=self._receipt_store,
                spec=spec,
                status="FAILED",
                input_digest=input_digest,
                started_at=result.started_at,
                completed_at=result.completed_at,
                error_code="COMPUTE_OUTPUT_REJECTED",
            )

    def _stage_inputs(
        self, user_id: str, input_paths: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        records: list[dict[str, Any]] = []
        blobs: dict[str, bytes] = {}
        total = 0
        for raw in input_paths:
            try:
                safe = _safe_path(raw, "inputPaths")
            except ContractValidationError as error:
                raise ComputeServiceError("input path is unsafe") from error
            try:
                data = self._input_store.read_file(user_id, safe)
            except Exception as error:
                raise ComputeServiceError("input file is unavailable") from error
            if not isinstance(data, (bytes, bytearray)):
                raise ComputeServiceError("input file is not bytes")
            data = bytes(data)
            if len(data) > _MAX_INPUT_FILE_BYTES:
                raise ComputeServiceError("input file exceeds the size cap")
            total += len(data)
            if total > self._profile.max_output_total_bytes:
                raise ComputeServiceError("input set exceeds the total-bytes cap")
            records.append(
                {
                    "path": safe,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
            blobs[safe] = data
        records.sort(key=lambda record: record["path"])
        return records, blobs

    def _fresh_job_root(self, user_id: str, job_id: str) -> Path:
        root = self._workspace_root / _safe_path(user_id + "/" + job_id, "jobRoot")
        # A fresh directory per job: never reuse a prior tree.
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _queued(job_id: str) -> AdapterOutcome:
        return AdapterOutcome(
            status="SUCCEEDED",
            data={"jobId": job_id, "status": "QUEUED"},
        )

    # -- compute.status -----------------------------------------------------

    def status(self, admitted: Any) -> AdapterOutcome:
        user_id = admitted.grant.sub
        job_id = admitted.call.arguments["jobId"]
        receipt = self._receipt_store.get_receipt(user_id, job_id)
        if receipt is None or receipt.job_id != job_id:
            # Unknown or foreign job: no leak. A QUEUED placeholder never
            # reveals another user's receipt.
            return AdapterOutcome(
                status="SUCCEEDED",
                data={"jobId": job_id, "outputs": [], "status": "QUEUED"},
            )
        outputs = (
            [dict(record) for record in receipt.to_mapping()["outputFiles"]]
            if receipt.status == "SUCCEEDED"
            else []
        )
        return AdapterOutcome(
            status="SUCCEEDED",
            data={"jobId": job_id, "outputs": outputs, "status": receipt.status},
        )


class ComputeRunAdapter:
    """Capability adapter dispatching compute.run through the service."""

    def __init__(self, service: ComputeService) -> None:
        self._service = service

    def invoke(self, admitted: Any) -> AdapterOutcome:
        return self._service.run(admitted)


class ComputeStatusAdapter:
    """Capability adapter dispatching compute.status through the service."""

    def __init__(self, service: ComputeService) -> None:
        self._service = service

    def invoke(self, admitted: Any) -> AdapterOutcome:
        return self._service.status(admitted)


__all__ = [
    "ComputeRunAdapter",
    "ComputeRunner",
    "ComputeService",
    "ComputeServiceError",
    "ComputeStatusAdapter",
    "RunnerBreach",
    "RunnerResult",
]
