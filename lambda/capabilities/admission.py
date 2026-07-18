"""Fail-closed live admission for the Personal Operator capability gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .contracts import (
    CapabilityCallV1,
    CapabilityCatalogV1,
    CapabilityInstallationV1,
    ContractValidationError,
    TargetGrantV1,
    TurnCapabilityGrantV1,
    canonical_json_bytes,
)


class AdmissionDenied(RuntimeError):
    """A valid capability call lacks currently live authority."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LiveTargetGrant:
    grant: TargetGrantV1
    uses: int

    def __post_init__(self) -> None:
        if not isinstance(self.grant, TargetGrantV1):
            raise TypeError("live target state requires a validated target grant")
        if (
            isinstance(self.uses, bool)
            or not isinstance(self.uses, int)
            or self.uses < 0
        ):
            raise TypeError("live target use count must be a non-negative integer")


class AdmissionRepository(Protocol):
    """Strong-read authority repository; implementations must not return cached data."""

    def strong_read_global_kill_switch(self) -> bool: ...

    def strong_read_deletion_fence(self, user_id: str) -> bool: ...

    def strong_read_user(self, user_id: str) -> Mapping[str, Any] | None: ...

    def strong_read_session(self, session_id: str) -> Mapping[str, Any] | None: ...

    def strong_read_runtime(
        self, runtime_arn: str, runtime_qualifier: str
    ) -> Mapping[str, Any] | None: ...

    def strong_read_installation(
        self, user_id: str, pack_id: str
    ) -> CapabilityInstallationV1 | Mapping[str, Any] | None: ...

    def strong_read_target_grant(self, target_hash: str) -> LiveTargetGrant | None: ...

    def claim_target_use(self, target_hash: str, call_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AdmittedCall:
    call: CapabilityCallV1
    grant: TurnCapabilityGrantV1
    pack: Mapping[str, Any]
    operation: Mapping[str, Any]
    installation: CapabilityInstallationV1
    target: LiveTargetGrant | None

    @property
    def retry_mode(self) -> str:
        return self.pack["retryPolicy"]["mode"]

    @property
    def pack_id(self) -> str:
        return self.pack["packId"]


def _deny(code: str) -> None:
    raise AdmissionDenied(code)


def _exact_record(
    value: Mapping[str, Any] | None,
    fields: frozenset[str],
    missing_code: str,
    corrupt_code: str,
) -> Mapping[str, Any]:
    if value is None:
        _deny(missing_code)
    if not isinstance(value, Mapping) or set(value) != fields:
        _deny(corrupt_code)
    return value


def _operation_registry(catalog: CapabilityCatalogV1) -> dict[str, tuple[dict, dict]]:
    registry: dict[str, tuple[dict, dict]] = {}
    for pack_value in catalog.packs:
        pack = {key: value for key, value in pack_value.items()}
        operations = pack["operations"]
        if len(operations) != 1:
            raise ValueError("frozen catalog pack does not contain one operation")
        operation = {key: value for key, value in operations[0].items()}
        registry[operation["operationId"]] = (pack, operation)
    if len(registry) != 10:
        raise ValueError("frozen catalog must contain ten exact operations")
    return registry


class AdmissionGate:
    """Validate one call against release state and strong-read live authority."""

    def __init__(
        self,
        *,
        catalog: CapabilityCatalogV1,
        repository: AdmissionRepository,
        allowed_caller_arn: str,
        clock: Callable[[], int],
    ) -> None:
        if not isinstance(catalog, CapabilityCatalogV1):
            raise TypeError("admission requires a validated capability catalog")
        if not isinstance(allowed_caller_arn, str) or not allowed_caller_arn:
            raise TypeError("admission requires one exact IAM caller ARN")
        if not callable(clock):
            raise TypeError("admission requires a trusted clock")
        self._catalog = catalog
        self._repository = repository
        self._allowed_caller_arn = allowed_caller_arn
        self._clock = clock
        self._operations = _operation_registry(catalog)

    def _strong(self, callback, *args):
        try:
            return callback(*args)
        except AdmissionDenied:
            raise
        except Exception:
            _deny("LIVE_AUTHORITY_UNAVAILABLE")

    def admit(
        self, call: CapabilityCallV1, iam_context: Mapping[str, Any]
    ) -> AdmittedCall:
        if not isinstance(call, CapabilityCallV1):
            raise TypeError("admission requires a validated capability call")
        if not isinstance(iam_context, Mapping) or set(iam_context) != {
            "callerArn",
            "turnGrant",
        }:
            _deny("IAM_CONTEXT_INVALID")
        if iam_context["callerArn"] != self._allowed_caller_arn:
            _deny("IAM_CALLER_DENIED")
        try:
            grant = TurnCapabilityGrantV1.from_mapping(iam_context["turnGrant"])
        except (ContractValidationError, TypeError, ValueError):
            _deny("GRANT_INVALID")

        if grant.release_commit != self._catalog.release_commit:
            _deny("RELEASE_DRIFT")
        if grant.catalog_digest != self._catalog.catalog_digest:
            _deny("CATALOG_DRIFT")
        if (
            call.catalog_digest != grant.catalog_digest
            or call.invocation_id != grant.invocation_id
        ):
            _deny("CALL_GRANT_MISMATCH")

        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            _deny("TRUSTED_CLOCK_INVALID")
        if now < grant.iat:
            _deny("GRANT_NOT_YET_VALID")
        if now >= grant.exp:
            _deny("GRANT_EXPIRED")

        row = self._operations.get(call.operation_id)
        if row is None:
            _deny("OPERATION_NOT_CATALOGUED")
        pack, operation = row
        if operation["toolName"] != call.tool_name:
            _deny("TOOL_OPERATION_MISMATCH")
        if call.operation_id not in grant.allowed_operation_ids:
            _deny("OPERATION_NOT_GRANTED")
        if pack["packId"] not in grant.allowed_pack_ids:
            _deny("PACK_NOT_GRANTED")
        input_size = len(canonical_json_bytes(call.arguments))
        if input_size > pack["quotaPolicy"]["maxInputBytes"]:
            _deny("CAPABILITY_INPUT_QUOTA_EXCEEDED")

        if self._strong(self._repository.strong_read_deletion_fence, grant.sub):
            _deny("DELETION_FENCE")
        if self._strong(self._repository.strong_read_global_kill_switch):
            _deny("GLOBAL_KILL_SWITCH")

        user = _exact_record(
            self._strong(self._repository.strong_read_user, grant.sub),
            frozenset({"userId", "state", "deletionFence"}),
            "USER_NOT_ACTIVE",
            "USER_RECORD_INVALID",
        )
        if user["userId"] != grant.sub or user["state"] != "ACTIVE":
            _deny("USER_NOT_ACTIVE")
        if user["deletionFence"] is not False:
            _deny("DELETION_FENCE")

        session = _exact_record(
            self._strong(self._repository.strong_read_session, grant.session_id),
            frozenset(
                {
                    "sessionId",
                    "userId",
                    "runtimeArn",
                    "runtimeQualifier",
                    "state",
                }
            ),
            "SESSION_NOT_LIVE",
            "SESSION_RECORD_INVALID",
        )
        if session["state"] != "ACTIVE":
            _deny("SESSION_NOT_LIVE")
        if (
            session["sessionId"] != grant.session_id
            or session["userId"] != grant.sub
            or session["runtimeArn"] != grant.runtime_arn
            or session["runtimeQualifier"] != grant.runtime_qualifier
        ):
            _deny("SESSION_BINDING_MISMATCH")

        runtime = _exact_record(
            self._strong(
                self._repository.strong_read_runtime,
                grant.runtime_arn,
                grant.runtime_qualifier,
            ),
            frozenset(
                {
                    "runtimeArn",
                    "runtimeQualifier",
                    "sessionId",
                    "userId",
                    "releaseCommit",
                    "catalogDigest",
                    "state",
                }
            ),
            "RUNTIME_NOT_LIVE",
            "RUNTIME_RECORD_INVALID",
        )
        if runtime["state"] != "READY":
            _deny("RUNTIME_NOT_LIVE")
        if (
            runtime["runtimeArn"] != grant.runtime_arn
            or runtime["runtimeQualifier"] != grant.runtime_qualifier
            or runtime["sessionId"] != grant.session_id
            or runtime["userId"] != grant.sub
            or runtime["releaseCommit"] != grant.release_commit
            or runtime["catalogDigest"] != grant.catalog_digest
        ):
            _deny("RUNTIME_BINDING_MISMATCH")

        raw_installation = self._strong(
            self._repository.strong_read_installation,
            grant.sub,
            pack["packId"],
        )
        if raw_installation is None:
            _deny("PACK_NOT_ENABLED")
        try:
            installation = (
                raw_installation
                if isinstance(raw_installation, CapabilityInstallationV1)
                else CapabilityInstallationV1.from_mapping(raw_installation)
            )
        except (ContractValidationError, TypeError, ValueError):
            _deny("INSTALLATION_INVALID")
        if (
            installation.user_id != grant.sub
            or installation.pack_id != pack["packId"]
            or installation.catalog_digest != grant.catalog_digest
        ):
            _deny("INSTALLATION_BINDING_MISMATCH")
        if installation.kill_switch:
            _deny("PACK_KILL_SWITCH")
        if installation.state != "ENABLED":
            _deny("PACK_NOT_ENABLED")

        target = self._admit_target(call, grant, pack, now)
        return AdmittedCall(
            call=call,
            grant=grant,
            pack=pack,
            operation=operation,
            installation=installation,
            target=target,
        )

    def _admit_target(
        self,
        call: CapabilityCallV1,
        grant: TurnCapabilityGrantV1,
        pack: Mapping[str, Any],
        now: int,
    ) -> LiveTargetGrant | None:
        if pack["approvalPolicy"]["mode"] != "CURRENT_REQUEST_TARGET_GRANT":
            return None
        requested_url = call.arguments.get("url")
        expired = False
        exhausted = False
        for target_hash in grant.target_grant_hashes:
            live = self._strong(
                self._repository.strong_read_target_grant, target_hash
            )
            if live is None:
                continue
            if not isinstance(live, LiveTargetGrant):
                _deny("TARGET_GRANT_INVALID")
            target = live.grant
            if target.target_hash != target_hash:
                _deny("TARGET_GRANT_INVALID")
            if target.normalized_target != requested_url or target.method != "GET":
                continue
            if now >= target.expires_at:
                expired = True
                continue
            if live.uses >= target.max_uses:
                exhausted = True
                continue
            return live
        if expired:
            _deny("TARGET_GRANT_EXPIRED")
        if exhausted:
            _deny("TARGET_GRANT_EXHAUSTED")
        _deny("TARGET_GRANT_MISMATCH")

    def claim_target(self, admitted: AdmittedCall) -> None:
        if admitted.target is None:
            return
        target_hash = admitted.target.grant.target_hash
        claimed = self._strong(
            self._repository.claim_target_use,
            target_hash,
            admitted.call.call_id,
        )
        if claimed is not True:
            _deny("TARGET_GRANT_EXHAUSTED")

    def recheck_deletion_fence(self, admitted: AdmittedCall) -> None:
        if self._strong(
            self._repository.strong_read_deletion_fence,
            admitted.grant.sub,
        ):
            _deny("DELETION_FENCE")


__all__ = [
    "AdmissionDenied",
    "AdmissionGate",
    "AdmissionRepository",
    "AdmittedCall",
    "LiveTargetGrant",
]
