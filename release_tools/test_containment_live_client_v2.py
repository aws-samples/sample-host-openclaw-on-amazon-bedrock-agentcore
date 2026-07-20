"""Synthetic tests for the live-AWS teardown client.

These tests NEVER make a real AWS call.  A fake boto3-like client map presents
botocore-shaped responses, and the accepted synthetic teardown adapter
(``ProductionDestructiveProviderV1`` / ``ProductionTeardownObserverV1``) drives a
full containment + purge to a terminal journal state through
``LiveTeardownClientV1``.  No credentials are ever constructed or printed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from release_tools.containment_v2 import (
    CONTAINMENT_RESOURCE_KINDS,
    ContainmentJournalV1,
    ContainmentPlanV1,
    ExactContainedJournalAuthorityV1,
    OwnedResourceIdentityV1,
    PurgePlanV1,
    PurgeTargetV1,
    ReleaseClosureBindingV1,
    RetainedReleaseEvidenceV1,
)
from release_tools.containment_adapter_v2 import (
    AttestedTeardownClientV1,
    ContainmentAdapterUncertain,
    ProductionDestructiveProviderV1,
    ProductionRetainedReleaseEvidenceMinterV1,
    ProductionTeardownObserverV1,
)
from release_tools.containment_live_client_v2 import (
    LiveTeardownClientError,
    LiveTeardownClientV1,
)

import hashlib


ACCOUNT = "123456789012"
REGION = "eu-west-1"

_SERVICES = (
    "cloudformation",
    "bedrock-agentcore-control",
    "s3",
    "ecr",
    "signer",
    "dynamodb",
    "kms",
    "logs",
)

_S3_VERSION = re.compile(r"s3://([^/?#]+)/([^?#]+)\?versionId=([^&#]+)")
_S3_UPLOAD = re.compile(r"s3://([^/?#]+)/([^?#]+)\?uploadId=([^&#]+)")


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


# --- synthetic release evidence (mirrors the accepted adapter test) ---------


def _binding(label: str = "release") -> ReleaseClosureBindingV1:
    return ReleaseClosureBindingV1(
        account=ACCOUNT,
        region=REGION,
        release_plan_sha256=_sha(f"{label}:plan"),
        release_transaction_id=f"tx-{label}",
        release_transaction_sha256=_sha(f"{label}:transaction"),
        release_journal_path_sha256=_sha(f"{label}:journal-path"),
        release_journal_execution_id=_sha(f"{label}:journal-execution"),
        evidence_store_sha256=_sha(f"{label}:evidence-store"),
        release_evidence_sha256=_sha(f"{label}:evidence"),
    )


def _owned(label: str = "release") -> tuple[OwnedResourceIdentityV1, ...]:
    return tuple(
        OwnedResourceIdentityV1(
            resource_kind=kind,
            resource_identity=(
                f"arn:aws:test:eu-west-1:123456789012:resource/{ordinal}-{label}"
            ),
            ownership_proof_sha256=_sha(f"{label}:{kind}:ownership"),
            observation_evidence_sha256=_sha(f"{label}:{kind}:observation"),
        )
        for ordinal, kind in enumerate(CONTAINMENT_RESOURCE_KINDS)
    )


def _purge_targets(label: str = "release") -> tuple[PurgeTargetV1, ...]:
    root = _binding(label).release_evidence_sha256
    return (
        PurgeTargetV1(
            target_kind="S3_MULTIPART_UPLOAD",
            resource_identity="s3://po-evidence-bucket/exact-key?uploadId=upload-1",
            ownership_proof_sha256=_sha(f"{label}:upload:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:upload:evidence"),
        ),
        PurgeTargetV1(
            target_kind="S3_OBJECT_VERSION",
            resource_identity="s3://po-evidence-bucket/exact-key?versionId=version-1",
            ownership_proof_sha256=_sha(f"{label}:version:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:version:evidence"),
        ),
        PurgeTargetV1(
            target_kind="S3_BUCKET",
            resource_identity="po-evidence-bucket",
            ownership_proof_sha256=_sha(f"{label}:bucket:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:bucket:evidence"),
        ),
        PurgeTargetV1(
            target_kind="ECR_IMAGE_REFERENCE",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:repository/"
                f"personal-operator/bridge@sha256:{_sha('image')}"
            ),
            ownership_proof_sha256=_sha(f"{label}:image:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:image:evidence"),
        ),
        PurgeTargetV1(
            target_kind="ECR_SIGNING_CONFIGURATION",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:"
                "signing-configuration/personal-operator"
            ),
            ownership_proof_sha256=_sha(f"{label}:signing-config:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:signing-config:evidence"),
        ),
        PurgeTargetV1(
            target_kind="SIGNER_SIGNING_PROFILE",
            resource_identity=(
                "arn:aws:signer:eu-west-1:123456789012:"
                "signing-profile/personal_operator_bridge"
            ),
            ownership_proof_sha256=_sha(f"{label}:signing-profile:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:signing-profile:evidence"),
        ),
        PurgeTargetV1(
            target_kind="ECR_REPOSITORY",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:repository/"
                "personal-operator/bridge"
            ),
            ownership_proof_sha256=_sha(f"{label}:repository:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:repository:evidence"),
        ),
        PurgeTargetV1(
            target_kind="DYNAMODB_TABLE",
            resource_identity=(
                "arn:aws:dynamodb:eu-west-1:123456789012:"
                "table/personal-operator-retained"
            ),
            ownership_proof_sha256=_sha(f"{label}:table:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:table:evidence"),
        ),
        PurgeTargetV1(
            target_kind="CLOUDWATCH_LOG_GROUP",
            resource_identity=(
                "arn:aws:logs:eu-west-1:123456789012:log-group:"
                "/aws/personal-operator/exact"
            ),
            ownership_proof_sha256=_sha(f"{label}:logs:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:logs:evidence"),
        ),
        PurgeTargetV1(
            target_kind="KMS_KEY",
            resource_identity=(
                "arn:aws:kms:eu-west-1:123456789012:key/"
                "11111111-2222-3333-4444-555555555555"
            ),
            ownership_proof_sha256=_sha(f"{label}:key:ownership"),
            release_evidence_sha256=root,
            release_evidence_entry_sha256=_sha(f"{label}:key:evidence"),
        ),
    )


def _retained(label: str = "release") -> RetainedReleaseEvidenceV1:
    binding = _binding(label)
    return ProductionRetainedReleaseEvidenceMinterV1.mint(
        binding=binding,
        owned_resources=_owned(label),
        purge_targets=_purge_targets(label),
        release_evidence_sha256=binding.release_evidence_sha256,
        account=ACCOUNT,
        region=REGION,
    )


# --- fake boto3 client map (NEVER a real AWS call) --------------------------


class FakeClientError(Exception):
    """A botocore-shaped client error carrying ``.response``."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code, "Message": f"{code}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


