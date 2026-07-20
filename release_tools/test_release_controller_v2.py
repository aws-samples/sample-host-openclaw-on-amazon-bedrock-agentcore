from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import inspect
from pathlib import Path
from types import MappingProxyType

import pytest

from release_tools.agentcore_hardening_v2 import AgentCoreHardeningError
from release_tools.aws_authority_v2 import _authenticate_frozen_source
from release_tools.contracts import (
    ContractError,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.evidence_store_v2 import (
    EvidenceStoreV2Error,
    ReleaseEvidenceStoreV2,
)
from release_tools.release_artifact_store_v2 import ReleaseArtifactStoreV2
from release_tools.release_plan_v2 import ReleasePlanAssemblerV2
from release_tools.release_runner_v2 import (
    RELEASE_KIND_ROUTES_V2,
    ReleaseProviderRouteV2,
    ReleaseRunnerV2Error,
    ResolvedReleaseStepV2,
)
from release_tools.test_aws_authority_v2 import (
    CA_BUNDLE_PATH,
    FrozenCredentials,
    FrozenSession,
    SessionFactory,
    _config_factory,
)
from release_tools.test_release_plan_v2 import _preclosed_source
from release_tools.test_agentcore_hardening_v2 import _runtime
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
)


@dataclass
class _Harness:
    controller: object
    assembled: object
    journal: object
    frozen: FrozenSession
    authority: object
    bundle: object
    store: ReleaseArtifactStoreV2


@pytest.fixture()
def controller_harness(tmp_path: Path):
    from release_tools.release_controller_v2 import (
        AcceptedReleaseControllerV2,
        ReleaseControllerV2Error,
    )

    assembled = ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path / "source")
    )
    store = ReleaseArtifactStoreV2.create(tmp_path / "artifacts")
    bundle = store.persist(assembled)
    journal = _create_v2(tmp_path / "journal", assembled.plan)
    frozen = FrozenSession()
    authority = _authenticate_frozen_source(
        assembled.plan,
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )
    controller = AcceptedReleaseControllerV2(
        plan=assembled.plan,
        authority=authority,
        journal=journal,
        evidence_store=journal.evidence_store,
        artifact_bundle=bundle,
        envelope_directory=(tmp_path / "envelopes").absolute(),
        scratch_directory=(tmp_path / "snapshots").absolute(),
        runtime_context_root=(tmp_path / "runtime-context").absolute(),
    )
    harness = _Harness(
        controller,
        assembled,
        journal,
        frozen,
        authority,
        bundle,
        store,
    )
    try:
        yield harness
    finally:
        authority.close()
        bundle.close()
        store.close()
        journal.evidence_store.close()


def _provider_call_count(harness: _Harness) -> int:
    return sum(len(client.calls) for client in harness.frozen.clients.values())


def test_controller_catalog_has_exact_runner_route_parity() -> None:
    from release_tools.release_controller_v2 import (
        ACCEPTED_RELEASE_ROUTE_SUPPORT_V2,
    )
    from release_tools.release_runner_v2 import RELEASE_KIND_ROUTES_V2

    assert isinstance(ACCEPTED_RELEASE_ROUTE_SUPPORT_V2, MappingProxyType)
    assert set(ACCEPTED_RELEASE_ROUTE_SUPPORT_V2) == set(RELEASE_KIND_ROUTES_V2)
    assert {
        kind: support.lane
        for kind, support in ACCEPTED_RELEASE_ROUTE_SUPPORT_V2.items()
    } == {
        kind: route.lane for kind, route in RELEASE_KIND_ROUTES_V2.items()
    }
    unsupported = {
        kind
        for kind, support in ACCEPTED_RELEASE_ROUTE_SUPPORT_V2.items()
        if not support.supported
    }
    assert unsupported == set()
    assert (
        ACCEPTED_RELEASE_ROUTE_SUPPORT_V2["AGENTCORE_HARDEN"].implementation
        == "AgentCoreHardeningDispatcherV1"
    )


