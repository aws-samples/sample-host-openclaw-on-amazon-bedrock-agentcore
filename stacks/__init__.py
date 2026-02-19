"""OpenClaw CDK stacks package."""

from aws_cdk import aws_logs as logs

# Region prefix mapping for Bedrock cross-region inference profiles.
_REGION_PREFIX_MAP = {
    "us-east-1": "us", "us-east-2": "us", "us-west-2": "us",
    "eu-west-1": "eu", "eu-west-2": "eu", "eu-west-3": "eu",
    "eu-central-1": "eu", "eu-north-1": "eu",
    "ap-southeast-1": "ap", "ap-southeast-2": "ap",
    "ap-northeast-1": "ap", "ap-northeast-2": "ap",
    "ap-south-1": "ap",
    "ap-southeast-4": "au", "ap-southeast-5": "ap",
    "ca-central-1": "us", "sa-east-1": "us",
}


def cross_region_model_id(region: str, base_model_id: str) -> str:
    """Prepend the cross-region inference prefix for the given AWS region.

    Example: cross_region_model_id("ap-southeast-2", "anthropic.claude-sonnet-4-6")
             -> "ap.anthropic.claude-sonnet-4-6"
    """
    prefix = _REGION_PREFIX_MAP.get(region)
    if not prefix:
        raise ValueError(
            f"No Bedrock cross-region inference prefix mapped for region '{region}'. "
            f"Known regions: {', '.join(sorted(_REGION_PREFIX_MAP))}"
        )
    return f"{prefix}.{base_model_id}"

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