_OK = {"ResponseMetadata": {"HTTPStatusCode": 200}}
_DELETED = {"ResponseMetadata": {"HTTPStatusCode": 204}}


class FakeCloud:
    """Shared synthetic AWS state driving fake boto3 clients."""

    def __init__(self) -> None:
        self.generic: dict[str, str] = {}  # identity -> PRESENT/SCHEDULED/CANCELED
        self.buckets: set[str] = set()
        self.versions: dict[str, set[tuple[str, str]]] = {}
        self.uploads: dict[str, set[tuple[str, str]]] = {}
        self.calls: list[tuple[str, str, dict]] = []

    # -- seeding ----------------------------------------------------------
    def seed(self, action) -> None:
        kind = action.resource_kind
        identity = action.resource_identity
        if kind == "S3_BUCKET":
            self.buckets.add(identity)
        elif kind == "S3_OBJECT_VERSION":
            match = _S3_VERSION.fullmatch(identity)
            assert match is not None
            bucket, key, version = match.groups()
            self.buckets.add(bucket)
            self.versions.setdefault(bucket, set()).add((key, version))
        elif kind == "S3_MULTIPART_UPLOAD":
            match = _S3_UPLOAD.fullmatch(identity)
            assert match is not None
            bucket, key, upload = match.groups()
            self.buckets.add(bucket)
            self.uploads.setdefault(bucket, set()).add((key, upload))
        else:
            self.generic[identity] = "PRESENT"

    def present(self, identity: str) -> bool:
        return self.generic.get(identity) == "PRESENT"


