"""Hostile admission, replay, and adapter tests for the v1 capability gateway."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from capabilities.catalog import compile_catalog
from capabilities.contracts import (
    CapabilityCallV1,
    CapabilityInstallationV1,
    TargetGrantV1,
    canonical_sha256,
    derive_call_id,
    derive_target_hash,
)

RELEASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/example"
RUNTIME_QUALIFIER = f"release_{RELEASE_COMMIT}"
CALLER_ARN = (
    "arn:aws:iam::000000000000:role/" "openclaw-agentcore-execution-role-eu-west-1"
)
NOW = 1_800_000_100
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "specs/capabilities/schemas"


def _load_gateway_modules():
    try:
        from capabilities.admission import LiveTargetGrant
        from capabilities.gateway import AdapterOutcome, CapabilityGateway
        from capabilities.ledger import InMemoryCapabilityLedger
    except ImportError:
        return None
    return LiveTargetGrant, AdapterOutcome, CapabilityGateway, InMemoryCapabilityLedger


def _catalog():
    return compile_catalog(RELEASE_COMMIT, SCHEMA_DIR)[1]


def _operation_rows(catalog) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    rows = {}
    for pack in catalog.packs:
        pack_mapping = {key: value for key, value in pack.items()}
        operation = dict(pack_mapping["operations"][0])
        rows[operation["operationId"]] = (pack_mapping, operation)
    return rows


def _call(
    catalog,
    operation_id: str,
    arguments: Mapping[str, Any],
    *,
    tool_use_id="tooluse_12345678",
):
    _, operation = _operation_rows(catalog)[operation_id]
    args_hash = canonical_sha256(arguments)
    return CapabilityCallV1.from_mapping(
        {
            "schema": CapabilityCallV1.SCHEMA,
            "callId": derive_call_id(
                "invocation_12345678",
                tool_use_id,
                catalog.catalog_digest,
                operation_id,
                operation["toolName"],
                args_hash,
            ),
            "invocationId": "invocation_12345678",
            "toolUseId": tool_use_id,
            "catalogDigest": catalog.catalog_digest,
            "operationId": operation_id,
            "toolName": operation["toolName"],
            "arguments": dict(arguments),
            "argsHash": args_hash,
        }
    )


def _target_grant(*, expires_at=NOW + 120, max_uses=1):
    target_hash = derive_target_hash(
        "https://example.com/exact",
        "GET",
        "NO_REDIRECT",
        expires_at,
        max_uses,
        "request_12345678",
    )
    return TargetGrantV1.from_mapping(
        {
            "schema": TargetGrantV1.SCHEMA,
            "targetHash": target_hash,
            "normalizedTarget": "https://example.com/exact",
            "method": "GET",
            "redirectPolicy": "NO_REDIRECT",
            "expiresAt": expires_at,
            "maxUses": max_uses,
            "currentRequestId": "request_12345678",
        }
    )


def _turn_grant(catalog, *, target_grant=None, max_calls=8, overrides=None):
    rows = _operation_rows(catalog)
    mapping = {
        "schema": "personal-operator.turn-capability-grant.v1",
        "sub": "user_alpha",
        "sessionId": "session_12345678",
        "runtimeArn": RUNTIME_ARN,
        "runtimeQualifier": RUNTIME_QUALIFIER,
        "invocationId": "invocation_12345678",
        "releaseCommit": RELEASE_COMMIT,
        "catalogDigest": catalog.catalog_digest,
        "allowedPackIds": sorted({pack["packId"] for pack, _ in rows.values()}),
        "allowedOperationIds": sorted(rows),
        "targetGrantHashes": (
            [] if target_grant is None else [target_grant.target_hash]
        ),
        "iat": NOW - 60,
        "exp": NOW + 300,
        "maxCalls": max_calls,
        "nonce": "nonce_1234567890abcdef",
    }
    mapping.update(overrides or {})
    return mapping


class FakeRepository:
    def __init__(self, catalog, live_target_type, *, target_grant=None):
        self.catalog = catalog
        self.trace: list[str] = []
        self.global_kill_switch = False
        self.deletion_fence = False
        self.user = {
            "userId": "user_alpha",
            "state": "ACTIVE",
            "deletionFence": False,
        }
        self.session = {
            "sessionId": "session_12345678",
            "userId": "user_alpha",
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
            "state": "ACTIVE",
        }
        self.runtime = {
            "runtimeArn": RUNTIME_ARN,
            "runtimeQualifier": RUNTIME_QUALIFIER,
            "sessionId": "session_12345678",
            "userId": "user_alpha",
            "releaseCommit": RELEASE_COMMIT,
            "catalogDigest": catalog.catalog_digest,
            "state": "READY",
        }
        self.installations = {}
        for pack, _ in _operation_rows(catalog).values():
            self.installations[pack["packId"]] = CapabilityInstallationV1.from_mapping(
                {
                    "schema": CapabilityInstallationV1.SCHEMA,
                    "userId": "user_alpha",
                    "packId": pack["packId"],
                    "catalogDigest": catalog.catalog_digest,
                    "state": "ENABLED",
                    "policyRevision": 1,
                    "connectionRefs": [],
                    "killSwitch": False,
                }
            )
        self.targets = {}
        if target_grant is not None:
            self.targets[target_grant.target_hash] = live_target_type(
                grant=target_grant,
                uses=0,
            )
        self.claimed_target_calls: dict[str, set[str]] = {}

    def strong_read_global_kill_switch(self) -> bool:
        self.trace.append("global_kill")
        return self.global_kill_switch

    def strong_read_deletion_fence(self, user_id: str) -> bool:
        self.trace.append("deletion")
        return self.deletion_fence

    def strong_read_user(self, user_id: str):
        self.trace.append("user")
        return self.user if user_id == self.user.get("userId") else None

    def strong_read_session(self, session_id: str):
        self.trace.append("session")
        return self.session if session_id == self.session.get("sessionId") else None

    def strong_read_runtime(self, runtime_arn: str, runtime_qualifier: str):
        self.trace.append("runtime")
        if runtime_arn == self.runtime.get(
            "runtimeArn"
        ) and runtime_qualifier == self.runtime.get("runtimeQualifier"):
            return self.runtime
        return None

    def strong_read_installation(self, user_id: str, pack_id: str):
        self.trace.append("installation")
        if user_id != self.user.get("userId"):
            return None
        return self.installations.get(pack_id)

    def strong_read_target_grant(self, target_hash: str):
        self.trace.append("target")
        return self.targets.get(target_hash)

    def claim_target_use(self, target_hash: str, call_id: str) -> bool:
        self.trace.append("target_claim")
        target = self.targets.get(target_hash)
        if target is None:
            return False
        seen = self.claimed_target_calls.setdefault(target_hash, set())
        if call_id in seen:
            return True
        if target.uses + len(seen) >= target.grant.max_uses:
            return False
        seen.add(call_id)
        return True


class RecordingAdapter:
    def __init__(self, outcome, *, failures=()):
        self.outcome = outcome
        self.failures = list(failures)
        self.calls = []

    def invoke(self, admitted):
        self.calls.append(admitted)
        if self.failures:
            raise self.failures.pop(0)
        return self.outcome


def _gateway(*, operation_id="schedule.list", adapter=True, target_grant=None):
    loaded = _load_gateway_modules()
    assert loaded is not None, "gateway, admission, and ledger modules must exist"
    LiveTargetGrant, AdapterOutcome, CapabilityGateway, Ledger = loaded
    catalog = _catalog()
    repository = FakeRepository(catalog, LiveTargetGrant, target_grant=target_grant)
    outcome = AdapterOutcome(
        status="SUCCEEDED",
        data={"schedules": []},
        provenance_refs=("schedule:list",),
    )
    recording = RecordingAdapter(outcome)
    adapters = {operation_id: recording} if adapter else {}
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=repository,
        ledger=Ledger(),
        adapters=adapters,
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )
    return catalog, repository, recording, gateway, AdapterOutcome


def _rebind_repository(repository, *, user_id: str, session_id: str) -> None:
    repository.user = {
        "userId": user_id,
        "state": "ACTIVE",
        "deletionFence": False,
    }
    repository.session = {
        **repository.session,
        "sessionId": session_id,
        "userId": user_id,
    }
    repository.runtime = {
        **repository.runtime,
        "sessionId": session_id,
        "userId": user_id,
    }
    repository.installations = {
        pack_id: CapabilityInstallationV1.from_mapping(
            {
                **installation.to_mapping(),
                "userId": user_id,
            }
        )
        for pack_id, installation in repository.installations.items()
    }


class FailFirstCompletionLedger:
    """Inject one post-adapter persistence loss without test-only gateway hooks."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.failures = 1

    def begin(self, **kwargs):
        return self.delegate.begin(**kwargs)

    def complete(self, *args, **kwargs):
        if self.failures:
            self.failures -= 1
            raise OSError("synthetic completion loss")
        return self.delegate.complete(*args, **kwargs)


