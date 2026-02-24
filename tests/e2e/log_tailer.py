"""CloudWatch log tailing with pattern matching for E2E verification.

Polls filter_log_events on /openclaw/lambda/router starting from the test
timestamp. Matches verified log patterns from lambda/router/index.py to
confirm the full message lifecycle completed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from tests.e2e.config import E2EConfig

logger = logging.getLogger(__name__)

# --- Log patterns from lambda/router/index.py ---
# Each pattern captures specific fields from the router Lambda logs.

# Line 737-739: "Telegram: user=%s actor=%s session=%s text_len=%d images=%d"
RE_TELEGRAM_LOG = re.compile(
    r"Telegram: user=(\S+) actor=(\S+) session=(\S+) text_len=(\d+) images=(\d+)"
)

# Line 371: "Invoking AgentCore: arn=%s qualifier=%s session=%s"
RE_AGENTCORE_INVOKE = re.compile(
    r"Invoking AgentCore: arn=\S+ qualifier=\S+ session=(\S+)"
)

# Line 388: "AgentCore response body (first 500 chars): %s"
RE_AGENTCORE_RESPONSE = re.compile(
    r"AgentCore response body \(first 500 chars\): (.+)"
)

# Line 748: "Response to send (len=%d): %s" (response_text is first 200 chars)
RE_RESPONSE_TO_SEND = re.compile(
    r"Response to send \(len=(\d+)\): (.+)"
)

# Line 756: "Telegram response sent to chat_id=%s"
RE_RESPONSE_SENT = re.compile(
    r"Telegram response sent to chat_id=(\S+)"
)

# Line 276: "New session created: %s for %s"
RE_NEW_SESSION = re.compile(
    r"New session created: (\S+) for (\S+)"
)

# Line 236: "New user created: %s for %s"
RE_NEW_USER = re.compile(
    r"New user created: (\S+) for (\S+)"
)


@dataclass
class TailResult:
    """Parsed result from tailing CloudWatch logs."""

    user_id: str = ""
    actor_id: str = ""
    session_id: str = ""
    text_len: int = 0
    image_count: int = 0
    agentcore_invoked: bool = False
    response_preview: str = ""
    response_len: int = 0
    response_text: str = ""  # First 200 chars (truncated by Lambda)
    response_sent: bool = False
    response_chat_id: str = ""
    new_session: bool = False
    new_user: bool = False
    raw_lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    elapsed_seconds: float = 0.0

    @property
    def success(self) -> bool:
        """Whether the full message lifecycle completed."""
        return self.response_sent and not self.timed_out


def tail_logs(
    config: E2EConfig,
    *,
    start_time: int | None = None,
    chat_id: str | None = None,
    timeout: int = 300,
    poll_interval: float = 5.0,
) -> TailResult:
    """Poll CloudWatch logs for the message lifecycle completion.

    Args:
        config: E2E configuration.
        start_time: Epoch milliseconds to start filtering from.
                    Defaults to current time minus 5 seconds.
        chat_id: Chat ID to match in completion marker.
                 Defaults to config.chat_id.
        timeout: Maximum seconds to wait for completion.
        poll_interval: Seconds between polls.

    Returns:
        TailResult with all parsed fields.
    """
    if start_time is None:
        start_time = int((time.time() - 5) * 1000)
    if chat_id is None:
        chat_id = config.chat_id

    logs_client = boto3.client("logs", region_name=config.region)
    result = TailResult()
    seen_event_ids: set[str] = set()
    start_wall = time.monotonic()

    while (time.monotonic() - start_wall) < timeout:
        try:
            kwargs = {
                "logGroupName": config.log_group,
                "startTime": start_time,
                "interleaved": True,
            }
            # Paginate through all available log events
            while True:
                resp = logs_client.filter_log_events(**kwargs)

                for event in resp.get("events", []):
                    event_id = event.get("eventId", "")
                    if event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)

                    message = event.get("message", "")
                    result.raw_lines.append(message)
                    _parse_line(message, result)

                next_token = resp.get("nextToken")
                if not next_token:
                    break
                kwargs["nextToken"] = next_token

        except Exception as e:
            logger.warning("CloudWatch poll error (will retry): %s", e)
            time.sleep(poll_interval)
            continue

        # Check completion: response sent to our chat
        if result.response_sent and result.response_chat_id == chat_id:
            result.elapsed_seconds = time.monotonic() - start_wall
            return result

        time.sleep(poll_interval)

    result.timed_out = True
    result.elapsed_seconds = time.monotonic() - start_wall
    return result


def _parse_line(line: str, result: TailResult) -> None:
    """Parse a single log line and update the TailResult.

    Each log line matches at most one pattern. The early returns are
    intentional — a single CloudWatch log line contains only one log message.
    """
    m = RE_TELEGRAM_LOG.search(line)
    if m:
        result.user_id = m.group(1)
        result.actor_id = m.group(2)
        result.session_id = m.group(3)
        result.text_len = int(m.group(4))
        result.image_count = int(m.group(5))
        return

    m = RE_AGENTCORE_INVOKE.search(line)
    if m:
        result.agentcore_invoked = True
        if not result.session_id:
            result.session_id = m.group(1)
        return

    m = RE_AGENTCORE_RESPONSE.search(line)
    if m:
        result.response_preview = m.group(1)
        return

    m = RE_RESPONSE_TO_SEND.search(line)
    if m:
        result.response_len = int(m.group(1))
        result.response_text = m.group(2)
        return

    m = RE_RESPONSE_SENT.search(line)
    if m:
        result.response_sent = True
        result.response_chat_id = m.group(1)
        return

    m = RE_NEW_SESSION.search(line)
    if m:
        result.new_session = True
        return

    m = RE_NEW_USER.search(line)
    if m:
        result.new_user = True
        return
