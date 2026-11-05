"""Synth-time parsing of numeric limit context keys.

``int(ctx or "100")`` silently turns an explicit ``0`` back into the default,
so these helpers treat only an absent (or empty) key as "use the default" and
reject anything that is not a non-negative integer, so a typo never deploys.
"""

from decimal import Decimal, InvalidOperation

from constructs import Construct

PER_USER_DAILY_COST_KEY = "per_user_daily_cost_usd"
# Also the largest override scripts/manage-allowlist.sh set-cap accepts.
MAX_PER_USER_DAILY_COST_USD = Decimal(10000)


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


def per_user_daily_cost_usd(scope: Construct) -> str | None:
    """The per-user daily cost limit as the Lambdas' PER_USER_DAILY_COST_USD value, or None when off.

    ``per_user_daily_cost_usd`` is a number of USD from 0 to 10000; anything
    else fails the synth. Unset or 0 (the default) is off: the Router and the
    Cron Lambda then get no variable, so their configuration is unchanged.
    """
    raw = scope.node.try_get_context(PER_USER_DAILY_COST_KEY)
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    value = None
    if not isinstance(raw, bool):
        try:
            value = Decimal(str(raw).strip())
        except InvalidOperation:
            value = None
    if value is None or not value.is_finite() or not 0 <= value <= MAX_PER_USER_DAILY_COST_USD:
        raise ValueError(f"context '{PER_USER_DAILY_COST_KEY}' must be a number of USD from 0 to "
                         f"{MAX_PER_USER_DAILY_COST_USD}, got {raw!r}")
    return format(value.normalize(), "f") if value else None
