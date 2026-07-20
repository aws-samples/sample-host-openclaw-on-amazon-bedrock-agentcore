from __future__ import annotations

import hashlib
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
    ContainmentAdapterError,
    ContainmentAdapterUncertain,
    ProductionDestructiveProviderV1,
    ProductionRetainedReleaseEvidenceMinterV1,
    ProductionTeardownObserverV1,
    require_exact_teardown_identity,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"

# Services the teardown adapter routes to.
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


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


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


# --- synthetic attested clients + cloud -------------------------------------


class FakeCloud:
    """In-memory synthetic AWS: PRESENT until an effect flips it."""

    def __init__(self) -> None:
        # key -> live state; unknown keys read ABSENT.
        self._state: dict[str, str] = {}
        self._read_counter = 0
        self.calls: list[tuple[str, str, dict]] = []

    def seed_present(self, key: str, state: str = "PRESENT") -> None:
        self._state[key] = state

    def _key(self, kwargs: dict) -> str:
        if "Bucket" in kwargs:
            return "|".join(
                str(kwargs.get(field, ""))
                for field in ("Bucket", "Key", "VersionId", "UploadId")
            )
        if "KeyId" in kwargs:
            return str(kwargs["KeyId"])
        return str(kwargs.get("ResourceIdentity", ""))

    def mutate(self, service: str, method: str, kwargs: dict) -> dict:
        self.calls.append((service, method, dict(kwargs)))
        key = self._key(kwargs)
        # KMS schedules, signer cancels, everything else deletes to ABSENT.
        if method == "schedule_key_deletion":
            self._state[key] = "SCHEDULED"
        elif method == "cancel_signing_profile":
            self._state[key] = "CANCELED"
        else:
            self._state[key] = "ABSENT"
        return {"acknowledged": True}

    def read(self, service: str, method: str, kwargs: dict) -> dict:
        self.calls.append((service, method, dict(kwargs)))
        self._read_counter += 1
        key = self._key(kwargs)
        return {
            "liveState": self._state.get(key, "ABSENT"),
            "readSequence": self._read_counter,
        }


class FakeAttestedClient(AttestedTeardownClientV1):
    def __init__(
        self,
        cloud: FakeCloud,
        *,
        service: str,
        account: str,
        region: str,
        capability: str,
        read_mode: str = "read",
    ) -> None:
        self._cloud = cloud
        self._service = service
        self._account = account
        self._region = region
        self._capability = capability
        self._read_mode = read_mode  # "read" | "echo"
        self._echoed: dict | None = None

    def require_scope(
        self, *, service: str, account: str, region: str, capability: str
    ) -> None:
        if (service, account, region, capability) != (
            self._service,
            self._account,
            self._region,
            self._capability,
        ):
            raise ContainmentAdapterError("attested teardown client scope differs")

    def invoke(self, method_name: str, **kwargs):
        if self._capability == "mutation":
            return self._cloud.mutate(self._service, method_name, kwargs)
        if self._read_mode == "echo":
            # Return the same read (same sequence) twice: a single echoed read.
            if self._echoed is None:
                self._echoed = self._cloud.read(self._service, method_name, kwargs)
            return dict(self._echoed)
        return self._cloud.read(self._service, method_name, kwargs)


def _clients(
    cloud: FakeCloud, capability: str, *, read_mode: str = "read"
) -> dict:
    return {
        service: FakeAttestedClient(
            cloud,
            service=service,
            account=ACCOUNT,
            region=REGION,
            capability=capability,
            read_mode=read_mode,
        )
        for service in _SERVICES
    }


def _seed_all_present(cloud: FakeCloud, plan) -> None:
    for action in plan.actions:
        cloud.seed_present(_state_key(action))


def _state_key(action) -> str:
    from release_tools.containment_adapter_v2 import (
        _identity_kwargs,
        _ownership_kwargs,
    )

    kwargs = dict(_identity_kwargs(action))
    kwargs.update(_ownership_kwargs(action, ACCOUNT))
    if "Bucket" in kwargs:
        return "|".join(
            str(kwargs.get(f, "")) for f in ("Bucket", "Key", "VersionId", "UploadId")
        )
    if "KeyId" in kwargs:
        return str(kwargs["KeyId"])
    return str(kwargs.get("ResourceIdentity", ""))


def _containment_plan(label: str = "release"):
    evidence = _retained(label)
    plan = ContainmentPlanV1.create(
        operation_id=_sha(f"{label}:containment"),
        retained_evidence=evidence,
    )
    return plan, evidence


def _run_teardown(
    tmp_path: Path,
    plan,
    evidence,
    *,
    cloud: FakeCloud,
    observer_read_mode: str = "read",
) -> ContainmentJournalV1:
    journal = ContainmentJournalV1.create(
        tmp_path / "journal", plan=plan
    )
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=_clients(cloud, "mutation"),
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    observer = ProductionTeardownObserverV1(
        plan,
        clients=_clients(cloud, "observer", read_mode=observer_read_mode),
        account=ACCOUNT,
        region=REGION,
    )
    while not journal.terminal:
        action = plan.actions[journal.cursor]
        provider.dispatch(journal.arm_next(), action)
        journal.reconcile(observer.observe_current(journal))
    return journal


# --- tests ------------------------------------------------------------------


def test_minter_authenticates_and_rejects_bad_evidence_digest() -> None:
    binding = _binding()
    with pytest.raises(ContainmentAdapterError):
        ProductionRetainedReleaseEvidenceMinterV1.mint(
            binding=binding,
            owned_resources=_owned(),
            purge_targets=_purge_targets(),
            release_evidence_sha256=_sha("wrong-digest"),
            account=ACCOUNT,
            region=REGION,
        )


