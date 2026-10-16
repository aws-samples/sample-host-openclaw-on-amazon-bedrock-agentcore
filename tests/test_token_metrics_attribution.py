"""Token metrics Lambda: attribution from Bedrock requestMetadata.

The bridge proxy sets requestMetadata (openclaw.actor_id, openclaw.channel,
openclaw.session_id, openclaw.agent_type, ...) on every Converse /
ConverseStream call, and Bedrock copies it into the invocation log record.
Records written before that, or by another caller, have none and must keep
accumulating exactly as before: default-user / unknown / default-session, on
the old sort key.

    pytest tests/test_token_metrics_attribution.py -v
"""

import base64
import gzip
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from tests.test_token_metrics_totals import FakeTable, _load_module

DAY = "2026-10-16"
SESSION = "ses_user_abc_0123456789ab_cdef0123456789"


@pytest.fixture
def tm():
    mod = _load_module()
    mod.table = FakeTable()
    mod.cloudwatch = MagicMock()
    return mod


def _log(request_metadata=None, inp=1000, out=50, operation="ConverseStream",
         model="global.anthropic.claude-opus-5-5", **extra):
    """A model invocation log record in the shape Bedrock writes (schemaVersion 1.0)."""
    entry = {
        "schemaType": "ModelInvocationLog", "schemaVersion": "1.0",
        "timestamp": f"{DAY}T03:00:01Z", "accountId": "123456789012",
        "identity": {"arn": "arn:aws:sts::123456789012:assumed-role/ExampleRole/BedrockAgentCore-x"},
        "region": "us-west-2", "requestId": "00000000-0000-0000-0000-000000000000",
        "operation": operation, "modelId": model, "inferenceRegion": "us-west-2",
        "input": {"inputContentType": "application/json", "inputTokenCount": inp,
                  "cacheReadInputTokenCount": 0, "cacheWriteInputTokenCount": 0},
        "output": {"outputContentType": "application/json", "outputTokenCount": out},
    }
    if request_metadata is not None:
        entry["requestMetadata"] = request_metadata
    entry.update(extra)
    return entry


def _meta(actor="telegram:1001", channel="telegram", session=SESSION, agent="main", **extra):
    return {
        "openclaw.actor_id": actor, "openclaw.channel": channel,
        "openclaw.session_id": session, "openclaw.agent_type": agent,
        "openclaw.trigger": "cron" if agent == "cron" else "chat",
        "openclaw.identity": "unbound" if actor == "default-user" else "bound",
        "openclaw.id_source": "identity-file", "openclaw.model_route": "primary", **extra,
    }


def _rows(tm):
    return dict(tm.table.items)


def test_request_metadata_attributes_the_row_to_actor_channel_session_and_agent(tm):
    tm.process_log_entry(_log(_meta()))
    key = ("USER#telegram:1001", f"DATE#{DAY}#CHANNEL#telegram#SESSION#{SESSION}#AGENT#main")
    assert list(_rows(tm)) == [key]
    row = tm.table.items[key]
    assert row["actorId"] == "telegram:1001"
    assert row["channel"] == "telegram"
    assert row["sessionId"] == SESSION
    assert row["agentType"] == "main"
    assert row["attribution"] == "request-metadata"
    assert row["GSI1PK"] == "CHANNEL#telegram"
    assert row["inputTokens"] == 1000 and row["invocationCount"] == 1


def test_main_subagent_and_cron_calls_get_separate_rows(tm):
    for agent in ("main", "subagent", "subagent", "cron"):
        tm.process_log_entry(_log(_meta(agent=agent)))
    by_agent = {item["agentType"]: item for item in tm.table.items.values()}
    assert set(by_agent) == {"main", "subagent", "cron"}
    assert by_agent["subagent"]["invocationCount"] == 2
    assert by_agent["main"]["invocationCount"] == 1
    for agent, item in by_agent.items():
        assert item["SK"].endswith(f"#AGENT#{agent}")
        assert item["PK"] == "USER#telegram:1001"


