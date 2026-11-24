"""Scheduled-task delivery checks and the fire-rate guard in the Cron Lambda.

- Schedule records: SCHED#<userId>/CRON#<scheduleId> is read first and wins
  over USER#<userId>/CRON#<scheduleId>. The USER# record is read while
  cron_legacy_records is "read" (the default), and not with "off".
- Delivery target: on Telegram, WhatsApp, LINE and Discord a schedule whose
  stored target is not its owner's own chat does not run, and the owner gets
  one notice in their own chat per schedule (cron_target_check "refuse", the
  default). "log" delivers it with a warning. Slack and Feishu deliver a
  different target and log it.
- Fire-rate guard: a schedule runs at most once per 270 s (lastFiredAt on
  SCHED#<userId>/FIRE#<scheduleId>). A failed write lets the fire run.
- Order: verification, then the guard, then the usage limit, then the
  warm-up (synchronous path) or start_execution (async path). The async
  steps verify again but never run the guard.
- None of the new log lines matches the scheduled-task alarms.

Fakes only: boto3 is a MagicMock, the identity table an in-memory dict that
evaluates the conditional writes used here, and the warm-up, the AgentCore
call, the channel sends and Step Functions are recorders. All IDs are
synthetic placeholders.
"""

import contextlib
import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
index = importlib.import_module("index")

ROOT = Path(__file__).resolve().parents[2]
USER = "user_abcdefabcdefabcd"  # no digits, so a chat id in a log line can't come from it
SCHED = "0a1b2c3d"
NOW = 1_800_000_000
EIGHT_DAYS = 8 * 24 * 3600
SM_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:openclaw-cron-runner"
NOTICE = ("[Scheduled: Brief] This scheduled task is paused because it was set to deliver to a "
          "different chat. Create it again from the chat that should receive it.")
REFUSED = {"statusCode": 403, "body": "Delivery target is not the owner's chat"}
SKIPPED = {"statusCode": 200, "body": "Skipped: it ran less than 5 minutes ago"}
TARGET_WARNING = "delivers to a target that is not the actor's own chat id"
# channel -> (actor, the actor's own chat, another chat on the same channel)
ONE_TO_ONE = {
    "telegram": ("telegram:123456", "123456", "-1009876543210"),
    "whatsapp": ("whatsapp:15550000001", "15550000001", "15550000002"),
    "line": ("line:U00000000000000000000000000000001", "U00000000000000000000000000000001",
             "U00000000000000000000000000000002"),
    "discord": ("discord:100000000000000001", "100000000000000001", "100000000000000002"),
}
# channel -> (actor, the conversation the schedule delivers to)
CONVERSATION = {
    "slack": ("slack:U0000000001", "C0000000001"),
    "feishu": ("feishu:ou_0000000001", "oc_0000000001"),
}
TELEGRAM, OWN, OTHER = ONE_TO_ONE["telegram"]


class FakeClientError(Exception):
    """Stands in for botocore's ClientError: the code is in .response like a real one."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _condition_holds(expression, item, names, values):
    """The two condition forms the checks write: attribute_not_exists(#a) [OR #a <= :v]."""
    match = re.fullmatch(r"attribute_not_exists\((#\w+)\)(?: OR \1 <= (:\w+))?", expression)
    assert match, f"unexpected condition: {expression}"
    name = names[match.group(1)]
    if name not in item:
        return True
    return match.group(2) is not None and item[name] <= values[match.group(2)]


class FakeTable:
    """GetItem, and UpdateItem with SET/REMOVE and the conditions above (it creates a missing item)."""

    def __init__(self, items, log):
        self.items = {(item["PK"], item["SK"]): dict(item) for item in items}
        self.log = log
        self.reads = []
        self.read_error = None  # (PK prefix, exception)
        self.update_error = None

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        self.reads.append((Key["PK"], Key["SK"], ConsistentRead))
        if self.read_error and Key["PK"].startswith(self.read_error[0]):
            raise self.read_error[1]
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression,  # noqa: N803
                    ExpressionAttributeNames, ExpressionAttributeValues):
        self.log.append(("update", Key["PK"], Key["SK"]))
        if self.update_error:
            raise self.update_error
        key = (Key["PK"], Key["SK"])
        item = self.items.get(key, {})
        if not _condition_holds(ConditionExpression, item, ExpressionAttributeNames, ExpressionAttributeValues):
            raise FakeClientError("ConditionalCheckFailedException")
        updated = {**item, "PK": Key["PK"], "SK": Key["SK"]}
        assert UpdateExpression.startswith("SET "), UpdateExpression
        assignments, _, removals = UpdateExpression[len("SET "):].partition(" REMOVE ")
        for assignment in assignments.split(", "):
            alias, placeholder = assignment.split(" = ")
            updated[ExpressionAttributeNames[alias]] = ExpressionAttributeValues[placeholder]
        for alias in filter(None, removals.split(", ")):
            updated.pop(ExpressionAttributeNames[alias], None)
        self.items[key] = updated
        return {}

    def fire_item(self, user=USER, schedule=SCHED):
        return self.items.get((f"SCHED#{user}", f"FIRE#{schedule}"))

    def record_reads(self):
        return [(pk, sk) for pk, sk, _ in self.reads if sk.startswith("CRON#")]


