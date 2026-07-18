"""RED-first proof that compute wires through the gateway with no direct authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.catalog import compile_catalog
from capabilities.gateway import AdapterOutcome, CapabilityGateway
from capabilities.ledger import InMemoryCapabilityLedger
from capabilities.admission import LiveTargetGrant
from capabilities.test_gateway import (
    CALLER_ARN,
    NOW,
    FakeRepository,
    _call,
    _iam,
)

from compute import models
from compute.service import (
    ComputeRunAdapter,
    ComputeService,
    ComputeStatusAdapter,
    RunnerResult,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "specs/capabilities/schemas"
RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
PINNED_DIGEST = "sha256:" + "a" * 64


def _catalog():
    return compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)[1]


class FakeInputStore:
    def read_file(self, user_id, path):
        return b"alpha"


class FakeOutputStore:
    def __init__(self):
        self.objects = {}

    def commit_job(self, user_id, job_id, files):
        for path, data in sorted(files.items()):
            self.objects[f"{user_id}/jobs/{job_id}/{path}"] = data


class FakeReceiptStore:
    def __init__(self):
        self.receipts = {}

    def put_receipt(self, user_id, receipt):
        self.receipts[(user_id, receipt.job_id)] = receipt
        return "receipt_ref_" + receipt.job_id[4:20]

    def get_receipt(self, user_id, job_id):
        return self.receipts.get((user_id, job_id))


class FakeRunner:
    def run(self, *, spec, output_dir):
        (Path(output_dir) / "out.txt").write_bytes(b"done")
        return RunnerResult(breach=None, started_at=NOW, completed_at=NOW + 1)


def _compute_service(tmp_path):
    return ComputeService(
        runner=FakeRunner(),
        input_store=FakeInputStore(),
        output_store=FakeOutputStore(),
        receipt_store=FakeReceiptStore(),
        image_digest=PINNED_DIGEST,
        clock=lambda: NOW,
        profile=models.SMALL,
        workspace_root=tmp_path,
    )


def _gateway(tmp_path):
    catalog = _catalog()
    repository = FakeRepository(catalog, LiveTargetGrant)
    service = _compute_service(tmp_path)
    adapters = {
        "compute.run": ComputeRunAdapter(service),
        "compute.status": ComputeStatusAdapter(service),
    }
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=repository,
        ledger=InMemoryCapabilityLedger(),
        adapters=adapters,
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )
    return catalog, gateway, service


def _run_args():
    return {
        "command": {"mode": "ARGV", "value": ["python", "job.py"]},
        "inputPaths": ["in/a.txt"],
        "network": "NONE",
        "resourceProfile": "SMALL",
    }


def _payload(result):
    return result.to_mapping()["data"]


def test_compute_run_dispatches_through_gateway_and_returns_queued(tmp_path):
    catalog, gateway, _ = _gateway(tmp_path)
    call = _call(catalog, "compute.run", _run_args())
    result = gateway.invoke(call, _iam(catalog))
    assert result.status == "SUCCEEDED"
    assert _payload(result)["status"] == "QUEUED"
    assert _payload(result)["jobId"].startswith("job_")


def test_compute_status_dispatches_through_gateway(tmp_path):
    catalog, gateway, _ = _gateway(tmp_path)
    run_result = gateway.invoke(_call(catalog, "compute.run", _run_args()), _iam(catalog))
    job_id = _payload(run_result)["jobId"]
    status_call = _call(
        catalog,
        "compute.status",
        {"jobId": job_id},
        tool_use_id="tooluse_87654321",
    )
    status = gateway.invoke(status_call, _iam(catalog))
    assert status.status == "SUCCEEDED"
    assert _payload(status)["jobId"] == job_id
    assert _payload(status)["status"] == "SUCCEEDED"
    assert [r["path"] for r in _payload(status)["outputs"]] == ["out.txt"]


def test_third_run_in_one_turn_is_refused_by_pack_quota(tmp_path):
    catalog, gateway, _ = _gateway(tmp_path)
    iam = _iam(catalog)
    outcomes = []
    for index in range(3):
        args = _run_args()
        args["inputPaths"] = [f"in/a{index}.txt"]
        call = _call(
            catalog,
            "compute.run",
            args,
            tool_use_id=f"tooluse_run{index}0000",
        )
        outcomes.append(gateway.invoke(call, iam))
    assert outcomes[0].status == "SUCCEEDED"
    assert outcomes[1].status == "SUCCEEDED"
    # compute.run pack allows maxCallsPerTurn=2; the third is denied.
    assert outcomes[2].status == "DENIED"
    assert outcomes[2].error_code == "CAPABILITY_CALL_BUDGET_EXCEEDED"


def test_gateway_rejects_run_output_over_the_pack_quota(tmp_path):
    catalog, gateway, service = _gateway(tmp_path)

    class OversizeService(ComputeService):
        def run(self, admitted):
            # Force an outcome larger than the compute.run pack maxOutputBytes.
            return AdapterOutcome(
                status="SUCCEEDED",
                data={"jobId": "job_" + "a" * 64, "status": "QUEUED", "pad": "x" * 2_000_000},
            )

    oversize = OversizeService(
        runner=FakeRunner(),
        input_store=FakeInputStore(),
        output_store=FakeOutputStore(),
        receipt_store=FakeReceiptStore(),
        image_digest=PINNED_DIGEST,
        clock=lambda: NOW,
        profile=models.SMALL,
        workspace_root=tmp_path,
    )
    catalog2 = _catalog()
    gateway2 = CapabilityGateway(
        catalog=catalog2,
        repository=FakeRepository(catalog2, LiveTargetGrant),
        ledger=InMemoryCapabilityLedger(),
        adapters={"compute.run": ComputeRunAdapter(oversize)},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )
    call = _call(catalog2, "compute.run", _run_args())
    result = gateway2.invoke(call, _iam(catalog2))
    # The oversized adapter output is rejected by the gateway pack quota.
    assert result.status in {"UNCERTAIN", "FAILED_RETRYABLE"}


def test_composition_seam_only_accepts_compute_adapters(tmp_path):
    import shutil

    from capabilities import composition

    artifacts = tmp_path / "artifacts"
    shutil.copytree(SCHEMA_DIR.parent, artifacts)
    catalog = _catalog()
    env = {
        "AWS_REGION": "eu-west-1",
        "CAPABILITY_STATE_TABLE_NAME": "capability-state",
        "CAPABILITY_RELEASE_COMMIT": RELEASE_COMMIT,
        "CAPABILITY_CATALOG_DIGEST": catalog.catalog_digest,
        "CAPABILITY_ALLOWED_CALLER_ARN": CALLER_ARN,
    }

    class _Client:
        pass

    # A non-compute operation cannot be smuggled into the credential-free Lambda.
    with pytest.raises(RuntimeError, match="compute"):
        composition.build_production_composition(
            env=env,
            artifact_root=artifacts,
            dynamodb_client=_Client(),
            clock=lambda: NOW,
            compute_adapters={"web.exact.read": object()},
        )


def test_gateway_holds_no_compute_or_credential_authority_without_adapter(tmp_path):
    catalog = _catalog()
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=FakeRepository(catalog, LiveTargetGrant),
        ledger=InMemoryCapabilityLedger(),
        adapters={},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )
    result = gateway.invoke(_call(catalog, "compute.run", _run_args()), _iam(catalog))
    assert result.status == "DENIED"
    assert result.error_code == "ADAPTER_DISABLED"
