from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import release_tools.containment_v2 as containment_module

from release_tools.containment_v2 import (
    CONTAINMENT_RESOURCE_KINDS,
    ContainmentError,
    ContainmentJournalV1,
    ContainmentPlanV1,
    DestructiveObservationV1,
    ExactContainedJournalAuthorityV1,
    FakeContainmentProviderV1,
    FakeRetainedReleaseEvidenceBoundaryV1,
    FreshContainmentAuthorityV1,
    OwnedResourceIdentityV1,
    PurgePlanV1,
    PurgeTargetV1,
    ReleaseClosureBindingV1,
    RetainedReleaseEvidenceV1,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding(label: str = "release") -> ReleaseClosureBindingV1:
    return ReleaseClosureBindingV1(
        account="123456789012",
        region="eu-west-1",
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


def _containment(label: str = "release") -> ContainmentPlanV1:
    return ContainmentPlanV1.create(
        operation_id=_sha(f"{label}:containment"),
        retained_evidence=_retained(label),
    )


def _purge_targets(label: str = "release") -> tuple[PurgeTargetV1, ...]:
    return (
        PurgeTargetV1(
            target_kind="S3_MULTIPART_UPLOAD",
            resource_identity="s3://po-evidence-bucket/exact-key?uploadId=upload-1",
            ownership_proof_sha256=_sha(f"{label}:upload:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:upload:evidence"),
        ),
        PurgeTargetV1(
            target_kind="S3_OBJECT_VERSION",
            resource_identity="s3://po-evidence-bucket/exact-key?versionId=version-1",
            ownership_proof_sha256=_sha(f"{label}:version:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:version:evidence"),
        ),
        PurgeTargetV1(
            target_kind="S3_BUCKET",
            resource_identity="po-evidence-bucket",
            ownership_proof_sha256=_sha(f"{label}:bucket:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:bucket:evidence"),
        ),
        PurgeTargetV1(
            target_kind="ECR_IMAGE_REFERENCE",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:repository/"
                f"personal-operator/bridge@sha256:{_sha('image')}"
            ),
            ownership_proof_sha256=_sha(f"{label}:image:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:image:evidence"),
        ),
        PurgeTargetV1(
            target_kind="ECR_SIGNING_CONFIGURATION",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:"
                "signing-configuration/personal-operator"
            ),
            ownership_proof_sha256=_sha(f"{label}:signing-config:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(
                f"{label}:signing-config:evidence"
            ),
        ),
        PurgeTargetV1(
            target_kind="SIGNER_SIGNING_PROFILE",
            resource_identity=(
                "arn:aws:signer:eu-west-1:123456789012:"
                "signing-profile/personal_operator_bridge"
            ),
            ownership_proof_sha256=_sha(f"{label}:signing-profile:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(
                f"{label}:signing-profile:evidence"
            ),
        ),
        PurgeTargetV1(
            target_kind="ECR_REPOSITORY",
            resource_identity=(
                "arn:aws:ecr:eu-west-1:123456789012:repository/"
                "personal-operator/bridge"
            ),
            ownership_proof_sha256=_sha(f"{label}:repository:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:repository:evidence"),
        ),
        PurgeTargetV1(
            target_kind="DYNAMODB_TABLE",
            resource_identity=(
                "arn:aws:dynamodb:eu-west-1:123456789012:"
                "table/personal-operator-retained"
            ),
            ownership_proof_sha256=_sha(f"{label}:table:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:table:evidence"),
        ),
        PurgeTargetV1(
            target_kind="CLOUDWATCH_LOG_GROUP",
            resource_identity=(
                "arn:aws:logs:eu-west-1:123456789012:log-group:"
                "/aws/personal-operator/exact"
            ),
            ownership_proof_sha256=_sha(f"{label}:logs:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:logs:evidence"),
        ),
        PurgeTargetV1(
            target_kind="KMS_KEY",
            resource_identity=(
                "arn:aws:kms:eu-west-1:123456789012:key/"
                "11111111-2222-3333-4444-555555555555"
            ),
            ownership_proof_sha256=_sha(f"{label}:key:ownership"),
            release_evidence_sha256=_binding(label).release_evidence_sha256,
            release_evidence_entry_sha256=_sha(f"{label}:key:evidence"),
        ),
    )