class FakeBotoClient:
    """One synthetic per-service boto3-like client."""

    def __init__(self, cloud: FakeCloud, service: str) -> None:
        self._cloud = cloud
        self._service = service

    def _record(self, method: str, kwargs: dict) -> None:
        self._cloud.calls.append((self._service, method, dict(kwargs)))

    # -- cloudformation ---------------------------------------------------
    def delete_stack(self, **kwargs):
        self._record("delete_stack", kwargs)
        self._cloud.generic.pop(kwargs["StackName"], None)
        return dict(_DELETED)

    def describe_stacks(self, **kwargs):
        self._record("describe_stacks", kwargs)
        if self._cloud.present(kwargs["StackName"]):
            return {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
        raise FakeClientError("ValidationError", 400)

    # -- bedrock-agentcore-control ---------------------------------------
    def _agentcore_read(self, method: str, kwargs: dict, arn_field: str):
        self._record(method, kwargs)
        if self._cloud.present(kwargs[arn_field]):
            return {"status": "READY"}
        raise FakeClientError("ResourceNotFoundException", 404)

    def delete_resource_policy(self, **kwargs):
        self._record("delete_resource_policy", kwargs)
        self._cloud.generic.pop(kwargs["resourceArn"], None)
        return dict(_OK)

    def get_resource_policy(self, **kwargs):
        return self._agentcore_read("get_resource_policy", kwargs, "resourceArn")

    def delete_agent_runtime_endpoint(self, **kwargs):
        self._record("delete_agent_runtime_endpoint", kwargs)
        self._cloud.generic.pop(kwargs["agentRuntimeEndpointArn"], None)
        return dict(_OK)

    def get_agent_runtime_endpoint(self, **kwargs):
        return self._agentcore_read(
            "get_agent_runtime_endpoint", kwargs, "agentRuntimeEndpointArn"
        )

    def delete_agent_runtime(self, **kwargs):
        self._record("delete_agent_runtime", kwargs)
        self._cloud.generic.pop(kwargs["agentRuntimeArn"], None)
        return dict(_OK)

    def get_agent_runtime(self, **kwargs):
        return self._agentcore_read(
            "get_agent_runtime", kwargs, "agentRuntimeArn"
        )

    # -- s3 ---------------------------------------------------------------
    def abort_multipart_upload(self, **kwargs):
        self._record("abort_multipart_upload", kwargs)
        bucket = kwargs["Bucket"]
        self._cloud.uploads.get(bucket, set()).discard(
            (kwargs["Key"], kwargs["UploadId"])
        )
        return dict(_DELETED)

    def delete_object(self, **kwargs):
        self._record("delete_object", kwargs)
        bucket = kwargs["Bucket"]
        self._cloud.versions.get(bucket, set()).discard(
            (kwargs["Key"], kwargs["VersionId"])
        )
        return dict(_DELETED)

    def delete_bucket(self, **kwargs):
        self._record("delete_bucket", kwargs)
        self._cloud.buckets.discard(kwargs["Bucket"])
        return dict(_DELETED)

    def head_bucket(self, **kwargs):
        self._record("head_bucket", kwargs)
        if kwargs["Bucket"] in self._cloud.buckets:
            return dict(_OK)
        raise FakeClientError("404", 404)

    def list_object_versions(self, **kwargs):
        self._record("list_object_versions", kwargs)
        bucket = kwargs["Bucket"]
        return {
            "Versions": [
                {"Key": key, "VersionId": version}
                for (key, version) in sorted(self._cloud.versions.get(bucket, set()))
            ]
        }

    def list_multipart_uploads(self, **kwargs):
        self._record("list_multipart_uploads", kwargs)
        bucket = kwargs["Bucket"]
        return {
            "Uploads": [
                {"Key": key, "UploadId": upload}
                for (key, upload) in sorted(self._cloud.uploads.get(bucket, set()))
            ]
        }

    # -- ecr --------------------------------------------------------------
    def batch_delete_image(self, **kwargs):
        self._record("batch_delete_image", kwargs)
        self._cloud.generic.pop(kwargs["repositoryName"], None)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "failures": []}

    def batch_get_image(self, **kwargs):
        self._record("batch_get_image", kwargs)
        if self._cloud.present(kwargs["repositoryName"]):
            return {"images": [{"imageId": {}}], "failures": []}
        return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}

    def delete_repository(self, **kwargs):
        self._record("delete_repository", kwargs)
        self._cloud.generic.pop(kwargs["repositoryName"], None)
        return dict(_OK)

    def describe_repositories(self, **kwargs):
        self._record("describe_repositories", kwargs)
        if self._cloud.present(kwargs["repositoryName"]):
            return {"repositories": [{"repositoryName": kwargs["repositoryName"]}]}
        raise FakeClientError("RepositoryNotFoundException", 400)

    def delete_signing_configuration(self, **kwargs):
        self._record("delete_signing_configuration", kwargs)
        self._cloud.generic.pop(kwargs["repositoryName"], None)
        return dict(_OK)

    def get_signing_configuration(self, **kwargs):
        self._record("get_signing_configuration", kwargs)
        if self._cloud.present(kwargs["repositoryName"]):
            return dict(_OK)
        raise FakeClientError("SigningConfigurationNotFoundException", 400)

    # -- signer -----------------------------------------------------------
    def cancel_signing_profile(self, **kwargs):
        self._record("cancel_signing_profile", kwargs)
        self._cloud.generic[kwargs["profileName"]] = "CANCELED"
        return dict(_OK)

    def get_signing_profile(self, **kwargs):
        self._record("get_signing_profile", kwargs)
        state = self._cloud.generic.get(kwargs["profileName"])
        if state == "CANCELED":
            return {"status": "Canceled"}
        return {"status": "Active"}

    # -- dynamodb ---------------------------------------------------------
    def delete_table(self, **kwargs):
        self._record("delete_table", kwargs)
        self._cloud.generic.pop(kwargs["TableName"], None)
        return dict(_OK)

    def describe_table(self, **kwargs):
        self._record("describe_table", kwargs)
        if self._cloud.present(kwargs["TableName"]):
            return {"Table": {"TableStatus": "ACTIVE"}}
        raise FakeClientError("ResourceNotFoundException", 400)

    # -- kms --------------------------------------------------------------
    def schedule_key_deletion(self, **kwargs):
        self._record("schedule_key_deletion", kwargs)
        self._cloud.generic[kwargs["KeyId"]] = "SCHEDULED"
        return dict(_OK)

    def describe_key(self, **kwargs):
        self._record("describe_key", kwargs)
        state = self._cloud.generic.get(kwargs["KeyId"])
        key_state = "PendingDeletion" if state == "SCHEDULED" else "Enabled"
        return {"KeyMetadata": {"KeyState": key_state}}

    # -- logs -------------------------------------------------------------
    def delete_log_group(self, **kwargs):
        self._record("delete_log_group", kwargs)
        self._cloud.generic.pop(kwargs["logGroupName"], None)
        return dict(_OK)

    def describe_log_groups(self, **kwargs):
        self._record("describe_log_groups", kwargs)
        prefix = kwargs["logGroupNamePrefix"]
        if self._cloud.present(prefix):
            return {"logGroups": [{"logGroupName": prefix}]}
        return {"logGroups": []}


