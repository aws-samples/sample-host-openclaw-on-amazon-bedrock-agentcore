"""OpenClaw CDK stacks package."""

import os
import re
from dataclasses import dataclass

from aws_cdk import RemovalPolicy, aws_logs as logs

# Map integer days to the nearest valid RetentionDays enum member.
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
}


def retention_days(days: int) -> logs.RetentionDays:
    """Convert an integer number of days to a RetentionDays enum value."""
    if days in _RETENTION_MAP:
        return _RETENTION_MAP[days]
    # Find the closest valid value that is >= the requested days
    for d in sorted(_RETENTION_MAP):
        if d >= days:
            return _RETENTION_MAP[d]
    return logs.RetentionDays.ONE_YEAR


_ENV_SUFFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def environment_suffix(scope) -> str:
    """Return the validated deployment suffix from env or CDK context."""
    raw_suffix = os.environ.get("OPENCLAW_ENV_SUFFIX")
    if raw_suffix is None:
        raw_suffix = scope.node.try_get_context("environment_suffix")

    if raw_suffix is None:
        return ""

    suffix = str(raw_suffix).strip().lower().strip("-")
    if not suffix:
        return ""
    if not _ENV_SUFFIX_RE.fullmatch(suffix):
        raise ValueError(
            "environment_suffix must contain only lowercase letters, digits, "
            "and single hyphens between segments"
        )
    return suffix


def parse_optional_bool(raw_value) -> bool | None:
    """Parse common bool-like values, returning None when unset."""
    if raw_value is None:
        return None

    normalized = str(raw_value).strip().lower()
    if not normalized:
        return None
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def retain_stateful_resources(scope) -> bool:
    """Whether long-lived stateful resources should use RemovalPolicy.RETAIN."""
    raw_value = os.environ.get("RETAIN_STATEFUL_RESOURCES")
    if raw_value is None:
        raw_value = scope.node.try_get_context("retain_stateful_resources")

    parsed = parse_optional_bool(raw_value)
    if raw_value is not None and str(raw_value).strip() and parsed is None:
        raise ValueError(
            "retain_stateful_resources/RETAIN_STATEFUL_RESOURCES must be one of: "
            "true, false, 1, 0, yes, no, on, off"
        )
    if parsed is None:
        return True
    return parsed


def stateful_removal_policy(scope) -> RemovalPolicy:
    """Return the configured removal policy for long-lived stateful resources."""
    return (
        RemovalPolicy.RETAIN
        if retain_stateful_resources(scope)
        else RemovalPolicy.DESTROY
    )


def auto_delete_bucket_objects(scope) -> bool:
    """Delete bucket contents automatically when stateful retention is disabled."""
    return not retain_stateful_resources(scope)


@dataclass(frozen=True)
class DeploymentNamer:
    """Consistent environment-aware naming for stacks and physical resources."""

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
        return self.with_suffix(base, separator="_", suffix=self.runtime_suffix)
