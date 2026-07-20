from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from release_tools.contracts import (
    ContractError,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    write_new_private_mutation_envelope,
)
from release_tools.dispatch_attempt_v2 import (
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
)
from release_tools.evidence_store_v2 import ReleaseEvidenceStoreV2
from release_tools.production_observer_v2 import _new_observation
from release_tools.release_artifact_store_v2 import ReleaseArtifactBundleV2
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _plan_v2,
    _plan_v2_with_request_payload,
)
from release_tools.transaction import ObservationDisposition, TransactionJournalV2


REQUEST = b'{"schema":"runner-test","operation":"bootstrap"}\n'


class _Lane:
    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.observe_calls = 0
        self.dispatch_impl = None
        self.observe_impl = None

    def dispatch(
        self,
        *,
        resolution,
        verified_mutation,
        fresh_authority: FreshDispatchAuthorityV1,
    ) -> ReleaseDispatchAttemptV1:
        self.dispatch_calls += 1
        if self.dispatch_impl is None:
            raise AssertionError("unexpected dispatch")
        return self.dispatch_impl(
            resolution=resolution,
            verified_mutation=verified_mutation,
            fresh_authority=fresh_authority,
        )

    def observe(self, *, resolution):
        self.observe_calls += 1
        if self.observe_impl is None:
            raise AssertionError("unexpected observation")
        return self.observe_impl(resolution=resolution)


def _artifact_bundle(monkeypatch: pytest.MonkeyPatch, writer):
    bundle = object.__new__(ReleaseArtifactBundleV2)
    monkeypatch.setattr(
        ReleaseArtifactBundleV2,
        "write_private_mutation_envelope",
        writer,
    )
    return bundle


def _collaborators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane: _Lane,
    writer,
):
    from release_tools.release_runner_v2 import ReleaseRunnerCollaboratorsV2

    bundle = _artifact_bundle(monkeypatch, writer)
    return ReleaseRunnerCollaboratorsV2(
        artifact_bundle=bundle,
        envelope_directory=tmp_path / "private-envelopes",
        scratch_directory=tmp_path / "private-snapshots",
        cloudformation=lane,
        s3=lane,
        ecr=lane,
        agentcore=lane,
        local_filesystem=lane,
        verifier=lane,
    )


def _bootstrap_journal(tmp_path: Path):
    prototype = _plan_v2()
    ordinal = next(
        step.ordinal
        for step in prototype.steps
        if step.kind == "BOOTSTRAP_STACK"
    )
    plan = _plan_v2_with_request_payload(
        step_ordinal=ordinal,
        payload=REQUEST,
    )
    journal = _create_v2(tmp_path / "journal", plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "foundation:BOOTSTRAP_STACK")
    source = tmp_path / "request.json"
    source.write_bytes(REQUEST)
    return journal, source


def _write_envelope(source: Path, events: list[str]):
    def write(
        _bundle,
        target,
        *,
        resolved_request: ResolvedMutationRequestV2,
        transaction: StagingTransactionV2,
    ):
        events.append("envelope")
        return write_new_private_mutation_envelope(
            Path(target),
            resolved_request=resolved_request,
            request_artifact_path=source,
            plan=resolved_request_plan[0],
            transaction=transaction,
        )

    return write


# Set by the mutation fixtures immediately before the envelope adapter runs.
# Keeping the adapter deliberately tiny makes the runner, not a test driver,
# own the verification and ordering boundary under test.
resolved_request_plan: list[object] = []


def test_route_table_is_complete_fixed_and_immutable() -> None:
    from release_tools.release_runner_v2 import RELEASE_KIND_ROUTES_V2

    assert isinstance(RELEASE_KIND_ROUTES_V2, MappingProxyType)
    assert set(RELEASE_KIND_ROUTES_V2) == {
        "BASELINE_OBSERVE",
        "BOOTSTRAP_STACK",
        "ASSET_PUBLISH",
        "AGENTCORE_HARDEN",
        "STACK_CREATE",
        "STACK_UPDATE",
        "STACK_DRIFT_CHECK",
        "IMAGE_PUBLISH",
        "IMAGE_OBSERVE",
        "RUNTIME_CONTEXT_WRITE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
        "VERIFY",
    }
    assert RELEASE_KIND_ROUTES_V2["ASSET_PUBLISH"].provider == "S3"
    assert RELEASE_KIND_ROUTES_V2["IMAGE_PUBLISH"].provider == "ECR"
    assert RELEASE_KIND_ROUTES_V2["RUNTIME_CONTEXT_WRITE"].provider == (
        "LOCAL_FILESYSTEM"
    )
    assert RELEASE_KIND_ROUTES_V2["AGENTCORE_HARDEN"].lane == "agentcore"
    assert RELEASE_KIND_ROUTES_V2["VERIFY"].lane == "verifier"
    with pytest.raises(TypeError):
        RELEASE_KIND_ROUTES_V2["BOOTSTRAP_STACK"] = object()  # type: ignore[index]


