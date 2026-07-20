"""Legacy E2E helpers cannot use CloudWatch as a private response oracle."""

from __future__ import annotations

import pytest

from . import log_tailer


def test_log_tailer_refuses_before_any_observation() -> None:
    with pytest.raises(log_tailer.UnsafeLogInspection, match="direct invocation"):
        log_tailer.tail_logs(object())


def test_log_tailer_source_contains_no_payload_or_identity_patterns() -> None:
    source = log_tailer.__file__
    text = __import__("pathlib").Path(source).read_text(encoding="utf-8")
    for forbidden in (
        "Response to send",
        "AgentCore response",
        "Telegram: user=",
        "actor_id =",
        "response_text",
        "raw_lines",
        "boto3",
        "filter_log_events",
    ):
        assert forbidden not in text
