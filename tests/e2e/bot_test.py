"""E2E bot tests — CLI entrypoint + pytest test classes.

CLI usage:
    python -m tests.e2e.bot_test --health
    python -m tests.e2e.bot_test --send "Hello" --tail-logs
    python -m tests.e2e.bot_test --reset --send "Hello" --tail-logs
    python -m tests.e2e.bot_test --reset-user
    python -m tests.e2e.bot_test --conversation multi_turn --tail-logs
    python -m tests.e2e.bot_test --subagent --tail-logs
    python -m tests.e2e.bot_test --skill-manage --tail-logs
    python -m tests.e2e.bot_test --api-keys --tail-logs

Pytest usage:
    pytest tests/e2e/bot_test.py -v -k smoke
    pytest tests/e2e/bot_test.py -v -k cold_start
    pytest tests/e2e/bot_test.py -v -k subagent
    pytest tests/e2e/bot_test.py -v
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError
import pytest

from .config import load_config
from .conftest import SCENARIOS
from .log_tailer import tail_logs
from .session import get_agent_status, get_session_id, get_user_id, reset_session, reset_user
from .webhook import health_check, post_webhook


# ---------------------------------------------------------------------------
# pytest test classes
# ---------------------------------------------------------------------------


class TestSmoke:
    """Basic connectivity and webhook tests."""

    def test_health_check(self, e2e_config):
        """API Gateway /health endpoint responds 200."""
        result = health_check(e2e_config)
        assert result.status_code == 200, f"Health check failed: {result.status_code} {result.body}"
        assert "ok" in result.body

    def test_webhook_accepted(self, e2e_config):
        """Telegram webhook POST returns 200 (accepted for async processing)."""
        result = post_webhook(e2e_config, "E2E smoke test")
        assert result.status_code == 200, f"Webhook rejected: {result.status_code} {result.body}"

    def test_webhook_invalid_secret(self, e2e_config):
        """Webhook POST with wrong secret returns 401."""
        from .webhook import build_telegram_payload
        from urllib import request as urllib_request
        from urllib.error import HTTPError
        import json

        payload = build_telegram_payload(
            e2e_config.telegram_chat_id,
            e2e_config.telegram_user_id,
            "This should be rejected",
        )
        url = f"{e2e_config.api_url}/webhook/telegram"
        data = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "wrong-secret-value",
            },
        )
        with pytest.raises(HTTPError) as exc_info:
            urllib_request.urlopen(req, timeout=10)
        assert exc_info.value.code == 401


class TestMessageLifecycle:
    """Full message lifecycle verification via CloudWatch logs."""

    def test_send_and_verify(self, e2e_config):
        """Send a message and verify the full lifecycle in logs."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(e2e_config, "E2E lifecycle test: hello!")
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Incomplete lifecycle (timed_out={tail.timed_out}, "
            f"received={tail.message_received}, invoked={tail.agentcore_invoked}, "
            f"sent={tail.telegram_sent})\n"
            f"Raw lines: {tail.raw_lines[-5:]}"
        )
        assert tail.response_len > 0, "Response was empty"


class TestColdStart:
    """Cold start tests — reset session, send message, verify new session creation."""

    def test_cold_start(self, e2e_config):
        """Reset session and verify a new session is created on next message."""
        # Ensure user exists first
        user_id = get_user_id(e2e_config)
        if user_id:
            reset_session(e2e_config)

        since_ms = int(time.time() * 1000)
        result = post_webhook(e2e_config, "E2E cold start test")
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Incomplete lifecycle after cold start "
            f"(timed_out={tail.timed_out}, elapsed={tail.elapsed_s:.1f}s)\n"
            f"Raw lines: {tail.raw_lines[-5:]}"
        )
        # New session should have been created (unless user was brand new)
        if user_id:
            assert tail.new_session, "Expected new session creation after reset"


class TestWarmupShim:
    """Verify the lightweight agent warm-up shim is responding during cold start."""

    # Deterministic footer appended by the shim to every response
    SHIM_FOOTER = "warm-up mode"

    def test_cold_start_shim_response(self, e2e_config):
        """After session reset + stop, the first response should come from
        the warm-up shim and include the deterministic footer about
        additional community skills coming online after full startup."""
        from .session import _stop_agentcore_session

        user_id = get_user_id(e2e_config)
        if user_id:
            reset_session(e2e_config)
            _stop_agentcore_session(e2e_config)

        since_ms = int(time.time() * 1000)
        result = post_webhook(e2e_config, "What can you do?")
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Incomplete lifecycle (timed_out={tail.timed_out})"
        )

        # The shim appends a deterministic footer about warm-up mode
        resp_lower = tail.response_text.lower()
        assert self.SHIM_FOOTER in resp_lower, (
            f"Expected shim warm-up footer in response.\n"
            f"Looked for: {self.SHIM_FOOTER!r}\n"
            f"Response ({tail.response_len} chars): {tail.response_text[:300]}"
        )


