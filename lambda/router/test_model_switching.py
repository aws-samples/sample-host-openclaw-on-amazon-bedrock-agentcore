"""Tests for per-user model switching (/model) in the Router Lambda."""

import contextlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")

MODELS = {
    "haiku": "global.anthropic.claude-haiku-5-5",
    "sonnet": "global.anthropic.claude-sonnet-5-5",
    "opus": "global.anthropic.claude-opus-5-5",
}
DEFAULT = "global.anthropic.claude-haiku-5-5"
USER = "user_abc123"
PROFILE_KEY = {"PK": f"USER#{USER}", "SK": "PROFILE"}


def _route(actor_id):
    return SimpleNamespace(platform_version="V2", user_id=USER, actor_id=actor_id)


class ModelSwitchingCase(unittest.TestCase):
    """Enable the feature with the shipped allowlist and a fake identity table."""

    def setUp(self):
        self.table = MagicMock()
        self.table.get_item.return_value = {}
        for target, value in (("USER_SELECTABLE_MODELS", dict(MODELS)), ("DEFAULT_MODEL_ID", DEFAULT),
                              ("identity_table", self.table)):
            patcher = patch.object(index, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self, model_id):
        self.table.get_item.return_value = {"Item": {"modelOverride": model_id}}


class TestAllowlistParsing(unittest.TestCase):
    def test_valid_map_is_lowercased(self):
        self.assertEqual(index._load_user_selectable_models('{"Sonnet":"global.anthropic.claude-sonnet-5-5"}'),
                         {"sonnet": "global.anthropic.claude-sonnet-5-5"})

    def test_empty_or_missing_disables(self):
        for raw in ("", None, "{}"):
            self.assertEqual(index._load_user_selectable_models(raw), {})

    def test_malformed_config_disables(self):
        for raw in ("not json", "[]", '{"opus": 5}', '{"": "x"}', '{"opus": ""}'):
            self.assertEqual(index._load_user_selectable_models(raw), {}, raw)


class TestParseModelCommand(unittest.TestCase):
    def test_commands(self):
        cases = {
            "/model": "", "  /model  ": "", "/model sonnet": "sonnet", "/MODEL Opus": "Opus",
            "/model default": "default", "/model@OpenClawBot haiku": "haiku",
            " /model sonnet": "sonnet",  # Slack: leading space sends it as a message
            "/model sonnet please": "sonnet please",
        }
        for text, expected in cases.items():
            self.assertEqual(index._parse_model_command(text), expected, text)

    def test_ordinary_messages_are_not_commands(self):
        for text in ("", None, "model sonnet", "deepthinking", "normalmode", "/models", "/modelsonnet",
                     "please /model opus", "what /model are you?", "/start", "link"):
            self.assertIsNone(index._parse_model_command(text), text)


class TestOverridePersistence(ModelSwitchingCase):
    def test_set_writes_resolved_model_id_on_existing_profile(self):
        reply = index.handle_model_command(USER, "Sonnet")
        kwargs = self.table.update_item.call_args.kwargs
        self.assertEqual(kwargs["Key"], PROFILE_KEY)
        self.assertEqual(kwargs["ExpressionAttributeValues"][":model"], MODELS["sonnet"])
        self.assertEqual(kwargs["ConditionExpression"], "attribute_exists(PK)")
        self.assertIn("SET modelOverride = :model", kwargs["UpdateExpression"])
        self.assertIn("switched to `sonnet`", reply)
        self.assertIn(MODELS["sonnet"], reply)

    def test_default_clears_override(self):
        reply = index.handle_model_command(USER, "default")
        kwargs = self.table.update_item.call_args.kwargs
        self.assertEqual(kwargs["Key"], PROFILE_KEY)
        self.assertEqual(kwargs["UpdateExpression"], "REMOVE modelOverride, modelOverrideUpdatedAt")
        self.assertNotIn("ExpressionAttributeValues", kwargs)
        self.assertIn("reset to default", reply)
        self.assertIn(DEFAULT, reply)

    def test_unknown_alias_is_rejected_without_writing(self):
        for argument in ("gpt", "global.anthropic.claude-fable-5-1", "sonnet please", "../opus"):
            reply = index.handle_model_command(USER, argument)
            self.assertTrue(reply.startswith("Unknown model."), argument)
            self.assertNotIn(argument, reply, "user input is not echoed")
        self.table.update_item.assert_not_called()

    def test_raw_model_id_is_not_accepted_as_alias(self):
        index.handle_model_command(USER, MODELS["opus"])
        self.table.update_item.assert_not_called()

    def test_write_failure_is_reported(self):
        self.table.update_item.side_effect = RuntimeError("ConditionalCheckFailed")
        self.assertIn("couldn't switch", index.handle_model_command(USER, "opus"))
        self.assertIn("couldn't reset", index.handle_model_command(USER, "default"))

    def test_bare_command_shows_current_and_available(self):
        self.stored(MODELS["opus"])
        reply = index.handle_model_command(USER, "")
        self.assertIn("Current model: `opus` (`global.anthropic.claude-opus-5-5`)", reply)
        self.assertIn("`haiku`, `sonnet`, `opus`, `default`", reply)
        self.table.update_item.assert_not_called()
        self.assertEqual(self.table.get_item.call_args.kwargs["Key"], PROFILE_KEY)
        self.assertTrue(self.table.get_item.call_args.kwargs["ConsistentRead"])

    def test_bare_command_without_override_shows_default(self):
        self.assertIn(f"Current model: default (`{DEFAULT}`)", index.handle_model_command(USER, ""))

    def test_plain_format_for_feishu(self):
        reply = index.handle_model_command(USER, "", code=False)
        self.assertNotIn("`", reply)
        self.assertIn("haiku, sonnet, opus, default", reply)


