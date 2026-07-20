"""Deterministic consumer commands that never enter the agent runtime.

This module intentionally contains no network or provider client. Later product
workflows can replace ``DeterministicProductCommandHandler`` through the
worker's injected command-handler interface while preserving the command
boundary defined here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


COMMAND_NAMES = (
    "/start",
    "/connect",
    "/scan",
    "/tasks",
    "/workspace",
    "/status",
    "/delete",
)

_COMMAND_PATTERN = re.compile(
    r"^/(?P<name>[A-Za-z]+)(?:@(?P<bot>[A-Za-z][A-Za-z0-9_]{2,31}))?$"
)


@dataclass(frozen=True, slots=True)
class ProductCommand:
    """A canonical, argument-free command selected by the trusted router."""

    name: str

    def __post_init__(self) -> None:
        if self.name not in COMMAND_NAMES:
            raise ValueError("unsupported product command")


def parse_product_command(text: object) -> ProductCommand | None:
    """Return a canonical known command, or ``None`` for all other input.

    Telegram's optional ``@bot_username`` suffix is accepted. Arguments are
    deliberately rejected so a command's meaning cannot depend on untrusted
    free-form text accidentally interpreted by the control plane.
    """

    if not isinstance(text, str) or not text or len(text) > 64:
        return None
    match = _COMMAND_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    canonical = f"/{match.group('name').lower()}"
    if canonical not in COMMAND_NAMES:
        return None
    return ProductCommand(canonical)


class DeterministicProductCommandHandler:
    """Safe placeholder behavior until workflow-specific handlers are wired."""

    _RESPONSES = {
        "/start": (
            "Personal Operator is ready. Send me a request, or use /connect, "
            "/scan, /tasks, /workspace, /status, or /delete."
        ),
        "/connect": (
            "Connections open in the secure Personal Operator web surface. "
            "Provider credentials never enter your AI workspace."
        ),
        "/scan": "Connect Gmail first with /connect, then /scan can find follow-ups.",
        "/tasks": "No governed tasks are waiting right now.",
        "/workspace": "Your private workspace is managed separately from connected-app credentials.",
        "/status": "Personal Operator accepted your request and the trusted worker is available.",
        "/delete": (
            "Your account was not deleted. Account deletion requires confirmation in the secure web surface."
        ),
    }

    def handle(
        self,
        *,
        user_id: str,
        command: ProductCommand,
        channel: str,
        trace_id: str,
        idempotency_key: str,
    ) -> str:
        """Render a context-independent response for one canonical command."""

        del user_id, channel, trace_id, idempotency_key
        if not isinstance(command, ProductCommand):
            raise TypeError("command must be a ProductCommand")
        return self._RESPONSES[command.name]
