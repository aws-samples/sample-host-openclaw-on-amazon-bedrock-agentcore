"""Hostile tests for the exact stack-drift dispatch/observation boundary."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from botocore.config import Config
from botocore.session import Session
from botocore.stub import Stubber
import pytest

from release_tools.aws_authority_v2 import AttestedAwsClientV2, _CLIENT_TOKEN
from release_tools.contracts import (
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    RetainedStepEvidenceV2,
    ResolvedMutationRequestV2,
    StagingTransactionV2,
    StackDriftDispatchReceiptV1,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    write_new_private_mutation_envelope,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)
from release_tools.production_observer_v2 import (
    CanonicalReadObservationV2,
    ProductionObserverV2Error,
    _new_observation,
)
from release_tools.stack_drift_v2 import (
    StackDriftDispatchAmbiguous,
    StackDriftDispatcherV1,
    StackDriftError,
    StackDriftObservationAmbiguous,
    StackDriftObserverV1,
    StackDriftOperationV1,
    StackDriftReceiptSinkV1,
    VerifiedStackDriftDispatchV1,
    VerifiedStackDriftPreflightV1,
    VerifiedStackDriftReceiptV1,
    _new_stack_drift_receipt_sink,
    _verified_retained_receipt,
    validate_stack_drift_dispatch,
    validate_stack_drift_preflight,
)
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
)
from release_tools.transaction import ObservationDisposition


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
STACK_NAME = "CDKToolkit"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_NAME}/"
    "12345678-1234-1234-1234-123456789abc"
)
OTHER_STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_NAME}/"
    "87654321-4321-4321-4321-cba987654321"
)
DETECTION_ID = "12345678-1234-1234-1234-123456789abc"
DRIFT_TIMESTAMP = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
REVIEWED_RESOURCE_DRIFT_STATUSES = ["IN_SYNC", "MODIFIED", "DELETED"]
PRODUCTION_DRIFT_OCCURRENCES = (
    ("foundation", "CDKToolkit", "foundation-drift-cdktoolkit"),
    ("foundation", "OpenClawVpc", "foundation-drift-openclawvpc"),
    ("foundation", "OpenClawSecurity", "foundation-drift-openclawsecurity"),
    ("foundation", "OpenClawGuardrails", "foundation-drift-openclawguardrails"),
    (
        "foundation",
        "PersonalOperatorCapabilities",
        "foundation-drift-personaloperatorcapabilities",
    ),
    ("foundation", "OpenClawAgentCore", "foundation-drift-openclawagentcore"),
    (
        "foundation",
        "OpenClawObservability",
        "foundation-drift-openclawobservability",
    ),
    ("runtime", "OpenClawAgentCore", "runtime-drift-agentcore"),
    ("endpoint", "OpenClawAgentCore", "endpoint-drift-agentcore"),
    ("router-cron", "OpenClawRouter", "router-cron-drift-openclawrouter"),
    ("router-cron", "OpenClawCron", "router-cron-drift-openclawcron"),
    (
        "scheduler",
        "PersonalOperatorScheduler",
        "scheduler-drift-personaloperatorscheduler",
    ),
    ("web", "PersonalOperatorWeb", "web-drift-personaloperatorweb"),
)


def _operation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": StackDriftOperationV1.SCHEMA,
        "account": ACCOUNT,
        "region": REGION,
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "stackName": STACK_NAME,
        "phase": "foundation",
        "occurrence": "foundation-drift-cdktoolkit",
    }
    value.update(overrides)
    return value


def _plan_for_operation(payload: bytes) -> ReleasePlanV2:
    operation = StackDriftOperationV1.from_bytes(payload)
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    step = next(
        item
        for item in steps
        if item["kind"] == "STACK_DRIFT_CHECK"
        and item["phase"] == operation.phase
        and item["subject"] == operation.subject
    )
    artifact = next(
        item for item in artifacts if item["path"] == step["requestArtifact"]
    )
    digest = hashlib.sha256(payload).hexdigest()
    step["id"] = operation.occurrence
    step["requestSha256"] = digest
    step["expectedRequestSha256"] = digest
    artifact["sha256"] = digest
    artifact["size"] = len(payload)
    return ReleasePlanV2.from_mapping(value)


def _predecessor_record(journal: object) -> RetainedStepEvidenceV2:
    current = journal.current
    evidence_sha256 = current.completed_steps[-1].evidence_sha256
    records = journal.evidence_store._all_records(
        plan_sha256=journal.plan.digest()
    )
    retained = next(record for record in records if record.digest() == evidence_sha256)
    return retained


@contextmanager
def _verified_drift(
    tmp_path: Path,
    *,
    raw: dict[str, object] | None = None,
) -> Iterator[
    tuple[
        VerifiedPrivateMutationV2,
        VerifiedStackDriftPreflightV1,
        StackDriftReceiptSinkV1,
        "MemoryReceiptBackend",
    ]
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(raw or _operation())
    plan = _plan_for_operation(payload)
    preflight = validate_stack_drift_preflight(
        StackDriftOperationV1.from_bytes(payload), release_plan=plan
    )
    journal = _create_v2(tmp_path / "journal", plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "foundation:BOOTSTRAP_STACK")
    predecessor_step = plan.steps[journal.current.completed_step_count]
    journal.begin_step()
    provider_observation = _new_observation(
        service="cloudformation",
        operation="describe_stacks",
        subject=predecessor_step.subject,
        disposition=ObservationDisposition.PRESENT,
        provider_status="CREATE_COMPLETE",
        projection={
            "stackId": STACK_ID,
            "stackName": STACK_NAME,
            "stackStatus": "CREATE_COMPLETE",
            "templateSha256": "1" * 64,
            "templateParameterSha256": (
                predecessor_step.expected_template_parameter_sha256
            ),
            "observedRequestSha256": (
                predecessor_step.expected_observed_request_sha256
            ),
            "parameters": [],
            "outputs": {},
        },
    )
    composer = journal.evidence_store.composer(
        plan=plan,
        journal_path=journal.path,
        journal_execution_id=journal.journal_execution_id,
    )
    predecessor_outcome = composer.compose(
        transaction=journal.current,
        provider_observation=provider_observation,
    )
    journal.reconcile_step(outcome=predecessor_outcome)
    assert journal.resume_step()["kind"] == "STACK_DRIFT_CHECK"
    journal.begin_step()
    predecessor = _predecessor_record(journal)
    resolved_value = _resolved_mutation_request(
        journal, request_artifact_size=len(payload)
    ).to_mapping()
    resolved_value.update(
        {
            "predecessorStackId": STACK_ID,
            "predecessorEvidenceSha256": predecessor.digest(),
            "predecessorObserverEvidenceSha256": (
                predecessor.observer_evidence_sha256
            ),
        }
    )
    resolved = ResolvedMutationRequestV2.from_mapping(resolved_value)
    request_path = tmp_path / "stack-drift-operation.json"
    request_path.write_bytes(payload)
    envelope_path = tmp_path / "private-mutation.bin"
    write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=resolved,
        request_artifact_path=request_path,
        plan=plan,
        transaction=journal.current,
    )
    backend = MemoryReceiptBackend()
    backend.transaction = journal.current
    backend.predecessor = predecessor
    sink = _new_stack_drift_receipt_sink(
        backend,
        transaction=journal.current,
        predecessor_evidence=predecessor,
    )
    with PrivateMutationEnvelopeV2.open_verified(
        envelope_path,
        plan=plan,
        transaction=journal.current,
        scratch_dir=tmp_path / "scratch",
    ) as verified:
        yield verified, preflight, sink, backend


class MemoryReceiptBackend:
    def __init__(self) -> None:
        self.attempted = False
        self.payload: bytes | None = None
        self.retain_replacement: bytes | None = None
        self.retain_error: BaseException | None = None
        self.load_error_after_retain: BaseException | None = None
        self.transaction: StagingTransactionV2 | None = None
        self.predecessor: RetainedStepEvidenceV2 | None = None

    def load(self) -> tuple[bool, bytes | None]:
        if self.payload is not None and self.load_error_after_retain is not None:
            raise self.load_error_after_retain
        return self.attempted, self.payload

    def begin_attempt(self) -> bool:
        if self.attempted:
            return False
        self.attempted = True
        return True

    def retain(self, payload: bytes) -> None:
        if self.retain_error is not None:
            raise self.retain_error
        self.payload = self.retain_replacement or payload


class FakeCloudFormation:
    def __init__(
        self,
        *,
        account: str = ACCOUNT,
        region: str = REGION,
        service: str = "cloudformation",
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, list[object]] = {}
        self._personal_operator_attested_account = account
        self.meta = SimpleNamespace(
            region_name=region,
            service_model=SimpleNamespace(service_name=service),
            config=SimpleNamespace(
                region_name=region,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    def queue(self, method: str, *responses: object) -> None:
        self.responses.setdefault(method, []).extend(responses)

    def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kwargs: object) -> object:
            self.calls.append((name, kwargs))
            queued = self.responses.get(name, [])
            if not queued:
                raise AssertionError(f"unexpected cloudformation.{name}")
            result = queued.pop(0)
            if isinstance(result, BaseException):
                raise result
            return deepcopy(result)

        return call


def _status(
    *,
    detection_status: str = "DETECTION_COMPLETE",
    drift_status: str | None = "IN_SYNC",
    count: int | None = 0,
    stack_id: str = STACK_ID,
    detection_id: str = DETECTION_ID,
    reason: str | None = None,
    timestamp: datetime = DRIFT_TIMESTAMP,
) -> dict[str, object]:
    value: dict[str, object] = {
        "StackId": stack_id,
        "StackDriftDetectionId": detection_id,
        "DetectionStatus": detection_status,
        "Timestamp": timestamp,
    }
    if drift_status is not None:
        value["StackDriftStatus"] = drift_status
    if count is not None:
        value["DriftedStackResourceCount"] = count
    if reason is not None:
        value["DetectionStatusReason"] = reason
    return value


def _stack(
    *,
    stack_id: str = STACK_ID,
    status: str = "CREATE_COMPLETE",
    drift_status: str = "IN_SYNC",
    drift_timestamp: datetime = DRIFT_TIMESTAMP,
) -> dict[str, object]:
    return {
        "Stacks": [
            {
                "StackId": stack_id,
                "StackName": STACK_NAME,
                "StackStatus": status,
                "EnableTerminationProtection": True,
                "Parameters": [],
                "Outputs": [],
                "Tags": [
                    {"Key": "SourceCommit", "Value": COMMIT},
                    {"Key": "SourceTree", "Value": TREE},
                ],
                "DriftInformation": {
                    "StackDriftStatus": drift_status,
                    "LastCheckTimestamp": drift_timestamp,
                },
            }
        ]
    }


def _template(marker: str = "one") -> dict[str, object]:
    return {
        "StagesAvailable": ["Original", "Processed"],
        "TemplateBody": {"Resources": {}, "Metadata": {"marker": marker}},
    }


def _queue_present(fake: FakeCloudFormation) -> None:
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue("describe_stack_resource_drifts", {"StackResourceDrifts": []})
    fake.queue("describe_stacks", _stack(), _stack())
    fake.queue("get_template", _template(), _template())
    fake.queue("get_stack_policy", {"StackPolicyBody": ""}, {"StackPolicyBody": ""})


def _resource_drift(
    *,
    status: str = "IN_SYNC",
    stack_id: str = STACK_ID,
    timestamp: datetime = DRIFT_TIMESTAMP,
) -> dict[str, object]:
    return {
        "StackId": stack_id,
        "LogicalResourceId": "ReviewedResource",
        "ResourceType": "AWS::S3::Bucket",
        "StackResourceDriftStatus": status,
        "Timestamp": timestamp,
    }


def _botocore_cloudformation_client() -> object:
    return Session().create_client(
        "cloudformation",
        region_name=REGION,
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
        endpoint_url=f"https://cloudformation.{REGION}.amazonaws.com",
        config=Config(
            region_name=REGION,
            ignore_configured_endpoint_urls=True,
            proxies={},
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )


def _dispatched(
    tmp_path: Path,
    fake: FakeCloudFormation | None = None,
) -> tuple[VerifiedStackDriftReceiptV1, FakeCloudFormation, MemoryReceiptBackend]:
    provider = fake or FakeCloudFormation()
    provider.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(provider, service="cloudformation") as client:
            attempt = StackDriftDispatcherV1(client).dispatch(
                authority,
                _fresh_dispatch_authority(authority),
            )
        assert attempt.provider == "CLOUDFORMATION"
        receipt = _verified_receipt_from_backend(authority, backend)
    return receipt, provider, backend


def _fresh_dispatch_authority(
    authority: VerifiedStackDriftDispatchV1,
    *,
    provider: str = "CLOUDFORMATION",
    operation_sha256: str | None = None,
    resolved_request_sha256: str | None = None,
) -> FreshDispatchAuthorityV1:
    (
        operation,
        resolved,
        _sink,
        plan,
        transaction,
        predecessor,
    ) = authority._binding()
    request = resolved.mutation_request
    attempt = ReleaseDispatchAttemptV1.from_mapping(
        {
            "schema": ReleaseDispatchAttemptV1.SCHEMA,
            "releasePlanSha256": plan.digest(),
            "evidenceStoreSha256": predecessor.evidence_store_sha256,
            "journalPathSha256": predecessor.journal_path_sha256,
            "journalExecutionId": predecessor.journal_execution_id,
            "journalRevision": transaction.revision,
            "completedPrefixSha256": request.completed_prefix_sha256,
            "stepId": request.step_id,
            "subject": operation.subject,
            "operationSha256": operation_sha256 or request.operation_sha256,
            "resolvedRequestSha256": (
                resolved_request_sha256 or resolved.digest()
            ),
            "provider": provider,
        }
    )
    return _mint_fresh_dispatch_authority(attempt)


def _verified_receipt_from_backend(
    authority: VerifiedStackDriftDispatchV1,
    backend: MemoryReceiptBackend,
) -> VerifiedStackDriftReceiptV1:
    payload = backend.payload
    assert payload is not None
    operation, resolved, _sink, plan, transaction, predecessor = (
        authority._binding()
    )
    return _verified_retained_receipt(
        payload,
        resolved=resolved,
        plan=plan,
        transaction=transaction,
        predecessor=predecessor,
        stack_id=(
            f"arn:aws:cloudformation:{operation.region}:{operation.account}:"
            f"stack/{operation.stack_name}/"
            "12345678-1234-1234-1234-123456789abc"
        ),
    )


@pytest.mark.parametrize(
    "mode",
    (
        "missing",
        "duck",
        "crossed-provider",
        "crossed-operation",
        "crossed-resolved",
        "consumed",
    ),
)
def test_dispatch_requires_exact_fresh_attempt_before_any_provider_effect(
    tmp_path: Path,
    mode: str,
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        fresh: object | None
        if mode == "missing":
            fresh = None
        elif mode == "duck":
            fresh = SimpleNamespace(consume=lambda **_kwargs: None)
        elif mode == "crossed-provider":
            fresh = _fresh_dispatch_authority(authority, provider="S3")
        elif mode == "crossed-operation":
            fresh = _fresh_dispatch_authority(
                authority, operation_sha256="sha256:" + "0" * 64
            )
        elif mode == "crossed-resolved":
            fresh = _fresh_dispatch_authority(
                authority, resolved_request_sha256="0" * 64
            )
        else:
            fresh = _fresh_dispatch_authority(authority)
            assert isinstance(fresh, FreshDispatchAuthorityV1)
            bound = authority._binding()[1]
            fresh.consume(
                provider="CLOUDFORMATION",
                operation_sha256=bound.mutation_request.operation_sha256,
                resolved_request_sha256=bound.digest(),
            )
        with attested_test_client(fake, service="cloudformation") as client:
            with pytest.raises((StackDriftError, DispatchAttemptError)):
                StackDriftDispatcherV1(client).dispatch(authority, fresh)
        assert backend.attempted is False
        assert backend.payload is None
    assert fake.calls == []


def test_stack_drift_operation_is_strict_canonical_and_closed() -> None:
    parsed = StackDriftOperationV1.from_bytes(canonical_json_bytes(_operation()))

    assert parsed.to_mapping() == _operation()
    assert parsed.to_bytes() == canonical_json_bytes(_operation())
    assert parsed.digest() == hashlib.sha256(parsed.to_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        ({"extra": True}, "fields"),
        ({"account": "000000000000"}, "account"),
        ({"region": "us-east-1"}, "region"),
        ({"sourceCommit": "A" * 40}, "commit"),
        ({"sourceTree": "B" * 40}, "tree"),
        ({"stackName": "PersonalOperatorBrowser"}, "stack"),
        ({"phase": "image"}, "phase"),
        ({"occurrence": "runtime-drift-agentcore"}, "phase|occurrence"),
    ],
)
def test_stack_drift_operation_rejects_open_or_cross_subject_input(
    damage: dict[str, object], match: str
) -> None:
    with pytest.raises(StackDriftError, match=match):
        StackDriftOperationV1.from_bytes(canonical_json_bytes(_operation(**damage)))


def test_stack_drift_preflight_binds_unique_exact_plan_artifact() -> None:
    payload = canonical_json_bytes(_operation())
    plan = _plan_for_operation(payload)
    preflight = validate_stack_drift_preflight(
        StackDriftOperationV1.from_bytes(payload), release_plan=plan
    )

    assert isinstance(preflight, VerifiedStackDriftPreflightV1)

    hostile = plan.to_mapping()
    drift = next(
        step for step in hostile["steps"] if step["kind"] == "STACK_DRIFT_CHECK"
    )
    drift["subject"] = str(drift["subject"]).replace(":drift", ":other:drift")
    with pytest.raises(Exception, match="drift|subject"):
        validate_stack_drift_preflight(
            StackDriftOperationV1.from_bytes(payload),
            release_plan=ReleasePlanV2.from_mapping(hostile),
        )

    crossed_occurrence = plan.to_mapping()
    crossed_step = next(
        step
        for step in crossed_occurrence["steps"]
        if step["requestSha256"] == hashlib.sha256(payload).hexdigest()
    )
    crossed_step["id"] = "foundation-drift-openclawvpc"
    with pytest.raises(StackDriftError, match="exact plan step"):
        validate_stack_drift_preflight(
            StackDriftOperationV1.from_bytes(payload),
            release_plan=ReleasePlanV2.from_mapping(crossed_occurrence),
        )


def test_all_13_production_drift_occurrences_are_unique_and_preflight_bound() -> None:
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    payloads: list[bytes] = []
    for phase, stack_name, occurrence in PRODUCTION_DRIFT_OCCURRENCES:
        subject = (
            f"cfn:{ACCOUNT}:{REGION}:stack:{stack_name}:release:{COMMIT}:drift"
        )
        matches = [
            step
            for step in steps
            if step["kind"] == "STACK_DRIFT_CHECK"
            and step["phase"] == phase
            and step["subject"] == subject
        ]
        assert len(matches) == 1
        step = matches[0]
        payload = canonical_json_bytes(
            _operation(
                stackName=stack_name,
                phase=phase,
                occurrence=occurrence,
            )
        )
        digest = hashlib.sha256(payload).hexdigest()
        step["id"] = occurrence
        step["requestSha256"] = digest
        step["expectedRequestSha256"] = digest
        artifact = next(
            item for item in artifacts if item["path"] == step["requestArtifact"]
        )
        artifact["sha256"] = digest
        artifact["size"] = len(payload)
        payloads.append(payload)

    plan = ReleasePlanV2.from_mapping(value)
    assert len(payloads) == 13
    assert len({hashlib.sha256(item).digest() for item in payloads}) == 13
    for payload in payloads:
        preflight = validate_stack_drift_preflight(
            StackDriftOperationV1.from_bytes(payload),
            release_plan=plan,
        )
        assert isinstance(preflight, VerifiedStackDriftPreflightV1)


def test_dispatch_calls_exact_retained_stack_id_once_and_synchronously_retains_receipt(
    tmp_path: Path,
) -> None:
    receipt, fake, backend = _dispatched(tmp_path)
    transaction = backend.transaction
    predecessor = backend.predecessor
    assert transaction is not None
    assert predecessor is not None
    retained = receipt.receipt

    assert fake.calls == [("detect_stack_drift", {"StackName": STACK_ID})]
    assert backend.attempted is True
    assert backend.payload == retained.to_bytes()
    assert retained.drift_detection_id == DETECTION_ID
    assert retained.stack_id == STACK_ID
    assert retained.journal_revision == transaction.revision
    assert retained.evidence_store_sha256 == predecessor.evidence_store_sha256
    assert retained.journal_path_sha256 == predecessor.journal_path_sha256
    assert retained.journal_execution_id == predecessor.journal_execution_id
    assert retained.predecessor_evidence_sha256 == predecessor.digest()
    assert (
        retained.predecessor_observer_evidence_sha256
        == predecessor.observer_evidence_sha256
    )


def test_identical_retained_receipt_blocks_replay_without_second_provider_call(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, _):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            first = dispatcher.dispatch(
                authority, _fresh_dispatch_authority(authority)
            )
            with pytest.raises(StackDriftDispatchAmbiguous, match="replayed"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )

    assert first.provider == "CLOUDFORMATION"
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        RuntimeError("unknown provider effect"),
        {},
        [],
        {"StackDriftDetectionId": "not-a-uuid"},
    ],
)
def test_provider_exception_or_malformed_response_is_permanently_ambiguous(
    tmp_path: Path, response: object
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", response)
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            with pytest.raises(StackDriftDispatchAmbiguous, match="UNCERTAIN"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
            with pytest.raises(StackDriftDispatchAmbiguous, match="UNCERTAIN"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )

    assert backend.attempted is True
    assert backend.payload is None
    assert len(fake.calls) == 1


def test_crash_or_retention_mismatch_after_dispatch_never_authorizes_retry(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        backend.retain_replacement = b"{}\n"
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            with pytest.raises(StackDriftDispatchAmbiguous, match="receipt"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
            with pytest.raises(StackDriftError, match="receipt"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )

    assert len(fake.calls) == 1


def test_post_effect_receipt_read_failure_stays_uncertain_without_retry(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        backend.load_error_after_retain = OSError("receipt store unavailable")
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            with pytest.raises(StackDriftDispatchAmbiguous, match="UNCERTAIN"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
            backend.load_error_after_retain = None
            with pytest.raises(StackDriftDispatchAmbiguous, match="replayed"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
            retained = _verified_receipt_from_backend(authority, backend)

    assert retained.receipt.drift_detection_id == DETECTION_ID
    assert len(fake.calls) == 1


def test_missing_receipt_after_prior_attempt_is_uncertain_without_provider_call(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        backend.attempted = True
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            with pytest.raises(StackDriftDispatchAmbiguous, match="UNCERTAIN"):
                StackDriftDispatcherV1(client).dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
    assert fake.calls == []


@pytest.mark.parametrize("authority_damage", ("transaction", "predecessor"))
def test_dispatch_authority_rejects_rebound_current_state_or_predecessor_record(
    tmp_path: Path,
    authority_damage: str,
) -> None:
    with _verified_drift(tmp_path) as (verified, preflight, _, fixture):
        transaction = fixture.transaction
        predecessor = fixture.predecessor
        assert transaction is not None
        assert predecessor is not None
        if authority_damage == "transaction":
            transaction = replace(transaction, revision=transaction.revision + 1)
        else:
            predecessor = replace(
                predecessor,
                evidence_store_sha256="9" * 64,
            )
        hostile_sink = _new_stack_drift_receipt_sink(
            MemoryReceiptBackend(),
            transaction=transaction,
            predecessor_evidence=predecessor,
        )

        with pytest.raises(StackDriftError, match="predecessor|transaction|authority"):
            validate_stack_drift_dispatch(verified, preflight, hostile_sink)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("releasePlanSha256", "9" * 64),
        (
            "subject",
            f"cfn:999999999999:{REGION}:stack:{STACK_NAME}:release:{COMMIT}:drift",
        ),
        (
            "subject",
            f"cfn:{ACCOUNT}:us-east-1:stack:{STACK_NAME}:release:{COMMIT}:drift",
        ),
        (
            "subject",
            f"cfn:{ACCOUNT}:{REGION}:stack:PersonalOperatorWeb:release:{COMMIT}:drift",
        ),
        ("stackId", OTHER_STACK_ID),
        ("driftDetectionId", "87654321-4321-4321-4321-cba987654321"),
        ("predecessorEvidenceSha256", "8" * 64),
        ("predecessorObserverEvidenceSha256", "7" * 64),
    ),
)
def test_crossed_or_duplicate_retained_receipt_never_redirects_dispatch(
    tmp_path: Path, field: str, value: object
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            dispatcher.dispatch(authority, _fresh_dispatch_authority(authority))
            receipt = _verified_receipt_from_backend(authority, backend)
            hostile = receipt.receipt.to_mapping()
            hostile[field] = value
            backend.payload = canonical_json_bytes(hostile)
            with pytest.raises(StackDriftError, match="receipt"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )

    assert len(fake.calls) == 1


def test_concatenated_duplicate_receipt_bytes_are_rejected_without_retry(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    fake.queue("detect_stack_drift", {"StackDriftDetectionId": DETECTION_ID})
    with _verified_drift(tmp_path) as (verified, preflight, sink, backend):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with attested_test_client(fake, service="cloudformation") as client:
            dispatcher = StackDriftDispatcherV1(client)
            dispatcher.dispatch(authority, _fresh_dispatch_authority(authority))
            receipt = _verified_receipt_from_backend(authority, backend)
            backend.payload = receipt.receipt.to_bytes() * 2
            with pytest.raises(StackDriftError, match="receipt"):
                dispatcher.dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )

    assert len(fake.calls) == 1


def test_dispatch_and_observer_reject_raw_or_crossed_clients(tmp_path: Path) -> None:
    raw = FakeCloudFormation()
    with _verified_drift(tmp_path / "dispatch") as (verified, preflight, sink, _):
        authority = validate_stack_drift_dispatch(verified, preflight, sink)
        with pytest.raises(StackDriftError, match="attested"):
            StackDriftDispatcherV1(raw).dispatch(
                authority, _fresh_dispatch_authority(authority)
            )
    assert raw.calls == []

    receipt, _, _ = _dispatched(tmp_path / "observer")
    with pytest.raises(StackDriftError, match="attested"):
        StackDriftObserverV1(raw).observe(receipt)

    crossed = AttestedAwsClientV2(
        FakeCloudFormation(account="999999999999"),
        service="cloudformation",
        account="999999999999",
        region=REGION,
        capability="observer",
        _token=_CLIENT_TOKEN,
    )
    try:
        with pytest.raises(StackDriftError, match="authority"):
            StackDriftObserverV1(crossed).observe(receipt)
    finally:
        crossed.close()


@pytest.mark.parametrize(
    ("service", "account", "region", "capability"),
    (
        ("cloudformation", "999999999999", REGION, "mutation"),
        ("cloudformation", ACCOUNT, "us-east-1", "mutation"),
        ("s3", ACCOUNT, REGION, "mutation"),
        ("cloudformation", ACCOUNT, REGION, "observer"),
    ),
)
def test_dispatch_rejects_every_crossed_attested_client_scope(
    tmp_path: Path,
    service: str,
    account: str,
    region: str,
    capability: str,
) -> None:
    fake = FakeCloudFormation(account=account, region=region, service=service)
    client = AttestedAwsClientV2(
        fake,
        service=service,
        account=account,
        region=region,
        capability=capability,
        _token=_CLIENT_TOKEN,
    )
    try:
        with _verified_drift(tmp_path) as (verified, preflight, sink, _):
            authority = validate_stack_drift_dispatch(verified, preflight, sink)
            with pytest.raises(StackDriftError, match="authority"):
                StackDriftDispatcherV1(client).dispatch(
                    authority, _fresh_dispatch_authority(authority)
                )
    finally:
        client.close()
    assert fake.calls == []


def test_observer_returns_pending_only_for_the_exact_detection_id(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue(
        "describe_stack_drift_detection_status",
        _status(
            detection_status="DETECTION_IN_PROGRESS",
            drift_status=None,
            count=None,
        ),
    )
    with attested_test_client(fake, service="cloudformation") as client:
        result = StackDriftObserverV1(client).observe(receipt)

    assert result.disposition is ObservationDisposition.PENDING
    assert fake.calls == [
        (
            "describe_stack_drift_detection_status",
            {"StackDriftDetectionId": DETECTION_ID},
        )
    ]
    assert result.projection()["dispatchReceiptSha256"] == receipt.receipt.digest()


@pytest.mark.parametrize(
    ("method", "error"),
    (
        ("describe_stack_drift_detection_status", TimeoutError("timeout")),
        ("describe_stack_resource_drifts", RuntimeError("read failed")),
    ),
)
def test_provider_read_errors_never_become_authoritative(
    tmp_path: Path, method: str, error: BaseException
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    if method == "describe_stack_resource_drifts":
        fake.queue("describe_stack_drift_detection_status", _status())
    fake.queue(method, error)
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="authoritative"):
            StackDriftObserverV1(client).observe(receipt)


def test_detection_failed_and_drifted_are_authoritative_retained_failures(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    for index, response in enumerate(
        (
            _status(
                detection_status="DETECTION_FAILED",
                drift_status="UNKNOWN",
                reason="provider failed",
            ),
            _status(drift_status="DRIFTED", count=1),
        )
    ):
        fake = FakeCloudFormation()
        fake.queue("describe_stack_drift_detection_status", response)
        with attested_test_client(fake, service="cloudformation") as client:
            result = StackDriftObserverV1(client).observe(receipt)
        assert result.disposition is ObservationDisposition.FAILED_RETAINED, index
        assert result.provider_status in {"DETECTION_FAILED", "DRIFTED"}


def test_failed_detection_allows_documented_optional_fields_to_be_absent(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue(
        "describe_stack_drift_detection_status",
        _status(
            detection_status="DETECTION_FAILED",
            drift_status=None,
            count=None,
        ),
    )
    with attested_test_client(fake, service="cloudformation") as client:
        result = StackDriftObserverV1(client).observe(receipt)

    assert result.disposition is ObservationDisposition.FAILED_RETAINED
    assert result.provider_status == "DETECTION_FAILED"


@pytest.mark.parametrize(
    "response",
    [
        _status(
            detection_status="DETECTION_IN_PROGRESS",
            drift_status="IN_SYNC",
            count=None,
        ),
        _status(
            detection_status="DETECTION_IN_PROGRESS",
            drift_status=None,
            count=1,
        ),
        _status(
            detection_status="DETECTION_FAILED",
            drift_status="IN_SYNC",
            count=None,
        ),
        _status(
            detection_status="DETECTION_FAILED",
            drift_status=None,
            count=1,
        ),
        _status(drift_status=None, count=0),
        _status(drift_status="IN_SYNC", count=None),
    ],
)
def test_detection_optional_fields_are_status_dependent_and_fail_closed(
    tmp_path: Path,
    response: dict[str, object],
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", response)
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="status"):
            StackDriftObserverV1(client).observe(receipt)


def test_complete_in_sync_zero_resource_drift_requires_stable_exact_closure(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    _queue_present(fake)
    with attested_test_client(fake, service="cloudformation") as client:
        result = StackDriftObserverV1(client).observe(receipt)

    assert isinstance(result, CanonicalReadObservationV2)
    assert result.service == "cloudformation"
    assert result.operation == "describe_stack_drift_detection_status"
    assert result.subject.endswith(":drift")
    assert result.disposition is ObservationDisposition.PRESENT
    assert result.provider_status == "IN_SYNC"
    assert result.projection() == {
        "closingStackPolicySha256": result.projection()["closingStackPolicySha256"],
        "closingStackSha256": result.projection()["closingStackSha256"],
        "closingTemplateSha256": result.projection()["closingTemplateSha256"],
        "dispatchReceiptSha256": receipt.receipt.digest(),
        "driftDetectionId": DETECTION_ID,
        "driftDetectionTimestamp": DRIFT_TIMESTAMP.isoformat(),
        "predecessorEvidenceSha256": receipt.receipt.predecessor_evidence_sha256,
        "predecessorObserverEvidenceSha256": (
            receipt.receipt.predecessor_observer_evidence_sha256
        ),
        "resourceDriftCount": 0,
        "stackId": STACK_ID,
    }


def test_resource_drift_request_is_botocore_valid_and_exactly_filtered() -> None:
    raw = _botocore_cloudformation_client()
    model = raw.meta.service_model.operation_model(  # type: ignore[attr-defined]
        "DescribeStackResourceDrifts"
    )
    assert tuple(model.input_shape.members) == (
        "StackName",
        "StackResourceDriftStatusFilters",
        "NextToken",
        "MaxResults",
    )
    assert tuple(
        model.input_shape.members["StackResourceDriftStatusFilters"].member.enum
    ) == (
        "IN_SYNC",
        "MODIFIED",
        "DELETED",
        "NOT_CHECKED",
        "UNKNOWN",
        "UNSUPPORTED",
    )
    expected = {
        "StackName": STACK_ID,
        "StackResourceDriftStatusFilters": REVIEWED_RESOURCE_DRIFT_STATUSES,
        "MaxResults": 100,
    }
    with Stubber(raw) as stubber:
        stubber.add_response(
            "describe_stack_resource_drifts",
            {"StackResourceDrifts": []},
            expected,
        )
        client = AttestedAwsClientV2(
            raw,
            service="cloudformation",
            account=ACCOUNT,
            region=REGION,
            capability="observer",
            _token=_CLIENT_TOKEN,
        )
        try:
            observer = StackDriftObserverV1(client)
            assert observer._resource_drift_count(
                STACK_ID,
                detection_timestamp=DRIFT_TIMESTAMP,
            ) == 0
        finally:
            client.close()


def test_minimal_pending_detection_is_botocore_valid_and_authoritative(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    raw = _botocore_cloudformation_client()
    model = raw.meta.service_model.operation_model(  # type: ignore[attr-defined]
        "DescribeStackDriftDetectionStatus"
    )
    assert tuple(model.output_shape.required_members) == (
        "StackId",
        "StackDriftDetectionId",
        "DetectionStatus",
        "Timestamp",
    )
    assert "CHECK_IN_PROGRESS" not in model.output_shape.members[
        "StackDriftStatus"
    ].enum
    response = _status(
        detection_status="DETECTION_IN_PROGRESS",
        drift_status=None,
        count=None,
    )
    with Stubber(raw) as stubber:
        stubber.add_response(
            "describe_stack_drift_detection_status",
            response,
            {"StackDriftDetectionId": DETECTION_ID},
        )
        client = AttestedAwsClientV2(
            raw,
            service="cloudformation",
            account=ACCOUNT,
            region=REGION,
            capability="observer",
            _token=_CLIENT_TOKEN,
        )
        try:
            result = StackDriftObserverV1(client).observe(receipt)
        finally:
            client.close()

    assert result.disposition is ObservationDisposition.PENDING


def test_in_sync_resource_inventory_is_not_misclassified_as_drift(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue(
        "describe_stack_resource_drifts",
        {"StackResourceDrifts": [_resource_drift()]},
    )
    fake.queue("describe_stacks", _stack(), _stack())
    fake.queue("get_template", _template(), _template())
    fake.queue("get_stack_policy", {"StackPolicyBody": ""}, {"StackPolicyBody": ""})
    with attested_test_client(fake, service="cloudformation") as client:
        result = StackDriftObserverV1(client).observe(receipt)

    assert result.disposition is ObservationDisposition.PRESENT
    assert (
        "describe_stack_resource_drifts",
        {
            "StackName": STACK_ID,
            "StackResourceDriftStatusFilters": REVIEWED_RESOURCE_DRIFT_STATUSES,
            "MaxResults": 100,
        },
    ) in fake.calls


@pytest.mark.parametrize("status", ["NOT_CHECKED", "UNKNOWN", "UNSUPPORTED"])
def test_unreviewed_resource_drift_status_is_ambiguous(
    tmp_path: Path,
    status: str,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status())
    fake.queue(
        "describe_stack_resource_drifts",
        {"StackResourceDrifts": [_resource_drift(status=status)]},
    )
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(
            StackDriftObservationAmbiguous,
            match="resource drift status",
        ):
            StackDriftObserverV1(client).observe(receipt)


@pytest.mark.parametrize(
    "crossing",
    ["status", "timestamp", "latest"],
)
def test_detection_or_latest_stack_crossing_cannot_close_as_in_sync(
    tmp_path: Path,
    crossing: str,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    later = DRIFT_TIMESTAMP + timedelta(seconds=1)
    second = (
        _status(
            detection_status="DETECTION_IN_PROGRESS",
            drift_status=None,
            count=None,
        )
        if crossing == "status"
        else _status(timestamp=later)
        if crossing == "timestamp"
        else _status()
    )
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), second)
    fake.queue(
        "describe_stack_resource_drifts",
        {"StackResourceDrifts": []},
    )
    crossed_stack = (
        _stack(drift_timestamp=later) if crossing == "latest" else _stack()
    )
    fake.queue("describe_stacks", crossed_stack, crossed_stack)
    fake.queue("get_template", _template(), _template())
    fake.queue("get_stack_policy", {"StackPolicyBody": ""}, {"StackPolicyBody": ""})
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(
            StackDriftObservationAmbiguous,
            match="changed|latest|timestamp|status",
        ):
            StackDriftObserverV1(client).observe(receipt)


@pytest.mark.parametrize(
    ("offset_seconds", "accepted"),
    [(-1, False), (0, True), (1, True)],
)
def test_resource_check_timestamp_never_precedes_detection_initiation(
    tmp_path: Path,
    offset_seconds: int,
    accepted: bool,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / f"dispatch-{offset_seconds}")
    resource_timestamp = DRIFT_TIMESTAMP + timedelta(seconds=offset_seconds)
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue(
        "describe_stack_resource_drifts",
        {
            "StackResourceDrifts": [
                _resource_drift(timestamp=resource_timestamp)
            ]
        },
    )
    fake.queue("describe_stacks", _stack(), _stack())
    fake.queue("get_template", _template(), _template())
    fake.queue("get_stack_policy", {"StackPolicyBody": ""}, {"StackPolicyBody": ""})
    with attested_test_client(fake, service="cloudformation") as client:
        if accepted:
            result = StackDriftObserverV1(client).observe(receipt)
            assert result.disposition is ObservationDisposition.PRESENT
        else:
            with pytest.raises(
                StackDriftObservationAmbiguous,
                match="timestamp|stale|precedes",
            ):
                StackDriftObserverV1(client).observe(receipt)


def test_resource_drift_pagination_is_bounded_and_crossing_is_ambiguous(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue(
        "describe_stack_resource_drifts",
        {"StackResourceDrifts": [], "NextToken": "page-2"},
        {"StackResourceDrifts": [_resource_drift(status="MODIFIED")]},
    )
    fake.queue("describe_stacks", _stack(), _stack())
    fake.queue("get_template", _template(), _template())
    fake.queue("get_stack_policy", {"StackPolicyBody": ""}, {"StackPolicyBody": ""})
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="crosses"):
            StackDriftObserverV1(client).observe(receipt)

    assert (
        "describe_stack_resource_drifts",
        {
            "StackName": STACK_ID,
            "StackResourceDriftStatusFilters": REVIEWED_RESOURCE_DRIFT_STATUSES,
            "MaxResults": 100,
            "NextToken": "page-2",
        },
    ) in fake.calls


@pytest.mark.parametrize(
    "pages",
    [
        [
            {"StackResourceDrifts": [], "NextToken": "same"},
            {"StackResourceDrifts": [], "NextToken": "same"},
        ],
        [{"StackResourceDrifts": "not-a-list"}],
        [{"StackResourceDrifts": [], "NextToken": 7}],
    ],
)
def test_malformed_or_cyclic_resource_drift_pagination_is_ambiguous(
    tmp_path: Path, pages: list[dict[str, object]]
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue("describe_stack_resource_drifts", *pages)
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="resource drift"):
            StackDriftObserverV1(client).observe(receipt)


@pytest.mark.parametrize(
    "response",
    [
        _status(detection_status="DETECTION_IN_PROGRESS", drift_status="IN_SYNC"),
        _status(detection_status="DETECTION_COMPLETE", drift_status="UNKNOWN"),
        _status(detection_status="DETECTION_FAILED", drift_status="IN_SYNC"),
        _status(drift_status="IN_SYNC", count=1),
        _status(drift_status="DRIFTED", count=0),
    ],
)
def test_contradictory_detection_status_fields_are_ambiguous(
    tmp_path: Path, response: dict[str, object]
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", response)
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="status"):
            StackDriftObserverV1(client).observe(receipt)


@pytest.mark.parametrize("cross", ["stack", "detection"])
def test_observer_rejects_crossed_provider_identity(
    tmp_path: Path, cross: str
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue(
        "describe_stack_drift_detection_status",
        _status(
            stack_id=OTHER_STACK_ID if cross == "stack" else STACK_ID,
            detection_id=(
                "87654321-4321-4321-4321-cba987654321"
                if cross == "detection"
                else DETECTION_ID
            ),
        ),
    )
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftError, match="identity"):
            StackDriftObserverV1(client).observe(receipt)


@pytest.mark.parametrize("unstable", ["stack", "template", "policy"])
def test_closing_stack_template_or_policy_instability_is_ambiguous(
    tmp_path: Path, unstable: str
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue("describe_stack_resource_drifts", {"StackResourceDrifts": []})
    fake.queue(
        "describe_stacks",
        _stack(),
        _stack(status="UPDATE_COMPLETE") if unstable == "stack" else _stack(),
    )
    fake.queue(
        "get_template",
        _template(),
        _template("changed") if unstable == "template" else _template(),
    )
    fake.queue(
        "get_stack_policy",
        {"StackPolicyBody": ""},
        (
            {"StackPolicyBody": {"Statement": []}}
            if unstable == "policy"
            else {"StackPolicyBody": ""}
        ),
    )
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftObservationAmbiguous, match="changed"):
            StackDriftObserverV1(client).observe(receipt)


def test_same_name_recreated_stack_cannot_close_the_drift_step(tmp_path: Path) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue("describe_stack_drift_detection_status", _status(), _status())
    fake.queue("describe_stack_resource_drifts", {"StackResourceDrifts": []})
    fake.queue("describe_stacks", _stack(stack_id=OTHER_STACK_ID))
    with attested_test_client(fake, service="cloudformation") as client:
        with pytest.raises(StackDriftError, match="retained StackId"):
            StackDriftObserverV1(client).observe(receipt)


def test_historical_stack_drift_information_never_completes_without_detection(
    tmp_path: Path,
) -> None:
    receipt, _, _ = _dispatched(tmp_path / "dispatch")
    fake = FakeCloudFormation()
    fake.queue(
        "describe_stack_drift_detection_status",
        _status(
            detection_status="DETECTION_IN_PROGRESS",
            drift_status=None,
            count=None,
        ),
    )
    with attested_test_client(fake, service="cloudformation") as client:
        result = StackDriftObserverV1(client).observe(receipt)
    assert result.disposition is ObservationDisposition.PENDING
    assert all(name != "describe_stacks" for name, _ in fake.calls)


def test_authority_and_receipt_capabilities_are_not_directly_constructible() -> None:
    operation = StackDriftOperationV1.from_bytes(canonical_json_bytes(_operation()))
    with pytest.raises(StackDriftError, match="constructible"):
        StackDriftOperationV1(
            account=ACCOUNT,
            region=REGION,
            source_commit=COMMIT,
            source_tree=TREE,
            stack_name=STACK_NAME,
            phase="foundation",
            occurrence="foundation-drift-cdktoolkit",
        )
    for constructor, kwargs in (
        (
            VerifiedStackDriftPreflightV1,
            {
                "release_plan_sha256": "1" * 64,
                "request_sha256": operation.digest(),
                "operation": operation,
            },
        ),
        (StackDriftReceiptSinkV1, {"backend": MemoryReceiptBackend()}),
        (VerifiedStackDriftDispatchV1, {}),
        (VerifiedStackDriftReceiptV1, {}),
    ):
        with pytest.raises(
            (StackDriftError, TypeError), match="constructible|required"
        ):
            constructor(**kwargs)

    with pytest.raises(ProductionObserverV2Error, match="constructible"):
        CanonicalReadObservationV2(
            service="cloudformation",
            operation="describe_stack_drift_detection_status",
            subject="cfn:subject:drift",
            disposition=ObservationDisposition.PRESENT,
            provider_status="IN_SYNC",
            projection_bytes=b"{}\n",
        )


def test_module_has_no_sdk_credential_filesystem_or_process_authority() -> None:
    source = (Path(__file__).parent / "stack_drift_v2.py").read_text(encoding="utf-8")
    assert "boto3" not in source
    assert "botocore" not in source
    assert "subprocess" not in source
    assert "Path(" not in source
    assert "open(" not in source