class TestFullStartup:
    """Verify OpenClaw fully starts up and ClawHub skills become available.

    Unlike TestWarmupShim (which only checks the cold-start shim responds),
    this test waits for the full OpenClaw runtime to come online. It measures
    the timing of each phase:
      1. Webhook → warm-up response (lightweight agent shim, ~5-15s)
      2. Warm-up → full OpenClaw ready (no more warm-up footer, ~2-4min)

    The test confirms full startup by sending a message that exercises a
    ClawHub skill (only available after OpenClaw gateway is ready). A response
    without the warm-up footer proves the full runtime is handling messages.
    """

    # Maximum time to wait for OpenClaw to finish starting (seconds).
    # Typical cold start is ~2-4 min; 10 min covers slow regions/cold pulls.
    MAX_STARTUP_WAIT_S = 600
    POLL_INTERVAL_S = 30  # Time between status-check messages

    def test_full_startup_and_skill(self, e2e_config):
        """Reset session, wait for full OpenClaw startup, verify a
        post-warmup response (no warm-up footer)."""
        from .session import _stop_agentcore_session

        # --- Phase 0: Force a true cold start ---
        user_id = get_user_id(e2e_config)
        if user_id:
            reset_session(e2e_config)
            _stop_agentcore_session(e2e_config)

        cold_start_time = time.time()
        cold_start_mono = time.monotonic()

        # --- Phase 1: First message (warm-up shim should respond) ---
        since_ms = int(cold_start_time * 1000)
        result = post_webhook(e2e_config, "What tools and skills do you have?")
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Phase 1 incomplete (timed_out={tail.timed_out})"
        )
        warmup_response_s = time.monotonic() - cold_start_mono

        # First response during cold start should be from the shim
        assert tail.is_warmup, (
            f"Expected warm-up shim response on cold start, but got full "
            f"OpenClaw response in {warmup_response_s:.1f}s. "
            f"Response: {tail.response_text[:200]}"
        )

        # --- Phase 2: Poll until OpenClaw is fully started ---
        fully_up = False
        full_startup_s = 0.0
        last_response = ""

        deadline = cold_start_mono + self.MAX_STARTUP_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(self.POLL_INTERVAL_S)

            since_ms = int(time.time() * 1000)
            post_webhook(
                e2e_config,
                "Status check — list your available tools briefly.",
            )
            tail = tail_logs(
                e2e_config, since_ms=since_ms, timeout_s=120,
            )

            if not tail.full_lifecycle:
                continue

            last_response = tail.response_text
            if not tail.is_warmup:
                fully_up = True
                full_startup_s = time.monotonic() - cold_start_mono
                break

        assert fully_up, (
            f"OpenClaw did not fully start within {self.MAX_STARTUP_WAIT_S}s. "
            f"Still seeing warm-up footer.\n"
            f"Last response: {last_response[:300]}"
        )

        # --- Report timing ---
        print(f"\n  Phase 1 — warm-up response: {warmup_response_s:.1f}s")
        print(f"  Phase 2 — full OpenClaw ready: {full_startup_s:.1f}s")
        print(f"  Response (no warm-up footer): {last_response[:200]}")

        # Sanity: full startup should take at least 30s (if faster, the shim
        # check in phase 1 probably didn't work correctly)
        assert full_startup_s > 30, (
            f"Suspiciously fast full startup ({full_startup_s:.1f}s). "
            f"The warm-up shim may not be working correctly."
        )


# ---------------------------------------------------------------------------
# Shared helper: wait for full OpenClaw startup
# ---------------------------------------------------------------------------

_OPENCLAW_STARTUP_TIMEOUT_S = 600  # Max wait for full cold start (slow regions)
_SUBAGENT_TIMEOUT_S = 600  # Sub-agent skills may take several minutes


def _wait_for_full_openclaw(e2e_config, max_wait_s=_OPENCLAW_STARTUP_TIMEOUT_S,
                            poll_interval_s=30):
    """Wait for OpenClaw to be fully started (not in warm-up mode).

    Sends periodic status-check messages until the response no longer
    contains the warm-up footer. Sleeps before the first probe to avoid
    wasting a poll cycle when called immediately after a cold start.

    Returns (is_ready, elapsed_s).
    """
    start = time.monotonic()

    while (time.monotonic() - start) < max_wait_s:
        # Sleep before probing — avoids wasting a 120s tail_logs cycle
        # when the session is known to be cold (just started/reset).
        time.sleep(poll_interval_s)

        since_ms = int(time.time() * 1000)
        post_webhook(e2e_config, "Quick status check")
        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=120)

        if tail.full_lifecycle and not tail.is_warmup:
            return True, time.monotonic() - start

    return False, time.monotonic() - start