def test_minter_rejects_account_crossing() -> None:
    binding = _binding()
    with pytest.raises(ContainmentAdapterError):
        ProductionRetainedReleaseEvidenceMinterV1.mint(
            binding=binding,
            owned_resources=_owned(),
            purge_targets=_purge_targets(),
            release_evidence_sha256=binding.release_evidence_sha256,
            account="210987654321",
            region=REGION,
        )


def test_happy_path_full_containment_teardown(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = _run_teardown(tmp_path, plan, evidence, cloud=cloud)
    assert journal.terminal
    assert journal.state == "CONTAINED"


def test_kms_ends_scheduled_and_signer_ends_canceled(tmp_path: Path) -> None:
    # Purge plan includes KMS + signer.
    plan, evidence = _purge_plan(tmp_path)
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = _run_teardown(tmp_path / "purge", plan, evidence, cloud=cloud)
    assert journal.terminal
    assert journal.state == "PURGED_WITH_SCHEDULED_KEYS"
    assert journal.scheduled_key_count == 1


def test_exact_deletion_order_enforced(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    _run_teardown(tmp_path, plan, evidence, cloud=cloud)
    dispatched = [
        (svc, method)
        for (svc, method, _kw) in cloud.calls
        if method
        in {
            "delete_stack",
            "delete_resource_policy",
            "delete_agent_runtime_endpoint",
            "delete_agent_runtime",
        }
    ]
    # Contract order: 4 stacks, endpoint-policy, endpoint, runtime-policy,
    # runtime, then more stacks.
    assert dispatched[:8] == [
        ("cloudformation", "delete_stack"),
        ("cloudformation", "delete_stack"),
        ("cloudformation", "delete_stack"),
        ("cloudformation", "delete_stack"),
        ("bedrock-agentcore-control", "delete_resource_policy"),
        ("bedrock-agentcore-control", "delete_agent_runtime_endpoint"),
        ("bedrock-agentcore-control", "delete_resource_policy"),
        ("bedrock-agentcore-control", "delete_agent_runtime"),
    ]


def test_wildcard_identity_rejected() -> None:
    for bad in ("arn:aws:test:eu-west-1:123456789012:resource/*", "prefix:", "a/"):
        with pytest.raises(ContainmentAdapterError):
            require_exact_teardown_identity(bad)


def test_uncertain_effect_does_not_replay(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)

    class UnacknowledgedClient(FakeAttestedClient):
        def invoke(self, method_name: str, **kwargs):
            self._cloud.calls.append((self._service, method_name, dict(kwargs)))
            return {"acknowledged": False}

    clients = {
        svc: UnacknowledgedClient(
            cloud, service=svc, account=ACCOUNT, region=REGION, capability="mutation"
        )
        for svc in _SERVICES
    }
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
    # Journal stayed UNCERTAIN; the single-use authority is spent and cannot re-arm.
    assert journal.state == "UNCERTAIN"
    with pytest.raises(Exception):
        journal.arm_next()


def test_two_sweeps_must_be_distinct_single_echo_rejected(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=_clients(cloud, "mutation"),
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    observer = ProductionTeardownObserverV1(
        plan,
        clients=_clients(cloud, "observer", read_mode="echo"),
        account=ACCOUNT,
        region=REGION,
    )
    action = plan.actions[journal.cursor]
    provider.dispatch(journal.arm_next(), action)
    with pytest.raises(ContainmentAdapterUncertain):
        observer.observe_current(journal)


def test_s3_account_ownership_enforced(tmp_path: Path) -> None:
    plan, evidence = _purge_plan(tmp_path)
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = _run_teardown(tmp_path / "purge", plan, evidence, cloud=cloud)
    assert journal.terminal
    # Every S3 call carried the ExpectedBucketOwner ownership assertion.
    s3_calls = [kw for (svc, _m, kw) in cloud.calls if svc == "s3"]
    assert s3_calls
    assert all(kw.get("ExpectedBucketOwner") == ACCOUNT for kw in s3_calls)


def test_crash_after_effect_consumes_authority(tmp_path: Path) -> None:
    plan, evidence = _containment_plan()
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    provider = ProductionDestructiveProviderV1(
        plan,
        clients=_clients(cloud, "mutation"),
        account=ACCOUNT,
        region=REGION,
        retained_evidence=evidence,
    )
    action = plan.actions[journal.cursor]
    authority = journal.arm_next()
    with pytest.raises(RuntimeError):
        provider.dispatch(authority, action, crash_after_effect=True)
    # Authority already consumed; re-consuming with a fresh call is rejected.
    with pytest.raises(Exception):
        provider.dispatch(authority, action)
    # Journal cannot re-arm the same UNCERTAIN slot.
    assert journal.state == "UNCERTAIN"
    with pytest.raises(Exception):
        journal.arm_next()


def _purge_plan(tmp_path: Path, label: str = "release"):
    plan, evidence = _containment_plan(label)
    cloud = FakeCloud()
    _seed_all_present(cloud, plan)
    journal = _run_teardown(tmp_path / "contain", plan, evidence, cloud=cloud)
    contained: ExactContainedJournalAuthorityV1 = journal.authorize_purge(evidence)
    purge = PurgePlanV1.create(
        operation_id=_sha(f"{label}:purge"),
        retained_evidence=evidence,
        contained_journal=contained,
    )
    return purge, evidence
