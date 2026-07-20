"""Tests for deterministic product commands kept outside the agent runtime."""

from __future__ import annotations

import sys
from pathlib import Path


ROUTER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROUTER_DIR))

from product_commands import (  # noqa: E402
    COMMAND_NAMES,
    DeterministicProductCommandHandler,
    parse_product_command,
)


def test_all_product_commands_are_recognized_and_canonicalized():
    assert COMMAND_NAMES == (
        "/start",
        "/connect",
        "/scan",
        "/tasks",
        "/workspace",
        "/status",
        "/delete",
    )

    for name in COMMAND_NAMES:
        parsed = parse_product_command(name.upper())
        assert parsed is not None
        assert parsed.name == name


def test_telegram_bot_suffix_is_removed_without_accepting_arguments():
    assert parse_product_command("/status@Personal_Operator_Bot").name == "/status"
    assert parse_product_command("/status now") is None
    assert parse_product_command("/unknown") is None
    assert parse_product_command("hello") is None


def test_default_command_handler_is_deterministic_and_context_free():
    handler = DeterministicProductCommandHandler()

    for name in COMMAND_NAMES:
        command = parse_product_command(name)
        first = handler.handle(
            user_id="user_one",
            command=command,
            channel="telegram",
            trace_id="trace_a",
            idempotency_key="event_a",
        )
        second = handler.handle(
            user_id="user_two",
            command=command,
            channel="telegram",
            trace_id="trace_b",
            idempotency_key="event_b",
        )
        assert first == second
        assert isinstance(first, str)
        assert first.strip()


def test_default_responses_do_not_claim_unimplemented_external_effects():
    handler = DeterministicProductCommandHandler()

    scan = handler.handle(
        user_id="user_one",
        command=parse_product_command("/scan"),
        channel="telegram",
        trace_id="trace",
        idempotency_key="event",
    )
    delete = handler.handle(
        user_id="user_one",
        command=parse_product_command("/delete"),
        channel="telegram",
        trace_id="trace",
        idempotency_key="event",
    )

    assert "connect" in scan.lower()
    assert "not deleted" in delete.lower()

