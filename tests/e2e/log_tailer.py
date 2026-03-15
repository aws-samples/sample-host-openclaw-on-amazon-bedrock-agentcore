"""CloudWatch log tailing with pattern matching for E2E verification.

Uses a two-phase approach:
1. Filter by actor_id to find the Lambda invocation that handled the message
2. Extract the Lambda request ID, then fetch ALL logs from that invocation
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set

import boto3

from .config import E2EConfig

# Log patterns matching lambda/router/index.py log format strings
PATTERNS = {
    "message_received": re.compile(
        r"Telegram: user=(\S+) actor=(\S+) session=(\S+) text_len=(\d+) images=(\d+)"
    ),
    "agentcore_invoked": re.compile(
        r"Invoking AgentCore: arn=(\S+) qualifier=(\S+) session=(\S+)"
    ),
    "agentcore_response": re.compile(
        r"AgentCore response \(len=\d+, first \d+ chars\): (.+)", re.DOTALL
    ),
    "response_to_send": re.compile(
        r"Response to send \(len=(\d+)\): (.+)", re.DOTALL
    ),
    "telegram_sent": re.compile(
        r"Telegram response sent to chat_id=(\S+)"
    ),
    "new_session": re.compile(
        r"New session created: (\S+) for (\S+)"
    ),
    "new_user": re.compile(
        r"New user created: (\S+) for (\S+)"
    ),
}

# Lambda log line format: [LEVEL]\tTIMESTAMP\tREQUEST_ID\tMESSAGE
REQUEST_ID_PATTERN = re.compile(r"\t([0-9a-f-]{36})\t")


@dataclass
class TailResult:
    """Aggregated results from tailing CloudWatch logs."""
    message_received: bool = False
    agentcore_invoked: bool = False
    agentcore_response: str = ""
    response_text: str = ""
    response_len: int = 0
    telegram_sent: bool = False
    new_session: bool = False
    new_user: bool = False
    session_id: str = ""
    user_id: str = ""
    actor_id: str = ""
    request_id: str = ""
    raw_lines: List[str] = field(default_factory=list)
    timed_out: bool = False
    elapsed_s: float = 0.0

    @property
    def full_lifecycle(self) -> bool:
        """True if the complete message lifecycle was observed."""
        return (
            self.message_received
            and self.agentcore_invoked
            and self.telegram_sent
        )

    @property
    def is_warmup(self) -> bool:
        """True if response came from the lightweight agent warm-up shim.

        The shim appends a deterministic footer containing 'warm-up mode'.
        When OpenClaw is fully running, responses route through the WebSocket
        bridge and never contain this footer.
        """
        return "warm-up mode" in self.response_text.lower()


def _parse_line(line: str, result: TailResult) -> None:
    """Parse a single log line and update the result."""
    for name, pattern in PATTERNS.items():
        m = pattern.search(line)
        if not m:
            continue

        if name == "message_received":
            result.message_received = True
            result.user_id = m.group(1)
            result.actor_id = m.group(2)
            result.session_id = m.group(3)
        elif name == "agentcore_invoked":
            result.agentcore_invoked = True
        elif name == "agentcore_response":
            result.agentcore_response = m.group(1)
            # Pre-extraction fallback — only set if response_to_send
            # hasn't provided the post-extraction text yet
            if not result.response_text:
                try:
                    body = json.loads(m.group(1))
                    if isinstance(body, dict) and "response" in body:
                        result.response_text = body["response"]
                        result.response_len = len(result.response_text)
                except (json.JSONDecodeError, TypeError):
                    pass
        elif name == "response_to_send":
            result.response_len = int(m.group(1))
            # Always update — this is the post-extraction text that
            # actually gets sent to the user
            result.response_text = m.group(2)
        elif name == "telegram_sent":
            result.telegram_sent = True
        elif name == "new_session":
            result.new_session = True
            result.session_id = m.group(1)
        elif name == "new_user":
            result.new_user = True
            result.user_id = m.group(1)
        break


def _extract_request_id(log_message: str) -> Optional[str]:
    """Extract the Lambda request ID from a CloudWatch log line."""
    m = REQUEST_ID_PATTERN.search(log_message)
    return m.group(1) if m else None


def tail_logs(
    cfg: E2EConfig,
    *,
    since_ms: Optional[int] = None,
    timeout_s: int = 300,
    poll_interval_s: int = 5,
) -> TailResult:
    """Poll CloudWatch logs for the E2E user's message lifecycle.

    Two-phase approach:
    1. Poll for the actor_id to find the async Lambda invocation request ID
    2. Once found, poll for ALL logs from that request ID to capture the
       full lifecycle (AgentCore invoke, response, Telegram send)

    Args:
        cfg: E2E configuration
        since_ms: Start time in epoch milliseconds (default: now - 10s)
        timeout_s: Maximum time to poll (default: 300s for cold start)
        poll_interval_s: Seconds between polls

    Returns:
        TailResult with matched log patterns
    """
    logs_client = boto3.client("logs", region_name=cfg.region)
    result = TailResult()
    start = time.monotonic()

    if since_ms is None:
        since_ms = int((time.time() - 10) * 1000)

    actor_id = f"telegram:{cfg.telegram_user_id}"
    seen_event_ids: Set[str] = set()
    request_id: Optional[str] = None

    while (time.monotonic() - start) < timeout_s:
        try:
            if request_id:
                # Phase 2: fetch all logs from the async Lambda invocation
                resp = logs_client.filter_log_events(
                    logGroupName=cfg.log_group,
                    startTime=since_ms,
                    filterPattern=f'"{request_id}"',
                    limit=200,
                    interleaved=True,
                )
            else:
                # Phase 1: find the async invocation that has our actor_id
                resp = logs_client.filter_log_events(
                    logGroupName=cfg.log_group,
                    startTime=since_ms,
                    filterPattern=f'"{actor_id}"',
                    limit=100,
                    interleaved=True,
                )
        except Exception as e:
            err_msg = f"[log_tailer] CloudWatch error: {type(e).__name__}: {e}"
            if err_msg not in result.raw_lines:
                result.raw_lines.append(err_msg)
                print(err_msg, flush=True)
            time.sleep(poll_interval_s)
            continue

        for event in resp.get("events", []):
            event_id = event.get("eventId", "")
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)

            msg = event.get("message", "")
            result.raw_lines.append(msg)
            _parse_line(msg, result)

            # Phase 1 -> Phase 2 transition: extract request ID from the
            # "Telegram: user=... actor=..." line (the async dispatch)
            if not request_id and result.message_received:
                rid = _extract_request_id(msg)
                if rid:
                    request_id = rid
                    result.request_id = rid
                    # Reset seen_event_ids to re-fetch with new filter
                    seen_event_ids.clear()
                    break  # Break inner loop to switch to phase 2

        # Early exit if full lifecycle observed
        if result.full_lifecycle:
            result.elapsed_s = time.monotonic() - start
            return result

        time.sleep(poll_interval_s)

    result.timed_out = True
    result.elapsed_s = time.monotonic() - start
    return result