def _boto_map(cloud: FakeCloud) -> dict:
    return {service: FakeBotoClient(cloud, service) for service in _SERVICES}


def _mutation_clients(cloud: FakeCloud) -> dict:
    return LiveTeardownClientV1.mutation_clients(
        _boto_map(cloud), account=ACCOUNT, region=REGION
    )


def _observer_clients(cloud: FakeCloud) -> dict:
    return LiveTeardownClientV1.observer_clients(
        _boto_map(cloud), account=ACCOUNT, region=REGION
    )


def _seed_all(cloud: FakeCloud, plan) -> None:
    for action in plan.actions:
        cloud.seed(action)


def _containment_plan(label: str = "release"):
    evidence = _retained(label)
    plan = ContainmentPlanV1.create(
        operation_id=_sha(f"{label}:containment"),
        retained_evidence=evidence,
    )
    return plan, evidence


def _run_teardown(journal_dir: Path, plan, evidence, *, cloud: FakeCloud):
    journal = ContainmentJournalV1.create(journal_dir, plan=plan)
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=_mutation_clients(cloud),
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    observer = ProductionTeardownObserverV1(
        plan,
        clients=_observer_clients(cloud),
        account=ACCOUNT,
        region=REGION,
    )
    while not journal.terminal:
        action = plan.actions[journal.cursor]
        provider.dispatch(journal.arm_next(), action)
        journal.reconcile(observer.observe_current(journal))
    return journal


def _purge_plan(tmp_path: Path, label: str = "release"):
    plan, evidence = _containment_plan(label)
    cloud = FakeCloud()
    _seed_all(cloud, plan)
    journal = _run_teardown(tmp_path / "contain", plan, evidence, cloud=cloud)
    contained: ExactContainedJournalAuthorityV1 = journal.authorize_purge(evidence)
    purge = PurgePlanV1.create(
        operation_id=_sha(f"{label}:purge"),
        retained_evidence=evidence,
        contained_journal=contained,
    )
    return purge, evidence


