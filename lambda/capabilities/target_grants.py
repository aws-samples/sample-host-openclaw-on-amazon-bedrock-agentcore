"""Derive target grants exclusively from the current authenticated message.

The single public entry point, :func:`derive_target_grants`, accepts ONLY the
raw current-message text. It cannot read previous-turn context, conversation
history, or workspace/file content because its signature exposes no such seam.
A URL that appears only in a prior turn or in a workspace file therefore yields
no grant. All work is pure hashing; no network, DNS, or socket is ever touched.
"""

from __future__ import annotations

import re
from typing import Sequence

from .admission import LiveTargetGrant
from .contracts import (
    ContractValidationError,
    TargetGrantV1,
    derive_target_hash,
    derive_target_tenant_binding,
)

# Literal https token scan. We never rewrite or guess a URL: a candidate is a
# contiguous run of URL-legal characters that begins with the exact ``https://``
# scheme. Every candidate is then run through the authoritative URL gate
# (``_public_https_url`` via ``derive_target_hash``); anything it rejects is
# silently dropped and never becomes a grant.
_HTTPS_TOKEN = re.compile(r"https://[^\s\"'<>`{}|\\^\[\]]+")

# Trailing punctuation that commonly abuts a URL in prose but is not part of it.
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"


def _candidate_urls(message_text: str) -> list[str]:
    candidates: list[str] = []
    for match in _HTTPS_TOKEN.finditer(message_text):
        token = match.group(0)
        while token and token[-1] in _TRAILING_PUNCTUATION:
            token = token[:-1]
        if token:
            candidates.append(token)
    return candidates


def derive_target_grants(
    message_text: str,
    *,
    current_request_id: str,
    tenant_id: str,
    now: int,
    ttl_seconds: int,
    redirect_policy: str = "NO_REDIRECT",
    max_uses: int = 1,
) -> list[TargetGrantV1]:
    """Mint target grants for the exact https URLs literally in ``message_text``.

    Returns ``[]`` when the message carries no admissible exact URL. Each
    surviving URL is bound to ``current_request_id`` and ``tenant_id`` so a
    stale or cross-tenant grant fails admission. The request identity must be
    the trusted turn's invocation ID. Pure; never touches the network.
    """

    if not isinstance(message_text, str):
        raise TypeError("message text must be a string")
    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise ValueError("now must be a non-negative integer")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")

    expires_at = now + ttl_seconds
    tenant_binding = derive_target_tenant_binding(tenant_id)
    grants: list[TargetGrantV1] = []
    seen: set[str] = set()
    for candidate in _candidate_urls(message_text):
        if candidate in seen:
            continue
        try:
            target_hash = derive_target_hash(
                candidate,
                "GET",
                redirect_policy,
                expires_at,
                max_uses,
                current_request_id,
                tenant_binding,
            )
            grant = TargetGrantV1.from_mapping(
                {
                    "schema": TargetGrantV1.SCHEMA,
                    "targetHash": target_hash,
                    "normalizedTarget": candidate,
                    "method": "GET",
                    "redirectPolicy": redirect_policy,
                    "expiresAt": expires_at,
                    "maxUses": max_uses,
                    "currentRequestId": current_request_id,
                    "tenantBinding": tenant_binding,
                }
            )
        except (ContractValidationError, TypeError, ValueError):
            # Private/metadata IP, encoded, non-canonical, or non-https URLs are
            # dropped silently and never become a grant.
            continue
        seen.add(candidate)
        grants.append(grant)
    return grants


def project_target_grant_hashes(grants: Sequence[TargetGrantV1]) -> list[str]:
    """Return the sorted-unique targetGrantHashes for a turn grant."""

    return sorted({grant.target_hash for grant in grants})


def project_live_target_rows(
    grants: Sequence[TargetGrantV1],
) -> list[LiveTargetGrant]:
    """Return fresh (uses=0) live repository rows for the derived grants."""

    return [LiveTargetGrant(grant=grant, uses=0) for grant in grants]


__all__ = [
    "derive_target_grants",
    "project_live_target_rows",
    "project_target_grant_hashes",
]
