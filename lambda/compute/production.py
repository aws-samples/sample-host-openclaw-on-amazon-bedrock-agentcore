"""Inactive source-local contract model for future compute task launches.

This module has no active caller, ambient AWS client, staging implementation,
launcher, or collection transport. Its seams model checks that a future
credential-free implementation would need before output publication; they are
not production authority or deployment evidence.

Real object-store, ECS/Fargate, Docker, image-scan, and run evidence remain
external gates. This module does not claim that those gates have run.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Callable, Mapping, Protocol

from capabilities.contracts import _safe_path

from . import importer, models
from .service import RunnerBreach, RunnerResult


_TASK_DEFINITION = re.compile(
    r"arn:aws:ecs:eu-west-1:[0-9]{12}:task-definition/"
    r"personal-operator-compute:[1-9][0-9]*"
)
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{7,127}")
_SECURITY_GROUP_ID = re.compile(r"sg-[0-9a-f]{8,17}")
_SUBNET_ID = re.compile(r"subnet-[0-9a-f]{8,17}")


@dataclass(frozen=True, slots=True)
class ComputeNetworkBinding:
    """Exact awsvpc launch boundary for one zero-egress compute task."""

    security_group_id: str
    subnet_ids: tuple[str, ...]
    assign_public_ip: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.security_group_id, str)
            or _SECURITY_GROUP_ID.fullmatch(self.security_group_id) is None
        ):
            raise ValueError("compute security-group identity is invalid")
        if (
            not isinstance(self.subnet_ids, tuple)
            or not self.subnet_ids
            or len(self.subnet_ids) > 16
            or len(set(self.subnet_ids)) != len(self.subnet_ids)
            or any(
                not isinstance(subnet_id, str)
                or _SUBNET_ID.fullmatch(subnet_id) is None
                for subnet_id in self.subnet_ids
            )
        ):
            raise ValueError("compute subnet binding is invalid")
        if self.assign_public_ip != "DISABLED":
            raise ValueError("compute public-IP assignment must be disabled")


@dataclass(frozen=True, slots=True)
class StagedJob:
    """One fresh, content-bound staging namespace owned by the trusted plane."""

    job_id: str
    namespace_id: str
    input_digest: str

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.job_id) is None:
            raise ValueError("staged job identity is invalid")
        if _IDENTIFIER.fullmatch(self.namespace_id) is None:
            raise ValueError("staged output namespace is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.input_digest) is None:
            raise ValueError("staged input digest is invalid")


@dataclass(frozen=True, slots=True)
class LaunchReceipt:
    """Attested identity returned immediately after the exact task is launched."""

    task_ref: str
    task_definition_arn: str
    image_digest: str
    output_namespace_id: str
    network_binding: ComputeNetworkBinding

    def __post_init__(self) -> None:
        if _IDENTIFIER.fullmatch(self.task_ref) is None:
            raise ValueError("task reference is invalid")
        if _TASK_DEFINITION.fullmatch(self.task_definition_arn) is None:
            raise ValueError("task-definition attestation is invalid")
        if _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("image attestation is invalid")
        if _IDENTIFIER.fullmatch(self.output_namespace_id) is None:
            raise ValueError("output namespace attestation is invalid")
        if not isinstance(self.network_binding, ComputeNetworkBinding):
            raise TypeError("launch receipt network attestation is invalid")


@dataclass(frozen=True, slots=True)
class TaskCompletion:
    """Terminal task observation from the injected trusted launcher."""

    status: str
    started_at: int
    completed_at: int
    output_namespace_id: str
    error_code: str | None

    def __post_init__(self) -> None:
        if self.status not in {"SUCCEEDED", "FAILED", "OOM", "PIDS", "FSIZE"}:
            raise ValueError("task completion status is invalid")
        if (
            isinstance(self.started_at, bool)
            or not isinstance(self.started_at, int)
            or isinstance(self.completed_at, bool)
            or not isinstance(self.completed_at, int)
            or self.started_at < 0
            or self.completed_at < self.started_at
        ):
            raise ValueError("task completion time is invalid")
        if _IDENTIFIER.fullmatch(self.output_namespace_id) is None:
            raise ValueError("task completion namespace is invalid")
        if self.status == "SUCCEEDED" and self.error_code is not None:
            raise ValueError("successful task cannot carry an error code")
        if self.status != "SUCCEEDED" and (
            not isinstance(self.error_code, str) or not self.error_code
        ):
            raise ValueError("failed task requires an error code")


class ComputeStaging(Protocol):
    def stage_fresh(
        self, *, spec_bytes: bytes, input_files: Mapping[str, bytes]
    ) -> StagedJob: ...

    def read_fresh_outputs(
        self, *, staged_job: StagedJob, task_ref: str, namespace_id: str
    ) -> Mapping[str, bytes]: ...

    def discard(self, staged_job: StagedJob) -> None: ...


class ExactTaskLauncher(Protocol):
    def launch(
        self,
        *,
        task_definition_arn: str,
        image_digest: str,
        staged_job: StagedJob,
        deadline: int,
        network: str,
        network_binding: ComputeNetworkBinding,
    ) -> LaunchReceipt: ...

    def wait(self, task_ref: str, *, deadline: int) -> TaskCompletion: ...

    def terminate_tree(self, task_ref: str) -> None: ...


class ProductionComputeRunner:
    """Bind trusted staging to one exact task and validated fresh outputs."""

    def __init__(
        self,
        *,
        staging: ComputeStaging,
        launcher: ExactTaskLauncher,
        task_definition_arn: str,
        image_digest: str,
        network_binding: ComputeNetworkBinding,
        clock: Callable[[], int],
    ) -> None:
        if _TASK_DEFINITION.fullmatch(task_definition_arn) is None:
            raise ValueError("production runner requires one exact task definition")
        if _IMAGE_DIGEST.fullmatch(image_digest) is None:
            raise ValueError("production runner requires one exact image digest")
        if not isinstance(network_binding, ComputeNetworkBinding):
            raise TypeError("production runner requires one exact network binding")
        if not callable(clock):
            raise TypeError("production runner requires a trusted clock")
        self._staging = staging
        self._launcher = launcher
        self._task_definition_arn = task_definition_arn
        self._image_digest = image_digest
        self._network_binding = network_binding
        self._clock = clock

    def run(self, *, spec, output_dir: Path, input_dir: Path) -> RunnerResult:
        started_at = self._clock()
        staged_job: StagedJob | None = None
        launch: LaunchReceipt | None = None
        if spec.deadline <= started_at:
            return self._breach(
                "TIMEOUT", "COMPUTE_DEADLINE_EXCEEDED", started_at, started_at
            )
        if spec.image_digest != self._image_digest or spec.network != "NONE":
            return self._breach(
                "FAILED", "COMPUTE_LAUNCH_BINDING_MISMATCH", started_at
            )

        try:
            input_files, input_digest = self._read_bound_inputs(spec, Path(input_dir))
        except Exception:
            return self._breach(
                "FAILED", "COMPUTE_INPUT_BINDING_MISMATCH", started_at
            )

        try:
            staged_job = self._staging.stage_fresh(
                spec_bytes=spec.to_bytes(), input_files=input_files
            )
            if (
                not isinstance(staged_job, StagedJob)
                or staged_job.job_id != spec.job_id
                or staged_job.input_digest != input_digest
            ):
                return self._breach(
                    "FAILED", "COMPUTE_STAGING_ATTESTATION_MISMATCH", started_at
                )

            launch = self._launcher.launch(
                task_definition_arn=self._task_definition_arn,
                image_digest=self._image_digest,
                staged_job=staged_job,
                deadline=spec.deadline,
                network="NONE",
                network_binding=self._network_binding,
            )
            if not isinstance(launch, LaunchReceipt):
                return self._breach(
                    "FAILED", "COMPUTE_LAUNCH_ATTESTATION_MISMATCH", started_at
                )
            if launch.network_binding != self._network_binding:
                self._launcher.terminate_tree(launch.task_ref)
                return self._breach(
                    "FAILED", "COMPUTE_LAUNCH_BINDING_MISMATCH", started_at
                )
            if (
                launch.task_definition_arn != self._task_definition_arn
                or launch.image_digest != self._image_digest
                or launch.output_namespace_id != staged_job.namespace_id
            ):
                self._launcher.terminate_tree(launch.task_ref)
                return self._breach(
                    "FAILED", "COMPUTE_LAUNCH_ATTESTATION_MISMATCH", started_at
                )

            try:
                completion = self._launcher.wait(
                    launch.task_ref, deadline=spec.deadline
                )
            except TimeoutError:
                self._launcher.terminate_tree(launch.task_ref)
                return self._breach(
                    "TIMEOUT", "COMPUTE_DEADLINE_EXCEEDED", started_at
                )
            if not isinstance(completion, TaskCompletion):
                self._launcher.terminate_tree(launch.task_ref)
                return self._breach(
                    "FAILED", "COMPUTE_TASK_OBSERVATION_INVALID", started_at
                )
            if completion.output_namespace_id != staged_job.namespace_id:
                self._launcher.terminate_tree(launch.task_ref)
                return self._breach(
                    "FAILED", "COMPUTE_OUTPUT_NAMESPACE_MISMATCH", started_at
                )
            if completion.completed_at > spec.deadline:
                self._launcher.terminate_tree(launch.task_ref)
                return RunnerResult(
                    breach=RunnerBreach(
                        kind="TIMEOUT", error_code="COMPUTE_DEADLINE_EXCEEDED"
                    ),
                    started_at=completion.started_at,
                    completed_at=completion.completed_at,
                )
            if completion.status != "SUCCEEDED":
                self._launcher.terminate_tree(launch.task_ref)
                kind = (
                    completion.status
                    if completion.status in {"OOM", "PIDS", "FSIZE"}
                    else "FAILED"
                )
                return RunnerResult(
                    breach=RunnerBreach(
                        kind=kind,
                        error_code=completion.error_code or "COMPUTE_TASK_FAILED",
                    ),
                    started_at=completion.started_at,
                    completed_at=completion.completed_at,
                )

            outputs = self._staging.read_fresh_outputs(
                staged_job=staged_job,
                task_ref=launch.task_ref,
                namespace_id=completion.output_namespace_id,
            )
            self._publish_validated_outputs(
                outputs=outputs,
                output_dir=Path(output_dir),
                profile=models.resolve_profile(spec.resource_profile),
                job_id=spec.job_id,
            )
            return RunnerResult(
                breach=None,
                started_at=completion.started_at,
                completed_at=completion.completed_at,
            )
        except Exception:
            if launch is not None:
                self._launcher.terminate_tree(launch.task_ref)
            return self._breach("FAILED", "COMPUTE_OUTPUT_REJECTED", started_at)
        finally:
            if staged_job is not None:
                self._staging.discard(staged_job)

    def _breach(
        self,
        kind: str,
        error_code: str,
        started_at: int,
        completed_at: int | None = None,
    ) -> RunnerResult:
        return RunnerResult(
            breach=RunnerBreach(kind=kind, error_code=error_code),
            started_at=started_at,
            completed_at=max(started_at, self._clock())
            if completed_at is None
            else completed_at,
        )

    @staticmethod
    def _read_bound_inputs(spec, input_dir: Path) -> tuple[dict[str, bytes], str]:
        if input_dir.is_symlink():
            raise ValueError("staged input root cannot be a symlink")
        root = input_dir.resolve(strict=True)
        expected = {record["path"]: record for record in spec.input_files}
        actual: dict[str, bytes] = {}
        records: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            before = path.lstat()
            if stat.S_ISDIR(before.st_mode):
                continue
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
            ):
                raise ValueError("staged input is not one regular copied file")
            data = path.read_bytes()
            after = path.lstat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("staged input changed while it was read")
            record = expected.get(relative)
            digest = hashlib.sha256(data).hexdigest()
            if record is None or record["size"] != len(data) or record["sha256"] != digest:
                raise ValueError("staged input does not match its bound manifest")
            actual[relative] = data
            records.append({"path": relative, "sha256": digest, "size": len(data)})
        if set(actual) != set(expected):
            raise ValueError("staged input inventory is incomplete")
        return actual, models.derive_input_digest(records)

    @staticmethod
    def _publish_validated_outputs(
        *, outputs: Mapping[str, bytes], output_dir: Path, profile, job_id: str
    ) -> None:
        if not isinstance(outputs, Mapping):
            raise TypeError("fresh outputs must be a mapping")
        if not output_dir.is_dir() or output_dir.is_symlink() or any(output_dir.iterdir()):
            raise ValueError("output namespace is not fresh")
        candidate = Path(
            tempfile.mkdtemp(prefix=f".{job_id}.remote-", dir=output_dir.parent)
        )
        try:
            for raw_path, raw_data in outputs.items():
                safe = _safe_path(raw_path, "computeOutput")
                if not isinstance(raw_data, (bytes, bytearray)):
                    raise TypeError("compute output is not bytes")
                target = candidate / safe
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(raw_data))
            importer.collect_outputs(candidate, profile)
            output_dir.rmdir()
            os.replace(candidate, output_dir)
        finally:
            shutil.rmtree(candidate, ignore_errors=True)


__all__ = [
    "ComputeNetworkBinding",
    "ComputeStaging",
    "ExactTaskLauncher",
    "LaunchReceipt",
    "ProductionComputeRunner",
    "StagedJob",
    "TaskCompletion",
]
