from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Iterator, Mapping

import pytest

import release_tools.baseline_observer_v2 as baseline_observer

from release_tools.aws_authority_v2 import AttestedAwsClientV2
from release_tools.baseline_observer_v2 import (
    BASELINE_STACK_INVENTORY,
    BaselineObservationRequestV1,
    BaselineObserverV2,
    BaselineObserverV2Ambiguous,
    BaselineObserverV2Error,
)
from release_tools.contracts import canonical_json_bytes
from release_tools.production_observer_v2 import (
    CanonicalReadObservationV2,
    ProductionObserverV2Error,
)
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.transaction import ObservationDisposition


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40

EXPECTED_INVENTORY = (
    "CDKToolkit",
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
    "PersonalOperatorBrowser",
    "PersonalOperatorCompute",
    "OpenClawTokenMonitoring",
)


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeCloudFormation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: list[object] = []
        self.meta = SimpleNamespace(
            region_name=REGION,
            service_model=SimpleNamespace(service_name="cloudformation"),
            config=SimpleNamespace(
                region_name=REGION,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    def queue(self, *responses: object) -> None:
        self.responses.extend(responses)

    def close(self) -> None:
        return None

    def describe_stacks(self, **kwargs: object) -> object:
        self.calls.append(("describe_stacks", kwargs))
        if not self.responses:
            raise AssertionError("unexpected cloudformation.describe_stacks")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)


def _missing(stack_name: str) -> ProviderError:
    return ProviderError(
        "ValidationError",
        f"Stack with id {stack_name} does not exist",
        400,
    )


def _present(
    stack_name: str,
    *,
    stack_status: str = "CREATE_COMPLETE",
    opaque_id: str = "exact-id",
) -> dict[str, object]:
    return {
        "Stacks": [
            {
                "StackName": stack_name,
                "StackId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
                    f"{stack_name}/{opaque_id}"
                ),
                "StackStatus": stack_status,
            }
        ]
    }


def _sweep(
    overrides: Mapping[str, object] | None = None,
) -> list[object]:
    values = overrides or {}
    return [values.get(name, _missing(name)) for name in EXPECTED_INVENTORY]


def _queue_sweeps(
    fake: FakeCloudFormation,
    *,
    first: Mapping[str, object] | None = None,
    second: Mapping[str, object] | None = None,
) -> None:
    fake.queue(*_sweep(first), *_sweep(second))


def _request() -> BaselineObservationRequestV1:
    return BaselineObservationRequestV1.from_mapping(
        {
            "schema": "personal-operator.baseline-observation-request.v1",
            "account": ACCOUNT,
            "region": REGION,
            "sourceCommit": COMMIT,
        }
    )


@contextmanager
def _observer(
    fake: FakeCloudFormation,
) -> Iterator[BaselineObserverV2]:
    with attested_test_client(fake, service="cloudformation") as client:
        assert isinstance(client, AttestedAwsClientV2)
        yield BaselineObserverV2(
            account=ACCOUNT,
            region=REGION,
            cloudformation=client,
        )


def test_inventory_is_the_exact_ordered_active_and_forbidden_set() -> None:
    assert BASELINE_STACK_INVENTORY == EXPECTED_INVENTORY
    assert len(BASELINE_STACK_INVENTORY) == 14
    assert len(set(BASELINE_STACK_INVENTORY)) == 14


def test_request_is_exact_canonical_and_commit_bound() -> None:
    request = _request()
    assert request.to_mapping() == {
        "schema": "personal-operator.baseline-observation-request.v1",
        "account": ACCOUNT,
        "region": REGION,
        "sourceCommit": COMMIT,
    }
    assert request.to_bytes() == canonical_json_bytes(request.to_mapping())
    assert len(request.digest()) == 64

    for field, value in (
        ("account", "000000000000"),
        ("account", "123"),
        ("region", "us-east-1"),
        ("sourceCommit", "A" * 40),
        ("sourceCommit", "a" * 39),
    ):
        raw = request.to_mapping()
        raw[field] = value
        with pytest.raises(BaselineObserverV2Error):
            BaselineObservationRequestV1.from_mapping(raw)

    extra = request.to_mapping()
    extra["sourceTree"] = "b" * 40
    with pytest.raises(BaselineObserverV2Error, match="fields"):
        BaselineObservationRequestV1.from_mapping(extra)


