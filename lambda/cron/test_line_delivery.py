"""Tests for LINE delivery in the Cron Lambda (synthetic IDs only)."""

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

USER_ID = "U" + "0" * 31 + "1"
SECRET = json.dumps({"channelSecret": "synthetic-secret", "channelAccessToken": "synthetic-token"})


class TestCronLineDelivery(unittest.TestCase):
    def setUp(self):
        index.LINE_TOKEN_SECRET_ID = "openclaw/channels/line"

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_pushes_text(self, _s):
        with patch.object(index.line_channel.urllib_request, "urlopen") as mock_open:
            index.deliver_response("line", USER_ID, "Your brief")
        req = mock_open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.line.me/v2/bot/message/push")
        body = json.loads(req.data)
        self.assertEqual((body["to"], body["messages"][0]["text"]), (USER_ID, "Your brief"))

    @patch.object(index, "_get_secret", return_value="disabled-placeholder")
    def test_unconfigured_does_not_send(self, _s):
        with patch.object(index.line_channel.urllib_request, "urlopen") as mock_open:
            index.deliver_response("line", USER_ID, "Your brief")
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