def test_controller_assembles_exact_closed_collaborators_without_provider_calls(
    controller_harness: _Harness,
) -> None:
    from release_tools.release_runner_v2 import ReleaseRunnerCollaboratorsV2

    collaborators = controller_harness.controller.collaborators

    assert type(collaborators) is ReleaseRunnerCollaboratorsV2
    assert collaborators.artifact_bundle is controller_harness.bundle
    assert _provider_call_count(controller_harness) == 0
    for kind, route in RELEASE_KIND_ROUTES_V2.items():
        collaborator = getattr(collaborators, route.lane)
        assert callable(collaborator.observe)
        if route.mutation:
            signature = inspect.signature(collaborator.dispatch)
            assert tuple(signature.parameters) == (
                "resolution",
                "verified_mutation",
                "fresh_authority",
            ), kind


def test_missing_constructor_capability_fails_before_any_provider_effect(
    controller_harness: _Harness,
    tmp_path: Path,
) -> None:
    from release_tools.release_controller_v2 import (
        AcceptedReleaseControllerV2,
        ReleaseControllerV2Error,
    )

    assert _provider_call_count(controller_harness) == 0
    with pytest.raises(
        ReleaseControllerV2Error, match="authenticated AWS authority"
    ):
        AcceptedReleaseControllerV2(
            plan=controller_harness.assembled.plan,
            authority=object(),  # type: ignore[arg-type]
            journal=controller_harness.journal,
            evidence_store=controller_harness.journal.evidence_store,
            artifact_bundle=controller_harness.bundle,
            envelope_directory=(tmp_path / "other-envelopes").absolute(),
            scratch_directory=(tmp_path / "other-snapshots").absolute(),
            runtime_context_root=(tmp_path / "other-context").absolute(),
        )
    assert _provider_call_count(controller_harness) == 0


def test_controller_runs_baseline_through_the_fixed_production_observer(
    controller_harness: _Harness,
) -> None:
    from release_tools.baseline_observer_v2 import BASELINE_STACK_INVENTORY

    class _ExactlyAbsent(Exception):
        def __init__(self, stack_name: str) -> None:
            self.response = {
                "Error": {
                    "Code": "ValidationError",
                    "Message": f"Stack with id {stack_name} does not exist",
                },
                "ResponseMetadata": {"HTTPStatusCode": 400},
            }

    cloudformation = controller_harness.frozen.clients["cloudformation"]

    def describe_stacks(**kwargs: object):
        cloudformation.calls.append(("describe_stacks", dict(kwargs)))
        raise _ExactlyAbsent(str(kwargs["StackName"]))

    cloudformation.describe_stacks = describe_stacks  # type: ignore[attr-defined]

    result = controller_harness.controller.run_one()

    assert result is not None
    assert result.kind == "BASELINE_OBSERVE"
    assert result.action == "OBSERVED_READ_ONLY"
    assert result.state == "PREFLIGHTED"
    assert controller_harness.journal.current.completed_step_count == 1
    assert cloudformation.calls == [
        ("describe_stacks", {"StackName": stack_name})
        for _sweep in range(2)
        for stack_name in BASELINE_STACK_INVENTORY
    ]
    assert all(
        not client.calls
        for service, client in controller_harness.frozen.clients.items()
        if service != "cloudformation"
    )


