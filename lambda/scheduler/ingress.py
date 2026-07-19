"""EventBridge Scheduler target Lambda entry for the trusted scheduler.

The composition root creates no client at import time and holds no effect
authority of any kind. It parses the opaque fire payload (rejecting any user
content or extra keys) and delegates to ``SchedulerService.fire``.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

try:  # package import
    from scheduler.models import SchedulePayloadV1
    from scheduler.service import SchedulerOutcome, SchedulerService
except ImportError:  # direct Lambda asset / focused tests
    from models import SchedulePayloadV1  # type: ignore[no-redef]
    from service import SchedulerOutcome, SchedulerService  # type: ignore[no-redef]


REQUIRED_REGION = "eu-west-1"

_service_factory: Callable[[], SchedulerService] | None = None
_production_service: SchedulerService | None = None


def handle_fire(event: Any, service: SchedulerService) -> SchedulerOutcome:
    """Parse the opaque payload and delegate to the trusted fire path."""

    if isinstance(event, str):
        payload = SchedulePayloadV1.from_json(event)
    else:
        payload = SchedulePayloadV1.from_mapping(event)
    return service.fire(payload)


def configure_service_factory(
    factory: Callable[[], SchedulerService] | None,
) -> None:
    """Install (or clear) the deployment composition root."""

    global _service_factory, _production_service
    if factory is not None and not callable(factory):
        raise TypeError("scheduler service factory must be callable")
    _service_factory = factory
    _production_service = None


def build_scheduler_service(
    *, env: Mapping[str, str] = os.environ
) -> SchedulerService:
    """Create the exact-region trusted service only inside the deployed Lambda.

    No effect-plane client is ever constructed here. This function fails closed
    on region drift before creating any AWS resource.
    """

    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if region != REQUIRED_REGION:
        raise RuntimeError("scheduler ingress requires exact eu-west-1 region")
    # The concrete DynamoDB/EventBridge/SQS adapters are wired by the deployment
    # composition, which is intentionally out of this trusted module's import
    # surface. Deployments install them through configure_service_factory.
    raise RuntimeError("scheduler ingress composition must be installed by the factory")


def lambda_handler(event: Any, _context: Any) -> dict[str, str]:
    global _production_service
    if _service_factory is not None:
        service = _service_factory()
    else:
        if _production_service is None:
            _production_service = build_scheduler_service()
        service = _production_service
    outcome = handle_fire(event, service)
    return {"status": outcome.status}


__all__ = [
    "REQUIRED_REGION",
    "build_scheduler_service",
    "configure_service_factory",
    "handle_fire",
    "lambda_handler",
]
