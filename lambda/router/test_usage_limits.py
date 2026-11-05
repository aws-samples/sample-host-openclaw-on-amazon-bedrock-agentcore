"""Per-user daily cost limit (per_user_daily_cost_usd) and /usage in the Router Lambda.

The token-metrics Lambda keeps one item per internal user and UTC day in the
identity table (USAGE#<user ID> / DAY#<day>); USAGE#<user ID> / CAP is an
operator override. invoke_agent_runtime reads them before every turn and
refuses a user at or over the limit without calling AgentCore. /usage is
answered by the Router on every channel and never reaches the agent.

All IDs below are synthetic.
"""

import contextlib
import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
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

index = importlib.import_module("index")
import usage_limits  # noqa: E402  (the module index uses)

USER = "user_0123456789abcdef"
GROUP_USER = "user_fedcba9876543210"
TG_ID = 987654321
GROUP_ID = -1001234567890
DAY = "2026-11-05"
# 6 h 12 min before 00:00 UTC.
NOW = datetime(2026, 11, 5, 17, 48, tzinfo=timezone.utc)

CAPPED = ("You have reached today's usage limit (about $5.03 of $5.00). It resets at 00:00 UTC, "
          "in 6 h 12 min. Send /usage to see today's usage.")
GROUP_CAPPED = CAPPED.replace("You have", "This conversation has")
REPORT = (
    "Usage today (UTC 2026-11-05), estimated:\n"
    "- Cost: $1.24 of your $5.00 daily limit (25%)\n"
    "- Model calls: 37, of which 4 from scheduled tasks\n"
    "- Tokens: 400,100 in, 12,245 out\n"
    "Resets at 00:00 UTC, in 6 h 12 min. New usage can take a few minutes to appear."
)
GROUP_REPORT = (
    "Usage today for this conversation (UTC 2026-11-05), estimated:\n"
    "- Cost: $1.24 of this conversation's $5.00 daily limit (25%)\n"
    "- Model calls: 37, of which 4 from scheduled tasks\n"
    "- Tokens: 400,100 in, 12,245 out\n"
    "Resets at 00:00 UTC, in 6 h 12 min. New usage can take a few minutes to appear."
)
UNLIMITED_REPORT = REPORT.replace("of your $5.00 daily limit (25%)", "(no daily limit)")


class UsageTable:
    """The identity table as the usage check sees it."""

    def __init__(self):
        self.items = {}
        self.queries = []
        self.error = None

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.error:
            raise self.error
        assert kwargs["KeyConditionExpression"] == "PK = :pk AND SK BETWEEN :cap AND :day"
        # Eventually consistent: a cost guard, unlike the allowlist reads.
        assert "ConsistentRead" not in kwargs
        values = kwargs["ExpressionAttributeValues"]
        return {"Items": [dict(item) for (pk, sk), item in sorted(self.items.items())
                          if pk == values[":pk"] and values[":cap"] <= sk <= values[":day"]]}

    def get_item(self, **kwargs):
        return {}

    def put_item(self, **kwargs):
        return {}

    def update_item(self, **kwargs):
        raise AssertionError("the Router never writes usage items")


def day_item(cost, user=USER, day=DAY, **counts):
    return {"PK": f"USAGE#{user}", "SK": f"DAY#{day}", "costUsd": Decimal(cost),
            **{name: Decimal(value) for name, value in counts.items()}}


def cap_item(value, user=USER):
    return {"PK": f"USAGE#{user}", "SK": "CAP", "dailyCostUsd": value, "setAt": "2026-11-01T00:00:00Z"}


REPORT_COUNTS = {"calls": 37, "cronCalls": 4, "inputTokens": 400100, "outputTokens": 12245}


def _route(actor_id):
    route = MagicMock(actor_id=actor_id, runtime_arn="arn", endpoint_name="release_1",
                      session_id="ses_1", generation=1)
    route.payload_context.return_value = {"runtimeSessionId": "ses_1", "routingGeneration": 1}
    route.invocation.return_value = {"agentRuntimeArn": "arn", "qualifier": "release_1",
                                     "runtimeSessionId": "ses_1"}
    return route


