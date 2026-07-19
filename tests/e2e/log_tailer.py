"""Retired CloudWatch response inspection boundary.

Runtime and router logs are intentionally metadata-only. They are not a test
transport and must never be queried for identities, prompts, model responses,
provider data, or workspace content. Live tests must inspect the direct
AgentCore invocation result or a synthetic in-memory channel instead.
"""

from __future__ import annotations


class UnsafeLogInspection(RuntimeError):
    """A caller attempted to use retained logs as private application data."""


def tail_logs(
    _config: object,
    *,
    since_ms: int | None = None,
    timeout_s: int = 300,
    poll_interval_s: int = 5,
):
    """Fail before any CloudWatch call or private observation is attempted."""

    del since_ms, timeout_s, poll_interval_s
    raise UnsafeLogInspection(
        "CloudWatch response inspection is prohibited; use direct invocation "
        "or synthetic channel evidence"
    )


__all__ = ["UnsafeLogInspection", "tail_logs"]