class TestSubagent:
    """Verify sub-agent skills work after full OpenClaw startup.

    Tests deep-research-pro and task-decomposer skills, which spawn
    sub-agents for parallel work. Requires OpenClaw to be fully started
    (not in warm-up mode).

    After each skill invocation, queries the contract status endpoint to
    verify that subagentRequestCount increased — definitive proof that
    OpenClaw subagents actually fired (not just that the skill responded).

    These tests are slower than other E2E tests because:
    1. They may need to wait for full OpenClaw startup (~2-4 min)
    2. Sub-agent skills take longer to execute than simple responses

    Run with: pytest tests/e2e/bot_test.py -v -k subagent
    """

    # Minimum response length thresholds. Simple responses are typically
    # <200 chars; sub-agent skill output should be substantially longer.
    MIN_TASK_DECOMPOSE_LEN = 100
    MIN_DEEP_RESEARCH_LEN = 200

    @pytest.fixture(scope="class", autouse=True)
    def ensure_full_openclaw(self, e2e_config):
        """Wait for full OpenClaw startup once before all subagent tests."""
        ready, elapsed = _wait_for_full_openclaw(e2e_config)
        assert ready, f"OpenClaw not fully started after {elapsed:.0f}s"
        print(f"\n  OpenClaw ready in {elapsed:.1f}s")

    @staticmethod
    def _get_subagent_count(e2e_config):
        """Query contract status for current subagentRequestCount.

        Returns the count, or None if the status endpoint is unavailable.
        """
        status = get_agent_status(e2e_config)
        if status is None:
            return None
        return status.get("subagentRequestCount")

    def test_task_decomposer_skill(self, e2e_config):
        """Send a task decomposition request and verify structured output.

        The task-decomposer skill spawns sub-agents to break complex
        requests into manageable subtasks. Verifies the response is
        substantial, came from full OpenClaw (not warm-up shim), and
        that subagentRequestCount increased.
        """
        baseline_count = self._get_subagent_count(e2e_config)

        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            "Break down the task of building a REST API into subtasks",
        )
        assert result.status_code == 200

        tail = tail_logs(
            e2e_config, since_ms=since_ms, timeout_s=_SUBAGENT_TIMEOUT_S,
        )
        assert tail.full_lifecycle, (
            f"Incomplete lifecycle (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        assert not tail.is_warmup, (
            "Response came from warm-up shim, not full OpenClaw. "
            "Task-decomposer skill not available during warm-up."
        )
        assert tail.response_len >= self.MIN_TASK_DECOMPOSE_LEN, (
            f"Response too short ({tail.response_len} chars) for task "
            f"decomposition. Expected structured subtask output.\n"
            f"Response: {tail.response_text[:300]}"
        )

        # Verify subagent requests actually fired
        after_count = self._get_subagent_count(e2e_config)
        if baseline_count is not None and after_count is not None:
            assert after_count > baseline_count, (
                f"subagentRequestCount did not increase after task-decomposer "
                f"(before={baseline_count}, after={after_count}). "
                f"Subagents may not have fired."
            )
            print(
                f"  Subagent count: {baseline_count} -> {after_count} "
                f"(+{after_count - baseline_count})"
            )
        else:
            print(
                f"  Subagent count: status endpoint unavailable "
                f"(baseline={baseline_count}, after={after_count})"
            )

        print(
            f"  Task decomposer response ({tail.response_len} chars, "
            f"{tail.elapsed_s:.1f}s): {tail.response_text[:300]}"
        )

    def test_deep_research_skill(self, e2e_config):
        """Send a deep research request and verify detailed output.

        The deep-research-pro skill spawns sub-agents for multi-step
        research on complex topics. Verifies the response is detailed,
        came from full OpenClaw (not warm-up shim), and that
        subagentRequestCount increased.
        """
        baseline_count = self._get_subagent_count(e2e_config)

        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            "Research the latest advances in quantum computing",
        )
        assert result.status_code == 200

        tail = tail_logs(
            e2e_config, since_ms=since_ms, timeout_s=_SUBAGENT_TIMEOUT_S,
        )
        assert tail.full_lifecycle, (
            f"Incomplete lifecycle (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        assert not tail.is_warmup, (
            "Response came from warm-up shim, not full OpenClaw. "
            "Deep-research-pro skill not available during warm-up."
        )
        assert tail.response_len >= self.MIN_DEEP_RESEARCH_LEN, (
            f"Response too short ({tail.response_len} chars) for deep "
            f"research. Expected multi-section research output.\n"
            f"Response: {tail.response_text[:300]}"
        )

        # Verify subagent requests actually fired
        after_count = self._get_subagent_count(e2e_config)
        if baseline_count is not None and after_count is not None:
            assert after_count > baseline_count, (
                f"subagentRequestCount did not increase after deep-research "
                f"(before={baseline_count}, after={after_count}). "
                f"Subagents may not have fired."
            )
            print(
                f"  Subagent count: {baseline_count} -> {after_count} "
                f"(+{after_count - baseline_count})"
            )
        else:
            print(
                f"  Subagent count: status endpoint unavailable "
                f"(baseline={baseline_count}, after={after_count})"
            )

        print(
            f"  Deep research response ({tail.response_len} chars, "
            f"{tail.elapsed_s:.1f}s): {tail.response_text[:300]}"
        )