class UsageCase(unittest.TestCase):
    """A 5 USD default limit, a fixed clock and an AgentCore stand-in."""

    def setUp(self):
        self.table = UsageTable()
        self.runtime = MagicMock()
        self.runtime.invoke_agent_runtime.return_value = {
            "statusCode": 200, "response": b'{"response": "agent reply"}'}
        self.stub(index, "identity_table", self.table)
        self.stub(index, "agentcore_client", self.runtime)
        self.stub(index, "PER_USER_DAILY_COST_USD", Decimal(5))
        self.authorize = self.stub(index, "authorize_channel_actor", MagicMock())
        self.stub(index, "admit_route", MagicMock(return_value=contextlib.nullcontext(MagicMock())))
        self.stub(index, "is_unknown_outcome", MagicMock(return_value=False))
        self.stub(usage_limits, "utc_now", MagicMock(return_value=NOW))

    def stub(self, target, name, value):
        patcher = patch.object(target, name, value)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def store(self, *items):
        for item in items:
            self.table.items[(item["PK"], item["SK"])] = dict(item)

    def invoke(self, user=USER, channel="telegram"):
        return index.invoke_agent_runtime(_route(f"{channel}:{TG_ID}"), user, f"{channel}:{TG_ID}", channel,
                                          "hello there", channel_target=TG_ID)


class TestTurnCheck(UsageCase):
    def test_limit_off_reads_nothing(self):
        self.store(day_item("50"), cap_item(Decimal(2)))
        with patch.object(index, "PER_USER_DAILY_COST_USD", Decimal(0)):
            self.assertEqual(self.invoke(), {"response": "agent reply"})
        self.assertEqual(self.table.queries, [])
        self.runtime.invoke_agent_runtime.assert_called_once()

    def test_under_the_limit_runs_the_turn(self):
        self.store(day_item("4.99"))
        self.assertEqual(self.invoke(), {"response": "agent reply"})
        (query,) = self.table.queries
        self.assertEqual(query["ExpressionAttributeValues"],
                         {":pk": f"USAGE#{USER}", ":cap": "CAP", ":day": f"DAY#{DAY}"})
        self.runtime.invoke_agent_runtime.assert_called_once()

    def test_no_usage_item_counts_as_no_usage(self):
        self.assertEqual(self.invoke(), {"response": "agent reply"})

    def test_at_or_over_the_limit_no_agentcore_call(self):
        for cost in ("5", "5.00000001", "5.03", "120"):
            self.table.items.clear()
            self.store(day_item(cost))
            result = self.invoke()
            self.assertEqual(result["status"], "capped", cost)
            self.assertIn("It resets at 00:00 UTC, in 6 h 12 min.", result["response"])
        self.runtime.invoke_agent_runtime.assert_not_called()
        self.authorize.assert_not_called()
        index.admit_route.assert_not_called()

    def test_refusal_text(self):
        self.store(day_item("5.03"))
        self.assertEqual(self.invoke(), {"response": CAPPED, "status": "capped"})

    def test_a_conversation_identity_is_named_as_such(self):
        self.store(day_item("5.03", user=GROUP_USER))
        for channel in ("telegram_group", "slack_channel", "feishu_group"):
            self.assertEqual(self.invoke(GROUP_USER, channel)["response"], GROUP_CAPPED, channel)

    def test_only_today_counts(self):
        self.store(day_item("50", day="2026-11-04"), day_item("50", day="2026-11-03"), day_item("1"))
        self.assertEqual(self.invoke(), {"response": "agent reply"})

    def test_the_limit_resets_at_midnight_utc(self):
        self.store(day_item("50"))
        usage_limits.utc_now.return_value = datetime(2026, 11, 5, 23, 59, 30, tzinfo=timezone.utc)
        result = self.invoke()
        self.assertEqual(result["status"], "capped")
        self.assertIn("It resets at 00:00 UTC, in 1 min.", result["response"])
        self.assertEqual(self.table.queries[-1]["ExpressionAttributeValues"][":day"], f"DAY#{DAY}")
        usage_limits.utc_now.return_value = datetime(2026, 11, 6, 0, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(self.invoke(), {"response": "agent reply"})
        self.assertEqual(self.table.queries[-1]["ExpressionAttributeValues"][":day"], "DAY#2026-11-06")

    def test_override_zero_exempts_the_user(self):
        self.store(day_item("50"), cap_item(Decimal(0)))
        self.assertEqual(self.invoke(), {"response": "agent reply"})

    def test_override_replaces_the_default(self):
        self.store(day_item("2.50"), cap_item(Decimal(2)))
        self.assertIn("(about $2.50 of $2.00)", self.invoke()["response"])
        self.store(day_item("7"), cap_item(Decimal(10)))
        self.assertEqual(self.invoke(), {"response": "agent reply"})
        self.runtime.invoke_agent_runtime.assert_called_once()

    def test_malformed_override_is_ignored_with_a_warning(self):
        for bad in ("2", Decimal(-1), Decimal("10000.01"), True, None, {"usd": 2}):
            self.table.items.clear()
            self.store(day_item("4"), cap_item(bad))
            with self.assertLogs(level="WARNING") as logs:
                self.assertEqual(self.invoke(), {"response": "agent reply"}, bad)
            self.assertIn(f"Ignoring a malformed usage limit override for user {USER}", "\n".join(logs.output))
            self.store(day_item("6"))
            with self.assertLogs(level="WARNING"):
                self.assertIn("of $5.00)", self.invoke()["response"], bad)

    def test_read_failure_runs_the_turn_and_warns(self):
        self.table.error = RuntimeError("ProvisionedThroughputExceededException: slow down")
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.invoke(), {"response": "agent reply"})
        self.assertIn(f"Usage read failed for user {USER}: RuntimeError", "\n".join(logs.output))
        self.assertNotIn("slow down", "\n".join(logs.output))

    def test_refusal_log_names_the_user_id_only(self):
        self.store(day_item("9"))
        with self.assertLogs(level="DEBUG") as logs:
            self.invoke()
        text = "\n".join(logs.output)
        self.assertIn(f"Rejecting turn: channel=telegram reason=usage-limit user={USER}", text)
        for secret in (str(TG_ID), "hello there"):
            self.assertNotIn(secret, text)