def test_hardening_dispatch_then_restart_observes_only_receipted_version(
    controller_harness: _Harness,
    tmp_path: Path,
) -> None:
    from release_tools.release_controller_v2 import (
        AcceptedReleaseControllerV2,
        ReleaseControllerV2Error,
    )

    journal = controller_harness.journal
    journal.advance_preflight()
    image_observe = next(
        step
        for step in journal.plan.steps
        if step.phase == "image" and step.kind == "IMAGE_OBSERVE"
    )
    _advance_v2_until_phase(
        journal,
        "runtime:AGENTCORE_HARDEN",
        derived_overrides={
            image_observe.step_id: {
                "runtime_image_digest": journal.plan.runtime_image_digest
            }
        },
    )
    step = journal.plan.steps[journal.current.completed_step_count]
    artifact = next(
        item for item in journal.plan.artifacts if item.path == step.request_artifact
    )
    resolved = _resolved_mutation_request(
        journal, request_artifact_size=artifact.size
    )
    prior = _runtime(resolved, metadata={"requireMMDSV2": False})
    hardened = _runtime(
        resolved,
        version=str(int(resolved.runtime_version) + 1),
        metadata={"requireMMDSV2": True},
        service_s3_endpoint=False,
    )
    raw = controller_harness.frozen.clients["bedrock-agentcore-control"]
    responses = [deepcopy(prior), deepcopy(prior)]

    def get_agent_runtime(**kwargs: object):
        raw.calls.append(("get_agent_runtime", dict(kwargs)))
        return responses.pop(0)

    def update_agent_runtime(**kwargs: object):
        raw.calls.append(("update_agent_runtime", dict(kwargs)))
        return {
            "agentRuntimeId": resolved.runtime_id,
            "agentRuntimeVersion": str(int(resolved.runtime_version) + 1),
            "status": "UPDATING",
        }

    raw.get_agent_runtime = get_agent_runtime  # type: ignore[attr-defined]
    raw.update_agent_runtime = update_agent_runtime  # type: ignore[attr-defined]

    dispatched = controller_harness.controller.run_one()
    assert dispatched is not None
    assert dispatched.kind == "AGENTCORE_HARDEN"
    assert dispatched.action == "DISPATCHED_UNCERTAIN"
    assert journal.current.state == "UNCERTAIN"

    def restarted_controller():
        return AcceptedReleaseControllerV2(
            plan=controller_harness.assembled.plan,
            authority=controller_harness.authority,
            journal=journal,
            evidence_store=journal.evidence_store,
            artifact_bundle=controller_harness.bundle,
            envelope_directory=(
                controller_harness.controller._state.envelope_directory
            ),
            scratch_directory=(tmp_path / "restarted-snapshots").absolute(),
            runtime_context_root=(tmp_path / "restarted-context").absolute(),
        )

    plan_root = journal.evidence_store.root / journal.plan.digest()
    precondition_path = next(
        plan_root.glob("receipt-agentcore-hardening-*-precondition.bin")
    )
    receipt_path = next(
        plan_root.glob("receipt-agentcore-hardening-*-receipt.bin")
    )
    precondition_bytes = precondition_path.read_bytes()
    receipt_bytes = receipt_path.read_bytes()

    def write_record(path: Path, payload: bytes) -> None:
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(payload)
        path.chmod(0o400)

    def reject_damage(path: Path, payload: bytes | None) -> None:
        original = path.read_bytes()
        path.chmod(0o600)
        if payload is None:
            path.unlink()
        else:
            path.write_bytes(payload)
            path.chmod(0o400)
        calls_before = list(raw.calls)
        try:
            with pytest.raises(
                (
                    AgentCoreHardeningError,
                    ContractError,
                    EvidenceStoreV2Error,
                    ReleaseControllerV2Error,
                    ReleaseRunnerV2Error,
                )
            ):
                restarted_controller().run_one()
            assert raw.calls == calls_before
            assert journal.current.state == "UNCERTAIN"
        finally:
            write_record(path, original)

    reject_damage(precondition_path, None)
    reject_damage(precondition_path, precondition_bytes[:-1])
    crossed_precondition = parse_canonical_object(precondition_bytes)
    crossed_precondition["resolvedRequestSha256"] = "f" * 64
    reject_damage(
        precondition_path, canonical_json_bytes(crossed_precondition)
    )
    reject_damage(receipt_path, None)
    reject_damage(receipt_path, receipt_bytes[:-1])
    crossed_receipt = parse_canonical_object(receipt_bytes)
    crossed_receipt["preconditionSha256"] = "f" * 64
    reject_damage(receipt_path, canonical_json_bytes(crossed_receipt))
    duplicate = precondition_path.with_name(
        precondition_path.name.replace(
            "-precondition.bin", "-duplicate-precondition.bin"
        )
    )
    write_record(duplicate, precondition_bytes)
    calls_before_duplicate = list(raw.calls)
    try:
        with pytest.raises(
            (EvidenceStoreV2Error, ReleaseRunnerV2Error),
            match="receipt|inventory|audited",
        ):
            restarted_controller().run_one()
        assert raw.calls == calls_before_duplicate
        assert journal.current.state == "UNCERTAIN"
    finally:
        duplicate.unlink()

    responses.extend([deepcopy(hardened), deepcopy(hardened)])
    restarted = restarted_controller()

    observed = restarted.run_one()

    assert observed is not None
    assert observed.kind == "AGENTCORE_HARDEN"
    assert observed.action == "OBSERVED_UNCERTAIN"
    assert journal.current.state == "RUNTIME_READY"
    assert [name for name, _ in raw.calls] == [
        "get_agent_runtime",
        "get_agent_runtime",
        "update_agent_runtime",
        "get_agent_runtime",
        "get_agent_runtime",
    ]
    assert raw.calls[-2:] == [
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": resolved.runtime_id,
                "agentRuntimeVersion": str(int(resolved.runtime_version) + 1),
            },
        ),
        (
            "get_agent_runtime",
            {
                "agentRuntimeId": resolved.runtime_id,
                "agentRuntimeVersion": str(int(resolved.runtime_version) + 1),
            },
        ),
    ]