class UnavailableLedger:
    def begin(self, **_kwargs):
        raise OSError("synthetic durable ledger outage")

    def complete(self, **_kwargs):  # pragma: no cover - begin always fails
        raise AssertionError("completion cannot follow a failed begin")


def _iam(catalog, *, target_grant=None, grant_overrides=None):
    return {
        "callerArn": CALLER_ARN,
        "turnGrant": _turn_grant(
            catalog,
            target_grant=target_grant,
            overrides=grant_overrides,
        ),
    }


def test_gateway_modules_and_interfaces_exist():
    loaded = _load_gateway_modules()
    assert loaded is not None
    _, AdapterOutcome, CapabilityGateway, Ledger = loaded
    assert callable(AdapterOutcome)
    assert callable(CapabilityGateway)
    assert callable(Ledger)


def test_happy_path_strong_reads_every_live_binding_and_rechecks_deletion_last():
    catalog, repository, adapter, gateway, _ = _gateway()
    call = _call(catalog, "schedule.list", {})

    result = gateway.invoke(call, _iam(catalog))

    assert result.status == "SUCCEEDED"
    assert result.to_mapping()["data"] == {"schedules": []}
    assert len(adapter.calls) == 1
    assert repository.trace == [
        "deletion",
        "global_kill",
        "user",
        "session",
        "runtime",
        "installation",
        "deletion",
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda repo, iam: iam.update(
                callerArn="arn:aws:iam::000000000000:role/wrong"
            ),
            "IAM_CALLER_DENIED",
        ),
        (lambda repo, iam: iam["turnGrant"].update(sub="user_beta"), "USER_NOT_ACTIVE"),
        (
            lambda repo, iam: repo.session.update(userId="user_beta"),
            "SESSION_BINDING_MISMATCH",
        ),
        (
            lambda repo, iam: repo.runtime.update(sessionId="session_other"),
            "RUNTIME_BINDING_MISMATCH",
        ),
        (
            lambda repo, iam: repo.runtime.update(
                runtimeQualifier="release_" + "f" * 40
            ),
            "RUNTIME_NOT_LIVE",
        ),
        (
            lambda repo, iam: iam["turnGrant"].update(
                releaseCommit="f" * 40,
                runtimeQualifier="release_" + "f" * 40,
            ),
            "RELEASE_DRIFT",
        ),
        (
            lambda repo, iam: iam["turnGrant"].update(catalogDigest="a" * 64),
            "CATALOG_DRIFT",
        ),
        (
            lambda repo, iam: iam["turnGrant"].update(
                iat=NOW - 400,
                exp=NOW,
            ),
            "GRANT_EXPIRED",
        ),
        (
            lambda repo, iam: setattr(repo, "global_kill_switch", True),
            "GLOBAL_KILL_SWITCH",
        ),
        (lambda repo, iam: setattr(repo, "deletion_fence", True), "DELETION_FENCE"),
    ],
)
def test_wrong_live_binding_drift_expiry_and_fences_deny_without_adapter(
    mutation, expected_code
):
    catalog, repository, adapter, gateway, _ = _gateway()
    iam = _iam(catalog)
    mutation(repository, iam)

    result = gateway.invoke(_call(catalog, "schedule.list", {}), iam)

    assert result.status == "DENIED"
    assert result.error_code == expected_code
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("state", "kill_switch", "expected_code"),
    [
        ("PAUSED", False, "PACK_NOT_ENABLED"),
        ("REVOKED", False, "PACK_NOT_ENABLED"),
        ("PAUSED", True, "PACK_KILL_SWITCH"),
    ],
)
def test_disabled_or_killed_pack_denies_without_adapter(
    state, kill_switch, expected_code
):
    catalog, repository, adapter, gateway, _ = _gateway()
    installation = repository.installations["schedule.list"].to_mapping()
    installation.update(state=state, killSwitch=kill_switch)
    repository.installations["schedule.list"] = installation

    result = gateway.invoke(_call(catalog, "schedule.list", {}), _iam(catalog))

    assert result.status == "DENIED"
    assert result.error_code == expected_code
    assert adapter.calls == []


