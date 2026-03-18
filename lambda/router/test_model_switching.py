"""Tests for per-user runtime model switching via deepthinking/normalmode commands."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# Set required env vars before importing the module
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ.setdefault("DEEPTHINK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")

# Mock boto3 before importing the module
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


class TestDetectModelSwitchCommand(unittest.TestCase):
    """Tests for detect_model_switch_command()."""

    def test_deepthinking_keyword(self):
        result = index.detect_model_switch_command("deepthinking")
        self.assertEqual(result, "deepthink")

    def test_deepthinking_mixed_case(self):
        result = index.detect_model_switch_command("DeepThinking")
        self.assertEqual(result, "deepthink")

    def test_deepthinking_in_sentence(self):
        result = index.detect_model_switch_command("switch to deepthinking mode")
        self.assertEqual(result, "deepthink")

    def test_normalmode_keyword(self):
        result = index.detect_model_switch_command("normalmode")
        self.assertEqual(result, "normal")

    def test_normalthinking_keyword(self):
        result = index.detect_model_switch_command("normalthinking")
        self.assertEqual(result, "normal")

    def test_normalmode_mixed_case(self):
        result = index.detect_model_switch_command("NormalMode")
        self.assertEqual(result, "normal")

    def test_slash_model_opus(self):
        result = index.detect_model_switch_command("/model opus")
        self.assertEqual(result, "deepthink")

    def test_slash_model_default(self):
        result = index.detect_model_switch_command("/model default")
        self.assertEqual(result, "normal")

    def test_normal_message_returns_none(self):
        result = index.detect_model_switch_command("hello how are you")
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = index.detect_model_switch_command("")
        self.assertIsNone(result)

    def test_none_returns_none(self):
        result = index.detect_model_switch_command(None)
        self.assertIsNone(result)

    def test_feature_disabled_when_no_deepthink_model(self):
        """When DEEPTHINK_MODEL_ID is empty, command detection returns None."""
        original = index.DEEPTHINK_MODEL_ID
        try:
            index.DEEPTHINK_MODEL_ID = ""
            result = index.detect_model_switch_command("deepthinking")
            self.assertIsNone(result)
        finally:
            index.DEEPTHINK_MODEL_ID = original


class TestHandleModelSwitchCommand(unittest.TestCase):
    """Tests for handle_model_switch_command() DynamoDB persistence."""

    def setUp(self):
        self.original_deepthink = index.DEEPTHINK_MODEL_ID
        index.DEEPTHINK_MODEL_ID = "global.anthropic.claude-opus-4-6-v1"

    def tearDown(self):
        index.DEEPTHINK_MODEL_ID = self.original_deepthink

    @patch.object(index, "identity_table")
    def test_deepthink_sets_model_override(self, mock_table):
        """deepthink command sets modelOverride on USER# PROFILE."""
        index.handle_model_switch_command("deepthink", "user_abc123")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        self.assertEqual(call_kwargs["Key"], {"PK": "USER#user_abc123", "SK": "PROFILE"})
        self.assertIn(":model", call_kwargs["ExpressionAttributeValues"])
        self.assertEqual(
            call_kwargs["ExpressionAttributeValues"][":model"],
            "global.anthropic.claude-opus-4-6-v1",
        )

    @patch.object(index, "identity_table")
    def test_normal_removes_model_override(self, mock_table):
        """normalmode command removes modelOverride from USER# PROFILE."""
        index.handle_model_switch_command("normal", "user_abc123")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        self.assertEqual(call_kwargs["Key"], {"PK": "USER#user_abc123", "SK": "PROFILE"})
        self.assertIn("REMOVE", call_kwargs["UpdateExpression"])


class TestGetModelOverride(unittest.TestCase):
    """Tests for get_model_override() reading from DynamoDB."""

    @patch.object(index, "identity_table")
    def test_returns_model_override_when_set(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "USER#user_abc123",
                "SK": "PROFILE",
                "userId": "user_abc123",
                "modelOverride": "global.anthropic.claude-opus-4-6-v1",
            }
        }
        result = index.get_model_override("user_abc123")
        self.assertEqual(result, "global.anthropic.claude-opus-4-6-v1")

    @patch.object(index, "identity_table")
    def test_returns_empty_when_no_override(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "USER#user_abc123",
                "SK": "PROFILE",
                "userId": "user_abc123",
            }
        }
        result = index.get_model_override("user_abc123")
        self.assertEqual(result, "")

    @patch.object(index, "identity_table")
    def test_returns_empty_when_no_item(self, mock_table):
        mock_table.get_item.return_value = {}
        result = index.get_model_override("user_abc123")
        self.assertEqual(result, "")


class TestInvokeAgentRuntimeWithModelOverride(unittest.TestCase):
    """Tests that invoke_agent_runtime passes modelOverride in payload."""

    @patch.object(index, "agentcore_client")
    def test_model_override_in_payload(self, mock_client):
        """modelOverride is included in the AgentCore invocation payload."""
        mock_response = MagicMock()
        mock_response.get.side_effect = lambda k, d=None: {
            "statusCode": 200,
            "response": MagicMock(read=MagicMock(return_value=b'{"response":"ok"}')),
        }.get(k, d)
        mock_client.invoke_agent_runtime.return_value = mock_response

        index.invoke_agent_runtime(
            "ses_test", "user_abc", "telegram:123", "telegram", "hello",
            model_override="global.anthropic.claude-opus-4-6-v1",
        )

        call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
        payload = json.loads(call_kwargs["payload"])
        self.assertEqual(payload["modelOverride"], "global.anthropic.claude-opus-4-6-v1")

    @patch.object(index, "agentcore_client")
    def test_no_model_override_when_empty(self, mock_client):
        """modelOverride is omitted from payload when empty."""
        mock_response = MagicMock()
        mock_response.get.side_effect = lambda k, d=None: {
            "statusCode": 200,
            "response": MagicMock(read=MagicMock(return_value=b'{"response":"ok"}')),
        }.get(k, d)
        mock_client.invoke_agent_runtime.return_value = mock_response

        index.invoke_agent_runtime(
            "ses_test", "user_abc", "telegram:123", "telegram", "hello",
        )

        call_kwargs = mock_client.invoke_agent_runtime.call_args[1]
        payload = json.loads(call_kwargs["payload"])
        self.assertNotIn("modelOverride", payload)


if __name__ == "__main__":
    unittest.main()