def identity(actor, user=USER):
    return [
        {"PK": f"CHANNEL#{actor}", "SK": "PROFILE", "userId": user},
        {"PK": f"ACTOR#{actor}", "SK": "OWNER", "userId": user, "actorId": actor, "platformVersion": "V2",
         "ownerGeneration": 1, "runtimeActorId": actor},
        {"PK": f"USER#{user}", "SK": "PROFILE"},
    ]


def record(actor, target, pk=f"USER#{USER}", **extra):
    item = {"PK": pk, "SK": f"CRON#{SCHED}", "scheduleId": SCHED, "channelActorId": actor, "actorId": actor,
            "channel": actor.split(":", 1)[0], "channelTarget": target, "message": "Daily summary",
            "scheduleName": "Brief"}
    item.update(extra)
    return {key: value for key, value in item.items() if value is not None}


def event(actor, target):
    return {"userId": USER, "channelActorId": actor, "actorId": actor, "channel": actor.split(":", 1)[0],
            "channelTarget": target, "message": "Daily summary", "scheduleId": SCHED, "scheduleName": "Brief"}


@contextlib.contextmanager
def cron_lambda(items, *, async_on=False, target_check="refuse", legacy_records="read"):
    """The Cron Lambda with fakes around it. calls records the order of checks, runs and sends."""
    env = SimpleNamespace(calls=[], deliveries=[], turns=[], sfn=MagicMock())
    env.table = FakeTable(items, env.calls)
    route = SimpleNamespace(actor_id=None, namespace="ns_abcdefghijklmnopqrstuvwxyz", payload_context=dict)

    def warmup(*args, **kwargs):
        env.calls.append("warmup")
        return True

    def invoke(route, action, user_id, actor_id, channel, message=None, **kwargs):
        env.calls.append(action)
        env.turns.append((action, message))
        return {"response": "agent output"}

    def deliver(channel, target, text):
        env.calls.append("deliver")
        env.deliveries.append((channel, target, text))

    def usage(job):
        env.calls.append("usage")
        return None

    env.sfn.start_execution.side_effect = lambda **kwargs: env.calls.append("start_execution") or {}
    values = {
        "identity_table": env.table, "get_or_create_session": lambda user_id: route,
        "authorize_channel_actor": MagicMock(), "warmup_and_wait": warmup, "invoke_agentcore": invoke,
        "deliver_response": deliver, "skip_over_usage_limit": usage,
        "find_run_audio_key": lambda *args, **kwargs: None, "sfn_client": env.sfn,
        "CRON_ASYNC_STATE_MACHINE_ARN": SM_ARN if async_on else "",
        "CRON_TARGET_CHECK": target_check, "CRON_LEGACY_RECORDS": legacy_records,
    }
    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(patch.object(index, name, value))
        yield env


def fire(payload, at=NOW):
    """One schedule invocation at epoch second `at`."""
    with patch.object(index.time, "time", return_value=float(at)):
        return index.handler(dict(payload), None)