def test_absent_adapter_is_explicitly_disabled_after_admission():
    catalog, _, adapter, gateway, _ = _gateway(adapter=False)
    result = gateway.invoke(_call(catalog, "schedule.list", {}), _iam(catalog))
    assert result.status == "DENIED"
    assert result.error_code == "ADAPTER_DISABLED"
    assert adapter.calls == []


def test_exact_replay_returns_cached_result_and_argument_mutation_is_denied():
    catalog, _, adapter, gateway, AdapterOutcome = _gateway(
        operation_id="workspace.file.read"
    )
    adapter.outcome = AdapterOutcome(
        status="SUCCEEDED",
        data={"path": "notes/a.md", "content": "hello"},
        provenance_refs=("workspace:notes/a.md",),
    )
    original = _call(catalog, "workspace.file.read", {"path": "notes/a.md"})

    first = gateway.invoke(original, _iam(catalog))
    replay = gateway.invoke(original, _iam(catalog))
    mutated = gateway.invoke(
        _call(
            catalog,
            "workspace.file.read",
            {"path": "notes/b.md"},
            tool_use_id=original.tool_use_id,
        ),
        _iam(catalog),
    )

    assert first.to_bytes() == replay.to_bytes()
    assert len(adapter.calls) == 1
    assert mutated.status == "DENIED"
    assert mutated.error_code == "CAPABILITY_ARGUMENT_MUTATION"
    assert len(adapter.calls) == 1


