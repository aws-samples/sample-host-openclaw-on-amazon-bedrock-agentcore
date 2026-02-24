"""E2E Bot Testing CLI and pytest test cases.

CLI usage:
    python -m tests.e2e.bot_test --health
    python -m tests.e2e.bot_test --send "Hello" --tail-logs
    python -m tests.e2e.bot_test --reset --send "Hello" --tail-logs
    python -m tests.e2e.bot_test --reset-user
    python -m tests.e2e.bot_test --conversation multi_turn --tail-logs

pytest usage:
    pytest tests/e2e/bot_test.py -v -k smoke
    pytest tests/e2e/bot_test.py -v -k cold_start
    pytest tests/e2e/bot_test.py -v -k conversation
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from tests.e2e.config import load_config
from tests.e2e.log_tailer import tail_logs
from tests.e2e.session import reset_session, reset_user
from tests.e2e.webhook import send_conversation, send_webhook

try:
    import pytest
except ImportError:
    # CLI-only mode: provide a stub so test class definitions don't fail
    class _StubMark:
        def __getattr__(self, _):
            return lambda fn: fn

    class _StubPytest:
        mark = _StubMark()
        def fail(self, msg):
            raise AssertionError(msg)

    pytest = _StubPytest()  # type: ignore[assignment]


# --- Conversation scenarios (shared between CLI and pytest) ---

SCENARIOS = {
    "greeting": [
        "Hey! How are you doing today?",
    ],
    "multi_turn": [
        "Hi there! I'm testing out this bot.",
        "What kinds of things can you help me with?",
        "That sounds great. Can you write me a short haiku about coding?",
    ],
    "task_request": [
        "Can you help me brainstorm 3 creative names for a pet cat?",
    ],
    "rapid_fire": [
        "First question: what's 2+2?",
        "Second question: what color is the sky?",
    ],
}


# ---------------------------------------------------------------------------
# pytest test classes
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """API Gateway reachability."""

    def test_health_endpoint(self, e2e_config):
        """GET /health returns 200 with expected JSON body."""
        req = urllib_request.Request(e2e_config.health_url)
        resp = urllib_request.urlopen(req, timeout=10)
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert body["status"] == "ok"
        assert body["service"] == "openclaw-router"


class TestWebhookValidation:
    """Webhook secret validation."""

    def test_missing_secret_returns_401(self, e2e_config):
        """POST without secret header is rejected."""
        result = send_webhook(e2e_config, "test", include_secret=False)
        assert result.status_code == 401

    def test_wrong_secret_returns_401(self, e2e_config):
        """POST with wrong secret header is rejected."""
        result = send_webhook(e2e_config, "test", wrong_secret=True)
        assert result.status_code == 401


class TestSmokeTest:
    """Single message lifecycle verification."""

    @pytest.mark.smoke
    def test_send_message_lifecycle(self, e2e_config, tail):
        """Send a natural message and verify the full lifecycle via logs."""
        start_time = int(time.time() * 1000)
        result = send_webhook(e2e_config, "Hey, what can you help me with?")
        assert result.status_code == 200

        tail_result = tail(start_time=start_time, timeout=300)
        assert tail_result.success, (
            f"Message lifecycle did not complete within timeout. "
            f"Timed out: {tail_result.timed_out}, "
            f"Response sent: {tail_result.response_sent}, "
            f"AgentCore invoked: {tail_result.agentcore_invoked}"
        )
        assert tail_result.agentcore_invoked
        assert tail_result.response_len > 0


class TestColdStart:
    """Session reset + cold start verification."""

    @pytest.mark.cold_start
    def test_cold_start_creates_new_session(self, e2e_config, tail):
        """Reset session, send message, verify new session is created."""
        reset_result = reset_session(e2e_config)
        # User might not exist yet on first run, that's OK
        if reset_result.error and "not found" not in reset_result.error.lower():
            pytest.fail(f"Session reset failed: {reset_result.error}")

        start_time = int(time.time() * 1000)
        result = send_webhook(e2e_config, "Hi! I'm back, testing cold start.")
        assert result.status_code == 200

        tail_result = tail(start_time=start_time, timeout=300)
        assert tail_result.success, (
            f"Cold start lifecycle did not complete. "
            f"Elapsed: {tail_result.elapsed_seconds:.1f}s, "
            f"Timed out: {tail_result.timed_out}"
        )
        assert tail_result.new_session or tail_result.new_user, (
            "Expected new session or new user after reset"
        )


class TestConversation:
    """Multi-turn conversation tests."""

    @pytest.mark.conversation
    def test_multi_turn_conversation(self, e2e_config):
        """Send a multi-turn conversation and verify session continuity."""
        messages = SCENARIOS["multi_turn"]
        results = send_conversation(
            e2e_config,
            messages,
            delay_between=5.0,
            tail_fn=tail_logs,
            tail_timeout=300,
        )

        session_ids = set()
        for i, (webhook_result, tail_result) in enumerate(results):
            assert webhook_result.status_code == 200, (
                f"Message {i + 1} webhook failed: {webhook_result.status_code}"
            )
            assert tail_result is not None, f"Message {i + 1} had no tail result"
            assert tail_result.success, (
                f"Message {i + 1} lifecycle did not complete. "
                f"Elapsed: {tail_result.elapsed_seconds:.1f}s"
            )
            assert tail_result.response_len > 10, (
                f"Message {i + 1} got very short response: len={tail_result.response_len}"
            )
            if tail_result.session_id:
                session_ids.add(tail_result.session_id)

        # All turns should use the same session (continuity)
        if len(session_ids) > 1:
            # First message might create a new session; subsequent should reuse
            # Allow at most 1 session (or 2 if first was new user)
            assert len(session_ids) <= 2, (
                f"Expected session continuity but found {len(session_ids)} "
                f"different sessions: {session_ids}"
            )


class TestRapidMessages:
    """Rapid-fire message handling."""

    @pytest.mark.rapid
    def test_rapid_fire_messages(self, e2e_config):
        """Send messages in quick succession, verify all are processed."""
        messages = SCENARIOS["rapid_fire"]
        results = send_conversation(
            e2e_config,
            messages,
            delay_between=1.0,  # Quick succession
            tail_fn=tail_logs,
            tail_timeout=300,
        )

        for i, (webhook_result, tail_result) in enumerate(results):
            assert webhook_result.status_code == 200, (
                f"Rapid message {i + 1} webhook failed: {webhook_result.status_code}"
            )
            assert tail_result is not None, f"Rapid message {i + 1} had no tail result"
            assert tail_result.success, (
                f"Rapid message {i + 1} lifecycle did not complete. "
                f"Elapsed: {tail_result.elapsed_seconds:.1f}s"
            )


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _print_tail_result(tail_result):
    """Print a formatted TailResult summary."""
    status = "PASS" if tail_result.success else "FAIL"
    print(f"\n{'=' * 60}")
    print(f"  Status: {status}")
    print(f"  Elapsed: {tail_result.elapsed_seconds:.1f}s")
    print(f"  User: {tail_result.user_id}")
    print(f"  Session: {tail_result.session_id}")
    print(f"  AgentCore invoked: {tail_result.agentcore_invoked}")
    print(f"  New session: {tail_result.new_session}")
    print(f"  New user: {tail_result.new_user}")
    print(f"  Response length: {tail_result.response_len}")
    if tail_result.response_text:
        print(f"  Response preview: {tail_result.response_text[:200]}")
    if tail_result.timed_out:
        print(f"  TIMED OUT after {tail_result.elapsed_seconds:.1f}s")
    print(f"{'=' * 60}")


def cli_health(config):
    """Check API Gateway health endpoint."""
    try:
        req = urllib_request.Request(config.health_url)
        resp = urllib_request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode("utf-8"))
        print(f"Health check: {resp.status} - {body}")
        return resp.status == 200
    except (HTTPError, URLError) as e:
        print(f"Health check FAILED: {e}")
        return False


def cli_send(config, text, *, do_tail=False, timeout=300):
    """Send a single webhook message."""
    print(f"Sending: {text!r}")
    start_time = int(time.time() * 1000)
    result = send_webhook(config, text)
    print(f"Webhook response: {result.status_code} ({result.elapsed_ms:.0f}ms)")

    if result.status_code != 200:
        print(f"Body: {result.body}")
        return False

    if do_tail:
        print(f"Tailing CloudWatch logs (timeout={timeout}s)...")
        tail_result = tail_logs(config, start_time=start_time, timeout=timeout)
        _print_tail_result(tail_result)
        return tail_result.success

    print("Message sent (use --tail-logs to verify lifecycle)")
    return True


def cli_conversation(config, scenario_name, *, do_tail=False, timeout=300):
    """Run a conversation scenario."""
    if scenario_name not in SCENARIOS:
        print(f"Unknown scenario: {scenario_name!r}")
        print(f"Available: {', '.join(SCENARIOS.keys())}")
        return False

    messages = SCENARIOS[scenario_name]
    print(f"Running conversation scenario: {scenario_name} ({len(messages)} messages)")

    tail_fn = tail_logs if do_tail else None
    results = send_conversation(
        config,
        messages,
        delay_between=5.0 if scenario_name != "rapid_fire" else 1.0,
        tail_fn=tail_fn,
        tail_timeout=timeout,
    )

    all_ok = True
    for i, (webhook_result, tail_result) in enumerate(results):
        print(f"\n--- Message {i + 1}/{len(messages)}: {messages[i]!r} ---")
        print(f"Webhook: {webhook_result.status_code} ({webhook_result.elapsed_ms:.0f}ms)")

        if webhook_result.status_code != 200:
            print(f"FAILED: {webhook_result.body}")
            all_ok = False
            continue

        if tail_result:
            _print_tail_result(tail_result)
            if not tail_result.success:
                all_ok = False

    print(f"\n{'PASS' if all_ok else 'FAIL'}: Conversation {scenario_name}")
    return all_ok


def cli_reset_session(config):
    """Reset the user's session."""
    result = reset_session(config)
    if result.error:
        print(f"Session reset: {result.error}")
    else:
        print(f"Session reset: deleted session for user {result.user_id}")
    return not result.error