def test_raw_client_and_cross_subject_request_are_rejected() -> None:
    raw = FakeCloudFormation()
    with pytest.raises(BaselineObserverV2Error, match="attested"):
        BaselineObserverV2(
            account=ACCOUNT,
            region=REGION,
            cloudformation=raw,
        )

    fake = FakeCloudFormation()
    with _observer(fake) as observer:
        crossed = BaselineObservationRequestV1.from_mapping(
            {
                **_request().to_mapping(),
                "account": "999999999999",
            }
        )
        with pytest.raises(BaselineObserverV2Error, match="crosses"):
            observer.observe(crossed)
    assert fake.calls == []


def test_clean_account_requires_two_complete_ordered_absent_sweeps() -> None:
    fake = FakeCloudFormation()
    _queue_sweeps(fake)

    with _observer(fake) as observer:
        result = observer.observe(_request())

    assert isinstance(result, CanonicalReadObservationV2)
    assert result.to_mapping()["schema"] == (
        "personal-operator.canonical-read-observation.v2"
    )
    assert result.service == "cloudformation"
    assert result.operation == "describe_stacks"
    assert result.subject == f"release:{ACCOUNT}:{REGION}:{COMMIT}:baseline"
    assert result.disposition is ObservationDisposition.PRESENT
    assert result.provider_status == "CLEAN_ACCOUNT"
    assert result.projection() == {
        "account": ACCOUNT,
        "inventory": [
            {"stackName": name, "state": "ABSENT"}
            for name in EXPECTED_INVENTORY
        ],
        "region": REGION,
        "requestSha256": _request().digest(),
        "sourceCommit": COMMIT,
        "sweeps": 2,
    }
    assert result.to_bytes() == canonical_json_bytes(result.to_mapping())
    assert len(result.digest()) == 64
    assert fake.calls == [
        ("describe_stacks", {"StackName": stack_name})
        for _ in range(2)
        for stack_name in EXPECTED_INVENTORY
    ]
    assert fake.responses == []


def test_public_inventory_rebinding_cannot_weaken_internal_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCloudFormation()
    _queue_sweeps(fake)
    monkeypatch.setattr(baseline_observer, "BASELINE_STACK_INVENTORY", ())

    with _observer(fake) as observer:
        result = observer.observe(_request())

    assert result.disposition is ObservationDisposition.PRESENT
    assert result.provider_status == "CLEAN_ACCOUNT"
    assert result.projection()["inventory"] == [
        {"stackName": name, "state": "ABSENT"}
        for name in EXPECTED_INVENTORY
    ]
    assert fake.calls == [
        ("describe_stacks", {"StackName": stack_name})
        for _ in range(2)
        for stack_name in EXPECTED_INVENTORY
    ]
    assert fake.responses == []


def test_stable_present_inventory_is_failed_retained_after_full_sweeps() -> None:
    present = {
        "OpenClawAgentCore": _present("OpenClawAgentCore"),
        "PersonalOperatorBrowser": _present(
            "PersonalOperatorBrowser",
            stack_status="UPDATE_COMPLETE",
            opaque_id="legacy-id",
        ),
    }
    fake = FakeCloudFormation()
    _queue_sweeps(fake, first=present, second=present)

    with _observer(fake) as observer:
        result = observer.observe(_request())

    assert result.disposition is ObservationDisposition.FAILED_RETAINED
    assert result.provider_status == "NONEMPTY_ACCOUNT"
    projection = result.projection()
    assert projection["inventory"] == [
        (
            {
                "stackId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
                    f"{name}/"
                    + ("legacy-id" if name == "PersonalOperatorBrowser" else "exact-id")
                ),
                "stackName": name,
                "stackStatus": (
                    "UPDATE_COMPLETE"
                    if name == "PersonalOperatorBrowser"
                    else "CREATE_COMPLETE"
                ),
                "state": "PRESENT",
            }
            if name in present
            else {"stackName": name, "state": "ABSENT"}
        )
        for name in EXPECTED_INVENTORY
    ]
    assert len(fake.calls) == 28
    assert fake.responses == []