class TestStaleOverride(ModelSwitchingCase):
    def test_allowlisted_override_is_returned(self):
        self.stored(MODELS["sonnet"])
        self.assertEqual(index.get_model_override(USER), MODELS["sonnet"])

    def test_override_removed_from_allowlist_falls_back_to_default(self):
        self.stored("global.anthropic.claude-sonnet-4-6")
        self.assertIsNone(index.get_model_override(USER))
        self.assertIn(f"Current model: default (`{DEFAULT}`)", index.handle_model_command(USER, ""))

    def test_non_string_or_missing_override_is_ignored(self):
        for item in ({}, {"modelOverride": 7}, {"modelOverride": ""}):
            self.table.get_item.return_value = {"Item": item}
            self.assertIsNone(index.get_model_override(USER))

    def test_lookup_failure_uses_default(self):
        self.table.get_item.side_effect = RuntimeError("throttled")
        self.assertIsNone(index.get_model_override(USER))

    def test_disabled_feature_never_reads_profile(self):
        with patch.object(index, "USER_SELECTABLE_MODELS", {}):
            self.stored(MODELS["opus"])
            self.assertIsNone(index.get_model_override(USER))
            self.assertEqual(index.handle_model_command(USER, "opus"),
                             "Model switching is not enabled on this bot.")
        self.table.get_item.assert_not_called()
        self.table.update_item.assert_not_called()


class TestPayloadForwarding(ModelSwitchingCase):
    def invoke(self, **kwargs):
        route = MagicMock(actor_id="telegram:123", runtime_arn="arn", endpoint_name="release_1",
                          session_id="ses_1", generation=1)
        route.payload_context.return_value = {"runtimeSessionId": "ses_1", "routingGeneration": 1}
        route.invocation.return_value = {"agentRuntimeArn": "arn", "qualifier": "release_1",
                                         "runtimeSessionId": "ses_1"}
        client = MagicMock()
        client.invoke_agent_runtime.return_value = {"statusCode": 200, "response": b'{"response": "ok"}'}
        with patch.object(index, "agentcore_client", client), \
                patch.object(index, "authorize_channel_actor"), \
                patch.object(index, "admit_route", return_value=contextlib.nullcontext(MagicMock())), \
                patch.object(index, "is_unknown_outcome", return_value=False):
            result = index.invoke_agent_runtime(route, USER, "telegram:123", "telegram", "hi",
                                                channel_target=123, **kwargs)
        self.assertEqual(result, {"response": "ok"})
        return json.loads(client.invoke_agent_runtime.call_args.kwargs["payload"])

    def test_override_is_sent_in_payload(self):
        payload = self.invoke(model_override=MODELS["opus"])
        self.assertEqual(payload["modelOverride"], MODELS["opus"])
        self.assertEqual(payload["action"], "chat")
        self.assertEqual(payload["routingGeneration"], 1)

    def test_payload_is_unchanged_without_override(self):
        payload = self.invoke()
        self.assertNotIn("modelOverride", payload)
        self.assertEqual(set(payload), {"action", "userId", "actorId", "channelActorId", "channel",
                                        "channelTarget", "message", "runtimeSessionId", "routingGeneration"})


