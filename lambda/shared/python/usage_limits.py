"""Per-user daily cost limit (per_user_daily_cost_usd) and the /usage report.

The token-metrics Lambda (lambda/token_metrics/index.py) adds the estimated
cost and the token counts of every Bedrock call to one item per internal user
and UTC day in the identity table, with one atomic UpdateItem:

    PK USAGE#<user ID>  SK DAY#<yyyy-mm-dd>  costUsd, inputTokens, outputTokens, calls,
                                            cronCostUsd, cronCalls, ttl, updatedAt
    PK USAGE#<user ID>  SK CAP               dailyCostUsd, setAt (manage-allowlist.sh set-cap)

The partition is outside USER#<user ID>, so the agent's scoped credentials
cannot write it. A CAP override above 0 replaces the default limit for that
user and 0 exempts the user; overrides apply only while the default limit is
on (above 0).

The Router checks before every chat turn and the Cron Lambda before every
scheduled run, and neither calls AgentCore for a user at or over the limit.
The limit is soft: a call reaches the day item only after the invocation-log
pipeline has run (a few minutes), and a turn that is already running is not
stopped. The day resets at 00:00 UTC, the day boundary of the token-usage rows.

A failed read allows the turn and logs a WARNING (fail open): this is a cost
guard, while access control (the allowlist and the disable switch) fails
closed. Costs are the token-metrics Lambda's estimates (MODEL_PRICING).
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

logger = logging.getLogger(__name__)

# Keys, shared with lambda/token_metrics/index.py and scripts/manage-allowlist.sh.
PK_PREFIX = "USAGE#"
DAY_PREFIX = "DAY#"
CAP_SK = "CAP"
# The largest limit accepted, as at synth (per_user_daily_cost_usd) and by set-cap.
MAX_LIMIT_USD = Decimal(10000)
_CENT = Decimal("0.01")


def utc_now():
    return datetime.now(timezone.utc)


def utc_day(now):
    """The UTC day of now as yyyy-mm-dd, the day of the usage item."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def time_to_reset(now):
    """Time until the next 00:00 UTC, e.g. "6 h 12 min", rounded up to a whole minute."""
    now = now.astimezone(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    hours, minutes = divmod(max(1, math.ceil((midnight - now).total_seconds() / 60)), 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def parse_limit(raw):
    """The default limit in USD from the Lambda environment, as a Decimal; 0 (off) when unset.

    The synth validates the value, so anything else is a manual change: it is
    logged and the limit stays off.
    """
    if raw is None or not str(raw).strip():
        return Decimal(0)
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0 or value > MAX_LIMIT_USD:
        logger.error("PER_USER_DAILY_COST_USD is not a number from 0 to %s; the daily usage limit is off",
                     MAX_LIMIT_USD)
        return Decimal(0)
    return value


def _number(value):
    """A DynamoDB number (boto3 returns Decimal), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None


def _count(value):
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


@dataclass(frozen=True)
class DailyUsage:
    """One user's usage on one UTC day, and the limit that applies to it (None: no limit)."""

    day: str
    cost: Decimal
    calls: int
    cron_calls: int
    input_tokens: int
    output_tokens: int
    limit: Decimal | None
    cron_notice_sent: bool

    @property
    def reached(self):
        return self.limit is not None and self.cost >= self.limit


def _limit(default_limit, cap_item, user_id):
    """The limit for this user: the CAP override while the limit is on, else the default."""
    if default_limit <= 0:
        return None
    if cap_item is not None:
        override = _number(cap_item.get("dailyCostUsd"))
        if override is None or override < 0 or override > MAX_LIMIT_USD:
            logger.warning("Ignoring a malformed usage limit override for user %s", user_id)
        else:
            return override if override > 0 else None
    return default_limit


def read_usage(table, user_id, default_limit, now):
    """Today's usage and limit from one eventually consistent Query, or None if it failed.

    The range CAP .. DAY#<today> returns the override, today's item and older
    days that TTL has not removed yet (a day item expires two days after its
    day ends). A missing day item counts as no usage.
    """
    day = utc_day(now)
    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND SK BETWEEN :cap AND :day",
            ExpressionAttributeValues={":pk": PK_PREFIX + user_id, ":cap": CAP_SK, ":day": DAY_PREFIX + day},
        )
    except Exception as error:  # noqa: BLE001 -- fail open: the caller allows the turn
        logger.warning("Usage read failed for user %s: %s", user_id, type(error).__name__)
        return None
    items = {item.get("SK"): item for item in response.get("Items") or [] if isinstance(item, dict)}
    today = items.get(DAY_PREFIX + day) or {}
    return DailyUsage(
        day=day,
        cost=_number(today.get("costUsd")) or Decimal(0),
        calls=_count(today.get("calls")),
        cron_calls=_count(today.get("cronCalls")),
        input_tokens=_count(today.get("inputTokens")),
        output_tokens=_count(today.get("outputTokens")),
        limit=_limit(default_limit, items.get(CAP_SK), user_id),
        cron_notice_sent="cronNoticeAt" in today,
    )


def over_limit(table, user_id, default_limit, now=None):
    """Today's usage if the user is at or over the limit, else None (the turn may run).

    Makes no read while the limit is off (default_limit 0), and returns None
    when the read fails.
    """
    if default_limit <= 0:
        return None
    usage = read_usage(table, user_id, default_limit, now or utc_now())
    return usage if usage is not None and usage.reached else None


def _usd(value):
    with localcontext() as context:
        context.prec = 60
        return f"${value.quantize(_CENT, rounding=ROUND_HALF_UP):,.2f}"


def _percent(usage):
    with localcontext() as context:
        context.prec = 60
        return (usage.cost * 100 / usage.limit).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def limit_reached_text(usage, now=None, conversation=False):
    """The reply sent instead of a turn. conversation: the identity is a group or channel."""
    who = "This conversation has" if conversation else "You have"
    return (f"{who} reached today's usage limit (about {_usd(usage.cost)} of {_usd(usage.limit)}). "
            f"It resets at 00:00 UTC, in {time_to_reset(now or utc_now())}. "
            "Send /usage to see today's usage.")


def usage_report(table, user_id, default_limit, now=None, conversation=False):
    """The /usage reply: today's estimated cost, calls and tokens, and the limit."""
    now = now or utc_now()
    usage = read_usage(table, user_id, default_limit, now)
    if usage is None:
        return "Sorry, I couldn't read today's usage. Please try again later."
    if usage.limit is None:
        cost = f"- Cost: {_usd(usage.cost)} (no daily limit)"
    else:
        whose = "this conversation's" if conversation else "your"
        cost = f"- Cost: {_usd(usage.cost)} of {whose} {_usd(usage.limit)} daily limit ({_percent(usage)}%)"
    calls = f"- Model calls: {usage.calls:,}"
    if usage.cron_calls:
        calls += f", of which {usage.cron_calls:,} from scheduled tasks"
    subject = " for this conversation" if conversation else ""
    return "\n".join([
        f"Usage today{subject} (UTC {usage.day}), estimated:",
        cost,
        calls,
        f"- Tokens: {usage.input_tokens:,} in, {usage.output_tokens:,} out",
        f"Resets at 00:00 UTC, in {time_to_reset(now)}. New usage can take a few minutes to appear.",
    ])


def claim_cron_notice(table, user_id, day, now=None):
    """Mark today's skipped-schedule notice as sent. False if it already was or the write failed.

    A conditional write on the day item, so a user is told at most once per
    UTC day, also when schedules fire at the same time.
    """
    try:
        table.update_item(
            Key={"PK": PK_PREFIX + user_id, "SK": DAY_PREFIX + day},
            UpdateExpression="SET #notice = :at",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#notice)",
            ExpressionAttributeNames={"#notice": "cronNoticeAt"},
            ExpressionAttributeValues={":at": (now or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        return True
    except Exception as error:  # noqa: BLE001 -- the notice is best effort; the run is skipped either way
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.warning("Usage notice record write failed for user %s: %s", user_id, type(error).__name__)
        return False