class TestUsageText(UsageCase):
    def report(self, user=USER, conversation=False):
        return index.handle_usage_command(user, conversation=conversation)

    def test_report_with_a_limit(self):
        self.store(day_item("1.24", **REPORT_COUNTS))
        self.assertEqual(self.report(), REPORT)

    def test_report_for_a_conversation(self):
        self.store(day_item("1.24", user=GROUP_USER, **REPORT_COUNTS))
        self.assertEqual(self.report(GROUP_USER, conversation=True), GROUP_REPORT)

    def test_report_without_a_limit(self):
        self.store(day_item("1.24", **REPORT_COUNTS))
        with patch.object(index, "PER_USER_DAILY_COST_USD", Decimal(0)):
            self.assertEqual(self.report(), UNLIMITED_REPORT)
        self.assertEqual(len(self.table.queries), 1)  # /usage reads even with the limit off

    def test_report_of_an_exempt_user(self):
        self.store(day_item("1.24", **REPORT_COUNTS), cap_item(Decimal(0)))
        self.assertEqual(self.report(), UNLIMITED_REPORT)

    def test_report_with_an_override(self):
        self.store(day_item("1.24", **REPORT_COUNTS), cap_item(Decimal("2.5")))
        self.assertIn("- Cost: $1.24 of your $2.50 daily limit (50%)", self.report())

    def test_report_over_the_limit(self):
        self.store(day_item("7", calls=3))
        self.assertIn("- Cost: $7.00 of your $5.00 daily limit (140%)", self.report())

    def test_report_of_a_new_day(self):
        self.assertEqual(self.report(), (
            "Usage today (UTC 2026-11-05), estimated:\n"
            "- Cost: $0.00 of your $5.00 daily limit (0%)\n"
            "- Model calls: 0\n"
            "- Tokens: 0 in, 0 out\n"
            "Resets at 00:00 UTC, in 6 h 12 min. New usage can take a few minutes to appear."
        ))

    def test_report_when_the_read_fails(self):
        self.table.error = RuntimeError("throttled")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.report(), "Sorry, I couldn't read today's usage. Please try again later.")

    def test_time_to_reset(self):
        # Rounded up to a whole minute, so it never reads "0 min".
        cases = {
            (17, 48, 0): "6 h 12 min", (23, 59, 30): "1 min", (23, 59, 59): "1 min", (23, 1, 0): "59 min",
            (23, 0, 1): "1 h", (22, 59, 1): "1 h 1 min", (18, 0, 0): "6 h", (0, 0, 0): "24 h",
            (12, 30, 0): "11 h 30 min",
        }
        for (hour, minute, second), expected in cases.items():
            now = datetime(2026, 11, 5, hour, minute, second, tzinfo=timezone.utc)
            self.assertEqual(usage_limits.time_to_reset(now), expected, (hour, minute, second))

    def test_parse_limit(self):
        for raw, expected in ((None, 0), ("", 0), ("0", 0), ("5", 5), ("2.5", Decimal("2.5"))):
            self.assertEqual(usage_limits.parse_limit(raw), expected, raw)
        for raw in ("abc", "-1", "nan", "inf", "10001"):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(usage_limits.parse_limit(raw), 0, raw)


