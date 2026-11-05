"""Tests for Discord delivery in the Cron Lambda (synthetic IDs and tokens only).

Scheduled-task results have no interaction to answer, so the bot opens the
user's DM channel and posts the result there. The HTTP layer is a fake that
answers like Discord's API.
"""

import email.message
import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

index = importlib.import_module("index")

API = "https://discord.com/api/v10"
USER_ID = "200000000000000002"
DM_CHANNEL_ID = "300000000000000003"
BOT_TOKEN = "synthetic.bot-token_0001"
SECRET = json.dumps({"applicationId": "100000000000000001", "publicKey": "0" * 64, "botToken": BOT_TOKEN})
JOB = {"channel": "discord", "channelTarget": USER_ID, "scheduleId": "schedule123", "scheduleName": "Morning brief"}


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self, _size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeDiscord:
    """Stands in for discord_channel's no-redirect opener and records each request.

    `failures` holds (URL fragment, status, body, headers) entries; the first
    entry whose fragment is in a request's URL is consumed and raised as an
    HTTPError.
    """

    def __init__(self):
        self.requests = []
        self.failures = []
        self.dm_channel = {"id": DM_CHANNEL_ID, "type": 1}

    def open(self, req, timeout=None):
        self.requests.append({"method": req.get_method(), "url": req.full_url, "body": json.loads(req.data),
                              "auth": req.get_header("Authorization"), "agent": req.get_header("User-agent")})
        for n, (fragment, status, body, headers) in enumerate(self.failures):
            if fragment in req.full_url:
                del self.failures[n]
                hdrs = email.message.Message()
                for name, value in headers.items():
                    hdrs[name] = value
                raise HTTPError(req.full_url, status, "error", hdrs, io.BytesIO(json.dumps(body).encode()))
        if req.full_url == f"{API}/users/@me/channels":
            return _Response(self.dm_channel)
        return _Response({"id": "400000000000000004", "channel_id": DM_CHANNEL_ID})

    def fail(self, fragment, status, body=None, headers=None):
        self.failures.append((fragment, status, body or {}, headers or {}))

    def messages(self):
        return [r["body"] for r in self.requests if r["url"] == f"{API}/channels/{DM_CHANNEL_ID}/messages"]


