"""OpenClaw CDK stacks package."""

import json
import os
import re
from dataclasses import dataclass

from aws_cdk import Tags, aws_logs as logs
from constructs import Construct, Node

# The retention values CloudWatch Logs accepts, as RetentionDays members.
_RETENTION_MAP = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    120: logs.RetentionDays.FOUR_MONTHS,
    150: logs.RetentionDays.FIVE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    400: logs.RetentionDays.THIRTEEN_MONTHS,
    545: logs.RetentionDays.EIGHTEEN_MONTHS,
    731: logs.RetentionDays.TWO_YEARS,
    1096: logs.RetentionDays.THREE_YEARS,
    1827: logs.RetentionDays.FIVE_YEARS,
    2192: logs.RetentionDays.SIX_YEARS,
    2557: logs.RetentionDays.SEVEN_YEARS,
    2922: logs.RetentionDays.EIGHT_YEARS,
    3288: logs.RetentionDays.NINE_YEARS,
    3653: logs.RetentionDays.TEN_YEARS,
}
_MAX_RETENTION_DAYS = max(_RETENTION_MAP)


def normalize_retention_days(days) -> int:
    """Return the CloudWatch Logs retention value (in days) to use for `days`.

    `days` is the `cloudwatch_log_retention_days` context value: an int from
    cdk.json or a string from `-c`. A value between two valid CloudWatch
    values is rounded up to the next one (45 becomes 60). Anything that is not
    a whole number from 1 to 3653 raises ValueError at synth time instead of
    failing the deployment.
    """
    if isinstance(days, bool):
        value = None
    elif isinstance(days, int):
        value = days
    else:
        text = str(days).strip()
        value = int(text) if re.fullmatch(r"\d+", text) else None
    if value is None or not 1 <= value <= _MAX_RETENTION_DAYS:
        raise ValueError(
            f"cloudwatch_log_retention_days must be a whole number of days from 1 to "
            f"{_MAX_RETENTION_DAYS}, got {days!r}. CloudWatch Logs accepts "
            f"{', '.join(str(d) for d in sorted(_RETENTION_MAP))}; other values are "
            "rounded up to the next of these."
        )
    return next(d for d in sorted(_RETENTION_MAP) if d >= value)


def retention_days(days) -> logs.RetentionDays:
    """Convert a number of days to a RetentionDays enum value.

    See normalize_retention_days for the accepted input and the rounding.
    """
    return _RETENTION_MAP[normalize_retention_days(days)]


def context_flag(node: Node, key: str, default: bool = False) -> bool:
    """Read a boolean context key from cdk.json or ``-c``.

    ``cdk -c key=value`` passes the value as a string, so ``"true"`` and
    ``"false"`` (any case) count the same as the JSON booleans in cdk.json.
    Unset or empty returns ``default``. Any other value raises ValueError at
    synth time, so a typo cannot silently turn a feature on or off.
    """
    value = node.try_get_context(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ValueError(f"context {key} must be true or false, got {value!r}")


# Lowercase letters/digits, single hyphens between segments. At most 12
# characters so the longest suffixed physical names stay inside AWS limits
# (S3 bucket 63, IAM role 64, with the longest supported region names).
_ENV_SUFFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ENV_SUFFIX_MAX_LEN = 12


def environment_suffix(scope) -> str:
    """Return the validated deployment suffix, or "" when none is set.

    Read from the OPENCLAW_ENV_SUFFIX environment variable, else the
    ``environment_suffix`` CDK context key. Empty (the default) leaves every
    stack and resource name exactly as before.
    """
    raw_suffix = os.environ.get("OPENCLAW_ENV_SUFFIX")
    if raw_suffix is None:
        raw_suffix = scope.node.try_get_context("environment_suffix")
    if raw_suffix is None:
        return ""
    suffix = str(raw_suffix).strip().lower().strip("-")
    if not suffix:
        return ""
    if not _ENV_SUFFIX_RE.fullmatch(suffix) or len(suffix) > ENV_SUFFIX_MAX_LEN:
        raise ValueError(
            "environment_suffix must be at most 12 characters of lowercase letters, "
            "digits, and single hyphens between segments"
        )
    return suffix


@dataclass(frozen=True)
class DeploymentNamer:
    """Environment-aware names for stacks and physical resources.

    With an empty suffix every method returns its input unchanged.
    """

    suffix: str = ""

    @classmethod
    def from_scope(cls, scope) -> "DeploymentNamer":
        return cls(environment_suffix(scope))

    @property
    def runtime_suffix(self) -> str:
        return self.suffix.replace("-", "_")

    def with_suffix(self, base: str, separator: str = "-", suffix: str | None = None) -> str:
        active_suffix = self.suffix if suffix is None else suffix
        return f"{base}{separator}{active_suffix}" if active_suffix else base

    def stack(self, base: str) -> str:
        return self.with_suffix(base)

    def name(self, base: str) -> str:
        return self.with_suffix(base)

    def runtime_name(self, base: str) -> str:
        """For names that allow only [a-zA-Z0-9_] (AgentCore runtime/browser, guardrail)."""
        return self.with_suffix(base, separator="_", suffix=self.runtime_suffix)


def table_tags_from_context(node: Node) -> dict[str, str]:
    """Read the ``table_tags`` context key as a string-to-string mapping.

    Accepts a JSON object from cdk.json, or a JSON string from
    ``cdk deploy -c table_tags='{"auto-delete":"no"}'``. Unset or empty means
    no extra tags.
    """
    value = node.try_get_context("table_tags")
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("table_tags context must be a JSON object") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("table_tags context must map tag keys to string values")
    return value


def apply_table_tags(table: Construct) -> None:
    """Tag a DynamoDB table with the ``table_tags`` context.

    CloudFormation does not put its ``aws:cloudformation:*`` tags on DynamoDB
    tables, so accounts with automated cleanup jobs may need an explicit
    opt-out tag. Tags are declared on the resource, so a replacement table
    gets them too.
    """
    for key, value in table_tags_from_context(table.node).items():
        Tags.of(table).add(key, value)