def test_ledger_isolates_identical_call_ids_tool_uses_and_budgets_per_tenant():
    loaded = _load_gateway_modules()
    assert loaded is not None
    LiveTargetGrant, AdapterOutcome, CapabilityGateway, Ledger = loaded
    catalog = _catalog()
    shared_ledger = Ledger()
    call = _call(catalog, "schedule.list", {})

    repository_alpha = FakeRepository(catalog, LiveTargetGrant)
    adapter_alpha = RecordingAdapter(
        AdapterOutcome(
            status="SUCCEEDED",
            data={"schedules": []},
            provenance_refs=("tenant:alpha",),
        )
    )
    gateway_alpha = CapabilityGateway(
        catalog=catalog,
        repository=repository_alpha,
        ledger=shared_ledger,
        adapters={"schedule.list": adapter_alpha},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )

    repository_beta = FakeRepository(catalog, LiveTargetGrant)
    _rebind_repository(
        repository_beta,
        user_id="user_beta",
        session_id="session_87654321",
    )
    adapter_beta = RecordingAdapter(
        AdapterOutcome(
            status="SUCCEEDED",
            data={"schedules": []},
            provenance_refs=("tenant:beta",),
        )
    )
    gateway_beta = CapabilityGateway(
        catalog=catalog,
        repository=repository_beta,
        ledger=shared_ledger,
        adapters={"schedule.list": adapter_beta},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )

    result_alpha = gateway_alpha.invoke(call, _iam(catalog))
    result_beta = gateway_beta.invoke(
        call,
        _iam(
            catalog,
            grant_overrides={
                "sub": "user_beta",
                "sessionId": "session_87654321",
                "nonce": "nonce_876543210abcdef",
            },
        ),
    )

    assert result_alpha.provenance_refs == ("tenant:alpha",)
    assert result_beta.provenance_refs == ("tenant:beta",)
    assert len(adapter_alpha.calls) == 1
    assert len(adapter_beta.calls) == 1


