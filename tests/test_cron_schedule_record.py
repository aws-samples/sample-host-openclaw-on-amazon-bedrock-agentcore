"""The cron executor takes a schedule's delivery target, channel identity and
message from its CRON# record, not from the schedule's input.

The eventbridge-cron skill and the gateway schedules tool write the same values
to both at create and update time, so they only differ when the input was
changed some other way. Records written before these fields existed keep
using the input.
"""

import json
import logging
from unittest.mock import MagicMock

from tests.test_runtime_routing import ACTOR, USER, FakeRuntime, cron_event, cron_table, load_lambda
from tests.test_cron_async import SM_ARN, Context, READY

RECORD = {
    "PK": f"USER#{USER}", "SK": "CRON#schedule123", "scheduleId": "schedule123", "scheduleName": "Desk",
    "channel": "telegram", "channelTarget": "123", "actorId": ACTOR, "channelActorId": ACTOR, "message": "hi",
}


def _lambda(record=RECORD, responses=None, async_on=False):
    module = load_lambda("cron")
    module.identity_table = cron_table()
    if record is not None:
        module.identity_table.items[(f"USER#{USER}", "CRON#schedule123")] = dict(record)
    module.agentcore_client = FakeRuntime(responses if responses is not None else [READY, {"response": "done"}])
    module.CRON_ASYNC_STATE_MACHINE_ARN = SM_ARN if async_on else ""
    module.sfn_client = MagicMock()
    module.deliver_response = MagicMock()
    module.deliver_brief_audio = MagicMock()
    module.find_run_audio_key = MagicMock(return_value=None)
    module.warmup_and_wait.__globals__["WARMUP_POLL_INTERVAL_SECONDS"] = 0
    return module


def _deliveries(module):
    return [call.args[:2] for call in module.deliver_response.call_args_list]


def _messages(module):
    return [json.loads(call["payload"]).get("message") for call in module.agentcore_client.calls]


def test_a_changed_delivery_target_and_message_in_the_input_are_not_used(caplog):
    module = _lambda()
    event = {**cron_event(), "channelTarget": "999", "message": "something else", "scheduleName": "Other"}
    with caplog.at_level(logging.WARNING):
        assert module.handler(event, Context())["statusCode"] == 200
    assert _deliveries(module) == [("telegram", "123")]
    assert _messages(module)[-1] == "[Scheduled task: Desk] hi"
    assert all(json.loads(c["payload"]).get("channelTarget") == "123" for c in module.agentcore_client.calls)
    assert "channelTarget differs from its record" in caplog.text
    # The log line never carries the values themselves.
    assert "999" not in caplog.text and "something else" not in caplog.text


def test_an_input_naming_another_channel_identity_of_the_same_user_is_refused():
    module = _lambda()
    other = "telegram:456"
    module.identity_table.items[(f"CHANNEL#{other}", "PROFILE")] = {
        "PK": f"CHANNEL#{other}", "SK": "PROFILE", "userId": USER,
    }
    event = {**cron_event(), "actorId": other, "channelTarget": "456"}
    assert module.handler(event, Context())["statusCode"] == 403
    assert module.agentcore_client.calls == []
    assert _deliveries(module) == []


def test_a_record_channel_that_does_not_match_the_identity_is_refused():
    module = _lambda({**RECORD, "channel": "slack"})
    assert module.handler(cron_event(), Context())["statusCode"] == 403
    assert module.agentcore_client.calls == []
    assert _deliveries(module) == []


def test_gateway_records_without_channel_actor_id_bind_on_actor_id():
    record = {key: value for key, value in RECORD.items() if key != "channelActorId"}
    module = _lambda(record)
    assert module.handler({**cron_event(), "channelTarget": "999"}, Context())["statusCode"] == 200
    assert _deliveries(module) == [("telegram", "123")]


def test_skill_records_of_a_bound_channel_deliver_to_that_channel():
    # Runtime owner on Telegram, schedule created from a bound Slack identity.
    slack = "slack:U0XXXXXXX"
    module = _lambda({**RECORD, "channel": "slack", "channelTarget": "C0XXXXXXX", "channelActorId": slack})
    module.identity_table.items[(f"CHANNEL#{slack}", "PROFILE")] = {
        "PK": f"CHANNEL#{slack}", "SK": "PROFILE", "userId": USER,
    }
    module.authorize_channel_actor = MagicMock()
    event = {**cron_event(), "actorId": ACTOR, "channelActorId": slack, "channel": "slack",
             "channelTarget": "C0XXXXXXX"}
    assert module.handler(event, Context())["statusCode"] == 200
    assert _deliveries(module) == [("slack", "C0XXXXXXX")]


def test_records_without_the_fields_keep_using_the_input():
    module = _lambda({"PK": f"USER#{USER}", "SK": "CRON#schedule123"})
    assert module.handler(cron_event(), Context())["statusCode"] == 200
    assert _deliveries(module) == [("telegram", "123")]
    assert _messages(module)[-1] == "[Scheduled task: schedule123] hi"


def test_async_steps_deliver_to_the_record_even_if_the_state_was_changed():
    module = _lambda(async_on=True)
    state = {**cron_event(), "channelTarget": "999", "message": "something else", "scheduleName": "Desk",
             "jobId": "schedule123-20261019T084700Z", "cronPhase": "deliver", "outcome": "completed",
             "response": "brief"}
    assert module.handler(state, Context())["next"] == "done"
    assert _deliveries(module) == [("telegram", "123")]
