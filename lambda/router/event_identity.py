"""One canonical identity for signed platform events and their retries."""

from __future__ import annotations

import hashlib
import re


_CHANNELS = frozenset({"telegram", "slack", "feishu"})
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_TRACE_ID = re.compile(r"po1_[0-9a-f]{64}")


def canonical_user_id(user_id: object) -> str:
    value = str(user_id or "")
    if _USER_ID.fullmatch(value) is None:
        raise ValueError("invalid internal user identity")
    return value


def derive_event_trace(channel: object, user_id: object, platform_event_id: object) -> str:
    if channel not in _CHANNELS:
        raise ValueError("unsupported invocation channel")
    canonical_user = canonical_user_id(user_id)
    event_id = str(platform_event_id or "").strip()
    if not event_id or len(event_id) > 512:
        raise ValueError("missing immutable platform event identity")
    canonical = (
        f"personal-operator-invocation-v1\0{channel}\0{canonical_user}\0{event_id}"
    )
    return "po1_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assert_event_trace(
    trace_id: object,
    *,
    channel: object,
    user_id: object,
    platform_event_id: object,
) -> str:
    if not isinstance(trace_id, str) or _TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError("invalid event trace identity")
    expected = derive_event_trace(channel, user_id, platform_event_id)
    if trace_id != expected:
        raise ValueError("event trace is not bound to this platform event")
    return trace_id