def test_crossed_store_and_collaborator_inputs_have_zero_provider_effects(
    controller_harness: _Harness,
    tmp_path: Path,
) -> None:
    from release_tools.release_controller_v2 import (
        AcceptedReleaseControllerV2,
        ReleaseControllerV2Error,
    )

    crossed_store = ReleaseEvidenceStoreV2(tmp_path / "crossed-evidence")
    try:
        with pytest.raises(ReleaseControllerV2Error, match="journal-bound"):
            AcceptedReleaseControllerV2(
                plan=controller_harness.assembled.plan,
                authority=controller_harness.authority,
                journal=controller_harness.journal,
                evidence_store=crossed_store,
                artifact_bundle=controller_harness.bundle,
                envelope_directory=(tmp_path / "crossed-envelopes").absolute(),
                scratch_directory=(tmp_path / "crossed-snapshots").absolute(),
                runtime_context_root=(tmp_path / "crossed-context").absolute(),
            )
    finally:
        crossed_store.close()

    controller_harness.journal.advance_preflight()
    baseline = controller_harness.assembled.plan.steps[0]
    assert baseline.kind == "BASELINE_OBSERVE"
    crossed = ResolvedReleaseStepV2(
        baseline,
        ReleaseProviderRouteV2("ECR", "ecr", False),
        None,
    )
    collaborators = controller_harness.controller.collaborators
    for collaborator in (
        collaborators.cloudformation,
        collaborators.s3,
        collaborators.ecr,
        collaborators.agentcore,
        collaborators.local_filesystem,
        collaborators.verifier,
    ):
        with pytest.raises(ReleaseControllerV2Error, match="crosses"):
            collaborator.observe(resolution=crossed)
        with pytest.raises(ReleaseControllerV2Error, match="exact resolved step"):
            collaborator.observe(resolution=object())
    assert _provider_call_count(controller_harness) == 0


def test_controller_source_has_no_dynamic_driver_or_stdout_evidence_path() -> None:
    import release_tools.release_controller_v2 as controller_module

    source = Path(controller_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "importlib",
        "__import__",
        "subprocess",
        "--driver",
        ".stdout",
    ):
        assert forbidden not in source