def _retained(label: str = "release") -> RetainedReleaseEvidenceV1:
    return FakeRetainedReleaseEvidenceBoundaryV1.retain(
        binding=_binding(label),
        owned_resources=_owned(label),
        purge_targets=_purge_targets(label),
    )


def _contained_authority(
    tmp_path: Path,
    *,
    label: str = "release",
) -> tuple[
    ContainmentPlanV1,
    RetainedReleaseEvidenceV1,
    ExactContainedJournalAuthorityV1,
]:
    evidence = _retained(label)
    containment = ContainmentPlanV1.create(
        operation_id=_sha(f"{label}:containment"),
        retained_evidence=evidence,
    )
    journal = ContainmentJournalV1.create(
        tmp_path / f"{label}.containment-journal",
        plan=containment,
    )
    provider = FakeContainmentProviderV1.from_plan(containment)
    while not journal.terminal:
        action = containment.actions[journal.cursor]
        provider.dispatch(journal.arm_next(), action)
        journal.reconcile(provider.observe_current(journal))
    return containment, evidence, journal.authorize_purge(evidence)


def _purge(tmp_path: Path, label: str = "release") -> PurgePlanV1:
    _containment_plan, evidence, contained = _contained_authority(
        tmp_path,
        label=label,
    )
    return PurgePlanV1.create(
        operation_id=_sha(f"{label}:purge"),
        retained_evidence=evidence,
        contained_journal=contained,
    )


def test_containment_plan_is_canonical_and_has_the_only_allowed_order() -> None:
    plan = _containment()
    assert ContainmentPlanV1.from_bytes(plan.to_bytes()) == plan
    assert tuple(action.resource_kind for action in plan.actions) == (
        CONTAINMENT_RESOURCE_KINDS
    )
    assert tuple(action.operation for action in plan.actions) == (
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_RESOURCE_POLICY",
        "DELETE_ENDPOINT",
        "DELETE_RESOURCE_POLICY",
        "DELETE_RUNTIME",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
        "DELETE_STACK",
    )


