"""Tests for Slack delivery in the Cron Lambda (synthetic IDs and tokens only)."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

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

BOT_TOKEN = "xoxb-synthetic-token"
CHANNEL = "D0SYNTHETIC1"


class TestCronSlackDelivery(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(index, "_get_slack_tokens", return_value=(BOT_TOKEN, "signing"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deliver(self, text, response):
        with patch.object(index.urllib_request, "urlopen") as urlopen:
            urlopen.return_value.read.return_value = response
            index.deliver_response("slack", CHANNEL, text)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {BOT_TOKEN}")
        return json.loads(req.data)

    def test_brief_is_sent_as_mrkdwn_with_model_mentions_escaped(self):
        with self.assertNoLogs(index.logger, level="WARNING"):
            payload = self._deliver("## Brief\n**Rain** <!here> [map](https://example.com/m)", b'{"ok": true}')
        self.assertEqual(payload, {"channel": CHANNEL,
                                   "text": "*Brief*\n*Rain* &lt;!here&gt; <https://example.com/m|map>"})

    def test_refused_brief_is_logged_with_slack_error_code_only(self):
        with self.assertLogs(index.logger, level="ERROR") as logs:
            self._deliver("Secret morning brief", b'{"ok": false, "error": "channel_not_found", "warning": "w"}')
        output = "\n".join(logs.output)
        self.assertIn("Slack refused the message: error=channel_not_found", output)
        self.assertIn("Cron response to Slack was not delivered", output)
        for leaked in ("Secret morning brief", BOT_TOKEN):
            self.assertNotIn(leaked, output)

    def test_missing_token_is_reported_as_not_delivered(self):
        with patch.object(index, "_get_slack_tokens", return_value=("", "")), \
                patch.object(index.urllib_request, "urlopen") as urlopen, \
                self.assertLogs(index.logger, level="ERROR") as logs:
            index.deliver_response("slack", CHANNEL, "brief")
        urlopen.assert_not_called()
        self.assertIn("Cron response to Slack was not delivered", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