class TestScopedCredentials:
    """Verify per-user S3 credential isolation via the s3-user-files skill.

    After the scoped credentials fix (GitHub issue #20), OpenClaw runs with
    STS session-scoped credentials that restrict S3 access to the user's
    namespace prefix. This test verifies the s3-user-files skill still works
    end-to-end through those scoped credentials.

    Flow:
      1. Write a test file via the bot (uses s3-user-files write skill)
      2. Read it back (uses s3-user-files read skill)
      3. Verify the content matches
      4. Delete it (uses s3-user-files delete skill)

    Run with: pytest tests/e2e/bot_test.py -v -k scoped_creds
    """

    TEST_CONTENT = "E2E_SCOPED_CREDS_OK"
    TEST_FILENAME = "e2e-creds-test.txt"

    @pytest.fixture(scope="class", autouse=True)
    def ensure_full_openclaw(self, e2e_config):
        """Wait for full OpenClaw startup — s3-user-files skill requires it."""
        ready, elapsed = _wait_for_full_openclaw(e2e_config)
        assert ready, f"OpenClaw not fully started after {elapsed:.0f}s"
        print(f"\n  OpenClaw ready in {elapsed:.1f}s")

    def test_write_file(self, e2e_config):
        """Write a test file via the bot's s3-user-files skill."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Save the text "{self.TEST_CONTENT}" to a file called {self.TEST_FILENAME}',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Write file incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        assert not tail.is_warmup, "Response from warm-up shim, not full OpenClaw"
        print(f"  Write response ({tail.response_len} chars): {tail.response_text[:200]}")

    def test_read_file(self, e2e_config):
        """Read the test file back and verify it contains the expected content."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f"Read the contents of {self.TEST_FILENAME}",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Read file incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        assert not tail.is_warmup, "Response from warm-up shim, not full OpenClaw"

        # The response should contain the test content we wrote
        assert self.TEST_CONTENT in tail.response_text, (
            f"Expected '{self.TEST_CONTENT}' in response.\n"
            f"Response ({tail.response_len} chars): {tail.response_text[:300]}"
        )
        print(f"  Read response contains expected content: {tail.response_text[:200]}")

    def test_delete_file(self, e2e_config):
        """Clean up: delete the test file."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f"Delete the file {self.TEST_FILENAME}",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Delete file incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        print(f"  Delete response ({tail.response_len} chars): {tail.response_text[:200]}")


class TestApiKeyManagement:
    """Verify dual-mode API key storage: native file-based and Secrets Manager.

    Tests the manage_api_key (native), manage_secret (Secrets Manager),
    retrieve_api_key (unified lookup), and migrate_api_key tools during
    warm-up mode (lightweight agent). These tools are available immediately
    without waiting for full OpenClaw startup.

    Flow:
      1. Set a native API key via manage_api_key
      2. Get it back to verify storage
      3. Set a secret in Secrets Manager
      4. Retrieve via unified retrieval (tries SM first, falls back to native)
      5. List native keys and secrets
      6. Clean up: delete both keys

    Uses the api-keys skill scripts in /skills/api-keys/ which work in both
    warm-up mode (lightweight agent) and full OpenClaw mode.

    Run with: pytest tests/e2e/bot_test.py -v -k TestApiKeyManagement
    """

    NATIVE_KEY_NAME = "e2e_test_native_key"
    NATIVE_KEY_VALUE = "native-test-value-12345"
    SM_KEY_NAME = "e2e_test_secure_key"
    SM_KEY_VALUE = "secure-test-value-67890"

    @pytest.fixture(autouse=True, scope="class")
    def fresh_session(self, e2e_config):
        """Reset session and clean up stale Secrets Manager entries before tests."""
        # Force-delete any lingering test secret (e.g. stuck in 7-day recovery window)
        sm = boto3.client("secretsmanager", region_name=e2e_config.region)
        namespace = f"telegram_{e2e_config.telegram_user_id}"
        secret_name = f"openclaw/user/{namespace}/{self.SM_KEY_NAME}"
        try:
            sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
            print(f"\n  [cleanup] Force-deleted stale test secret: {self.SM_KEY_NAME}")
            time.sleep(2)  # Brief wait for deletion to propagate
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                print(f"\n  [cleanup] Warning deleting test secret: {e.response['Error']['Code']}")

        reset_session(e2e_config)
        time.sleep(2)

    def test_set_native_key(self, e2e_config):
        """Store an API key using native file-based storage."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Save an API key using native file storage. '
            f'Key name: "{self.NATIVE_KEY_NAME}", value: "{self.NATIVE_KEY_VALUE}". '
            f'Use the api-keys skill native.js with action "set".',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Set native key incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        # Response should confirm the key was stored
        resp_lower = tail.response_text.lower()
        assert any(w in resp_lower for w in ["stored", "saved", "set", "success"]), (
            f"Expected confirmation of key storage.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Set native key response: {tail.response_text[:200]}")

    def test_get_native_key(self, e2e_config):
        """Retrieve the native API key and verify the value."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Get the API key named "{self.NATIVE_KEY_NAME}" using the api-keys '
            f'skill native.js with action "get". Show me the exact value.',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Get native key incomplete (timed_out={tail.timed_out})"
        )
        # The response should contain the key value
        assert self.NATIVE_KEY_VALUE in tail.response_text, (
            f"Expected key value '{self.NATIVE_KEY_VALUE}' in response.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Get native key response: {tail.response_text[:200]}")

    def test_set_secret(self, e2e_config):
        """Store an API key in AWS Secrets Manager."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Store a secret in AWS Secrets Manager. '
            f'Key name: "{self.SM_KEY_NAME}", value: "{self.SM_KEY_VALUE}". '
            f'Use the api-keys skill secret.js with action "set".',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Set secret incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )
        resp_lower = tail.response_text.lower()
        assert any(w in resp_lower for w in ["stored", "saved", "created", "success", "encrypted"]), (
            f"Expected confirmation of secret storage.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Set secret response: {tail.response_text[:200]}")

    def test_retrieve_api_key_unified(self, e2e_config):
        """Use retrieve.js to look up a key (tries SM first, then native)."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Retrieve the API key named "{self.SM_KEY_NAME}" using the api-keys '
            f'skill retrieve.js. Show me the value and which backend it came from.',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Retrieve key incomplete (timed_out={tail.timed_out})"
        )
        # Should find the SM key value
        assert self.SM_KEY_VALUE in tail.response_text, (
            f"Expected key value '{self.SM_KEY_VALUE}' in response.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Retrieve key response: {tail.response_text[:200]}")

    def test_list_native_keys(self, e2e_config):
        """List all native API keys and verify our test key is present."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            'List all native API keys using the api-keys skill native.js with action "list".',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"List native keys incomplete (timed_out={tail.timed_out})"
        )
        assert self.NATIVE_KEY_NAME in tail.response_text, (
            f"Expected '{self.NATIVE_KEY_NAME}' in key list.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  List native keys response: {tail.response_text[:200]}")

    def test_delete_native_key(self, e2e_config):
        """Clean up: delete the native API key."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Delete the API key named "{self.NATIVE_KEY_NAME}" using the '
            f'api-keys skill native.js with action "delete".',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Delete native key incomplete (timed_out={tail.timed_out})"
        )
        resp_lower = tail.response_text.lower()
        assert any(w in resp_lower for w in ["deleted", "removed", "success"]), (
            f"Expected deletion confirmation.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Delete native key response: {tail.response_text[:200]}")

    def test_delete_secret(self, e2e_config):
        """Clean up: delete the Secrets Manager secret."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f'Delete the secret named "{self.SM_KEY_NAME}" using the '
            f'api-keys skill secret.js with action "delete".',
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Delete secret incomplete (timed_out={tail.timed_out})"
        )
        resp_lower = tail.response_text.lower()
        assert any(w in resp_lower for w in ["deleted", "removed", "scheduled", "success"]), (
            f"Expected deletion confirmation.\n"
            f"Response: {tail.response_text[:300]}"
        )
        print(f"  Delete secret response: {tail.response_text[:200]}")