def test_cached_call_rejects_a_different_exact_grant_before_returning_result():
    catalog, _, adapter, gateway, _ = _gateway()
    call = _call(catalog, "schedule.list", {})

    first = gateway.invoke(call, _iam(catalog))
    changed = gateway.invoke(
        call,
        _iam(catalog, grant_overrides={"maxCalls": 9}),
    )

    assert first.status == "SUCCEEDED"
    assert changed.status == "DENIED"
    assert changed.error_code == "CAPABILITY_GRANT_BINDING_MISMATCH"
    assert len(adapter.calls) == 1


def test_durable_ledger_outage_fails_closed_before_adapter_dispatch():
    loaded = _load_gateway_modules()
    assert loaded is not None
    LiveTargetGrant, AdapterOutcome, CapabilityGateway, _ = loaded
    catalog = _catalog()
    repository = FakeRepository(catalog, LiveTargetGrant)
    adapter = RecordingAdapter(
        AdapterOutcome(status="SUCCEEDED", data={"schedules": []})
    )
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=repository,
        ledger=UnavailableLedger(),
        adapters={"schedule.list": adapter},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )

    result = gateway.invoke(
        _call(catalog, "schedule.list", {}),
        _iam(catalog),
    )

    assert result.status == "DENIED"
    assert result.error_code == "CAPABILITY_LEDGER_UNAVAILABLE"
    assert adapter.calls == []


def test_lost_mutation_completion_is_uncertain_and_fences_fresh_tool_use():
    loaded = _load_gateway_modules()
    assert loaded is not None
    LiveTargetGrant, AdapterOutcome, CapabilityGateway, Ledger = loaded
    catalog = _catalog()
    repository = FakeRepository(catalog, LiveTargetGrant)
    adapter = RecordingAdapter(
        AdapterOutcome(
            status="SUCCEEDED",
            data={"path": "notes/a.md", "bytes": 5},
        )
    )
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=repository,
        ledger=FailFirstCompletionLedger(Ledger()),
        adapters={"workspace.file.write": adapter},
        allowed_caller_arn=CALLER_ARN,
        clock=lambda: NOW,
    )
    original = _call(
        catalog,
        "workspace.file.write",
        {"path": "notes/a.md", "content": "hello"},
        tool_use_id="tooluse_11111111",
    )
    fresh_tool = _call(
        catalog,
        "workspace.file.write",
        {"path": "notes/a.md", "content": "hello"},
        tool_use_id="tooluse_22222222",
    )

    ambiguous = gateway.invoke(original, _iam(catalog))
    fenced = gateway.invoke(fresh_tool, _iam(catalog))

    assert ambiguous.status == "UNCERTAIN"
    assert ambiguous.retry_policy == "RECONCILE_ONLY"
    assert ambiguous.error_code == "CAPABILITY_COMPLETION_UNAVAILABLE"
    assert fenced.status == "UNCERTAIN"
    assert fenced.retry_policy == "RECONCILE_ONLY"
    assert fenced.error_code == "CAPABILITY_LOGICAL_EFFECT_UNCERTAIN"
    assert fenced.call_id == fresh_tool.call_id
    assert len(adapter.calls) == 1