class ScheduleRecordSource(unittest.TestCase):
    def test_the_sched_record_wins_when_both_exist(self):
        items = identity(TELEGRAM) + [
            record(TELEGRAM, OWN, pk=f"SCHED#{USER}", message="From the SCHED record"),
            record(TELEGRAM, OWN, message="From the USER record"),
        ]
        with cron_lambda(items) as env, self.assertLogs(index.logger, level="INFO") as logs:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
        self.assertEqual(env.table.record_reads(), [(f"SCHED#{USER}", f"CRON#{SCHED}")])
        self.assertEqual(env.turns[-1], ("cron", "[Scheduled task: Brief] From the SCHED record"))
        self.assertNotIn("legacy", "\n".join(logs.output))

    def test_a_record_under_sched_only_is_read_and_runs(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN, pk=f"SCHED#{USER}")]
        with cron_lambda(items) as env:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
        self.assertEqual(env.deliveries, [("telegram", OWN, "agent output")])

    def test_the_legacy_record_is_used_when_there_is_no_sched_record(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN)]
        with cron_lambda(items) as env, self.assertLogs(index.logger, level="INFO") as logs:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
        self.assertEqual(env.table.record_reads(), [(f"SCHED#{USER}", f"CRON#{SCHED}"),
                                                    (f"USER#{USER}", f"CRON#{SCHED}")])
        self.assertTrue(all(consistent for _, sk, consistent in env.table.reads if sk.startswith("CRON#")))
        self.assertIn(f"Scheduled task {SCHED} uses its legacy USER# record", "\n".join(logs.output))
        self.assertEqual(env.deliveries, [("telegram", OWN, "agent output")])

    def test_with_legacy_records_off_only_the_sched_record_is_read(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN)]
        with cron_lambda(items, legacy_records="off") as env:
            self.assertEqual(fire(event(TELEGRAM, OWN)),
                             {"statusCode": 403, "body": "Schedule ownership verification failed"})
        self.assertEqual(env.table.record_reads(), [(f"SCHED#{USER}", f"CRON#{SCHED}")])
        self.assertEqual(env.calls, [])
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN, pk=f"SCHED#{USER}")]
        with cron_lambda(items, legacy_records="off") as env:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)

    def test_a_failed_sched_read_does_not_fall_back_to_the_legacy_record(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN)]
        with cron_lambda(items) as env:
            env.table.read_error = ("SCHED#", FakeClientError("ProvisionedThroughputExceededException"))
            self.assertEqual(fire(event(TELEGRAM, OWN)),
                             {"statusCode": 500, "body": "Schedule ownership verification error"})
        self.assertEqual(env.table.record_reads(), [(f"SCHED#{USER}", f"CRON#{SCHED}")])
        self.assertEqual(env.calls, [])