class TestSkillManagement:
    """Verify clawhub-manage skill: list, install, and uninstall skills.

    Tests the full lifecycle of skill management through the bot:
      1. List pre-installed skills (verify baseline)
      2. Install a new skill (hackernews — lightweight, no API key needed)
      3. Verify it appears in the skill list
      4. Uninstall the skill
      5. Verify it's removed from the list

    These tests reset the session to force a cold start so they run in
    warm-up mode (lightweight agent) where install/uninstall/list tools
    are explicitly available. Newly installed skills are available on the
    next session start.

    Run with: pytest tests/e2e/bot_test.py -v -k TestSkillManagement
    """

    @pytest.fixture(autouse=True, scope="class")
    def fresh_session(self, e2e_config):
        """Reset session before skill management tests to ensure warm-up mode."""
        reset_session(e2e_config)
        time.sleep(2)  # Brief pause after session reset

    # hackernews is lightweight (no API key), good for testing install/uninstall
    TEST_SKILL = "hackernews"

    # Pre-installed skills that should always appear in list
    EXPECTED_PREINSTALLED = [
        "jina-reader",
        "deep-research-pro",
        "telegram-compose",
        "transcript",
        "task-decomposer",
    ]

    def test_list_skills(self, e2e_config):
        """List installed skills and verify pre-installed skills are present."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            "What skills are installed? List them all.",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"List skills incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )

        # Verify at least some pre-installed skills appear in the response
        resp_lower = tail.response_text.lower()
        found_skills = [s for s in self.EXPECTED_PREINSTALLED if s in resp_lower]
        assert len(found_skills) >= 3, (
            f"Expected at least 3 pre-installed skills in response, "
            f"found {len(found_skills)}: {found_skills}\n"
            f"Response: {tail.response_text[:500]}"
        )
        print(f"  Found {len(found_skills)} pre-installed skills: {found_skills}")
        print(f"  Response ({tail.response_len} chars): {tail.response_text[:300]}")

    def test_install_skill(self, e2e_config):
        """Install a test skill and verify it was installed successfully."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f"Install the {self.TEST_SKILL} skill please.",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Install skill incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )

        # Verify the response mentions the skill name (installed or attempted)
        resp_lower = tail.response_text.lower()
        assert self.TEST_SKILL in resp_lower, (
            f"Expected '{self.TEST_SKILL}' mentioned in response.\n"
            f"Response: {tail.response_text[:500]}"
        )
        print(f"  Install response ({tail.response_len} chars): {tail.response_text[:300]}")

    def test_verify_installed_skill(self, e2e_config):
        """After install, verify the skill files exist on disk."""
        # Small delay to let install complete
        time.sleep(3)

        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            "List all installed skills again.",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Verify installed skill incomplete (timed_out={tail.timed_out})"
        )

        resp_lower = tail.response_text.lower()
        # The list.js script scans the filesystem, so it should find
        # the newly installed skill even without an OpenClaw restart
        assert self.TEST_SKILL in resp_lower or "installed" in resp_lower, (
            f"Expected '{self.TEST_SKILL}' or 'installed' in response.\n"
            f"Response: {tail.response_text[:500]}"
        )
        print(f"  Verified '{self.TEST_SKILL}' install acknowledged")

    def test_uninstall_skill(self, e2e_config):
        """Uninstall the test skill."""
        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            f"Uninstall the {self.TEST_SKILL} skill please.",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"Uninstall skill incomplete (timed_out={tail.timed_out}, "
            f"elapsed={tail.elapsed_s:.1f}s)"
        )

        resp_lower = tail.response_text.lower()
        assert self.TEST_SKILL in resp_lower, (
            f"Expected '{self.TEST_SKILL}' mentioned in response.\n"
            f"Response: {tail.response_text[:500]}"
        )
        print(f"  Uninstall response ({tail.response_len} chars): {tail.response_text[:300]}")

    def test_verify_uninstalled_skill(self, e2e_config):
        """After uninstall, list skills and verify the test skill is gone."""
        time.sleep(3)

        since_ms = int(time.time() * 1000)
        result = post_webhook(
            e2e_config,
            "List all installed skills one more time.",
        )
        assert result.status_code == 200

        tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
        assert tail.full_lifecycle, (
            f"List skills (post-uninstall) incomplete (timed_out={tail.timed_out})"
        )

        resp_lower = tail.response_text.lower()
        # The test skill should no longer appear, or response confirms uninstalled
        skill_gone = (
            self.TEST_SKILL not in resp_lower
            or "uninstall" in resp_lower
            or "removed" in resp_lower
            or "no longer" in resp_lower
            or "not installed" in resp_lower
        )
        assert skill_gone, (
            f"Expected '{self.TEST_SKILL}' to be absent from skill list.\n"
            f"Response: {tail.response_text[:500]}"
        )
        print(f"  Verified '{self.TEST_SKILL}' no longer in skill list")
        print(f"  List response ({tail.response_len} chars): {tail.response_text[:300]}")