def test_read_retry_is_bounded_and_fresh_tool_use_cannot_bypass_same_call_fence():
    catalog, _, adapter, gateway, AdapterOutcome = _gateway(
        operation_id="workspace.file.read"
    )
    adapter.outcome = AdapterOutcome(
        status="SUCCEEDED",
        data={"path": "notes/a.md", "content": "hello"},
        provenance_refs=("workspace:notes/a.md",),
    )
    adapter.failures = [TimeoutError("first"), TimeoutError("second")]
    original = _call(
        catalog,
        "workspace.file.read",
        {"path": "notes/a.md"},
        tool_use_id="tooluse_11111111",
    )
    fresh_tool = _call(
        catalog,
        "workspace.file.read",
        {"path": "notes/a.md"},
        tool_use_id="tooluse_22222222",
    )

    first = gateway.invoke(original, _iam(catalog))
    bypass = gateway.invoke(fresh_tool, _iam(catalog))
    second = gateway.invoke(original, _iam(catalog))
    exhausted = gateway.invoke(original, _iam(catalog))

    assert first.status == "FAILED_RETRYABLE"
    assert bypass.status == "DENIED"
    assert bypass.error_code == "CAPABILITY_READ_RETRY_REQUIRES_SAME_CALL"
    assert second.status == "FAILED_RETRYABLE"
    assert exhausted.status == "DENIED"
    assert exhausted.error_code == "CAPABILITY_READ_RETRY_EXHAUSTED"
    assert len(adapter.calls) == 2


def test_grant_and_pack_call_quotas_are_atomic_and_deny_before_second_dispatch():
    catalog, _, adapter, gateway, _ = _gateway()
    iam = _iam(catalog, grant_overrides={"maxCalls": 1})

    first = gateway.invoke(
        _call(catalog, "schedule.list", {}, tool_use_id="tooluse_11111111"),
        iam,
    )
    second = gateway.invoke(
        _call(catalog, "schedule.list", {}, tool_use_id="tooluse_22222222"),
        iam,
    )

    assert first.status == "SUCCEEDED"
    assert second.status == "DENIED"
    assert second.error_code == "CAPABILITY_CALL_BUDGET_EXCEEDED"
    assert len(adapter.calls) == 1


def test_read_retry_is_same_call_idempotent_but_mutation_ambiguity_never_replays():
    catalog, _, read_adapter, gateway, AdapterOutcome = _gateway(
        operation_id="workspace.file.read"
    )
    read_adapter.outcome = AdapterOutcome(
        status="SUCCEEDED",
        data={"path": "notes/a.md", "content": "hello"},
        provenance_refs=("workspace:notes/a.md",),
    )
    read_adapter.failures = [TimeoutError("synthetic timeout")]
    read_call = _call(catalog, "workspace.file.read", {"path": "notes/a.md"})

    retryable = gateway.invoke(read_call, _iam(catalog))
    succeeded = gateway.invoke(read_call, _iam(catalog))
    assert retryable.status == "FAILED_RETRYABLE"
    assert retryable.retry_policy == "SAFE_RETRY"
    assert "synthetic timeout" not in str(retryable.to_mapping())
    assert succeeded.status == "SUCCEEDED"
    assert len(read_adapter.calls) == 2

    catalog2, _, mutation_adapter, gateway2, AdapterOutcome2 = _gateway(
        operation_id="workspace.file.write"
    )
    mutation_adapter.outcome = AdapterOutcome2(
        status="SUCCEEDED",
        data={"path": "notes/a.md", "bytes": 5},
    )
    mutation_adapter.failures = [TimeoutError("synthetic ambiguity")]
    mutation_call = _call(
        catalog2,
        "workspace.file.write",
        {"path": "notes/a.md", "content": "hello"},
    )
    uncertain = gateway2.invoke(mutation_call, _iam(catalog2))
    replay = gateway2.invoke(mutation_call, _iam(catalog2))
    assert uncertain.status == "UNCERTAIN"
    assert uncertain.retry_policy == "RECONCILE_ONLY"
    assert "synthetic ambiguity" not in str(uncertain.to_mapping())
    assert replay.to_bytes() == uncertain.to_bytes()
    assert len(mutation_adapter.calls) == 1


