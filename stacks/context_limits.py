"""Synth-time parsing of numeric limit context keys.

``int(ctx or "100")`` silently turns an explicit ``0`` back into the default,
so these helpers treat only an absent (or empty) key as "use the default" and
reject anything that is not a non-negative integer, so a typo never deploys.
"""

from constructs import Construct


def context_int(scope: Construct, key: str, default: int, minimum: int = 0) -> int:
    """Return the integer value of context ``key``, or ``default`` when unset."""
    raw = scope.node.try_get_context(key)
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return default
    if isinstance(raw, bool):
        raise ValueError(f"context '{key}' must be an integer, got {raw!r}")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"context '{key}' must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"context '{key}' must be >= {minimum}, got {value}")
    return value


def reserved_concurrency(scope: Construct, key: str) -> int | None:
    """Return a Lambda reserved-concurrency value, or None (unreserved).

    Opt-in: unset or ``0`` leaves the function on the shared, unreserved
    account pool (the previous behaviour). Reserving concurrency takes it out
    of that pool, and Lambda refuses a reservation that would leave fewer
    than 100 unreserved executions, so a deploy into a low-limit account
    fails if the reservations are too large.
    """
    value = context_int(scope, key, default=0)
    return value or None
