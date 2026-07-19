"""RED-first source-local proofs for the trusted compute production adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from capabilities.contracts import canonical_json_bytes
from compute import models
from compute.production import (
    ComputeNetworkBinding,
    LaunchReceipt,
    ProductionComputeRunner,
    StagedJob,
    TaskCompletion,
)


PINNED_DIGEST = "sha256:" + "a" * 64
TASK_DEFINITION = (
    "arn:aws:ecs:eu-west-1:123456789012:task-definition/"
    "personal-operator-compute:7"
)
NOW = 1_800_000_000
SECURITY_GROUP_ID = "sg-0123456789abcdef0"
SUBNET_IDS = ("subnet-0123456789abcdef0", "subnet-0123456789abcdef1")
NETWORK_BINDING = ComputeNetworkBinding(
    security_group_id=SECURITY_GROUP_ID,
    subnet_ids=SUBNET_IDS,
    assign_public_ip="DISABLED",
)


def test_compute_network_binding_is_exact_and_public_ip_disabled():
    from compute import production

    binding = production.ComputeNetworkBinding(
        security_group_id=SECURITY_GROUP_ID,
        subnet_ids=SUBNET_IDS,
        assign_public_ip="DISABLED",
    )
    assert binding.security_group_id == SECURITY_GROUP_ID
    assert binding.subnet_ids == SUBNET_IDS
    assert binding.assign_public_ip == "DISABLED"

    bad_bindings = (
        {
            "security_group_id": "sg-bad",
            "subnet_ids": SUBNET_IDS,
            "assign_public_ip": "DISABLED",
        },
        {
            "security_group_id": SECURITY_GROUP_ID,
            "subnet_ids": (),
            "assign_public_ip": "DISABLED",
        },
        {
            "security_group_id": SECURITY_GROUP_ID,
            "subnet_ids": (SUBNET_IDS[0], SUBNET_IDS[0]),
            "assign_public_ip": "DISABLED",
        },
        {
            "security_group_id": SECURITY_GROUP_ID,
            "subnet_ids": SUBNET_IDS,
            "assign_public_ip": "ENABLED",
        },
    )
    for values in bad_bindings:
        with pytest.raises((TypeError, ValueError)):
            production.ComputeNetworkBinding(**values)


def test_launch_receipt_attests_the_exact_network_binding():
    receipt = LaunchReceipt(
        task_ref="task_12345678",
        task_definition_arn=TASK_DEFINITION,
        image_digest=PINNED_DIGEST,
        output_namespace_id="namespace_fresh_12345678",
        network_binding=NETWORK_BINDING,
    )
    assert receipt.network_binding == NETWORK_BINDING


class RecordingStaging:
    def __init__(self, *, outputs=None):
        self.outputs = {"result.txt": b"done"} if outputs is None else outputs
        self.stage_calls = []
        self.read_calls = []
        self.discarded = []

    def stage_fresh(self, *, spec_bytes, input_files):
        self.stage_calls.append((spec_bytes, dict(input_files)))
        spec = json.loads(spec_bytes)
        return StagedJob(
            job_id=spec["jobId"],
            namespace_id="namespace_fresh_12345678",
            input_digest=models.derive_input_digest(
                [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                    for path, data in input_files.items()
                ]
            ),
        )

    def read_fresh_outputs(self, *, staged_job, task_ref, namespace_id):
        self.read_calls.append((staged_job, task_ref, namespace_id))
        return dict(self.outputs)

    def discard(self, staged_job):
        self.discarded.append(staged_job)


class RecordingLauncher:
    def __init__(self, *, receipt=None, completion=None, timeout=False):
        self.receipt = receipt or LaunchReceipt(
            task_ref="task_12345678",
            task_definition_arn=TASK_DEFINITION,
            image_digest=PINNED_DIGEST,
            output_namespace_id="namespace_fresh_12345678",
            network_binding=NETWORK_BINDING,
        )
        self.completion = completion or TaskCompletion(
            status="SUCCEEDED",
            started_at=NOW,
            completed_at=NOW + 1,
            output_namespace_id="namespace_fresh_12345678",
            error_code=None,
        )
        self.timeout = timeout
        self.launch_calls = []
        self.wait_calls = []
        self.terminated = []

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.receipt

    def wait(self, task_ref, *, deadline):
        self.wait_calls.append((task_ref, deadline))
        if self.timeout:
            raise TimeoutError("synthetic deadline")
        return self.completion

    def terminate_tree(self, task_ref):
        self.terminated.append(task_ref)


def _spec_and_input(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True)
    data = b"alpha"
    (input_dir / "in.txt").write_bytes(data)
    spec = models.build_job_spec(
        job_id="job_" + "a" * 64,
        user_id="user_alpha",
        image_digest=PINNED_DIGEST,
        command={"mode": "SCRIPT", "value": "print('safe')"},
        input_files=[
            {
                "path": "in.txt",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        ],
        profile=models.SMALL,
        now=NOW,
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return spec, input_dir, output_dir


def _runner(staging, launcher):
    return ProductionComputeRunner(
        staging=staging,
        launcher=launcher,
        task_definition_arn=TASK_DEFINITION,
        image_digest=PINNED_DIGEST,
        network_binding=NETWORK_BINDING,
        clock=lambda: NOW,
    )


def test_adapter_launches_only_with_exact_zero_egress_network_binding(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher()
    spec, input_dir, output_dir = _spec_and_input(tmp_path)
    subject = ProductionComputeRunner(
        staging=staging,
        launcher=launcher,
        task_definition_arn=TASK_DEFINITION,
        image_digest=PINNED_DIGEST,
        network_binding=NETWORK_BINDING,
        clock=lambda: NOW,
    )

    result = subject.run(spec=spec, input_dir=input_dir, output_dir=output_dir)

    assert result.breach is None
    assert launcher.launch_calls[0]["network_binding"] == NETWORK_BINDING


def test_adapter_hash_binds_inputs_and_launches_only_exact_task_and_image(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher()
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is None
    assert staging.stage_calls == [
        (canonical_json_bytes(spec.to_mapping()), {"in.txt": b"alpha"})
    ]
    assert launcher.launch_calls == [
        {
            "task_definition_arn": TASK_DEFINITION,
            "image_digest": PINNED_DIGEST,
            "staged_job": staging.stage_calls and staging.discarded[0],
            "deadline": spec.deadline,
            "network": "NONE",
            "network_binding": NETWORK_BINDING,
        }
    ]
    assert (output_dir / "result.txt").read_bytes() == b"done"
    assert staging.read_calls[0][2] == "namespace_fresh_12345678"
    assert staging.discarded


def test_adapter_refuses_task_definition_or_image_attestation_drift(tmp_path):
    for receipt in (
        LaunchReceipt(
            task_ref="task_12345678",
            task_definition_arn=TASK_DEFINITION.replace(":7", ":8"),
            image_digest=PINNED_DIGEST,
            output_namespace_id="namespace_fresh_12345678",
            network_binding=NETWORK_BINDING,
        ),
        LaunchReceipt(
            task_ref="task_12345678",
            task_definition_arn=TASK_DEFINITION,
            image_digest="sha256:" + "b" * 64,
            output_namespace_id="namespace_fresh_12345678",
            network_binding=NETWORK_BINDING,
        ),
    ):
        staging = RecordingStaging()
        launcher = RecordingLauncher(receipt=receipt)
        spec, input_dir, output_dir = _spec_and_input(tmp_path / receipt.image_digest[-1])

        result = _runner(staging, launcher).run(
            spec=spec, input_dir=input_dir, output_dir=output_dir
        )

        assert result.breach is not None
        assert result.breach.error_code == "COMPUTE_LAUNCH_ATTESTATION_MISMATCH"
        assert launcher.terminated == ["task_12345678"]
        assert staging.read_calls == []
        assert list(output_dir.iterdir()) == []


def test_adapter_refuses_network_binding_attestation_drift(tmp_path):
    drifted_binding = ComputeNetworkBinding(
        security_group_id="sg-1123456789abcdef0",
        subnet_ids=SUBNET_IDS,
        assign_public_ip="DISABLED",
    )
    launcher = RecordingLauncher(
        receipt=LaunchReceipt(
            task_ref="task_12345678",
            task_definition_arn=TASK_DEFINITION,
            image_digest=PINNED_DIGEST,
            output_namespace_id="namespace_fresh_12345678",
            network_binding=drifted_binding,
        )
    )
    staging = RecordingStaging()
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.error_code == "COMPUTE_LAUNCH_BINDING_MISMATCH"
    assert launcher.terminated == ["task_12345678"]
    assert staging.read_calls == []
    assert list(output_dir.iterdir()) == []


def test_adapter_timeout_terminates_tree_and_never_reads_or_publishes_output(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher(timeout=True)
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.kind == "TIMEOUT"
    assert launcher.terminated == ["task_12345678"]
    assert staging.read_calls == []
    assert list(output_dir.iterdir()) == []
    assert staging.discarded


def test_adapter_rejects_stale_output_namespace_before_read_or_import(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher(
        completion=TaskCompletion(
            status="SUCCEEDED",
            started_at=NOW,
            completed_at=NOW + 1,
            output_namespace_id="namespace_stale_12345678",
            error_code=None,
        )
    )
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.error_code == "COMPUTE_OUTPUT_NAMESPACE_MISMATCH"
    assert staging.read_calls == []
    assert list(output_dir.iterdir()) == []


def test_adapter_validates_fresh_outputs_before_publishing(tmp_path):
    staging = RecordingStaging(outputs={"../escape.txt": b"hostile"})
    launcher = RecordingLauncher()
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.error_code == "COMPUTE_OUTPUT_REJECTED"
    assert list(output_dir.iterdir()) == []
    assert not (tmp_path / "escape.txt").exists()


def test_adapter_rejects_changed_or_extra_staged_inputs_before_launch(tmp_path):
    for mutate in ("changed", "extra"):
        staging = RecordingStaging()
        launcher = RecordingLauncher()
        spec, input_dir, output_dir = _spec_and_input(tmp_path / mutate)
        if mutate == "changed":
            (input_dir / "in.txt").write_bytes(b"changed")
        else:
            (input_dir / "extra.txt").write_bytes(b"extra")

        result = _runner(staging, launcher).run(
            spec=spec, input_dir=input_dir, output_dir=output_dir
        )

        assert result.breach is not None
        assert result.breach.error_code == "COMPUTE_INPUT_BINDING_MISMATCH"
        assert staging.stage_calls == []
        assert launcher.launch_calls == []
        assert list(output_dir.iterdir()) == []


def test_adapter_rejects_a_symlinked_input_root_before_staging(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher()
    spec, input_dir, output_dir = _spec_and_input(tmp_path)
    alias = tmp_path / "input-alias"
    alias.symlink_to(input_dir, target_is_directory=True)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=alias, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.error_code == "COMPUTE_INPUT_BINDING_MISMATCH"
    assert staging.stage_calls == []
    assert launcher.launch_calls == []


def test_adapter_treats_success_observed_after_bound_deadline_as_timeout(tmp_path):
    staging = RecordingStaging()
    launcher = RecordingLauncher(
        completion=TaskCompletion(
            status="SUCCEEDED",
            started_at=NOW,
            completed_at=NOW + models.SMALL.deadline_seconds + 1,
            output_namespace_id="namespace_fresh_12345678",
            error_code=None,
        )
    )
    spec, input_dir, output_dir = _spec_and_input(tmp_path)

    result = _runner(staging, launcher).run(
        spec=spec, input_dir=input_dir, output_dir=output_dir
    )

    assert result.breach is not None
    assert result.breach.kind == "TIMEOUT"
    assert result.breach.error_code == "COMPUTE_DEADLINE_EXCEEDED"
    assert launcher.terminated == ["task_12345678"]
    assert staging.read_calls == []
    assert list(output_dir.iterdir()) == []


def test_adapter_outputs_cross_the_real_service_importer_before_store_commit(tmp_path):
    from compute.test_service import _admitted, _run_args, _service

    staging = RecordingStaging(outputs={"nested/result.txt": b"validated"})
    service = _service(
        tmp_path,
        runner_obj=_runner(staging, RecordingLauncher()),
    )

    outcome = service.run(_admitted(_run_args()))

    receipt = service._receipt_store.get_receipt(
        "user_alpha", outcome.data["jobId"]
    )
    assert receipt.status == "SUCCEEDED"
    assert service._output_store.objects == {
        f"user_alpha/jobs/{outcome.data['jobId']}/nested/result.txt": b"validated"
    }