class TestChannelHandlers(ModelSwitchingCase):
    """/model replies directly and never reaches AgentCore; normal turns carry the override."""

    def setUp(self):
        super().setUp()
        for target, kwargs in (
            ("resolve_user", {"return_value": (USER, False)}),
            ("authorize_channel_actor", {}),
            ("_get_telegram_token", {"return_value": "tg-token"}),
            ("_get_slack_tokens", {"return_value": ("xoxb-test", "secret")}),
            ("send_telegram_typing", {}),
            ("_periodic_typing", {}),
            ("_slack_progress_notify", {}),
            ("_feishu_progress_notify", {}),
        ):
            patcher = patch.object(index, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.invoke = patch.object(index, "invoke_agent_runtime", return_value={"response": "agent reply"}).start()
        self.addCleanup(patch.stopall)

    def telegram(self, text):
        sent = []
        with patch.object(index, "get_or_create_session", return_value=_route("telegram:42")), \
                patch.object(index, "send_telegram_message", side_effect=lambda c, t, k: sent.append(t)), \
                patch.object(index, "_send_telegram_chunks", side_effect=lambda c, t, k: sent.append(t)):
            index.handle_telegram({"message": {"text": text, "chat": {"id": 42},
                                               "from": {"id": 42, "first_name": "Ann"}}})
        return sent

    def slack(self, text):
        sent = []
        with patch.object(index, "get_or_create_session", return_value=_route("slack:U1")), \
                patch.object(index, "send_slack_message", side_effect=lambda c, t, k: sent.append(t)):
            index.handle_slack({"type": "event_callback", "event": {
                "type": "message", "user": "U1", "channel": "D1", "text": text, "ts": "1.1"}})
        return sent

    def feishu(self, text):
        sent = []
        with patch.object(index, "get_or_create_session", return_value=_route("feishu:ou_1")), \
                patch.object(index, "send_feishu_message", side_effect=lambda c, t: sent.append(t)):
            index.handle_feishu({"header": {"event_type": "im.message.receive_v1"}, "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_1"}},
                "message": {"chat_id": "oc_1", "message_type": "text", "chat_type": "p2p",
                            "content": json.dumps({"text": text})}}})
        return sent

    def test_set_command_in_every_channel_is_not_forwarded(self):
        for send, text in ((self.telegram, "/model opus"), (self.slack, "<@UBOT> /model opus"),
                           (self.feishu, "/model opus")):
            self.table.update_item.reset_mock()
            replies = send(text)
            self.assertEqual(len(replies), 1, text)
            self.assertIn("switched to", replies[0])
            self.assertEqual(self.table.update_item.call_args.kwargs["ExpressionAttributeValues"][":model"],
                             MODELS["opus"])
        self.invoke.assert_not_called()

    def test_status_and_clear_commands_are_not_forwarded(self):
        for send in (self.telegram, self.slack, self.feishu):
            self.assertIn("Current model:", send("/model")[0])
            self.assertIn("reset to default", send("/model default")[0])
        self.invoke.assert_not_called()

    def test_normal_message_forwards_stored_override(self):
        self.stored(MODELS["sonnet"])
        for send, channel in ((self.telegram, "telegram"), (self.slack, "slack"), (self.feishu, "feishu")):
            self.invoke.reset_mock()
            self.assertEqual(send("hello there")[-1], "agent reply")
            args, kwargs = self.invoke.call_args
            self.assertEqual(args[3], channel)
            self.assertEqual(kwargs["model_override"], MODELS["sonnet"])
        self.table.update_item.assert_not_called()

    def test_normal_message_with_stale_override_uses_default(self):
        self.stored("global.anthropic.claude-opus-4-6")
        self.telegram("hello")
        self.assertIsNone(self.invoke.call_args.kwargs["model_override"])

    def test_keyword_text_is_forwarded_as_ordinary_message(self):
        for text in ("deepthinking", "model opus", "which /model are you using?"):
            self.invoke.reset_mock()
            self.telegram(text)
            self.assertEqual(self.invoke.call_args.args[4], text)
        self.table.update_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