# --- tests ------------------------------------------------------------------


def test_live_client_is_attested_teardown_subclass() -> None:
    cloud = FakeCloud()
    client = LiveTeardownClientV1(
        FakeBotoClient(cloud, "kms"),
        service="kms",
        account=ACCOUNT,
        region=REGION,
        capability="mutation",
    )
    assert isinstance(client, AttestedTeardownClientV1)


def test_full_containment_teardown_to_terminal(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all(cloud, plan)
    journal = _run_teardown(tmp_path / "journal", plan, evidence, cloud=cloud)
    assert journal.terminal
    assert journal.state == "CONTAINED"


def test_full_purge_teardown_kms_scheduled_signer_canceled(tmp_path: Path) -> None:
    purge, evidence = _purge_plan(tmp_path)
    cloud = FakeCloud()
    _seed_all(cloud, purge)
    journal = _run_teardown(tmp_path / "purge", purge, evidence, cloud=cloud)
    assert journal.terminal
    assert journal.state == "PURGED_WITH_SCHEDULED_KEYS"
    assert journal.scheduled_key_count == 1
    # KMS teardown used ScheduleKeyDeletion only; never an immediate DeleteKey.
    kms_mutations = [
        method for (svc, method, _kw) in cloud.calls if svc == "kms"
    ]
    assert "schedule_key_deletion" in kms_mutations
    assert "delete_key" not in kms_mutations


def test_expected_bucket_owner_on_every_s3_call(tmp_path: Path) -> None:
    purge, evidence = _purge_plan(tmp_path)
    cloud = FakeCloud()
    _seed_all(cloud, purge)
    _run_teardown(tmp_path / "purge", purge, evidence, cloud=cloud)
    s3_calls = [kw for (svc, _m, kw) in cloud.calls if svc == "s3"]
    assert s3_calls
    assert all(kw.get("ExpectedBucketOwner") == ACCOUNT for kw in s3_calls)
    # The synthetic ExpectedAccount marker never reaches the raw boto3 client.
    assert all("ExpectedAccount" not in kw for (_s, _m, kw) in cloud.calls)
    assert all("ResourceIdentity" not in kw for (_s, _m, kw) in cloud.calls)


def test_method_allowlist_rejects_out_of_list_method() -> None:
    cloud = FakeCloud()
    kms = LiveTeardownClientV1(
        FakeBotoClient(cloud, "kms"),
        service="kms",
        account=ACCOUNT,
        region=REGION,
        capability="mutation",
    )
    with pytest.raises(LiveTeardownClientError):
        kms.invoke("delete_key", KeyId="arn:aws:kms:eu-west-1:123456789012:key/x")
    # An observer method is out of the mutation capability, too.
    with pytest.raises(LiveTeardownClientError):
        kms.invoke("describe_key", KeyId="arn:aws:kms:eu-west-1:123456789012:key/x")


def test_region_and_account_pinning_enforced() -> None:
    cloud = FakeCloud()
    with pytest.raises(LiveTeardownClientError):
        LiveTeardownClientV1(
            FakeBotoClient(cloud, "kms"),
            service="kms",
            account=ACCOUNT,
            region="us-east-1",
            capability="mutation",
        )
    with pytest.raises(LiveTeardownClientError):
        LiveTeardownClientV1(
            FakeBotoClient(cloud, "kms"),
            service="kms",
            account="00000000000",  # not 12 digits
            region=REGION,
            capability="mutation",
        )
    client = LiveTeardownClientV1(
        FakeBotoClient(cloud, "kms"),
        service="kms",
        account=ACCOUNT,
        region=REGION,
        capability="mutation",
    )
    with pytest.raises(LiveTeardownClientError):
        client.require_scope(
            service="kms", account="210987654321", region=REGION, capability="mutation"
        )
    with pytest.raises(LiveTeardownClientError):
        client.require_scope(
            service="kms", account=ACCOUNT, region=REGION, capability="observer"
        )
    # Exact scope passes.
    client.require_scope(
        service="kms", account=ACCOUNT, region=REGION, capability="mutation"
    )


def test_unacknowledged_mutation_keeps_release_uncertain(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all(cloud, plan)
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)

    class Non2xxCfn(FakeBotoClient):
        def delete_stack(self, **kwargs):
            self._record("delete_stack", kwargs)
            # A throttled / non-2xx response is an unknown outcome.
            return {"ResponseMetadata": {"HTTPStatusCode": 500}}

    boto_map = {service: FakeBotoClient(cloud, service) for service in _SERVICES}
    boto_map["cloudformation"] = Non2xxCfn(cloud, "cloudformation")
    clients = LiveTeardownClientV1.mutation_clients(
        boto_map, account=ACCOUNT, region=REGION
    )
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=clients,
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    action = plan.actions[journal.cursor]
    authority = journal.arm_next()
    with pytest.raises(ContainmentAdapterUncertain):
        provider.dispatch(authority, action)
    assert journal.state == "UNCERTAIN"
    # The single-use authority is spent; the slot cannot re-arm.
    with pytest.raises(Exception):
        journal.arm_next()


def test_raised_throttling_mutation_keeps_release_uncertain(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all(cloud, plan)
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)

    class ThrottledCfn(FakeBotoClient):
        def delete_stack(self, **kwargs):
            self._record("delete_stack", kwargs)
            raise FakeClientError("ThrottlingException", 429)

    boto_map = {service: FakeBotoClient(cloud, service) for service in _SERVICES}
    boto_map["cloudformation"] = ThrottledCfn(cloud, "cloudformation")
    clients = LiveTeardownClientV1.mutation_clients(
        boto_map, account=ACCOUNT, region=REGION
    )
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=clients,
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    action = plan.actions[journal.cursor]
    with pytest.raises(ContainmentAdapterUncertain):
        provider.dispatch(journal.arm_next(), action)
    assert journal.state == "UNCERTAIN"


def test_observer_two_reads_have_distinct_sequences() -> None:
    cloud = FakeCloud()
    cloud.generic["arn:aws:test:eu-west-1:123456789012:resource/0-x"] = "PRESENT"
    observer = LiveTeardownClientV1(
        FakeBotoClient(cloud, "cloudformation"),
        service="cloudformation",
        account=ACCOUNT,
        region=REGION,
        capability="observer",
    )
    first = observer.invoke(
        "describe_stacks",
        ResourceIdentity="arn:aws:test:eu-west-1:123456789012:resource/0-x",
        ExpectedAccount=ACCOUNT,
    )
    second = observer.invoke(
        "describe_stacks",
        ResourceIdentity="arn:aws:test:eu-west-1:123456789012:resource/0-x",
        ExpectedAccount=ACCOUNT,
    )
    assert first["liveState"] == "PRESENT"
    assert second["liveState"] == "PRESENT"
    assert first["readSequence"] != second["readSequence"]
    assert second["readSequence"] == first["readSequence"] + 1


def test_observer_maps_absent_and_scheduled_and_canceled() -> None:
    cloud = FakeCloud()
    # KMS scheduled.
    kms = LiveTeardownClientV1(
        FakeBotoClient(cloud, "kms"),
        service="kms",
        account=ACCOUNT,
        region=REGION,
        capability="observer",
    )
    key = "arn:aws:kms:eu-west-1:123456789012:key/abc"
    cloud.generic[key] = "SCHEDULED"
    assert kms.invoke("describe_key", KeyId=key, ExpectedAccount=ACCOUNT)[
        "liveState"
    ] == "SCHEDULED"
    # Signer canceled.
    signer = LiveTeardownClientV1(
        FakeBotoClient(cloud, "signer"),
        service="signer",
        account=ACCOUNT,
        region=REGION,
        capability="observer",
    )
    profile = "arn:aws:signer:eu-west-1:123456789012:signing-profile/x"
    cloud.generic[profile] = "CANCELED"
    assert signer.invoke(
        "get_signing_profile", ResourceIdentity=profile, ExpectedAccount=ACCOUNT
    )["liveState"] == "CANCELED"
    # CloudFormation absent (not-found error) maps to ABSENT.
    cfn = LiveTeardownClientV1(
        FakeBotoClient(cloud, "cloudformation"),
        service="cloudformation",
        account=ACCOUNT,
        region=REGION,
        capability="observer",
    )
    missing = "arn:aws:test:eu-west-1:123456789012:resource/absent"
    assert cfn.invoke(
        "describe_stacks", ResourceIdentity=missing, ExpectedAccount=ACCOUNT
    )["liveState"] == "ABSENT"
