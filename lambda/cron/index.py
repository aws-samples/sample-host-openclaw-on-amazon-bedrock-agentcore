"""Fail-closed tombstone for the inherited direct cron executor.

Personal Operator v0 does not trust user-authored EventBridge payloads. The
direct path remains disabled until Task 4 introduces the trusted FIFO scheduler.
"""

import logging
import os


logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQUIRED_REGION = "eu-west-1"
DIRECT_CRON_DISABLED = True


def _require_region():
    """Reject an explicit non-canonical region before any AWS access."""
    for variable in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        configured = os.environ.get(variable)
        if configured and configured != REQUIRED_REGION:
            raise RuntimeError(
                f"{variable} must be exactly {REQUIRED_REGION}; got {configured}"
            )
    return REQUIRED_REGION


AWS_REGION = _require_region()


def handler(event, context):
    """Reject every legacy direct cron invocation without reading its payload."""
    del event, context
    logger.warning("Rejected inherited direct cron invocation")
    return {
        "statusCode": 410,
        "code": "DIRECT_CRON_DISABLED",
        "body": "Direct cron execution is disabled pending the trusted FIFO scheduler",
    }