def test_containment_plan_rejects_reorder_missing_resource_and_ownership_substitution() -> None:
    resources = list(_owned())
    with pytest.raises(ContainmentError, match="exact containment inventory"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=_binding(),
            owned_resources=(resources[1], resources[0], *resources[2:]),
            purge_targets=_purge_targets(),
        )
    with pytest.raises(ContainmentError, match="exact containment inventory"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=_binding(),
            owned_resources=resources[:-1],
            purge_targets=_purge_targets(),
        )

    plan = _containment()
    raw = json.loads(plan.to_bytes())
    raw["actions"][0]["ownershipProofSha256"] = _sha("substituted")
    payload = (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(ContainmentError, match="derived actions"):
        ContainmentPlanV1.from_bytes(payload)


@pytest.mark.parametrize(
    "identity",
    (
        "arn:aws:test:eu-west-1:123456789012:resource/*",
        "arn:aws:test:eu-west-1:123456789012:resource/prefix:",
        " arn:aws:test:eu-west-1:123456789012:resource/value",
    ),
)
def test_owned_resource_identity_rejects_non_exact_targets(identity: str) -> None:
    with pytest.raises(ContainmentError, match="exact"):
        OwnedResourceIdentityV1(
            CONTAINMENT_RESOURCE_KINDS[0], identity, _sha("a"), _sha("b")
        )


def test_durable_attempt_survives_crash_and_cannot_be_replayed(tmp_path: Path) -> None:
    plan = _containment()
    path = tmp_path / "containment.journal.json"
    journal = ContainmentJournalV1.create(path, plan=plan)
    initial_record = path.read_bytes()
    authority = journal.arm_next()
    assert path.read_bytes() == initial_record
    assert containment_module._record_path(path, 1).read_bytes() == journal.to_bytes()
    assert journal.state == "UNCERTAIN"
    provider = FakeContainmentProviderV1.from_plan(plan)

    with pytest.raises(RuntimeError, match="simulated crash"):
        provider.dispatch(
            authority,
            plan.actions[0],
            crash_after_effect=True,
        )
    with pytest.raises(ContainmentError, match="already consumed"):
        provider.dispatch(authority, plan.actions[0])

    recovered = ContainmentJournalV1.load(path, plan=plan)
    assert recovered.state == "UNCERTAIN"
    with pytest.raises(ContainmentError, match="already has a durable attempt"):
        recovered.arm_next()
    observation = provider.observe_current(recovered)
    recovered.reconcile(observation)
    assert recovered.state == "READY"
    assert recovered.cursor == 1
    assert provider.dispatch_count(plan.actions[0]) == 1
    raw = json.loads(recovered.to_bytes())
    assert len(raw["completedAttempts"]) == 1
    assert raw["completedAttempts"][0]["actionOrdinal"] == 0
    assert raw["completedObservations"] == [observation.to_mapping()]
    assert path.read_bytes() == initial_record
    assert containment_module._record_path(path, 2).read_bytes() == recovered.to_bytes()


def test_completed_attempt_and_observation_history_is_audited(tmp_path: Path) -> None:
    plan = _containment()
    path = tmp_path / "journal"
    journal = ContainmentJournalV1.create(path, plan=plan)
    provider = FakeContainmentProviderV1.from_plan(plan)
    provider.dispatch(journal.arm_next(), plan.actions[0])
    observation = provider.observe_current(journal)
    journal.reconcile(observation)

    raw = json.loads(journal.to_bytes())
    assert raw["completedAttempts"][0]["actionSha256"] == plan.actions[0].digest()
    assert raw["completedObservations"] == [observation.to_mapping()]
    raw["completedObservations"][0]["resourceIdentity"] = plan.actions[1].resource_identity
    containment_module._record_path(path, journal.revision).write_bytes(
        (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ContainmentError, match="completed observation"):
        ContainmentJournalV1.load(path, plan=plan)


def test_crash_before_effect_and_ambiguous_sweeps_stay_uncertain(tmp_path: Path) -> None:
    plan = _containment()
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    authority = journal.arm_next()
    provider = FakeContainmentProviderV1.from_plan(plan)
    with pytest.raises(RuntimeError, match="simulated crash"):
        provider.dispatch(authority, plan.actions[0], crash_before_effect=True)

    before = journal.to_bytes()
    present = provider.observe_current(journal)
    assert present.disposition == "PRESENT"
    journal.reconcile(present)
    assert journal.state == "UNCERTAIN"
    assert journal.to_bytes() == before

    provider.set_sweeps(plan.actions[0], "PRESENT", "ABSENT")
    ambiguous = provider.observe_current(journal)
    assert ambiguous.disposition == "AMBIGUOUS"
    journal.reconcile(ambiguous)
    assert journal.state == "UNCERTAIN"
    assert journal.to_bytes() == before


def test_reconcile_rejects_deserialized_cross_plan_resource_and_proof_substitution(
    tmp_path: Path,
) -> None:
    plan = _containment()
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    journal.arm_next()
    provider = FakeContainmentProviderV1.from_plan(plan)
    valid = provider.observe_current(journal)
    for field, replacement in (
        ("planSha256", _sha("other-plan")),
        ("resourceIdentity", plan.actions[1].resource_identity),
        ("ownershipProofSha256", _sha("other-proof")),
    ):
        forged = valid.to_mapping()
        forged[field] = replacement
        # Even a canonical parser reconstruction is retained history only; it
        # is never a live observer capability.
        if field in {"planSha256"}:
            # The aggregate evidence digest binds the plan, so preserve a
            # canonical mapping only for fields outside that digest.
            forged = valid.to_mapping()
        parsed = DestructiveObservationV1._from_mapping(forged)
        with pytest.raises(ContainmentError, match="fresh closed observer"):
            journal.reconcile(parsed)
    assert journal.state == "UNCERTAIN"


def test_wrong_plan_and_journal_substitution_are_rejected(tmp_path: Path) -> None:
    plan = _containment()
    path = tmp_path / "journal"
    journal = ContainmentJournalV1.create(path, plan=plan)
    other = _containment("other")
    with pytest.raises(ContainmentError, match="plan"):
        ContainmentJournalV1.load(path, plan=other)

    raw = json.loads(journal.to_bytes())
    raw["releaseEvidenceSha256"] = _sha("substituted-evidence")
    path.write_bytes(
        (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ContainmentError, match="binding"):
        ContainmentJournalV1.load(path, plan=plan)


def test_journal_persists_account_region_and_rejects_their_substitution(
    tmp_path: Path,
) -> None:
    plan = _containment()
    path = tmp_path / "journal"
    journal = ContainmentJournalV1.create(path, plan=plan)
    raw = json.loads(journal.to_bytes())
    assert raw["account"] == plan.binding.account
    assert raw["region"] == "eu-west-1"

    raw["account"] = "999999999999"
    path.write_bytes(
        (json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(ContainmentError, match="binding"):
        ContainmentJournalV1.load(path, plan=plan)


def test_journal_refuses_hardlink_target_and_lock_substitution(tmp_path: Path) -> None:
    plan = _containment()
    path = tmp_path / "journal"
    journal = ContainmentJournalV1.create(path, plan=plan)

    journal_link = tmp_path / "journal-link"
    path.rename(journal_link)
    path.hardlink_to(journal_link)
    with pytest.raises(ContainmentError, match="one regular file"):
        ContainmentJournalV1.load(path, plan=plan)

    path.unlink()
    journal_link.rename(path)
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch")
    victim.chmod(0o644)
    lock = tmp_path / ".journal.containment.lock"
    lock.hardlink_to(victim)
    with pytest.raises(ContainmentError, match="lock is unsafe"):
        journal.arm_next()
    assert victim.read_text() == "do-not-touch"
    assert victim.stat().st_mode & 0o777 == 0o644


def test_containment_reaches_only_contained_without_rollback(tmp_path: Path) -> None:
    plan = _containment()
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    provider = FakeContainmentProviderV1.from_plan(plan)
    for action in plan.actions:
        authority = journal.arm_next()
        provider.dispatch(authority, action)
        journal.reconcile(provider.observe_current(journal))
    assert journal.state == "CONTAINED"
    assert journal.terminal
    assert journal.cursor == len(plan.actions)
    assert not hasattr(journal, "rollback")
    with pytest.raises(ContainmentError, match="terminal"):
        journal.arm_next()


def test_purge_is_separate_containment_bound_and_strictly_ordered(
    tmp_path: Path,
) -> None:
    containment, evidence, contained = _contained_authority(tmp_path)
    plan = PurgePlanV1.create(
        operation_id=_sha("release:purge"),
        retained_evidence=evidence,
        contained_journal=contained,
    )
    with pytest.raises(ContainmentError, match="already consumed"):
        PurgePlanV1.create(
            operation_id=_sha("release:second-purge"),
            retained_evidence=evidence,
            contained_journal=contained,
        )
    assert PurgePlanV1.from_bytes(plan.to_bytes()) == plan
    assert plan.containment_plan_sha256 == containment.digest()
    assert tuple(action.resource_kind for action in plan.actions) == tuple(
        target.target_kind for target in _purge_targets()
    )
    assert tuple(action.resource_kind for action in plan.actions) == (
        "S3_MULTIPART_UPLOAD",
        "S3_OBJECT_VERSION",
        "S3_BUCKET",
        "ECR_IMAGE_REFERENCE",
        "ECR_SIGNING_CONFIGURATION",
        "SIGNER_SIGNING_PROFILE",
        "ECR_REPOSITORY",
        "DYNAMODB_TABLE",
        "CLOUDWATCH_LOG_GROUP",
        "KMS_KEY",
    )

    targets = list(_purge_targets())
    with pytest.raises(ContainmentError, match="safe exact order"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=containment.binding,
            owned_resources=_owned(),
            purge_targets=(targets[1], targets[0], *targets[2:]),
        )
    _, other_evidence, other_contained = _contained_authority(
        tmp_path,
        label="other",
    )
    with pytest.raises(ContainmentError, match="differ"):
        PurgePlanV1.create(
            operation_id=_sha("bad-containment"),
            retained_evidence=evidence,
            contained_journal=other_contained,
        )


@pytest.mark.parametrize(
    ("kind", "identity"),
    (
        ("S3_OBJECT_VERSION", "s3://bucket/prefix/*"),
        ("S3_OBJECT_VERSION", "s3://bucket/key"),
        ("S3_MULTIPART_UPLOAD", "s3://bucket/key?uploadId="),
        ("S3_BUCKET", "s3://bucket/prefix"),
        ("ECR_REPOSITORY", "arn:aws:ecr:eu-west-1:123456789012:repository/prefix*"),
        ("KMS_KEY", "arn:aws:kms:eu-west-1:123456789012:alias/guessed"),
    ),
)
def test_purge_targets_reject_prefix_wildcard_and_incomplete_identity(
    kind: str, identity: str
) -> None:
    with pytest.raises(ContainmentError, match="exact"):
        PurgeTargetV1(
            kind,
            identity,
            _sha("proof"),
            _binding().release_evidence_sha256,
            _sha("evidence"),
        )


def test_purge_rejects_cross_release_target_and_duplicate_exact_target() -> None:
    targets = list(_purge_targets())
    cross_release = replace(targets[0], release_evidence_sha256=_sha("other-release"))
    with pytest.raises(ContainmentError, match="release evidence root"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=_binding(),
            owned_resources=_owned(),
            purge_targets=(cross_release, *targets[1:]),
        )

    targets[0] = replace(
        targets[0], release_evidence_entry_sha256=_binding().release_evidence_sha256
    )
    # An entry digest may equal the root by coincidence, but a missing ownership
    # proof or duplicated exact target is never accepted.
    targets.append(targets[0])
    with pytest.raises(ContainmentError, match="duplicate"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=_binding(),
            owned_resources=_owned(),
            purge_targets=targets,
        )


def test_purge_terminal_records_scheduled_keys_and_never_replays(tmp_path: Path) -> None:
    plan = _purge(tmp_path)
    journal = ContainmentJournalV1.create(tmp_path / "purge", plan=plan)
    provider = FakeContainmentProviderV1.from_plan(plan)
    for action in plan.actions:
        authority = journal.arm_next()
        provider.dispatch(authority, action)
        observation = provider.observe_current(journal)
        if action.resource_kind == "SIGNER_SIGNING_PROFILE":
            assert observation.disposition == "CANCELED"
        journal.reconcile(observation)
    assert journal.state == "PURGED_WITH_SCHEDULED_KEYS"
    assert journal.scheduled_key_count == 1
    assert journal.terminal


def test_purge_without_a_scheduled_key_reaches_exact_purged(tmp_path: Path) -> None:
    targets = tuple(
        target for target in _purge_targets() if target.target_kind != "KMS_KEY"
    )
    evidence = FakeRetainedReleaseEvidenceBoundaryV1.retain(
        binding=_binding(),
        owned_resources=_owned(),
        purge_targets=targets,
    )
    containment = ContainmentPlanV1.create(
        operation_id=_sha("containment-no-key"),
        retained_evidence=evidence,
    )
    containment_journal = ContainmentJournalV1.create(
        tmp_path / "containment-no-key",
        plan=containment,
    )
    containment_provider = FakeContainmentProviderV1.from_plan(containment)
    while not containment_journal.terminal:
        action = containment.actions[containment_journal.cursor]
        containment_provider.dispatch(containment_journal.arm_next(), action)
        containment_journal.reconcile(
            containment_provider.observe_current(containment_journal)
        )
    plan = PurgePlanV1.create(
        operation_id=_sha("purge-no-key"),
        retained_evidence=evidence,
        contained_journal=containment_journal.authorize_purge(evidence),
    )
    journal = ContainmentJournalV1.create(tmp_path / "purge", plan=plan)
    provider = FakeContainmentProviderV1.from_plan(plan)
    while not journal.terminal:
        action = plan.actions[journal.cursor]
        provider.dispatch(journal.arm_next(), action)
        journal.reconcile(provider.observe_current(journal))
    assert journal.state == "PURGED"
    assert journal.scheduled_key_count == 0


def test_fresh_authority_cannot_be_constructed_or_cross_action_consumed(
    tmp_path: Path,
) -> None:
    plan = _containment()
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    authority = journal.arm_next()
    attempt = journal.current_attempt
    assert attempt is not None
    with pytest.raises(ContainmentError, match="not constructible"):
        FreshContainmentAuthorityV1(attempt)
    provider = FakeContainmentProviderV1.from_plan(plan)
    with pytest.raises(ContainmentError, match="binding"):
        provider.dispatch(authority, plan.actions[1])
    assert provider.dispatch_count(plan.actions[0]) == 0


def test_containment_creation_rejects_caller_asserted_resource_inventory() -> None:
    valid = _containment()
    with pytest.raises(ContainmentError, match="retained|evidence|capability"):
        ContainmentPlanV1(
            _sha("self-asserted-containment"),
            valid.binding,
            valid.retained_evidence_sha256,
            valid.owned_resources,
            valid.actions,
        )

    with pytest.raises(ContainmentError, match="not constructible"):
        RetainedReleaseEvidenceV1(
            _binding(),
            _owned(),
            _purge_targets(),
        )


def test_retained_evidence_exact_binds_account_region_and_release() -> None:
    resources = list(_owned())
    resources[0] = replace(
        resources[0],
        resource_identity=(
            "arn:aws:test:eu-west-1:999999999999:resource/cross-account"
        ),
    )
    with pytest.raises(ContainmentError, match="account or region"):
        FakeRetainedReleaseEvidenceBoundaryV1.retain(
            binding=_binding(),
            owned_resources=resources,
            purge_targets=_purge_targets(),
        )


def test_purge_creation_rejects_caller_asserted_contained_digest(
    tmp_path: Path,
) -> None:
    valid = _purge(tmp_path)
    with pytest.raises(ContainmentError, match="terminal|contained|capability"):
        PurgePlanV1(
            _sha("self-asserted-purge"),
            valid.binding,
            valid.retained_evidence_sha256,
            valid.containment_plan_sha256,
            _sha("caller-asserted-contained"),
            valid.targets,
            valid.actions,
        )

    evidence = _retained("not-contained")
    containment = ContainmentPlanV1.create(
        operation_id=_sha("not-contained:operation"),
        retained_evidence=evidence,
    )
    journal = ContainmentJournalV1.create(
        tmp_path / "not-contained",
        plan=containment,
    )
    with pytest.raises(ContainmentError, match="terminal CONTAINED"):
        journal.authorize_purge(evidence)
    with pytest.raises(ContainmentError, match="not constructible"):
        ExactContainedJournalAuthorityV1(
            binding=containment.binding,
            retained_evidence_sha256=containment.retained_evidence_sha256,
            containment_plan_sha256=containment.digest(),
            containment_journal_sha256=_sha("caller"),
        )


def test_observation_cannot_be_caller_minted_and_retains_two_sweep_proofs(
    tmp_path: Path,
) -> None:
    plan = _containment()
    journal = ContainmentJournalV1.create(tmp_path / "journal", plan=plan)
    journal.arm_next()
    with pytest.raises(ContainmentError, match="observer|constructible|token"):
        DestructiveObservationV1(
            _sha("plan"),
            _sha("path"),
            _sha("execution"),
            _sha("attempt"),
            _sha("action"),
            "RESOURCE",
            "arn:aws:test:eu-west-1:123456789012:resource/exact",
            _sha("proof"),
            "ABSENT",
            1,
            _sha("sweep-one"),
            "ABSENT",
            2,
            _sha("sweep-two"),
            _sha("observer"),
        )

    observation = FakeContainmentProviderV1.from_plan(plan).observe_current(journal)
    mapping = observation.to_mapping()
    assert mapping["sweepOneEvidenceSha256"] != mapping["sweepTwoEvidenceSha256"]
    assert mapping["sweepOneSequence"] < mapping["sweepTwoSequence"]
    mapping["sweepTwoSequence"] = mapping["sweepOneSequence"]
    with pytest.raises(ContainmentError, match="distinct and ordered"):
        DestructiveObservationV1._from_mapping(mapping)


def test_deserialized_plan_is_audit_data_not_a_destructive_capability(
    tmp_path: Path,
) -> None:
    plan = _containment()
    parsed = ContainmentPlanV1.from_bytes(plan.to_bytes())
    assert parsed == plan
    with pytest.raises(ContainmentError, match="retained plan capability"):
        ContainmentJournalV1.create(tmp_path / "parsed-plan", plan=parsed)


def test_journal_transition_never_overwrites_substituted_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _containment()
    path = tmp_path / "journal"
    journal = ContainmentJournalV1.create(path, plan=plan)
    attacker = b"attacker replacement\n"
    real_append = containment_module._append_record

    def substitute_then_append(target: Path, **kwargs: object) -> object:
        current = containment_module._record_path(target, int(kwargs["revision"]))
        current.unlink()
        current.write_bytes(attacker)
        current.chmod(0o600)
        return real_append(target, **kwargs)

    monkeypatch.setattr(containment_module, "_append_record", substitute_then_append)
    with pytest.raises(ContainmentError, match="substitut|changed|identity"):
        journal.arm_next()
    assert path.read_bytes() == attacker
