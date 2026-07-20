"""Trusted per-turn capability issuer and durable handoff boundary."""

from __future__ import annotations

import re
from typing import Callable, Mapping, Protocol, Sequence

from .admission import LiveTargetGrant
from .contracts import CapabilityCatalogV1, TurnCapabilityGrantV1
from .target_grants import (
    derive_target_grants,
    project_live_target_rows,
    project_target_grant_hashes,
)


class TurnAuthorityRepository(Protocol):
    def strong_read_enabled_pack_ids(
        self,
        *,
        user_id: str,
        issued_at: int,
    ) -> Sequence[str]: ...

    def prepare_turn(
        self,
        *,
        grant: TurnCapabilityGrantV1,
        targets: Sequence[LiveTargetGrant],
        delivery_context: Mapping[str, str] | None = None,
    ) -> None: ...


_SCHEDULED_READ_APPROVALS = frozenset({"NONE"})
_SCHEDULED_EXCLUDED_RISKS = frozenset(
    {"LOCAL_MUTATION", "DURABLE_MUTATION", "EXTERNAL_EFFECT", "IRREVERSIBLE_EFFECT"}
)
_TELEGRAM_ACTOR = re.compile(r"telegram:([1-9][0-9]{0,19})")


def _scheduled_allowed(pack) -> bool:
    if pack["credentialBoundary"] == "NETWORKLESS_COMPUTE":
        return False
    approval = pack["approvalPolicy"]["mode"]
    return approval == "EXACT_ONE_TIME_PROPOSAL" or (
        approval in _SCHEDULED_READ_APPROVALS
        and pack["riskClass"] not in _SCHEDULED_EXCLUDED_RISKS
    )


class TurnCapabilityIssuer:
    """Mint one short-lived grant only after its live authority is durable."""

    def __init__(
        self,
        *,
        catalog: CapabilityCatalogV1,
        authority_repository: TurnAuthorityRepository,
        runtime_arn: str,
        runtime_qualifier: str,
        clock: Callable[[], int],
        nonce_factory: Callable[[], str],
        ttl_seconds: int = 300,
    ) -> None:
        if not isinstance(catalog, CapabilityCatalogV1):
            raise TypeError("turn issuer requires the frozen capability catalog")
        if not callable(getattr(authority_repository, "prepare_turn", None)):
            raise TypeError("turn issuer requires a durable authority repository")
        if runtime_qualifier != f"release_{catalog.release_commit}":
            raise ValueError("turn issuer runtime qualifier differs from release")
        if not callable(clock) or not callable(nonce_factory):
            raise TypeError("turn issuer requires trusted clock and nonce sources")
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 1 <= ttl_seconds <= 900
        ):
            raise ValueError("turn issuer lifetime must be 1-900 seconds")
        self._catalog = catalog
        self._repository = authority_repository
        self._runtime_arn = runtime_arn
        self._runtime_qualifier = runtime_qualifier
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._ttl_seconds = ttl_seconds

    def mint(
        self,
        *,
        user_id: str,
        session_id: str,
        invocation_id: str,
        message_text: str,
        scheduled_read_only: bool,
        channel: str | None = None,
        actor_id: str | None = None,
    ) -> dict:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise RuntimeError("turn issuer clock is invalid")
        enabled_pack_ids = self._repository.strong_read_enabled_pack_ids(
            user_id=user_id,
            issued_at=now,
        )
        if (
            isinstance(enabled_pack_ids, (str, bytes))
            or not isinstance(enabled_pack_ids, Sequence)
            or any(not isinstance(pack_id, str) for pack_id in enabled_pack_ids)
            or list(enabled_pack_ids) != sorted(set(enabled_pack_ids))
        ):
            raise RuntimeError("turn installation authority is invalid")
        catalog_pack_ids = {pack["packId"] for pack in self._catalog.packs}
        if not set(enabled_pack_ids).issubset(catalog_pack_ids):
            raise RuntimeError("turn installation authority differs from catalog")
        packs = [
            pack
            for pack in self._catalog.packs
            if pack["packId"] in enabled_pack_ids
            and (not scheduled_read_only or _scheduled_allowed(pack))
        ]
        if not packs:
            raise RuntimeError("turn has no enabled capability installation")
        operations = sorted(pack["operations"][0]["operationId"] for pack in packs)
        pack_ids = sorted(pack["packId"] for pack in packs)
        # A scheduled prompt is persisted data, not a fresh authenticated user
        # request. It therefore cannot be a source of CURRENT_REQUEST target
        # authority even if the stored text contains an otherwise valid URL.
        targets = (
            []
            if scheduled_read_only
            else derive_target_grants(
                message_text,
                tenant_id=user_id,
                current_request_id=invocation_id,
                now=now,
                ttl_seconds=self._ttl_seconds,
            )
        )
        grant = TurnCapabilityGrantV1.from_mapping(
            {
                "schema": TurnCapabilityGrantV1.SCHEMA,
                "sub": user_id,
                "sessionId": session_id,
                "runtimeArn": self._runtime_arn,
                "runtimeQualifier": self._runtime_qualifier,
                "invocationId": invocation_id,
                "releaseCommit": self._catalog.release_commit,
                "catalogDigest": self._catalog.catalog_digest,
                "allowedPackIds": pack_ids,
                "allowedOperationIds": operations,
                "targetGrantHashes": project_target_grant_hashes(targets),
                "iat": now,
                "exp": now + self._ttl_seconds,
                "maxCalls": 64,
                "nonce": self._nonce_factory(),
            }
        )
        live_targets = project_live_target_rows(targets)
        delivery_context = None
        if channel == "telegram":
            matched = (
                _TELEGRAM_ACTOR.fullmatch(actor_id)
                if isinstance(actor_id, str)
                else None
            )
            if matched is None:
                raise ValueError("Telegram turn delivery identity is invalid")
            delivery_context = {
                "channel": "telegram",
                "actorId": actor_id,
                "chatId": matched.group(1),
            }
        self._repository.prepare_turn(
            grant=grant,
            targets=live_targets,
            delivery_context=delivery_context,
        )
        return grant.to_mapping()


__all__ = ["TurnAuthorityRepository", "TurnCapabilityIssuer"]
