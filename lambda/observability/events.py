"""Closed operational event schema for private-pilot evidence.

Only finite, release-owned metadata dimensions cross this boundary. There is
deliberately no escape hatch for free text, identifiers, provider/source data,
addresses, content, URLs, tokens, or credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, ClassVar


MAX_EVENT_COUNT = 1_000_000

ALLOWED_ENVIRONMENTS = frozenset({"preproduction", "synthetic"})
ALLOWED_COMPONENTS = frozenset(
    {
        "action_kernel",
        "capability_gateway",
        "cards",
        "compute",
        "connector",
        "control",
        "feedback",
        "gmail",
        "maintenance",
        "oauth",
        "portable",
        "queue",
        "scan",
        "scheduler",
        "workspace",
    }
)
ALLOWED_OPERATIONS = frozenset(
    {
        "capability",
        "card",
        "compute",
        "compute_isolation",
        "connect",
        "connector_drift",
        "deletion",
        "draft",
        "export",
        "feedback",
        "import",
        "invite",
        "maintenance_heartbeat",
        "queue",
        "scan",
        "schedule",
        "uncertain_effect",
        "workspace",
    }
)
ALLOWED_OUTCOMES = frozenset(
    {
        "aged",
        "denied",
        "disabled",
        "drifted",
        "failed",
        "inert",
        "pending",
        "replay_denied",
        "succeeded",
        "uncertain",
    }
)


class OperationalEventValidationError(ValueError):
    """An event attempted to cross the closed metadata boundary."""


def _allowlisted_token(value: object, allowlist: frozenset[str]) -> str:
    if type(value) is not str or value not in allowlist:
        raise OperationalEventValidationError(
            "operational event value is not in the release-owned allowlist"
        )
    return value


def _bounded_count(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_EVENT_COUNT:
        raise OperationalEventValidationError(
            "operational event count must be a bounded positive integer"
        )
    return value


@dataclass(frozen=True, slots=True, repr=False)
class OperationalEventV1:
    """One validated metadata-only event or aggregate event count."""

    SCHEMA: ClassVar[str] = "personal-operator.operational-event.v1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"schema", "environment", "component", "operation", "outcome", "count"}
    )

    environment: str
    component: str
    operation: str
    outcome: str
    count: int

    def __post_init__(self) -> None:
        _allowlisted_token(self.environment, ALLOWED_ENVIRONMENTS)
        _allowlisted_token(self.component, ALLOWED_COMPONENTS)
        _allowlisted_token(self.operation, ALLOWED_OPERATIONS)
        _allowlisted_token(self.outcome, ALLOWED_OUTCOMES)
        _bounded_count(self.count)

    @classmethod
    def from_mapping(cls, value: object) -> "OperationalEventV1":
        if not isinstance(value, Mapping) or set(value) != cls.FIELDS:
            raise OperationalEventValidationError(
                "operational event must contain the exact fields"
            )
        if value.get("schema") != cls.SCHEMA:
            raise OperationalEventValidationError(
                "operational event schema is not supported"
            )
        return cls(
            environment=_allowlisted_token(
                value.get("environment"), ALLOWED_ENVIRONMENTS
            ),
            component=_allowlisted_token(value.get("component"), ALLOWED_COMPONENTS),
            operation=_allowlisted_token(value.get("operation"), ALLOWED_OPERATIONS),
            outcome=_allowlisted_token(value.get("outcome"), ALLOWED_OUTCOMES),
            count=_bounded_count(value.get("count")),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "environment": self.environment,
            "component": self.component,
            "operation": self.operation,
            "outcome": self.outcome,
            "count": self.count,
        }

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_mapping(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


__all__ = [
    "ALLOWED_COMPONENTS",
    "ALLOWED_ENVIRONMENTS",
    "ALLOWED_OPERATIONS",
    "ALLOWED_OUTCOMES",
    "MAX_EVENT_COUNT",
    "OperationalEventV1",
    "OperationalEventValidationError",
]
