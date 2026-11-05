"""Regression: raw channel actors, phone numbers and delivery targets must not
appear in router logs. Covers the rejection, upload and send-failure log groups
for a WhatsApp actor (whose namespace is a phone number) and a Telegram actor
(whose chat_id equals the user ID).

All IDs below are synthetic placeholders.
"""

import importlib
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ.setdefault("S3_USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

index = importlib.import_module("index")

WA_PHONE = "15551234567"
WA_ACTOR = f"whatsapp:{WA_PHONE}"
TG_ID = "778899001"
TG_ACTOR = f"telegram:{TG_ID}"
# Nothing identifying for either actor may reach the logs.
FORBIDDEN = [WA_ACTOR, WA_PHONE, TG_ACTOR, TG_ID]


class FakeClientError(Exception):
    pass


class FakeTable:
    def __init__(self, items):
        self.items = dict(items)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 (AWS kwarg)
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


class CaptureLogs:
    """Capture index.logger output with its %-args already substituted."""

    def __enter__(self):
        self.messages = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.messages.append(record.getMessage())
        index.logger.addHandler(self.handler)
        self._level = index.logger.level
        index.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        index.logger.removeHandler(self.handler)
        index.logger.setLevel(self._level)

    @property
    def text(self):
        return "\n".join(self.messages)


def assert_no_pii(testcase, text):
    for bad in FORBIDDEN:
        testcase.assertNotIn(bad, text, f"log leaked {bad!r}")


def _owner(actor, user_id):
    return {"PK": f"ACTOR#{actor}", "SK": "OWNER", "userId": user_id, "actorId": actor,
            "platformVersion": "V2", "ownerGeneration": 1, "runtimeActorId": actor}


class TestRejectionLogs(unittest.TestCase):
    def setUp(self):
        self._patches = [patch.object(index, "REGISTRATION_OPEN", False),
                         patch.object(index, "ClientError", FakeClientError)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_revoked_user_logs_channel_and_user_not_actor(self):
        for channel, cid, actor in [("whatsapp", WA_PHONE, WA_ACTOR), ("telegram", TG_ID, TG_ACTOR)]:
            items = {
                (f"CHANNEL#{actor}", "PROFILE"): {"userId": "user_victim"},
                (f"ACTOR#{actor}", "OWNER"): _owner(actor, "user_victim"),
            }
            with patch.object(index, "identity_table", FakeTable(items)), CaptureLogs() as cap:
                self.assertEqual(index.resolve_user(channel, cid), (None, False))
            assert_no_pii(self, cap.text)
            self.assertIn("reason=revoked", cap.text)
            self.assertIn("user=user_victim", cap.text)

    def test_never_allowlisted_sender_logs_no_actor(self):
        for channel, cid in [("whatsapp", WA_PHONE), ("telegram", TG_ID)]:
            with patch.object(index, "identity_table", FakeTable({})), CaptureLogs() as cap:
                self.assertEqual(index.resolve_user(channel, cid), (None, False))
            assert_no_pii(self, cap.text)
            self.assertIn("reason=not-allowlisted", cap.text)

    def test_allowlist_read_error_logs_channel_only(self):
        class Boom(FakeTable):
            def get_item(self, Key, ConsistentRead=False):  # noqa: N803
                raise FakeClientError("boom")
        with patch.object(index, "identity_table", Boom({})), CaptureLogs() as cap:
            index._allow_record_exists(WA_ACTOR)
        assert_no_pii(self, cap.text)
        self.assertIn("channel=whatsapp", cap.text)


class TestUploadLogs(unittest.TestCase):
    def test_image_upload_logs_size_not_key(self):
        for actor_ns in (WA_PHONE, TG_ID):
            with patch.object(index, "s3_client", MagicMock()), CaptureLogs() as cap:
                key = index._upload_image_to_s3(b"x" * 100, actor_ns, "image/jpeg")
            self.assertTrue(key and key.startswith(f"{actor_ns}/_uploads/"))
            assert_no_pii(self, cap.text)
            self.assertIn("Uploaded image size=", cap.text)

    def test_rejected_screenshot_key_logs_without_namespace(self):
        for ns in (WA_PHONE, TG_ID):
            with CaptureLogs() as cap:
                index._fetch_s3_image(f"{ns}/_screenshots/../../etc/passwd", ns)  # traversal
                index._fetch_s3_image(f"{ns}/_uploads/not_a_screenshot.png", ns)  # outside prefix
            assert_no_pii(self, cap.text)
            self.assertIn("Rejected S3 screenshot key", cap.text)


class TestSendFailureLogs(unittest.TestCase):
    def test_telegram_send_failure_omits_the_target(self):
        def boom(*a, **k):
            raise RuntimeError("connection refused")
        with patch.object(index.urllib_request, "urlopen", side_effect=boom), CaptureLogs() as cap:
            self.assertFalse(index.send_telegram_message(TG_ID, "hi", "token-abc"))
        assert_no_pii(self, cap.text)
        self.assertIn("Failed to send Telegram message", cap.text)


if __name__ == "__main__":
    unittest.main()