class TestCronDiscordDelivery(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.get_secret = MagicMock(return_value=SECRET)
        self.sleep = MagicMock()
        for target, name, value in ((index, "DISCORD_TOKEN_SECRET_ID", "openclaw/channels/discord"),
                                    (index, "_get_secret", self.get_secret),
                                    (index.discord_channel, "_NO_REDIRECT_OPENER", self.discord),
                                    (index.discord_channel.time, "sleep", self.sleep)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_opens_the_dm_then_sends_the_reply(self):
        with self.assertNoLogs(index.logger, level="WARNING"):
            index.deliver_response("discord", USER_ID, "Your brief")
        self.assertEqual([(r["method"], r["url"], r["body"]) for r in self.discord.requests], [
            ("POST", f"{API}/users/@me/channels", {"recipient_id": USER_ID}),
            ("POST", f"{API}/channels/{DM_CHANNEL_ID}/messages",
             {"content": "Your brief", "allowed_mentions": {"parse": []}}),
        ])
        for request in self.discord.requests:
            self.assertEqual(request["auth"], f"Bot {BOT_TOKEN}")
            self.assertTrue(request["agent"].startswith("DiscordBot ("))
        self.get_secret.assert_called_with("openclaw/channels/discord")

    def test_long_reply_is_split_under_discords_limit(self):
        text = "@everyone " + " ".join(f"line{n:04d}" for n in range(1000))
        index.deliver_response("discord", USER_ID, text)
        messages = self.discord.messages()
        self.assertGreater(len(messages), 3)
        self.assertTrue(all(len(m["content"]) <= 2000 for m in messages))
        self.assertEqual("".join(m["content"] for m in messages), text)
        self.assertTrue(all(m["allowed_mentions"] == {"parse": []} for m in messages))
        # One DM channel for the whole reply.
        self.assertEqual(sum(r["url"].endswith("/users/@me/channels") for r in self.discord.requests), 1)

    def test_403_is_logged_with_status_and_code_only_and_not_retried(self):
        self.discord.fail("/messages", 403, {"message": "Cannot send messages to this user", "code": 50007})
        with self.assertLogs(index.logger, level="ERROR") as logs:
            index.deliver_response("discord", USER_ID, "Private brief text")
        self.assertEqual(len(self.discord.messages()), 1)
        self.sleep.assert_not_called()
        output = "\n".join(logs.output)
        self.assertIn("status=403 code=50007", output)
        for secret in ("Private brief text", BOT_TOKEN, "Cannot send messages", USER_ID):
            self.assertNotIn(secret, output)

    def test_403_when_opening_the_dm_sends_nothing(self):
        self.discord.fail("/users/@me/channels", 403, {"message": "Missing Access", "code": 50001})
        with self.assertLogs(index.logger, level="ERROR") as logs:
            index.deliver_response("discord", USER_ID, "Your brief")
        self.assertEqual(self.discord.messages(), [])
        self.assertIn("status=403 code=50001", "\n".join(logs.output))

    def test_short_rate_limit_is_waited_out_once(self):
        self.discord.fail("/messages", 429, {"message": "You are being rate limited.", "retry_after": 0.25})
        with self.assertNoLogs(index.logger, level="WARNING"):
            index.deliver_response("discord", USER_ID, "Your brief")
        self.sleep.assert_called_once_with(0.25)
        self.assertEqual([m["content"] for m in self.discord.messages()], ["Your brief", "Your brief"])

    def test_rate_limit_without_a_body_uses_the_retry_after_header(self):
        self.discord.fail("/messages", 429, {}, {"Retry-After": "1"})
        index.deliver_response("discord", USER_ID, "Your brief")
        self.sleep.assert_called_once_with(1.0)
        self.assertEqual(len(self.discord.messages()), 2)

    def test_long_rate_limits_repeated_429s_and_other_errors_are_not_retried(self):
        cases = (
            ("long 429", [(429, {"retry_after": 60})], "status=429", 1, 0),
            ("second 429", [(429, {"retry_after": 0.5}), (429, {"retry_after": 0.5})], "status=429", 2, 1),
            ("server error", [(500, {"message": "Internal Server Error", "code": 0})], "status=500 code=0", 1, 0),
            ("bad request", [(400, {"message": "Invalid Form Body", "code": 50035})], "status=400 code=50035", 1, 0),
        )
        for name, failures, logged, attempts, sleeps in cases:
            with self.subTest(name):
                self.discord.requests.clear()
                self.sleep.reset_mock()
                for status, body in failures:
                    self.discord.fail("/messages", status, body)
                with self.assertLogs(index.logger, level="ERROR") as logs:
                    index.deliver_response("discord", USER_ID, "Your brief")
                self.assertIn(logged, "\n".join(logs.output))
                self.assertEqual(len(self.discord.messages()), attempts)
                self.assertEqual(self.sleep.call_count, sleeps)

    def test_a_failed_chunk_stops_the_rest(self):
        self.discord.fail("/messages", 500)
        with self.assertLogs(index.logger, level="ERROR") as logs:
            index.deliver_response("discord", USER_ID, " ".join(["word"] * 1000))
        self.assertEqual(len(self.discord.messages()), 1)
        self.assertIn("after 0/3 messages", "\n".join(logs.output))

    def test_network_errors_are_logged_by_type(self):
        self.discord.open = MagicMock(side_effect=TimeoutError("timed out"))
        with self.assertLogs(index.logger, level="ERROR") as logs:
            index.deliver_response("discord", USER_ID, "Your brief")
        self.assertIn("Discord DM failed: TimeoutError", "\n".join(logs.output))

    def test_failure_and_warmup_notices_reach_discord(self):
        index.deliver_failure(JOB, "error")
        index.deliver_warmup_failure(JOB)
        contents = [m["content"] for m in self.discord.messages()]
        self.assertEqual(contents[0], f"[Scheduled: Morning brief] {index.CRON_FAILED_MESSAGE}")
        self.assertTrue(contents[1].startswith("[Scheduled: Morning brief] "))
        self.assertIn("the agent failed to start", contents[1])
        self.assertEqual(len(contents), 2)

    def test_missing_or_unusable_bot_token_sends_nothing(self):
        for raw in ("RandomPlaceholderSecret0123456789",
                    json.dumps({"applicationId": "100000000000000001", "publicKey": "0" * 64}),
                    json.dumps({"botToken": "synthetic\r\nX-Injected: 1"}),
                    json.dumps({"botToken": 12345}),
                    json.dumps(["botToken"]),
                    ""):
            with self.subTest(raw=raw[:24]):
                self.get_secret.return_value = raw
                with self.assertLogs(index.logger, level="ERROR") as logs:
                    index.deliver_response("discord", USER_ID, "Your brief")
                self.assertIn("Discord bot token not configured", "\n".join(logs.output))
        self.assertEqual(self.discord.requests, [])

    def test_no_secret_id_reads_no_secret(self):
        with patch.object(index, "DISCORD_TOKEN_SECRET_ID", ""), self.assertLogs(index.logger, level="ERROR"):
            index.deliver_response("discord", USER_ID, "Your brief")
        self.get_secret.assert_not_called()
        self.assertEqual(self.discord.requests, [])

    def test_bad_recipient_or_dm_channel_is_not_used(self):
        for target in ("../../evil", "", "١٢٣٤٥", "1" * 26):
            with self.subTest(target=target), self.assertLogs(index.logger, level="ERROR"):
                index.deliver_response("discord", target, "Your brief")
        self.assertEqual(self.discord.requests, [])
        self.discord.dm_channel = {"id": "../../webhooks/1/x"}
        with self.assertLogs(index.logger, level="ERROR"):
            index.deliver_response("discord", USER_ID, "Your brief")
        self.assertEqual(len(self.discord.requests), 1)
        self.assertEqual(self.discord.messages(), [])

    def test_empty_reply_sends_no_message(self):
        index.deliver_response("discord", USER_ID, "  \n")
        self.assertEqual(self.discord.requests, [])


if __name__ == "__main__":
    unittest.main()