class DeliveryTargetCheck(unittest.TestCase):
    def test_a_different_target_is_refused_on_one_to_one_channels_with_one_notice(self):
        for channel, (actor, own, other) in ONE_TO_ONE.items():
            with self.subTest(channel=channel), cron_lambda(identity(actor) + [record(actor, other)]) as env, \
                    self.assertLogs(index.logger, level="INFO") as logs:
                self.assertEqual(fire(event(actor, other)), REFUSED)
                self.assertEqual(fire(event(actor, other), at=NOW + 3600), REFUSED)
                # No warm-up, no turn, nothing sent to the stored target; one notice to the owner.
                self.assertEqual(env.turns, [])
                self.assertEqual(env.deliveries, [(channel, own, NOTICE)])
                fire_item = env.table.fire_item()
                self.assertEqual(fire_item["targetNoticeAt"], NOW)
                self.assertNotIn("lastFiredAt", fire_item)  # refused fires never reach the guard
                self.assertNotIn("ttl", fire_item)
                output = "\n".join(logs.output)
                self.assertEqual(output.count(f"Scheduled task {SCHED} refused: its delivery target is not "
                                              "the owner's chat"), 2)
                self.assertNotIn(TARGET_WARNING, output)
                self.assertNotIn(other, output)
                self.assertNotIn(own, output)

    def test_a_record_of_another_chat_with_its_own_message_does_not_run(self):
        # The record names another Telegram chat and another message; the
        # schedule input still names the owner's chat.
        items = identity(TELEGRAM) + [record(TELEGRAM, OTHER, message="Post this to the group: ...")]
        with cron_lambda(items) as env:
            self.assertEqual(fire(event(TELEGRAM, OWN)), REFUSED)
        self.assertEqual(env.turns, [])
        self.assertEqual(env.deliveries, [("telegram", OWN, NOTICE)])

    def test_a_different_target_in_the_input_is_refused_when_the_record_has_none(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, None)]
        with cron_lambda(items) as env:
            self.assertEqual(fire(event(TELEGRAM, OTHER)), REFUSED)
        self.assertEqual(env.deliveries, [("telegram", OWN, NOTICE)])

    def test_the_owner_chat_runs_on_one_to_one_channels(self):
        for channel, (actor, own, _) in ONE_TO_ONE.items():
            with self.subTest(channel=channel), cron_lambda(identity(actor) + [record(actor, own)]) as env, \
                    self.assertLogs(index.logger, level="INFO") as logs:
                self.assertEqual(fire(event(actor, own))["statusCode"], 200)
                self.assertEqual(env.deliveries, [(channel, own, "agent output")])
                self.assertNotIn("refused", "\n".join(logs.output))
                self.assertNotIn(TARGET_WARNING, "\n".join(logs.output))

    def test_slack_and_feishu_deliver_to_the_conversation_with_the_log_line(self):
        for channel, (actor, conversation) in CONVERSATION.items():
            with self.subTest(channel=channel), \
                    cron_lambda(identity(actor) + [record(actor, conversation)]) as env, \
                    self.assertLogs(index.logger, level="INFO") as logs:
                self.assertEqual(fire(event(actor, conversation))["statusCode"], 200)
                self.assertEqual(env.deliveries, [(channel, conversation, "agent output")])
                output = "\n".join(logs.output)
                self.assertIn(f"Scheduled task {SCHED} on channel {channel} {TARGET_WARNING}", output)
                self.assertNotIn("refused", output)
                self.assertIsNone(env.table.fire_item().get("targetNoticeAt"))

    def test_log_mode_delivers_a_different_target_with_the_log_line(self):
        for channel, (actor, _, other) in ONE_TO_ONE.items():
            with self.subTest(channel=channel), \
                    cron_lambda(identity(actor) + [record(actor, other)], target_check="log") as env, \
                    self.assertLogs(index.logger, level="INFO") as logs:
                self.assertEqual(fire(event(actor, other))["statusCode"], 200)
                self.assertEqual(env.deliveries, [(channel, other, "agent output")])
                self.assertIn(TARGET_WARNING, "\n".join(logs.output))
                self.assertIsNone(env.table.fire_item().get("targetNoticeAt"))

    def test_the_notice_mark_has_no_expiry_until_the_schedule_runs_again(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN)]
        stored = (f"USER#{USER}", f"CRON#{SCHED}")
        with cron_lambda(items) as env:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
            self.assertEqual(env.table.fire_item()["ttl"], NOW + EIGHT_DAYS)
            env.table.items[stored]["channelTarget"] = OTHER
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 3600), REFUSED)
            self.assertNotIn("ttl", env.table.fire_item())  # kept while the schedule is refused
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 7200), REFUSED)
            env.table.items[stored]["channelTarget"] = OWN
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 10800)["statusCode"], 200)
            self.assertEqual(env.table.fire_item()["ttl"], NOW + 10800 + EIGHT_DAYS)
        self.assertEqual([d for d in env.deliveries if d[2] == NOTICE], [("telegram", OWN, NOTICE)])
        self.assertEqual(len(env.deliveries), 3)  # two runs and one notice

    def test_a_failed_notice_write_sends_no_notice_and_still_refuses(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OTHER)]) as env, \
                self.assertLogs(index.logger, level="INFO") as logs:
            env.table.update_error = FakeClientError("ProvisionedThroughputExceededException")
            self.assertEqual(fire(event(TELEGRAM, OTHER)), REFUSED)
        self.assertEqual(env.deliveries, [])
        self.assertEqual(env.turns, [])
        self.assertIn(f"Scheduled task {SCHED} notice record write failed: FakeClientError",
                      "\n".join(logs.output))

    def test_no_notice_when_the_route_is_unavailable(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OTHER)]) as env, \
                patch.object(index, "get_or_create_session",
                             MagicMock(side_effect=index.RouteUnavailable("User is administratively disabled"))):
            self.assertEqual(fire(event(TELEGRAM, OTHER)), {"statusCode": 503, "body": "Runtime route unavailable"})
        self.assertEqual(env.deliveries, [])
        self.assertIsNone(env.table.fire_item())

    def test_async_launch_of_a_different_target_starts_no_execution(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OTHER)], async_on=True) as env:
            self.assertEqual(fire(event(TELEGRAM, OTHER)), REFUSED)
            self.assertEqual(fire(event(TELEGRAM, OTHER), at=NOW + 3600), REFUSED)
        env.sfn.start_execution.assert_not_called()
        self.assertEqual(env.deliveries, [("telegram", OWN, NOTICE)])

    def test_an_async_step_does_not_deliver_to_a_target_changed_during_the_run(self):
        items = identity(TELEGRAM) + [record(TELEGRAM, OTHER)]
        state = {**event(TELEGRAM, OWN), "jobId": "job-1", "cronPhase": "deliver", "outcome": "completed",
                 "response": "the brief"}
        with cron_lambda(items, async_on=True) as env:
            self.assertEqual(fire(state)["next"], "done")
        self.assertEqual(env.deliveries, [("telegram", OWN, NOTICE)])


