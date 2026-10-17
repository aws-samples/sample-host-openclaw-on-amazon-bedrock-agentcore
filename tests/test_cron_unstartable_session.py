"""V1 cron Lambda: a runtime session that AgentCore can no longer start is replaced.

Seen on 16 Oct 2026 (Sydney): one user's runtime session storage started
returning EREMOTEIO, and from then on every start of that session failed with
AgentCore's "Runtime initialization time exceeded" (RuntimeClientError) or a
500 after ~2 minutes, while a new session started normally. The Lambda logged
"Unexpected warmup status: failed — proceeding", sent the cron turn to the same
session, and every brief ended with "Your scheduled task did not return a
result this time." The Lambda now moves the user to a new session and runs the
brief there. All IDs are placeholders.
"""

import io
import json
import logging
from unittest.mock import patch

from botocore.exceptions import ClientError

from tests.cron_v1_helpers import USER, FakeTable, cron_event, load_cron_lambda

OLD = "ses_" + "s" * 40
NOTICE = (
    "[Scheduled: Morning brief] Your scheduled task did not return a result this time. "
    "It will run again at the next scheduled time."
)


def init_exceeded():
    return ClientError({"Error": {"Code": "RuntimeClientError", "Message":
                                  "Runtime initialization time exceeded. Please make sure that "
                                  "initialization completes in 120s."}}, "InvokeAgentRuntime")


def server_error():
    return ClientError({"Error": {"Code": "InternalServerException",
                                  "Message": "An unexpected error occurred while processing the request."}},
                       "InvokeAgentRuntime")


class SessionTable(FakeTable):
    """FakeTable whose SESSION update honours ConditionExpression sessionId = :old."""

    def __init__(self, replaced_by=None):
        super().__init__()
        self.replaced_by = replaced_by  # another Lambda wins the race with this session id
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if kwargs.get("ConditionExpression"):
            values = kwargs["ExpressionAttributeValues"]
            key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
            if self.replaced_by:
                self.items[key]["sessionId"] = self.replaced_by
            if self.items[key]["sessionId"] != values[":old"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                                  "UpdateItem")
            self.items[key]["sessionId"] = values[":new"]
        return {}

    @property
    def session_id(self):
        return self.items[(f"USER#{USER}", "SESSION")]["sessionId"]


class SessionRuntime:
    """The old session never starts (per-session queue of failures); any other session works."""

    def __init__(self, old_failures, brief="Here is your brief."):
        self.old_failures = list(old_failures)
        self.brief = brief
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        action = json.loads(kwargs["payload"])["action"]
        self.calls.append((kwargs["runtimeSessionId"], action))
        if kwargs["runtimeSessionId"] == OLD:
            raise (self.old_failures.pop(0) if len(self.old_failures) > 1 else self.old_failures[0])
        body = {"status": "ready"} if action == "warmup" else {"response": self.brief}
        return {"response": io.BytesIO(json.dumps(body).encode())}


def _run(module):
    with patch.object(module, "deliver_response") as deliver, patch.object(module.time, "sleep"):
        result = module.handler(cron_event(), None)
    return result, deliver


def test_brief_runs_on_a_new_session_when_the_old_one_cannot_start():
    module = load_cron_lambda()
    module.identity_table = SessionTable()
    module.agentcore_client = SessionRuntime([init_exceeded()])
    result, deliver = _run(module)
    assert result["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "Here is your brief.")
    new = module.identity_table.session_id
    assert new != OLD and new.startswith(f"ses_{USER}_") and len(new) >= 33
    assert module.agentcore_client.calls == [(OLD, "warmup"), (new, "warmup"), (new, "cron")]


def test_a_500_before_the_start_limit_error_is_retried_then_the_session_replaced():
    """22:30Z run: the first warmup got a 500 after 121 s, the next call the start-limit error."""
    module = load_cron_lambda()
    module.identity_table = SessionTable()
    module.agentcore_client = SessionRuntime([server_error(), init_exceeded()])
    result, deliver = _run(module)
    assert result["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "Here is your brief.")
    new = module.identity_table.session_id
    assert module.agentcore_client.calls == [(OLD, "warmup"), (OLD, "warmup"), (new, "warmup"), (new, "cron")]


def test_two_briefs_failing_together_share_one_new_session():
    """EN and CN briefs fire 2 minutes apart; the second must not replace the first's new session."""
    module = load_cron_lambda()
    other = "ses_" + "n" * 40
    module.identity_table = SessionTable(replaced_by=other)
    module.agentcore_client = SessionRuntime([init_exceeded()])
    result, deliver = _run(module)
    assert result["statusCode"] == 200
    assert module.identity_table.session_id == other
    assert module.agentcore_client.calls[1:] == [(other, "warmup"), (other, "cron")]


def test_other_invocation_failures_do_not_replace_the_session():
    module = load_cron_lambda()
    module.identity_table = SessionTable()
    module.agentcore_client = SessionRuntime([server_error()])
    result, deliver = _run(module)
    assert result["statusCode"] == 503
    deliver.assert_called_once_with("telegram", "123", NOTICE)
    assert module.identity_table.session_id == OLD
    assert not [u for u in module.identity_table.updates if u.get("ConditionExpression")]


def test_a_new_session_that_cannot_start_either_gets_the_start_notice(caplog):
    module = load_cron_lambda()
    module.identity_table = SessionTable()

    class NothingStarts(SessionRuntime):
        def invoke_agent_runtime(self, **kwargs):
            self.calls.append((kwargs["runtimeSessionId"], json.loads(kwargs["payload"])["action"]))
            raise init_exceeded()

    module.agentcore_client = NothingStarts([init_exceeded()])
    with caplog.at_level(logging.WARNING):
        result, deliver = _run(module)
    assert result["statusCode"] == 503
    assert [a for _, a in module.agentcore_client.calls] == ["warmup", "warmup"]
    assert "failed to start" in deliver.call_args[0][2]
    # The scheduled-task failure alarm matches "did not complete".
    assert "did not complete" in caplog.text


def test_a_cron_turn_that_hits_the_start_limit_replaces_the_session_for_the_next_run():
    module = load_cron_lambda()
    module.identity_table = SessionTable()

    class StartsThenNot(SessionRuntime):
        def invoke_agent_runtime(self, **kwargs):
            action = json.loads(kwargs["payload"])["action"]
            self.calls.append((kwargs["runtimeSessionId"], action))
            if action == "cron":
                raise init_exceeded()
            return {"response": io.BytesIO(json.dumps({"status": "ready"}).encode())}

    module.agentcore_client = StartsThenNot([])
    result, deliver = _run(module)
    assert result["statusCode"] == 503
    deliver.assert_called_once_with("telegram", "123", NOTICE)
    assert module.identity_table.session_id != OLD