def cli_reset_user(config):
    """Fully reset the user."""
    result = reset_user(config)
    print(f"User reset: deleted {result.items_deleted} items for user {result.user_id or 'unknown'}")
    if result.error:
        print(f"Errors: {result.error}")
    return not result.error


def main():
    parser = argparse.ArgumentParser(
        description="E2E Bot Testing CLI — simulate Telegram webhooks and verify via CloudWatch logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --health
  %(prog)s --send "Hello" --tail-logs
  %(prog)s --reset --send "Hello" --tail-logs
  %(prog)s --reset-user
  %(prog)s --conversation multi_turn --tail-logs
""",
    )
    parser.add_argument("--send", metavar="TEXT", help="Send a webhook message")
    parser.add_argument(
        "--conversation",
        metavar="SCENARIO",
        choices=list(SCENARIOS.keys()),
        help=f"Run a conversation scenario: {', '.join(SCENARIOS.keys())}",
    )
    parser.add_argument("--reset", "--reset-session", action="store_true",
                        help="Reset session before sending (forces cold start)")
    parser.add_argument("--reset-user", action="store_true",
                        help="Fully reset user (deletes all DynamoDB items)")
    parser.add_argument("--health", action="store_true", help="Check API Gateway health")
    parser.add_argument("--tail-logs", action="store_true",
                        help="Tail CloudWatch logs after sending to verify response")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Log tail timeout in seconds (default: 300)")
    parser.add_argument("--chat-id", help="Telegram chat ID (overrides E2E_TELEGRAM_CHAT_ID)")
    parser.add_argument("--user-id", help="Telegram user ID (overrides E2E_TELEGRAM_USER_ID)")
    parser.add_argument("--region", help="AWS region override")

    args = parser.parse_args()

    # At least one action required
    if not any([args.health, args.send, args.conversation, args.reset, args.reset_user]):
        parser.print_help()
        sys.exit(1)

    # Load config (health-only doesn't need Telegram IDs)
    try:
        if args.health and not args.send and not args.conversation and not args.reset and not args.reset_user:
            from tests.e2e.config import load_health_config
            config = load_health_config(region=args.region)
        else:
            config = load_config(
                region=args.region,
                chat_id=args.chat_id,
                user_id=args.user_id,
            )
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    ok = True

    if args.health:
        ok = cli_health(config) and ok

    if args.reset_user:
        ok = cli_reset_user(config) and ok

    if args.reset and not args.reset_user:
        ok = cli_reset_session(config) and ok

    if args.send:
        ok = cli_send(config, args.send, do_tail=args.tail_logs, timeout=args.timeout) and ok

    if args.conversation:
        ok = cli_conversation(config, args.conversation, do_tail=args.tail_logs,
                              timeout=args.timeout) and ok

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
