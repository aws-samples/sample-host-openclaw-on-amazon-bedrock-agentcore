from __future__ import annotations

import pytest

from observability.events import (
    MAX_EVENT_COUNT,
    OperationalEventV1,
    OperationalEventValidationError,
)


def _valid_mapping() -> dict[str, object]:
    return {
        "schema": "personal-operator.operational-event.v1",
        "environment": "synthetic",
        "component": "control",
        "operation": "invite",
        "outcome": "succeeded",
        "count": 1,
    }


def test_operational_event_accepts_only_the_exact_closed_shape() -> None:
    event = OperationalEventV1.from_mapping(_valid_mapping())

    assert event.to_mapping() == _valid_mapping()
    assert event.to_canonical_bytes() == (
        b'{"component":"control","count":1,"environment":"synthetic",'
        b'"operation":"invite","outcome":"succeeded",'
        b'"schema":"personal-operator.operational-event.v1"}\n'
    )

    missing = _valid_mapping()
    missing.pop("outcome")
    with pytest.raises(OperationalEventValidationError, match="exact fields"):
        OperationalEventV1.from_mapping(missing)

    extra = {**_valid_mapping(), "note": "aggregate only"}
    with pytest.raises(OperationalEventValidationError, match="exact fields"):
        OperationalEventV1.from_mapping(extra)


def test_direct_construction_cannot_bypass_the_closed_value_boundary() -> None:
    with pytest.raises(OperationalEventValidationError, match="allowlist"):
        OperationalEventV1(
            environment="participant-123",
            component="control",
            operation="invite",
            outcome="succeeded",
            count=1,
        )

    with pytest.raises(OperationalEventValidationError, match="count"):
        OperationalEventV1(
            environment="synthetic",
            component="control",
            operation="invite",
            outcome="succeeded",
            count=True,
        )


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "userId",
        "identity",
        "providerId",
        "sourceId",
        "address",
        "subject",
        "body",
        "excerpt",
        "url",
        "model",
        "workspace",
        "token",
        "credential",
    ],
)
def test_operational_event_rejects_private_or_source_fields(
    forbidden_field: str,
) -> None:
    with pytest.raises(OperationalEventValidationError, match="exact fields"):
        OperationalEventV1.from_mapping(
            {**_valid_mapping(), forbidden_field: "private-canary"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("environment", "participant-123"),
        ("component", "provider:gmail"),
        ("operation", "source-message-456"),
        ("outcome", "https://private.example/path"),
        ("outcome", "Subject: private"),
        ("outcome", "Body excerpt"),
        ("outcome", "workspace-content"),
        ("outcome", "model-output"),
        ("outcome", "oauth-token"),
        ("outcome", "credential-secret"),
    ],
)
def test_operational_event_values_are_finite_release_owned_tokens(
    field: str, value: str
) -> None:
    mapping = _valid_mapping()
    mapping[field] = value

    with pytest.raises(OperationalEventValidationError, match="allowlist"):
        OperationalEventV1.from_mapping(mapping)


@pytest.mark.parametrize(
    "count",
    [True, False, 0, -1, MAX_EVENT_COUNT + 1, 1.0, "1", None],
)
def test_operational_event_count_is_a_bounded_positive_integer(
    count: object,
) -> None:
    with pytest.raises(OperationalEventValidationError, match="count"):
        OperationalEventV1.from_mapping({**_valid_mapping(), "count": count})