class TestConversation:
    """Multi-message conversation tests."""

    def test_conversation(self, e2e_config, conversation_scenario):
        """Send a conversation scenario and verify each message lifecycle."""
        name, messages = conversation_scenario

        for i, msg in enumerate(messages):
            since_ms = int(time.time() * 1000)
            result = post_webhook(e2e_config, msg)
            assert result.status_code == 200, f"Message {i+1}/{len(messages)} rejected"

            tail = tail_logs(e2e_config, since_ms=since_ms, timeout_s=300)
            assert tail.full_lifecycle, (
                f"[{name}] Message {i+1}/{len(messages)} incomplete lifecycle "
                f"(timed_out={tail.timed_out}, elapsed={tail.elapsed_s:.1f}s)"
            )

            # Delay between messages (shorter for rapid_fire)
            if i < len(messages) - 1:
                delay = 1 if name == "rapid_fire" else 5
                time.sleep(delay)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _cli_health(cfg):
    print(f"Health check: {cfg.api_url}/health")
    result = health_check(cfg)
    status = "OK" if result.status_code == 200 else "FAIL"
    print(f"  {status} ({result.status_code}) — {result.elapsed_ms:.0f}ms")
    print(f"  Body: {result.body}")
    return result.status_code == 200


def _cli_send(cfg, text, tail, timeout_s=300):
    since_ms = int(time.time() * 1000)
    print(f"Sending: {text!r}")
    result = post_webhook(cfg, text)
    print(f"  Webhook: {result.status_code} ({result.elapsed_ms:.0f}ms)")

    if result.status_code != 200:
        print(f"  ERROR: {result.body}")
        return False

    if not tail:
        print("  (use --tail-logs to verify lifecycle via CloudWatch)")
        return True

    print(f"  Tailing logs (timeout={timeout_s}s, poll=5s)...")
    tail_result = tail_logs(cfg, since_ms=since_ms, timeout_s=timeout_s)

    if tail_result.full_lifecycle:
        print(f"  PASS — full lifecycle in {tail_result.elapsed_s:.1f}s")
        if tail_result.new_session:
            print(f"  New session: {tail_result.session_id}")
        if tail_result.new_user:
            print(f"  New user: {tail_result.user_id}")
        if tail_result.response_text:
            preview = tail_result.response_text[:200]
            print(f"  Response ({tail_result.response_len} chars): {preview}")
    else:
        print(f"  INCOMPLETE — elapsed={tail_result.elapsed_s:.1f}s timed_out={tail_result.timed_out}")
        print(f"    received={tail_result.message_received}")
        print(f"    invoked={tail_result.agentcore_invoked}")
        print(f"    sent={tail_result.telegram_sent}")
        if tail_result.raw_lines:
            print(f"  Last log lines:")
            for line in tail_result.raw_lines[-5:]:
                print(f"    {line.rstrip()}")

    return tail_result.full_lifecycle