@pytest.mark.parametrize(
    ("first", "second"),
    (
        (
            {"OpenClawVpc": _present("OpenClawVpc")},
            {},
        ),
        (
            {},
            {"OpenClawVpc": _present("OpenClawVpc")},
        ),
        (
            {"OpenClawVpc": _present("OpenClawVpc")},
            {
                "OpenClawVpc": _present(
                    "OpenClawVpc",
                    stack_status="UPDATE_IN_PROGRESS",
                )
            },
        ),
        (
            {"OpenClawVpc": _present("OpenClawVpc")},
            {"OpenClawVpc": _present("OpenClawVpc", opaque_id="changed-id")},
        ),
    ),
)
def test_inventory_instability_is_ambiguous(
    first: Mapping[str, object],
    second: Mapping[str, object],
) -> None:
    fake = FakeCloudFormation()
    _queue_sweeps(fake, first=first, second=second)

    with _observer(fake) as observer:
        with pytest.raises(BaselineObserverV2Ambiguous, match="changed"):
            observer.observe(_request())

    assert len(fake.calls) == 28
    assert fake.responses == []


@pytest.mark.parametrize(
    "malformed",
    (
        None,
        [],
        {},
        {"Stacks": []},
        {"Stacks": [{}, {}]},
        {"Stacks": ["not-an-object"]},
        {"Stacks": [{}]},
        {
            "Stacks": [
                {
                    "StackName": "OpenClawVpc",
                    "StackId": (
                        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
                        "OpenClawVpc/id"
                    ),
                    "StackStatus": "CREATE_COMPLETE",
                }
            ],
            "NextToken": "unexpected",
        },
        _present("OpenClawSecurity"),
        {
            "Stacks": [
                {
                    "StackName": "OpenClawVpc",
                    "StackId": (
                        "arn:aws:cloudformation:us-east-1:"
                        f"{ACCOUNT}:stack/OpenClawVpc/id"
                    ),
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        },
        {
            "Stacks": [
                {
                    "StackName": "OpenClawVpc",
                    "StackId": (
                        f"arn:aws:cloudformation:{REGION}:999999999999:"
                        "stack/OpenClawVpc/id"
                    ),
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        },
        {
            "Stacks": [
                {
                    "StackName": "OpenClawVpc",
                    "StackId": (
                        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
                        "OpenClawVpc/id"
                    ),
                    "StackStatus": "",
                }
            ]
        },
    ),
)
def test_malformed_provider_evidence_is_ambiguous(malformed: object) -> None:
    fake = FakeCloudFormation()
    fake.queue(malformed)

    with _observer(fake) as observer:
        with pytest.raises(BaselineObserverV2Ambiguous, match="malformed"):
            observer.observe(_request())


@pytest.mark.parametrize(
    "error",
    (
        ProviderError("AccessDenied", "denied", 403),
        ProviderError("ValidationError", "different message", 400),
        ProviderError(
            "ValidationError",
            "Stack with id OpenClawVpc does not exist",
            500,
        ),
        RuntimeError("transport failed"),
    ),
)
def test_provider_errors_other_than_exact_not_found_are_ambiguous(
    error: BaseException,
) -> None:
    fake = FakeCloudFormation()
    fake.queue(error)

    with _observer(fake) as observer:
        with pytest.raises(BaselineObserverV2Ambiguous, match="failed"):
            observer.observe(_request())


def test_observation_is_not_directly_constructible() -> None:
    assert not hasattr(baseline_observer, "CanonicalBaselineObservationV1")
    with pytest.raises(ProductionObserverV2Error, match="constructible"):
        CanonicalReadObservationV2(
            service="cloudformation",
            operation="describe_stacks",
            subject=f"release:{ACCOUNT}:{REGION}:{COMMIT}:baseline",
            disposition=ObservationDisposition.PRESENT,
            provider_status="CLEAN_ACCOUNT",
            projection_bytes=b"{}\n",
        )
