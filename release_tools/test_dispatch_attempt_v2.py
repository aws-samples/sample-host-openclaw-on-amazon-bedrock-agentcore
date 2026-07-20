"""Hostile contract tests for the universal v2 mutation-attempt fence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    DispatchAttemptStateV1,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)


PLAN = "a" * 64
STORE = "b" * 64
PATH = "c" * 64
EXECUTION = "d" * 64
PREFIX = "e" * 64
OPERATION = "sha256:" + "f" * 64
RESOLVED = "1" * 64


def _attempt() -> ReleaseDispatchAttemptV1:
    return ReleaseDispatchAttemptV1.from_mapping(
        {
            "schema": ReleaseDispatchAttemptV1.SCHEMA,
            "releasePlanSha256": PLAN,
            "evidenceStoreSha256": STORE,
            "journalPathSha256": PATH,
            "journalExecutionId": EXECUTION,
            "journalRevision": 7,
            "completedPrefixSha256": PREFIX,
            "stepId": "foundation:0004:STACK_CREATE:OpenClawVpc",
            "subject": "OpenClawVpc",
            "operationSha256": OPERATION,
            "resolvedRequestSha256": RESOLVED,
            "provider": "CLOUDFORMATION",
        }
    )


def test_dispatch_attempt_is_exact_canonical_and_round_trips() -> None:
    attempt = _attempt()
    assert ReleaseDispatchAttemptV1.from_bytes(attempt.to_bytes()) == attempt
    assert attempt.digest() == attempt.digest()
    assert len(attempt.digest()) == 64

    crossed = dict(attempt.to_mapping())
    crossed["extra"] = "not permitted"
    with pytest.raises(DispatchAttemptError, match="fields"):
        ReleaseDispatchAttemptV1.from_mapping(crossed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "personal-operator.release-dispatch-attempt.v2"),
        ("releasePlanSha256", "A" * 64),
        ("evidenceStoreSha256", ""),
        ("journalPathSha256", "0" * 63),
        ("journalExecutionId", "g" * 64),
        ("journalRevision", True),
        ("journalRevision", 0),
        ("completedPrefixSha256", "sha256:" + "e" * 64),
        ("stepId", ""),
        ("subject", "../crossed"),
        ("operationSha256", "sha256:" + "f" * 63),
        ("resolvedRequestSha256", "1" * 65),
        ("provider", "cloudformation"),
        ("provider", "BROWSER"),
    ],
)
def test_dispatch_attempt_rejects_malformed_identity(
    field: str, value: object
) -> None:
    raw = _attempt().to_mapping()
    raw[field] = value
    with pytest.raises(DispatchAttemptError):
        ReleaseDispatchAttemptV1.from_mapping(raw)


def test_fresh_authority_is_private_single_use_and_exactly_bound() -> None:
    attempt = _attempt()
    with pytest.raises(DispatchAttemptError, match="not constructible"):
        FreshDispatchAuthorityV1(attempt, _token=object())

    authority = _mint_fresh_dispatch_authority(attempt)
    assert authority.consume(
        provider="CLOUDFORMATION",
        operation_sha256=OPERATION,
        resolved_request_sha256=RESOLVED,
    ) == attempt
    with pytest.raises(DispatchAttemptError, match="already consumed"):
        authority.consume(
            provider="CLOUDFORMATION",
            operation_sha256=OPERATION,
            resolved_request_sha256=RESOLVED,
        )


@pytest.mark.parametrize(
    ("provider", "operation", "resolved"),
    [
        ("S3", OPERATION, RESOLVED),
        ("CLOUDFORMATION", "sha256:" + "0" * 64, RESOLVED),
        ("CLOUDFORMATION", OPERATION, "0" * 64),
    ],
)
def test_crossed_binding_does_not_consume_fresh_authority(
    provider: str, operation: str, resolved: str
) -> None:
    authority = _mint_fresh_dispatch_authority(_attempt())
    with pytest.raises(DispatchAttemptError, match="binding differs"):
        authority.consume(
            provider=provider,
            operation_sha256=operation,
            resolved_request_sha256=resolved,
        )
    assert authority.consume(
        provider="CLOUDFORMATION",
        operation_sha256=OPERATION,
        resolved_request_sha256=RESOLVED,
    ) == _attempt()


def test_dispatch_attempt_state_has_only_closed_absent_or_attempted_forms() -> None:
    absent = DispatchAttemptStateV1.absent()
    assert absent.attempted is False
    assert absent.attempt is None

    attempted = DispatchAttemptStateV1.retained(_attempt())
    assert attempted.attempted is True
    assert attempted.attempt == _attempt()

    with pytest.raises(DispatchAttemptError):
        DispatchAttemptStateV1(attempted=False, attempt=_attempt())
    with pytest.raises(DispatchAttemptError):
        DispatchAttemptStateV1(attempted=True, attempt=None)
    with pytest.raises(DispatchAttemptError):
        DispatchAttemptStateV1(
            attempted=True,
            attempt=replace(_attempt(), provider="BROWSER"),
        )


def test_attempt_contract_rejects_noncanonical_json() -> None:
    payload = _attempt().to_bytes().replace(
        b'"provider":"CLOUDFORMATION"',
        b'"provider": "CLOUDFORMATION"',
    )
    with pytest.raises(DispatchAttemptError, match="canonical"):
        ReleaseDispatchAttemptV1.from_bytes(payload)
