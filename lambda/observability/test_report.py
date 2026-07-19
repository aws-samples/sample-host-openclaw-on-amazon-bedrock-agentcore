from __future__ import annotations

import json

import pytest

from observability.events import OperationalEventV1
from observability.report import (
    MAX_PARTICIPANT_COUNT,
    CohortReportValidationError,
    build_cohort_report,
)


def _event(
    *,
    component: str,
    operation: str,
    outcome: str,
    count: int = 1,
) -> OperationalEventV1:
    return OperationalEventV1.from_mapping(
        {
            "schema": "personal-operator.operational-event.v1",
            "environment": "synthetic",
            "component": component,
            "operation": operation,
            "outcome": outcome,
            "count": count,
        }
    )


def test_cohort_report_is_order_independent_and_aggregates_only_counts() -> None:
    events = [
        _event(component="scan", operation="scan", outcome="succeeded"),
        _event(component="control", operation="invite", outcome="succeeded"),
        _event(
            component="scan", operation="scan", outcome="succeeded", count=2
        ),
        _event(component="compute", operation="compute", outcome="disabled"),
    ]

    report = build_cohort_report(events, participant_count=3)
    reverse_report = build_cohort_report(reversed(events), participant_count=3)

    assert report.to_mapping() == reverse_report.to_mapping()
    assert report.to_canonical_bytes() == reverse_report.to_canonical_bytes()
    assert report.to_mapping() == {
        "schema": "personal-operator.cohort-report.v1",
        "participantCount": 3,
        "events": [
            {
                "schema": "personal-operator.operational-event.v1",
                "environment": "synthetic",
                "component": "compute",
                "operation": "compute",
                "outcome": "disabled",
                "count": 1,
            },
            {
                "schema": "personal-operator.operational-event.v1",
                "environment": "synthetic",
                "component": "control",
                "operation": "invite",
                "outcome": "succeeded",
                "count": 1,
            },
            {
                "schema": "personal-operator.operational-event.v1",
                "environment": "synthetic",
                "component": "scan",
                "operation": "scan",
                "outcome": "succeeded",
                "count": 3,
            },
        ],
    }


def test_cohort_report_canonical_bytes_are_compact_json_with_one_final_lf() -> None:
    report = build_cohort_report(
        [_event(component="portable", operation="import", outcome="replay_denied")],
        participant_count=3,
    )

    encoded = report.to_canonical_bytes()
    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")
    assert b" " not in encoded
    assert json.loads(encoded) == report.to_mapping()


@pytest.mark.parametrize(
    "participant_count",
    [True, False, 0, -1, MAX_PARTICIPANT_COUNT + 1, 3.0, "3", None],
)
def test_cohort_report_requires_a_bounded_aggregate_participant_count(
    participant_count: object,
) -> None:
    with pytest.raises(CohortReportValidationError, match="participant count"):
        build_cohort_report([], participant_count=participant_count)  # type: ignore[arg-type]


def test_report_has_no_free_text_identifiers_timestamps_or_private_source_data() -> None:
    events = [
        _event(component="oauth", operation="connect", outcome="succeeded"),
        _event(component="workspace", operation="workspace", outcome="inert"),
        _event(component="portable", operation="import", outcome="replay_denied"),
    ]

    encoded = build_cohort_report(events, participant_count=3).to_canonical_bytes()
    lowered = encoded.lower()
    for forbidden in (
        b"userid",
        b"identity",
        b"providerid",
        b"sourceid",
        b"address",
        b"subject",
        b"body",
        b"excerpt",
        b"http://",
        b"https://",
        b"model",
        b"workspace-content",
        b"token",
        b"credential",
        b"timestamp",
        b"createdat",
        b"private-canary",
    ):
        assert forbidden not in lowered


def test_report_rejects_unvalidated_objects_and_aggregate_overflow() -> None:
    with pytest.raises(CohortReportValidationError, match="validated"):
        build_cohort_report([object()], participant_count=3)  # type: ignore[list-item]

    event = _event(
        component="queue",
        operation="queue",
        outcome="succeeded",
        count=1_000_000,
    )
    with pytest.raises(CohortReportValidationError, match="aggregate"):
        build_cohort_report([event, event], participant_count=3)
