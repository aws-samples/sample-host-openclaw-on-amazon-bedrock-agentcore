"""Disabled legacy cron stack.

The direct EventBridge Scheduler path is intentionally absent until the
trusted FIFO scheduler in Runtime Hardening Task 4 is implemented.
"""

from aws_cdk import CfnOutput, Stack
from constructs import Construct


REQUIRED_REGION = "eu-west-1"
DIRECT_CRON_DISABLED = True


class CronStack(Stack):
    """Compatibility tombstone that creates no Lambda, role, or schedule."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        region = Stack.of(self).region
        if region != REQUIRED_REGION:
            raise ValueError(
                f"CronStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        CfnOutput(self, "DirectCronStatus", value="DIRECT_CRON_DISABLED")