def test_old_record_without_request_metadata_keeps_the_old_row(tm):
    """Pre-change rows keep accumulating: same key, same defaults, no AGENT suffix."""
    old_key = ("USER#default-user", f"DATE#{DAY}#CHANNEL#unknown#SESSION#default-session")
    tm.table.items[old_key] = {"PK": old_key[0], "SK": old_key[1], "inputTokens": 5,
                               "invocationCount": 1, "estimatedCostUSD": Decimal("0.1")}
    tm.process_log_entry(_log(None))
    assert list(_rows(tm)) == [old_key]
    row = tm.table.items[old_key]
    assert row["invocationCount"] == 2
    assert row["inputTokens"] == 1005
    assert row["agentType"] == "unknown"
    assert row["attribution"] == "none"
    assert row["actorId"] == "default-user"


def test_unbound_call_with_metadata_is_explicitly_default_user(tm):
    tm.process_log_entry(_log(_meta(actor="default-user", channel="unknown", session="default-session")))
    (key,) = _rows(tm)
    assert key == ("USER#default-user", f"DATE#{DAY}#CHANNEL#unknown#SESSION#default-session#AGENT#main")
    assert tm.table.items[key]["attribution"] == "request-metadata"


@pytest.mark.parametrize("bad", [
    "telegram:1 2", "x" * 300, "line:\u00fcser", "", None, 42, {"a": 1}, "a\nb",
])
def test_malformed_values_fall_back_instead_of_reaching_keys(tm, bad):
    tm.process_log_entry(_log(_meta(actor=bad, channel=bad, session=bad, agent=bad)))
    (key,) = _rows(tm)
    assert key[0] == "USER#default-user"
    assert key[1] == f"DATE#{DAY}#CHANNEL#unknown#SESSION#default-session"
    assert tm.table.items[key]["agentType"] == "unknown"


def test_unknown_agent_type_keeps_the_attribution_but_not_a_new_row_type(tm):
    tm.process_log_entry(_log(_meta(agent="planner")))
    (key,) = _rows(tm)
    assert key == ("USER#telegram:1001", f"DATE#{DAY}#CHANNEL#telegram#SESSION#{SESSION}")
    assert tm.table.items[key]["agentType"] == "unknown"


def test_request_metadata_wins_over_legacy_agentcore_fields(tm):
    entry = _log(_meta(), sessionId="legacy-session",
                 agentRuntimeMetadata={"actorId": "legacy:actor", "sessionId": "legacy-session"})
    meta = tm.extract_openclaw_metadata(entry)
    assert meta["actor_id"] == "telegram:1001"
    assert meta["session_id"] == SESSION
    assert meta["attribution"] == "request-metadata"


def test_legacy_fields_still_fill_when_there_is_no_request_metadata(tm):
    meta = tm.extract_openclaw_metadata(_log(None, agentRuntimeMetadata={"actorId": "slack:U0XXXXXXX",
                                                                          "sessionId": "legacy-s"}))
    assert (meta["actor_id"], meta["session_id"], meta["agent_type"], meta["attribution"]) == \
        ("slack:U0XXXXXXX", "legacy-s", "unknown", "legacy")


def test_metrics_dimensions_are_unchanged(tm):
    tm.process_log_entry(_log(_meta(agent="subagent")))
    calls = tm.cloudwatch.put_metric_data.call_args_list
    dims = {tuple(d["Name"] for d in m["Dimensions"]) for c in calls for m in c.kwargs["MetricData"]}
    assert dims == {("ActorId", "Channel", "ModelId"), ()}
    actor_dims = [m["Dimensions"] for c in calls for m in c.kwargs["MetricData"] if m["Dimensions"]]
    assert actor_dims[0][0] == {"Name": "ActorId", "Value": "telegram:1001"}


def test_handler_mixes_old_and_new_records_in_one_batch(tm):
    events = [{"id": str(i), "timestamp": 0, "message": json.dumps(e)}
              for i, e in enumerate([_log(None), _log(_meta()), _log(_meta(agent="subagent"))])]
    payload = {"logGroup": "/aws/bedrock/invocation-logs", "logEvents": events}
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    result = tm.handler({"awslogs": {"data": data}}, None)
    assert result == {"processed": 3, "errors": 0, "total": 3}
    assert {item["attribution"] for item in tm.table.items.values()} == {"none", "request-metadata"}
    assert len(tm.table.items) == 3