def test_exact_current_request_target_grant_is_required_claimed_once_and_bounded():
    loaded = _load_gateway_modules()
    assert loaded is not None
    target = _target_grant(max_uses=1)
    catalog, repository, adapter, gateway, AdapterOutcome = _gateway(
        operation_id="web.exact.read", target_grant=target
    )
    adapter.outcome = AdapterOutcome(
        status="SUCCEEDED",
        data={
            "canonicalUrl": "https://example.com/exact",
            "contentDigest": "c" * 64,
            "retrievedAt": NOW,
            "sourceRef": "source_12345678",
            "text": "public text",
        },
        provenance_refs=("source_12345678",),
    )
    iam = _iam(catalog, target_grant=target)
    exact = _call(catalog, "web.exact.read", {"url": "https://example.com/exact"})

    first = gateway.invoke(exact, iam)
    replay = gateway.invoke(exact, iam)
    different = gateway.invoke(
        _call(
            catalog,
            "web.exact.read",
            {"url": "https://example.com/other"},
            tool_use_id="tooluse_87654321",
        ),
        iam,
    )

    assert first.status == "SUCCEEDED"
    assert replay.to_bytes() == first.to_bytes()
    assert different.status == "DENIED"
    assert different.error_code == "TARGET_GRANT_MISMATCH"
    assert len(adapter.calls) == 1
    assert repository.trace.count("target_claim") == 1


def test_expired_or_exhausted_target_grant_denies_with_zero_network_adapter_calls():
    loaded = _load_gateway_modules()
    assert loaded is not None
    LiveTargetGrant = loaded[0]
    expired = _target_grant(expires_at=NOW)
    catalog, repository, adapter, gateway, _ = _gateway(
        operation_id="web.exact.read", target_grant=expired
    )
    result = gateway.invoke(
        _call(catalog, "web.exact.read", {"url": "https://example.com/exact"}),
        _iam(catalog, target_grant=expired),
    )
    assert result.status == "DENIED"
    assert result.error_code == "TARGET_GRANT_EXPIRED"
    assert adapter.calls == []

    live = _target_grant(max_uses=1)
    catalog2, repository2, adapter2, gateway2, _ = _gateway(
        operation_id="web.exact.read", target_grant=live
    )
    repository2.targets[live.target_hash] = LiveTargetGrant(grant=live, uses=1)
    result2 = gateway2.invoke(
        _call(catalog2, "web.exact.read", {"url": "https://example.com/exact"}),
        _iam(catalog2, target_grant=live),
    )
    assert result2.status == "DENIED"
    assert result2.error_code == "TARGET_GRANT_EXHAUSTED"
    assert adapter2.calls == []


def test_deletion_fence_race_after_admission_denies_at_last_point_without_adapter():
    catalog, repository, adapter, gateway, _ = _gateway()
    reads = 0

    def fence(_user_id):
        nonlocal reads
        reads += 1
        repository.trace.append("deletion")
        return reads == 2

    repository.strong_read_deletion_fence = fence
    result = gateway.invoke(_call(catalog, "schedule.list", {}), _iam(catalog))
    assert result.status == "DENIED"
    assert result.error_code == "DELETION_FENCE"
    assert adapter.calls == []
    assert reads == 2


def test_lambda_handler_is_exact_and_fails_closed_on_invalid_production_configuration():
    from capabilities.gateway import lambda_handler

    catalog = _catalog()
    call = _call(catalog, "schedule.list", {})
    event = {
        "schema": "personal-operator.capability-relay-envelope.v1",
        "grant": _turn_grant(catalog),
        "call": call.to_mapping(),
    }
    result = lambda_handler(event, None)
    assert result["status"] == "DENIED"
    assert result["errorCode"] == "GATEWAY_CONFIGURATION_INVALID"
    assert "grant" not in result
    assert "nonce" not in str(result).lower()