class FireRateGuard(unittest.TestCase):
    def test_a_second_fire_within_270_s_is_skipped(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)]) as env, \
                self.assertLogs(index.logger, level="INFO") as logs:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 269), SKIPPED)
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 1), SKIPPED)  # a repeated invocation
        self.assertEqual([action for action, _ in env.turns], ["cron"])
        self.assertEqual(env.deliveries, [("telegram", OWN, "agent output")])
        self.assertEqual(env.table.fire_item()["lastFiredAt"], NOW)
        self.assertEqual(env.table.fire_item()["ttl"], NOW + EIGHT_DAYS)
        self.assertEqual("\n".join(logs.output).count(
            f"Scheduled task {SCHED} skipped: it ran less than 5 minutes ago"), 2)

    def test_a_fire_300_s_later_runs(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)]) as env:
            for at in (NOW, NOW + 300):
                self.assertEqual(fire(event(TELEGRAM, OWN), at=at)["statusCode"], 200)
        self.assertEqual(len(env.deliveries), 2)
        self.assertEqual(env.table.fire_item()["lastFiredAt"], NOW + 300)
        self.assertEqual(env.table.fire_item()["ttl"], NOW + 300 + EIGHT_DAYS)

    def test_a_fire_exactly_270_s_later_runs(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)]) as env:
            for at in (NOW, NOW + 270):
                self.assertEqual(fire(event(TELEGRAM, OWN), at=at)["statusCode"], 200)
        self.assertEqual(len(env.deliveries), 2)

    def test_a_schedule_firing_every_minute_runs_every_5_minutes(self):
        # Neither expression nor enabled is read from the record; the guard bounds the rate.
        items = identity(TELEGRAM) + [record(TELEGRAM, OWN, expression="rate(1 minute)", enabled=False)]
        with cron_lambda(items) as env:
            results = [fire(event(TELEGRAM, OWN), at=NOW + 60 * minute) for minute in range(11)]
        ran = [minute for minute, result in enumerate(results) if result["statusCode"] == 200
               and result != SKIPPED]
        self.assertEqual(ran, [0, 5, 10])
        self.assertEqual(len(env.deliveries), 3)

    def test_a_dynamodb_error_lets_the_fire_run(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)]) as env, \
                self.assertLogs(index.logger, level="INFO") as logs:
            env.table.update_error = FakeClientError("ProvisionedThroughputExceededException")
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 200)
        self.assertEqual(env.deliveries, [("telegram", OWN, "agent output")])
        self.assertIn(f"Scheduled task {SCHED} fire record write failed: FakeClientError", "\n".join(logs.output))

    def test_the_guard_runs_after_verification_and_before_the_usage_limit(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)]) as env:
            fire(event(TELEGRAM, OWN))
            self.assertEqual(env.calls, [("update", f"SCHED#{USER}", f"FIRE#{SCHED}"), "usage", "warmup",
                                         "cron", "deliver"])
            env.calls.clear()
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 60), SKIPPED)
            self.assertEqual(env.calls, [("update", f"SCHED#{USER}", f"FIRE#{SCHED}")])
        with cron_lambda(identity(TELEGRAM)) as env:  # no record: refused before the guard
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 403)
            self.assertEqual(env.calls, [])

    def test_async_launch_second_fire_within_270_s_starts_no_execution(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)], async_on=True) as env:
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 202)
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 60), SKIPPED)
            self.assertEqual(env.sfn.start_execution.call_count, 1)
            self.assertEqual(fire(event(TELEGRAM, OWN), at=NOW + 300)["statusCode"], 202)
            self.assertEqual(env.sfn.start_execution.call_count, 2)
            self.assertEqual(env.calls[:3], [("update", f"SCHED#{USER}", f"FIRE#{SCHED}"), "usage",
                                             "start_execution"])
        self.assertEqual(env.turns, [])

    def test_async_launch_with_a_dynamodb_error_starts_the_execution(self):
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)], async_on=True) as env:
            env.table.update_error = FakeClientError("InternalServerError")
            self.assertEqual(fire(event(TELEGRAM, OWN))["statusCode"], 202)
        env.sfn.start_execution.assert_called_once()

    def test_async_steps_are_not_rate_limited(self):
        # The state machine polls every 60 s; those steps verify but never touch FIRE#.
        state = {**event(TELEGRAM, OWN), "jobId": "job-1"}
        with cron_lambda(identity(TELEGRAM) + [record(TELEGRAM, OWN)], async_on=True) as env:
            for at, phase in ((NOW, "start"), (NOW + 60, "poll"), (NOW + 120, "poll")):
                fire({**state, "cronPhase": phase}, at=at)
        self.assertEqual([action for action, _ in env.turns], ["cron_start", "cron_status", "cron_status"])
        self.assertIsNone(env.table.fire_item())