def test_resolver_requires_exact_concrete_journal_store_and_current_binding(
    tmp_path: Path,
) -> None:
    from release_tools.release_runner_v2 import (
        ReleaseRunnerV2Error,
        resolve_current_step_v2,
    )

    journal, _source = _bootstrap_journal(tmp_path)
    other = ReleaseEvidenceStoreV2(tmp_path / "other-evidence")

    with pytest.raises(ReleaseRunnerV2Error, match="concrete v2 journal"):
        resolve_current_step_v2(object(), journal.evidence_store)  # type: ignore[arg-type]
    with pytest.raises(ReleaseRunnerV2Error, match="concrete evidence store"):
        resolve_current_step_v2(journal, object())  # type: ignore[arg-type]
    with pytest.raises(ReleaseRunnerV2Error, match="journal-bound"):
        resolve_current_step_v2(journal, other)
    with pytest.raises(ReleaseRunnerV2Error, match="durable UNCERTAIN"):
        resolve_current_step_v2(journal, journal.evidence_store)


def test_resolver_derives_foundation_only_from_the_audited_retained_owner(
    tmp_path: Path,
) -> None:
    from release_tools.release_runner_v2 import resolve_current_step_v2

    prototype = _plan_v2()
    ordinal = next(
        step.ordinal for step in prototype.steps if step.kind == "IMAGE_PUBLISH"
    )
    plan = _plan_v2_with_request_payload(
        step_ordinal=ordinal,
        payload=REQUEST,
    )
    journal = _create_v2(tmp_path / "downstream", plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "image:IMAGE_PUBLISH")
    journal.begin_step()

    resolution = resolve_current_step_v2(journal, journal.evidence_store)

    assert resolution is not None
    assert resolution.step.kind == "IMAGE_PUBLISH"
    assert resolution.resolved_request is not None
    foundation = resolution.resolved_request.foundation_runtime_inputs
    assert foundation is not None
    assert foundation.digest() == journal.current.foundation_inputs_sha256
    assert foundation.agent_core_stack_id == journal.current.agent_core_stack_id


def test_mutation_writes_intent_then_verifies_envelope_then_arms_then_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerV2

    journal, source = _bootstrap_journal(tmp_path)
    resolved_request_plan[:] = [journal.plan]
    events: list[str] = []
    lane = _Lane()

    def dispatch(*, resolution, verified_mutation, fresh_authority):
        assert journal.current.state == "UNCERTAIN"
        retained = journal.evidence_store.dispatch_attempt_state(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )
        assert retained.attempted
        assert events == ["envelope"]
        assert verified_mutation.resolved_request == resolution.resolved_request
        events.append("dispatch-authority")
        attempt = fresh_authority.consume(
            provider=resolution.route.provider,
            operation_sha256=(
                resolution.resolved_request.mutation_request.operation_sha256
            ),
            resolved_request_sha256=resolution.resolved_request.digest(),
        )
        events.append("effect")
        return attempt

    lane.dispatch_impl = dispatch
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=_write_envelope(source, events),
    )

    result = ReleaseRunnerV2(
        journal=journal,
        evidence_store=journal.evidence_store,
        collaborators=collaborators,
    ).run_one()

    assert result is not None
    assert result.action == "DISPATCHED_UNCERTAIN"
    assert result.provider == "CLOUDFORMATION"
    assert journal.current.state == "UNCERTAIN"
    assert events == ["envelope", "dispatch-authority", "effect"]
    assert lane.dispatch_calls == 1
    assert lane.observe_calls == 0

    attempted_revision = journal.current.revision

    def observe(*, resolution):
        return _new_observation(
            service="cloudformation",
            operation="describe_stacks",
            subject=resolution.step.subject,
            disposition=ObservationDisposition.PENDING,
            provider_status="CREATE_IN_PROGRESS",
            projection={"runnerTest": True},
        )

    lane.observe_impl = observe
    resumed = ReleaseRunnerV2(
        journal=journal,
        evidence_store=journal.evidence_store,
        collaborators=collaborators,
    ).run_one()

    assert resumed is not None
    assert resumed.action == "OBSERVED_UNCERTAIN"
    assert journal.current.state == "UNCERTAIN"
    assert journal.current.revision == attempted_revision
    assert lane.dispatch_calls == 1
    assert lane.observe_calls == 1