class TestParseUsageCommand(unittest.TestCase):
    def test_commands(self):
        for text in ("/usage", "  /usage  ", "/USAGE", "/usage@OpenClawBot", " /usage", "/usage today",
                     "/usage@OpenClawBot please"):
            self.assertTrue(index._is_usage_command(text), text)

    def test_ordinary_messages_are_not_commands(self):
        for text in ("", None, "usage", "/usages", "/usage@", "please /usage", "what is my /usage?", "/model",
                     "/usagetoday"):
            self.assertFalse(index._is_usage_command(text), text)


USAGE_TEXT = {"telegram_group": "/usage@OpenClawBot", "slack": " /usage"}
HELLO_TEXT = {"telegram_group": "@OpenClawBot hello there"}


class TestChannelHandlers(UsageCase):
    """/usage and the refusal on every channel, Telegram groups and channel-identity conversations included."""

    def run_with(self, patches, call):
        with contextlib.ExitStack() as stack:
            for target, name, value in patches:
                stack.enter_context(patch.object(target, name, value))
            call()

    def telegram(self, text):
        sent = []
        self.run_with([
            (index, "resolve_user", MagicMock(return_value=(USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route(f"telegram:{TG_ID}"))),
            (index, "_get_telegram_token", MagicMock(return_value="123456:tg-token")),
            (index, "send_telegram_typing", MagicMock()), (index, "_periodic_typing", MagicMock()),
            (index, "send_telegram_message", MagicMock(side_effect=lambda c, t, k, **kw: sent.append(t))),
            (index, "_send_telegram_chunks", MagicMock(side_effect=lambda c, t, k, **kw: sent.append(t))),
        ], lambda: index.handle_telegram({"message": {
            "message_id": 5, "text": text, "chat": {"id": TG_ID, "type": "private"},
            "from": {"id": TG_ID, "first_name": "Ann"}}}))
        return sent

    def telegram_group(self, text):
        sent = []
        self.threads = []

        def record(c, t, k, **kw):
            sent.append(t)
            self.threads.append(kw.get("reply_to"))

        self.run_with([
            (index, "TELEGRAM_GROUPS_ENABLED", True),
            (index, "_get_telegram_token", MagicMock(return_value="123456:tg-token")),
            (index, "_telegram_bot_username", MagicMock(return_value="OpenClawBot")),
            (index, "_is_telegram_group_allowed", MagicMock(return_value=True)),
            (index, "_is_disabled_telegram_sender", MagicMock(return_value=False)),
            (index, "resolve_user", MagicMock(return_value=(GROUP_USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route(f"telegram_group:{GROUP_ID}"))),
            (index, "send_telegram_typing", MagicMock()), (index, "_periodic_typing", MagicMock()),
            (index, "send_telegram_message", MagicMock(side_effect=record)),
            (index, "_send_telegram_chunks", MagicMock(side_effect=record)),
        ], lambda: index.handle_telegram({"message": {
            "message_id": 7, "text": text, "chat": {"id": GROUP_ID, "type": "supergroup", "title": "Team"},
            "from": {"id": TG_ID, "first_name": "Ann"}}}))
        return sent

    def slack(self, text):
        sent = []
        self.run_with([
            (index, "resolve_user", MagicMock(return_value=(USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route("slack:U0SYNTH01"))),
            (index, "_get_slack_tokens", MagicMock(return_value=("xoxb-test", "secret"))),
            (index, "_slack_progress_notify", MagicMock()),
            (index, "send_slack_message", MagicMock(side_effect=lambda c, t, k, **kw: sent.append(t))),
        ], lambda: index.handle_slack({"type": "event_callback", "event": {
            "type": "message", "channel_type": "im", "user": "U0SYNTH01", "channel": "D0SYNTH01",
            "text": text, "ts": "1700000000.000100"}}))
        return sent

    def slack_channel(self, text):
        sent = []
        self.run_with([
            (index, "CHANNEL_IDENTITY_MODE", "channel"),
            (index, "_allow_record_exists", MagicMock(return_value=True)),
            (index, "_is_disabled_sender", MagicMock(return_value=False)),
            (index, "_slack_member_name", MagicMock(return_value="Ann")),
            (index, "resolve_user", MagicMock(return_value=(GROUP_USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route("slack_channel:C0SYNTH01"))),
            (index, "_get_slack_tokens", MagicMock(return_value=("xoxb-test", "secret"))),
            (index, "_slack_progress_notify", MagicMock()),
            (index, "send_slack_message", MagicMock(side_effect=lambda c, t, k, **kw: sent.append(t))),
        ], lambda: index.handle_slack({"type": "event_callback", "event": {
            "type": "app_mention", "user": "U0SYNTH01", "channel": "C0SYNTH01",
            "text": f"<@U0BOT0001> {text}", "ts": "1700000000.000200"}}))
        return sent

    def feishu(self, text, chat_type="p2p", chat_id="oc_synth01"):
        sent = []
        self.run_with([
            (index, "CHANNEL_IDENTITY_MODE", "channel"),
            (index, "_allow_record_exists", MagicMock(return_value=True)),
            (index, "_feishu_group_status", MagicMock(return_value="internal")),
            (index, "_is_disabled_sender", MagicMock(return_value=False)),
            (index, "_feishu_member_name", MagicMock(return_value="Lin")),
            (index, "resolve_user", MagicMock(
                return_value=(USER if chat_type == "p2p" else GROUP_USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route(f"feishu:{chat_id}"))),
            (index, "_feishu_progress_notify", MagicMock()),
            (index, "send_feishu_message", MagicMock(side_effect=lambda c, t: sent.append(t))),
        ], lambda: index.handle_feishu({"header": {"event_type": "im.message.receive_v1"}, "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_synth01"}},
            "message": {"chat_id": chat_id, "message_type": "text", "chat_type": chat_type,
                        "content": json.dumps({"text": text})}}}))
        return sent

    def feishu_group(self, text):
        return self.feishu(text, chat_type="group", chat_id="oc_synthgroup01")

    def whatsapp(self, text):
        sent = []
        self.run_with([
            (index, "resolve_user", MagicMock(return_value=(USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route("whatsapp:15550001111"))),
            (index, "_whatsapp_progress_notify", MagicMock()),
            (index.whatsapp, "send_text", MagicMock(side_effect=lambda c, to, t: sent.append(t))),
        ], lambda: index._handle_whatsapp_message({}, {
            "from": "15550001111", "text": text, "image_id": None, "name": "Ann", "id": "wamid.SYNTH1"}))
        return sent

    def line(self, text):
        sent = []
        self.tokens = []

        def record(config, to, message, token):
            sent.append(message)
            self.tokens.append(token)

        self.run_with([
            (index, "resolve_user", MagicMock(return_value=(USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route("line:U0synth"))),
            (index.line_channel, "show_loading", MagicMock()),
            (index.line_channel, "send_text", MagicMock(side_effect=record)),
        ], lambda: index._handle_line_event({}, {
            "event_id": "01SYNTHEVENT", "user_id": "U" + "0" * 32, "reply_token": "synthreplytoken",
            "type": "text", "text": text, "message_id": "", "redelivery": False}))
        return sent

    def discord(self, text):
        sent = []
        self.run_with([
            (index, "resolve_user", MagicMock(return_value=(USER, False))),
            (index, "get_or_create_session", MagicMock(return_value=_route("discord:100000000000000001"))),
        ], lambda: index._handle_discord_command(
            {"user_id": "100000000000000001", "text": text, "attachment": None, "name": "Ann"}, sent.append))
        return sent

    def channels(self):
        """(name, send, whether the identity is a conversation)."""
        return (("telegram", self.telegram, False), ("telegram_group", self.telegram_group, True),
                ("slack", self.slack, False), ("slack_channel", self.slack_channel, True),
                ("feishu", self.feishu, False), ("feishu_group", self.feishu_group, True),
                ("whatsapp", self.whatsapp, False), ("line", self.line, False),
                ("discord", self.discord, False))

    def test_usage_on_every_channel_is_answered_without_a_turn(self):
        self.store(day_item("1.24", **REPORT_COUNTS), day_item("1.24", user=GROUP_USER, **REPORT_COUNTS))
        for name, send, conversation in self.channels():
            self.table.queries.clear()
            self.assertEqual(send(USAGE_TEXT.get(name, "/usage")), [GROUP_REPORT if conversation else REPORT], name)
            (query,) = self.table.queries
            owner = GROUP_USER if conversation else USER
            self.assertEqual(query["ExpressionAttributeValues"][":pk"], f"USAGE#{owner}", name)
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_usage_is_answered_while_capped(self):
        self.store(day_item("7"), day_item("7", user=GROUP_USER))
        for name, send, _ in self.channels():
            (reply,) = send(USAGE_TEXT.get(name, "/usage"))
            self.assertIn("$7.00 of", reply, name)
            self.assertIn("daily limit (140%)", reply, name)
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_capped_turn_on_every_channel_gets_the_notice(self):
        self.store(day_item("5.03"), day_item("5.03", user=GROUP_USER))
        for name, send, conversation in self.channels():
            self.assertEqual(send(HELLO_TEXT.get(name, "hello there")), [GROUP_CAPPED if conversation else CAPPED],
                             name)
        self.runtime.invoke_agent_runtime.assert_not_called()
        index.admit_route.assert_not_called()  # no admission is taken for a refused turn

    def test_turn_under_the_limit_runs_on_every_channel(self):
        self.store(day_item("1"), day_item("1", user=GROUP_USER))
        for name, send, _ in self.channels():
            self.assertEqual(send(HELLO_TEXT.get(name, "hello there")), ["agent reply"], name)
        self.assertEqual(self.runtime.invoke_agent_runtime.call_count, len(self.channels()))

    def test_limit_off_turns_read_nothing_on_any_channel(self):
        self.store(day_item("50"), day_item("50", user=GROUP_USER))
        with patch.object(index, "PER_USER_DAILY_COST_USD", Decimal(0)):
            for name, send, _ in self.channels():
                self.assertEqual(send(HELLO_TEXT.get(name, "hello there")), ["agent reply"], name)
        self.assertEqual(self.table.queries, [])

    def test_group_replies_are_threaded_and_a_bare_command_is_ignored(self):
        self.store(day_item("1.24", user=GROUP_USER, **REPORT_COUNTS))
        self.assertEqual(self.telegram_group("/usage@OpenClawBot"), [GROUP_REPORT])
        self.assertEqual(self.threads, [7])
        # Not addressed to the bot: no reply and no read.
        self.table.queries.clear()
        self.assertEqual(self.telegram_group("/usage"), [])
        self.assertEqual(self.table.queries, [])

    def test_line_uses_the_reply_token(self):
        self.store(day_item("1.24", **REPORT_COUNTS))
        self.assertEqual(self.line("/usage"), [REPORT])
        self.assertEqual(self.tokens, ["synthreplytoken"])

    def test_capped_turn_logs_no_message_text_or_platform_id(self):
        self.store(day_item("5.03"))
        with self.assertLogs(level="DEBUG") as logs:
            self.telegram("hello there")
        text = "\n".join(logs.output)
        self.assertIn("reason=usage-limit", text)
        for secret in (str(TG_ID), "hello there"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