def cron_alarm_terms():
    """The terms of the alarms on the Cron Lambda's log group, read from stacks/health_alarms.py."""
    source = (ROOT / "stacks" / "health_alarms.py").read_text()
    terms = []
    for args in re.findall(r'\(\s*"\w+",\s*cron_group,\s*logs\.FilterPattern\.any_term\(([^)]*)\)', source):
        terms += re.findall(r'"([^"]+)"', args)
    return terms


class AlarmTerms(unittest.TestCase):
    def test_the_alarm_terms_are_found(self):
        terms = cron_alarm_terms()
        self.assertIn("did not complete", terms)
        self.assertIn("could not start runtime session", terms)

    def test_no_new_log_line_or_notice_matches_the_scheduled_task_alarms(self):
        legacy, refused = identity(TELEGRAM) + [record(TELEGRAM, OWN)], identity(TELEGRAM) + [record(TELEGRAM, OTHER)]
        with self.assertLogs(index.logger, level="DEBUG") as logs:
            with cron_lambda(legacy):
                fire(event(TELEGRAM, OWN))                    # legacy record
                fire(event(TELEGRAM, OWN), at=NOW + 10)       # skipped by the guard
            with cron_lambda(legacy) as env:
                env.table.update_error = FakeClientError("InternalServerError")
                fire(event(TELEGRAM, OWN))                    # guard write failed
            with cron_lambda(refused):
                fire(event(TELEGRAM, OTHER))                  # refused, notice sent
            with cron_lambda(refused) as env:
                env.table.update_error = FakeClientError("InternalServerError")
                fire(event(TELEGRAM, OTHER))                  # refused, notice write failed
        output = "\n".join(logs.output)
        for line in (f"Scheduled task {SCHED} uses its legacy USER# record",
                     f"Scheduled task {SCHED} skipped: it ran less than 5 minutes ago",
                     f"Scheduled task {SCHED} fire record write failed: FakeClientError",
                     f"Scheduled task {SCHED} refused: its delivery target is not the owner's chat",
                     f"Scheduled task {SCHED} notice record write failed: FakeClientError"):
            self.assertIn(line, output)
        for term in cron_alarm_terms():
            self.assertNotIn(term, output, term)
            for text in (NOTICE, REFUSED["body"], SKIPPED["body"]):
                self.assertNotIn(term, text, term)


if __name__ == "__main__":
    unittest.main()