def test_arm_failure_keeps_uncertain_and_never_calls_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerV2

    journal, source = _bootstrap_journal(tmp_path)
    resolved_request_plan[:] = [journal.plan]
    events: list[str] = []
    lane = _Lane()

    def reject_arm(*_args, **_kwargs):
        events.append("arm-rejected")
        raise RuntimeError("marker already exists")

    monkeypatch.setattr(
        ReleaseEvidenceStoreV2,
        "arm_current_dispatch",
        reject_arm,
        raising=False,
    )
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=_write_envelope(source, events),
    )

    with pytest.raises(RuntimeError, match="marker already exists"):
        ReleaseRunnerV2(
            journal=journal,
            evidence_store=journal.evidence_store,
            collaborators=collaborators,
        ).run_one()

    assert journal.current.state == "UNCERTAIN"
    assert events == ["envelope", "arm-rejected"]
    assert lane.dispatch_calls == 0


def test_dispatcher_cannot_return_retained_marker_without_consuming_fresh_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import (
        ReleaseRunnerV2,
        ReleaseRunnerV2Error,
    )

    journal, source = _bootstrap_journal(tmp_path)
    resolved_request_plan[:] = [journal.plan]
    events: list[str] = []
    lane = _Lane()

    def dispatch(**_kwargs):
        retained = journal.evidence_store.dispatch_attempt_state(
            plan=journal.plan,
            transaction=journal.current,
            journal_path=journal.path,
            journal_execution_id=journal.journal_execution_id,
        )
        assert retained.attempt is not None
        return retained.attempt

    lane.dispatch_impl = dispatch
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=_write_envelope(source, events),
    )

    with pytest.raises(ReleaseRunnerV2Error, match="did not consume"):
        ReleaseRunnerV2(
            journal=journal,
            evidence_store=journal.evidence_store,
            collaborators=collaborators,
        ).run_one()

    assert journal.current.state == "UNCERTAIN"
    assert lane.dispatch_calls == 1


def test_invalid_envelope_fails_before_attempt_arm_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerV2

    journal, _source = _bootstrap_journal(tmp_path)
    lane = _Lane()
    arm_calls = 0

    def corrupt(_bundle, target, **_kwargs):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-an-envelope")
        return object()

    def arm(*_args, **_kwargs):
        nonlocal arm_calls
        arm_calls += 1
        raise AssertionError("attempt must not be armed")

    monkeypatch.setattr(
        ReleaseEvidenceStoreV2,
        "arm_current_dispatch",
        arm,
        raising=False,
    )
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=corrupt,
    )

    with pytest.raises((ContractError, OSError)):
        ReleaseRunnerV2(
            journal=journal,
            evidence_store=journal.evidence_store,
            collaborators=collaborators,
        ).run_one()

    assert journal.current.state == "UNCERTAIN"
    assert arm_calls == 0
    assert lane.dispatch_calls == 0


def test_preexisting_uncertain_intent_is_observed_and_never_redispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerV2

    journal, _source = _bootstrap_journal(tmp_path)
    journal.begin_step()
    lane = _Lane()

    def observe(*, resolution):
        assert resolution.resolved_request is not None
        return _new_observation(
            service="cloudformation",
            operation="describe_stacks",
            subject=resolution.step.subject,
            disposition=ObservationDisposition.PENDING,
            provider_status="CREATE_IN_PROGRESS",
            projection={"runnerTest": True},
        )

    lane.observe_impl = observe
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("UNCERTAIN resume must not rewrite an envelope")
        ),
    )

    result = ReleaseRunnerV2(
        journal=journal,
        evidence_store=journal.evidence_store,
        collaborators=collaborators,
    ).run_one()

    assert result is not None
    assert result.action == "OBSERVED_UNCERTAIN"
    assert journal.current.state == "UNCERTAIN"
    assert lane.observe_calls == 1
    assert lane.dispatch_calls == 0


def test_read_only_step_auto_preflights_and_uses_only_observer_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerV2

    journal = _create_v2(tmp_path / "baseline")
    lane = _Lane()

    def observe(*, resolution):
        assert resolution.step.kind == "BASELINE_OBSERVE"
        assert resolution.resolved_request is None
        return _new_observation(
            service="cloudformation",
            operation="describe_stacks",
            subject=resolution.step.subject,
            disposition=ObservationDisposition.PENDING,
            provider_status="INVENTORY_UNSTABLE",
            projection={"runnerTest": True},
        )

    lane.observe_impl = observe
    collaborators = _collaborators(
        tmp_path,
        monkeypatch,
        lane=lane,
        writer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only step must not create an envelope")
        ),
    )

    result = ReleaseRunnerV2(
        journal=journal,
        evidence_store=journal.evidence_store,
        collaborators=collaborators,
    ).run_one()

    assert result is not None
    assert result.action == "OBSERVED_READ_ONLY"
    assert journal.current.state == "PREFLIGHTED"
    assert journal.current.revision == 2
    assert lane.observe_calls == 1
    assert lane.dispatch_calls == 0