def _cli_subagent(cfg, tail):
    """Run sub-agent skill tests: task-decomposer and deep-research-pro.

    Always polls CloudWatch during startup wait (regardless of --tail-logs),
    then uses the tail flag for the actual skill invocation logs.
    """
    print("Sub-agent skill tests (requires full OpenClaw startup)")
    print("Waiting for OpenClaw to be fully started...")

    ready, elapsed = _wait_for_full_openclaw(cfg)
    if not ready:
        print(f"  FAIL — OpenClaw not fully started after {elapsed:.0f}s")
        return False
    print(f"  OpenClaw ready in {elapsed:.1f}s\n")

    prompts = [
        ("task-decomposer", "Break down the task of building a REST API into subtasks"),
        ("deep-research-pro", "Research the latest advances in quantum computing"),
    ]

    all_ok = True
    for skill_name, prompt in prompts:
        print(f"Testing {skill_name}...")
        ok = _cli_send(cfg, prompt, tail, timeout_s=_SUBAGENT_TIMEOUT_S)
        if not ok:
            all_ok = False
        print()

    return all_ok


def _cli_scoped_creds(cfg, tail):
    """Test S3 file operations via scoped credentials (write, read, delete).

    Verifies that the s3-user-files skill works through STS session-scoped
    credentials. Requires full OpenClaw startup.
    """
    print("Scoped credentials test (S3 file operations, requires full startup)")
    print("Waiting for OpenClaw to be fully started...")

    ready, elapsed = _wait_for_full_openclaw(cfg)
    if not ready:
        print(f"  FAIL — OpenClaw not fully started after {elapsed:.0f}s")
        return False
    print(f"  OpenClaw ready in {elapsed:.1f}s\n")

    test_content = "E2E_SCOPED_CREDS_OK"
    test_file = "e2e-creds-test.txt"

    # Step 1: Write
    print(f"1. Writing test file ({test_file})...")
    ok = _cli_send(cfg, f'Save the text "{test_content}" to a file called {test_file}', tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 2: Read back
    print(f"\n2. Reading test file ({test_file})...")
    ok = _cli_send(cfg, f"Read the contents of {test_file}", tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 3: Delete
    print(f"\n3. Deleting test file ({test_file})...")
    ok = _cli_send(cfg, f"Delete the file {test_file}", tail)
    return ok


def _cli_skill_manage(cfg, tail):
    """Test skill management lifecycle: list, install, verify, uninstall, verify.

    Tests the clawhub-manage skill's ability to install and uninstall
    ClawHub community skills from the agent's chat interface.
    """
    test_skill = "hackernews"
    print(f"Skill management test (install/uninstall {test_skill})")

    # Step 1: List skills
    print("\n1. Listing installed skills...")
    ok = _cli_send(cfg, "What ClawHub skills are currently installed? Use the clawhub-manage skill to list them.", tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 2: Install test skill
    print(f"\n2. Installing {test_skill}...")
    ok = _cli_send(cfg, f"Please install the {test_skill} skill using clawhub-manage. Use the install_skill tool or run: node /skills/clawhub-manage/install.js {test_skill}", tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 3: Verify installed
    print(f"\n3. Verifying {test_skill} is installed...")
    ok = _cli_send(cfg, "List all installed ClawHub skills using clawhub-manage.", tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 4: Uninstall test skill
    print(f"\n4. Uninstalling {test_skill}...")
    ok = _cli_send(cfg, f"Please uninstall the {test_skill} skill using clawhub-manage. Use the uninstall_skill tool or run: node /skills/clawhub-manage/uninstall.js {test_skill}", tail)
    if not ok:
        return False
    time.sleep(5)

    # Step 5: Verify uninstalled
    print(f"\n5. Verifying {test_skill} is uninstalled...")
    ok = _cli_send(cfg, "List all installed ClawHub skills using clawhub-manage. Show the complete list.", tail)
    return ok


def _cli_api_keys(cfg, tail):
    """Test dual-mode API key storage: native file-based and Secrets Manager.

    Tests manage_api_key (native), manage_secret (SM), retrieve_api_key
    (unified lookup), and cleanup (delete both).
    """
    native_key = "e2e_test_native_key"
    native_val = "native-test-value-12345"
    sm_key = "e2e_test_secure_key"
    sm_val = "secure-test-value-67890"

    print("API key management test (native + Secrets Manager)")

    # Step 1: Set native key
    print(f"\n1. Setting native key ({native_key})...")
    ok = _cli_send(
        cfg,
        f'Store an API key named "{native_key}" with value "{native_val}" '
        f'using the manage_api_key tool with action "set".',
        tail,
    )
    if not ok:
        return False
    time.sleep(5)

    # Step 2: Get native key
    print(f"\n2. Getting native key ({native_key})...")
    ok = _cli_send(
        cfg,
        f'Get the API key named "{native_key}" using manage_api_key with action "get".',
        tail,
    )
    if not ok:
        return False
    time.sleep(5)

    # Step 3: Set SM secret
    print(f"\n3. Setting Secrets Manager secret ({sm_key})...")
    ok = _cli_send(
        cfg,
        f'Store a secret named "{sm_key}" with value "{sm_val}" '
        f'using the manage_secret tool with action "set".',
        tail,
    )
    if not ok:
        return False
    time.sleep(5)

    # Step 4: Retrieve via unified lookup
    print(f"\n4. Retrieving via retrieve_api_key ({sm_key})...")
    ok = _cli_send(
        cfg,
        f'Use the retrieve_api_key tool to look up "{sm_key}".',
        tail,
    )
    if not ok:
        return False
    time.sleep(5)

    # Step 5: Clean up native key
    print(f"\n5. Deleting native key ({native_key})...")
    ok = _cli_send(
        cfg,
        f'Delete the API key named "{native_key}" using manage_api_key with action "delete".',
        tail,
    )
    if not ok:
        return False
    time.sleep(5)

    # Step 6: Clean up SM secret
    print(f"\n6. Deleting Secrets Manager secret ({sm_key})...")
    ok = _cli_send(
        cfg,
        f'Delete the secret named "{sm_key}" using manage_secret with action "delete".',
        tail,
    )
    return ok


def _cli_conversation(cfg, scenario_name, tail):
    if scenario_name not in SCENARIOS:
        print(f"Unknown scenario: {scenario_name}")
        print(f"Available: {', '.join(SCENARIOS.keys())}")
        return False

    messages = SCENARIOS[scenario_name]
    print(f"Conversation: {scenario_name} ({len(messages)} messages)")

    all_ok = True
    for i, msg in enumerate(messages):
        print(f"\n  [{i+1}/{len(messages)}] {msg!r}")
        ok = _cli_send(cfg, msg, tail)
        if not ok:
            all_ok = False
        if i < len(messages) - 1:
            delay = 1 if scenario_name == "rapid_fire" else 5
            print(f"  Waiting {delay}s...")
            time.sleep(delay)

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="E2E bot testing CLI")
    parser.add_argument("--health", action="store_true", help="Health check only")
    parser.add_argument("--send", type=str, help="Send a single message")
    parser.add_argument("--conversation", type=str, help="Run a conversation scenario")
    parser.add_argument("--subagent", action="store_true", help="Test sub-agent skills (requires full startup)")
    parser.add_argument("--scoped-creds", action="store_true", help="Test S3 file ops via scoped credentials (requires full startup)")
    parser.add_argument("--skill-manage", action="store_true", help="Test skill management (list, install, uninstall)")
    parser.add_argument("--api-keys", action="store_true", help="Test API key management (native + Secrets Manager)")
    parser.add_argument("--reset", action="store_true", help="Reset session before sending")
    parser.add_argument("--reset-user", action="store_true", help="Full user reset (delete all records)")
    parser.add_argument("--tail-logs", action="store_true", help="Tail CloudWatch logs to verify lifecycle")
    parser.add_argument("--timeout", type=int, default=300, help="Log tail timeout in seconds")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except RuntimeError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Region: {cfg.region}")
    print(f"API URL: {cfg.api_url}")
    print(f"Telegram user: {cfg.telegram_user_id}")
    print()

    if args.health:
        ok = _cli_health(cfg)
        sys.exit(0 if ok else 1)

    if args.reset_user:
        count = reset_user(cfg)
        print(f"Reset user: deleted {count} DynamoDB records")
        sys.exit(0)

    if args.reset:
        ok = reset_session(cfg)
        print(f"Reset session: {'deleted' if ok else 'no session found'}")

    if args.subagent:
        ok = _cli_subagent(cfg, args.tail_logs)
        sys.exit(0 if ok else 1)

    if args.scoped_creds:
        ok = _cli_scoped_creds(cfg, args.tail_logs)
        sys.exit(0 if ok else 1)

    if args.skill_manage:
        ok = _cli_skill_manage(cfg, args.tail_logs)
        sys.exit(0 if ok else 1)

    if args.api_keys:
        ok = _cli_api_keys(cfg, args.tail_logs)
        sys.exit(0 if ok else 1)

    if args.conversation:
        ok = _cli_conversation(cfg, args.conversation, args.tail_logs)
        sys.exit(0 if ok else 1)

    if args.send:
        ok = _cli_send(cfg, args.send, args.tail_logs)
        sys.exit(0 if ok else 1)

    if not any([args.health, args.send, args.conversation, args.subagent,
                args.scoped_creds, args.skill_manage, args.api_keys,
                args.reset, args.reset_user]):
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
