"""Async cron (release/v1): the schedule's Lambda invocation starts a Step
Functions run that starts the turn in the container (cron_start), polls it
(cron_status) and delivers the result with the Lambda's existing delivery code.

A brief is then not limited by the Lambda's 900 s timeout, and a second brief
queued behind the first no longer shares one Lambda's window. Every way a run
can end without a result sends the fixed failure notice and logs the line the
openclaw-scheduled-task-failures alarm matches.
"""

import json
import logging
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tests.cron_v1_helpers import ACCOUNT, REGION, ROOT, SCHEDULE_ID, USER, FakeRuntime, cron_event, load_cron_lambda

SM_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:openclaw-cron-runner"
READY = {"status": "ready"}
JOB = f"{SCHEDULE_ID}-20261019T084700Z"
FAILED = "[Scheduled: Morning brief] Your scheduled task did not return a result this time. " \
    "It will run again at the next scheduled time."


class Context:
    def get_remaining_time_in_millis(self):
        return 900_000


def _lambda(responses=(), async_on=True):
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime(responses)
    module.CRON_ASYNC_STATE_MACHINE_ARN = SM_ARN if async_on else ""
    module.sfn_client = MagicMock()
    module.deliver_response = MagicMock()
    module.deliver_brief_audio = MagicMock()
    module.find_run_audio_key = MagicMock(return_value=None)
    module.WARMUP_POLL_INTERVAL_SECONDS = 0
    return module


def _payloads(module):
    return [json.loads(call["payload"]) for call in module.agentcore_client.calls]


def _state(**extra):
    return {**cron_event(), "jobId": JOB, **extra}


def _sent(module):
    return [call.args[2] for call in module.deliver_response.call_args_list]


def test_job_id_is_the_schedule_and_its_scheduled_fire_time():
    module = _lambda()
    assert module.async_job_id(cron_event(scheduledTime="2026-10-19T08:47:00Z")) == JOB
    fired = time.mktime((2026, 10, 19, 8, 47, 4, 0, 0, 0)) - time.timezone
    assert module.async_job_id(cron_event(), now=fired) == JOB
    assert module.async_job_id({"scheduleId": "a:b/c d", "scheduledTime": "x"}, now=fired) == "abcd-20261019T084700Z"


def test_new_schedules_pass_their_scheduled_fire_time():
    create = (ROOT / "bridge/skills/eventbridge-cron/create.js").read_text()
    update = (ROOT / "bridge/skills/eventbridge-cron/update.js").read_text()
    assert 'scheduledTime: "<aws.scheduler.scheduled-time>"' in create
    assert '"<aws.scheduler.scheduled-time>"' in update


def test_launch_starts_one_named_execution_and_does_not_run_the_turn():
    module = _lambda()
    assert module.handler(cron_event(scheduledTime="2026-10-19T08:47:00Z"), Context())["statusCode"] == 202
    (call,) = module.sfn_client.start_execution.call_args_list
    assert call.kwargs["stateMachineArn"] == SM_ARN and call.kwargs["name"] == JOB
    state = json.loads(call.kwargs["input"])
    assert state["cronPhase"] == "start" and state["jobId"] == JOB
    assert module.agentcore_client.calls == [] and _sent(module) == []


def test_a_repeated_fire_does_not_start_a_second_run():
    module = _lambda()
    module.sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution")
    assert module.handler(cron_event(), Context())["statusCode"] == 200
    assert _sent(module) == []


def test_a_failed_start_sends_the_failure_notice(caplog):
    module = _lambda()
    module.sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution")
    with caplog.at_level(logging.WARNING):
        assert module.handler(cron_event(), Context())["statusCode"] == 503
    assert _sent(module) == [FAILED]
    assert "did not complete" in caplog.text


def test_launch_verifies_the_schedule_before_starting_anything():
    module = _lambda()
    module.identity_table.items.pop((f"USER#{USER}", f"CRON#{SCHEDULE_ID}"))
    assert module.handler(cron_event(), Context())["statusCode"] == 403
    module.sfn_client.start_execution.assert_not_called()


def test_without_the_state_machine_the_original_synchronous_path_runs():
    module = _lambda([READY, {"response": "done"}], async_on=False)
    assert module.handler(cron_event(), Context())["statusCode"] == 200
    module.sfn_client.start_execution.assert_not_called()
    assert module.agentcore_client.actions == ["warmup", "cron"]
    assert _sent(module) == ["done"]


def test_start_warms_up_and_sends_cron_start_with_the_job_id():
    module = _lambda([READY, {"status": "accepted", "jobId": JOB, "deadline": 123}])
    out = module.handler(_state(cronPhase="start"), Context())
    assert out["next"] == "wait" and out["cronPhase"] == "poll" and out["jobDeadline"] == 123
    assert out["sessionId"].startswith("ses_")
    start = _payloads(module)[1]
    assert start["action"] == "cron_start" and start["jobId"] == JOB
    assert start["message"] == "[Scheduled task: Morning brief] Send my morning brief"
    assert "callerBudgetMs" not in start


def test_start_on_an_unstartable_session_moves_the_user_first():
    unstartable = ClientError({"Error": {"Code": "RuntimeClientError",
                                         "Message": "Runtime initialization time exceeded"}}, "InvokeAgentRuntime")
    module = _lambda([unstartable, READY, {"status": "accepted", "jobId": JOB, "deadline": 1}])
    module.replace_unstartable_session = MagicMock(return_value="ses_" + "n" * 40)
    out = module.handler(_state(cronPhase="start"), Context())
    assert out["next"] == "wait" and out["sessionId"] == "ses_" + "n" * 40
    assert module.agentcore_client.calls[-1]["runtimeSessionId"] == "ses_" + "n" * 40


def test_start_when_the_agent_cannot_start_sends_the_start_notice():
    # V1 warm-up proceeds on an unexpected status; it gives up only when it times out.
    module = _lambda()
    module.WARMUP_MAX_WAIT_SECONDS = 0
    assert module.handler(_state(cronPhase="start"), Context())["next"] == "done"
    assert "could not run because the agent failed to start" in _sent(module)[0]


@pytest.mark.parametrize("reply", [{"status": "error"}, {"error": "bad"}, {"status": "accepted", "jobId": "other"}])
def test_a_refused_start_goes_to_delivery_as_a_failure(reply):
    module = _lambda([READY, reply])
    out = module.handler(_state(cronPhase="start"), Context())
    assert out["next"] == "deliver" and out["outcome"] != "completed"


def _poll(reply, **state):
    module = _lambda([reply])
    state.setdefault("jobDeadline", (time.time() + 600) * 1000)
    out = module.handler(_state(cronPhase="poll", sessionId="ses_" + "p" * 40, **state), Context())
    (call,) = module.agentcore_client.calls
    payload = json.loads(call["payload"])
    assert payload["action"] == "cron_status" and payload["jobId"] == JOB
    # Polls the session the job was started on.
    assert call["runtimeSessionId"] == "ses_" + "p" * 40
    return out


def test_a_running_job_waits_and_a_done_job_is_delivered():
    assert _poll({"status": "running"})["next"] == "wait"
    out = _poll({"status": "done", "outcome": "completed", "response": "the brief"})
    assert out["next"] == "deliver" and out["outcome"] == "completed" and out["response"] == "the brief"


@pytest.mark.parametrize("reply, outcome", [
    ({"status": "done", "outcome": "error", "response": ""}, "error"),
    ({"status": "unknown", "jobId": JOB}, "unknown_job"),
    ({"status": "unknown", "jobId": JOB, "reason": "lost"}, "unknown_job"),
])
def test_a_failed_or_lost_job_goes_to_delivery_as_a_failure(reply, outcome):
    out = _poll(reply)
    assert out["next"] == "deliver" and out["outcome"] == outcome


def test_a_job_still_running_past_its_deadline_is_a_timeout():
    out = _poll({"status": "running"}, jobDeadline=(time.time() - 301) * 1000)
    assert out["outcome"] == "timeout"


def test_status_checks_may_fail_twice_before_the_job_is_reported():
    module = _lambda([RuntimeError("x")] * 3)
    state = _state(cronPhase="poll", jobDeadline=(time.time() + 600) * 1000)
    for expected in ("wait", "wait", "deliver"):
        state = module.handler(state, Context())
        assert state["next"] == expected
    assert state["outcome"] == "status_failed"


def test_delivery_of_a_completed_job_uses_the_existing_delivery_and_audio_fallback():
    module = _lambda()
    out = module.handler(_state(cronPhase="deliver", outcome="completed", response="the brief",
                                runStarted=100.0, runFinished=200.0), Context())
    assert out["next"] == "done" and "response" not in out
    assert _sent(module) == ["the brief"]
    args = module.find_run_audio_key.call_args.args
    assert args[1:3] == (100.0, 200.0) and args[4] == "Send my morning brief"


@pytest.mark.parametrize("outcome", ["error", "timeout", "unknown_job", "status_failed", "failed", None])
def test_every_failure_sends_the_fixed_notice_and_the_alarm_line(outcome, caplog):
    module = _lambda()
    state = _state(cronPhase="deliver", response="secret partial text")
    if outcome:
        state["outcome"] = outcome
    with caplog.at_level(logging.WARNING):
        assert module.handler(state, Context())["next"] == "done"
    assert _sent(module) == [FAILED]
    assert f"Scheduled task {SCHEDULE_ID} did not complete" in caplog.text
    assert "secret partial text" not in caplog.text


def test_a_caught_step_error_is_reported_as_step_failed(caplog):
    module = _lambda()
    with caplog.at_level(logging.WARNING):
        module.handler(_state(cronPhase="deliver", caught={"Error": "x"}), Context())
    assert _sent(module) == [FAILED] and "status=step_failed" in caplog.text


def _synth(tmp_path, **extra):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({"account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
                    f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"], **extra})
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({"CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(tmp_path / "cdk.out"),
                "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true"})
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "cdk.out/OpenClawCron.template.json").read_text())["Resources"]


def _cron_fn(resources):
    return next(v for v in resources.values() if v["Type"] == "AWS::Lambda::Function"
                and v["Properties"].get("FunctionName") == "openclaw-cron-executor")


def test_the_runner_is_a_standard_wait_poll_loop_with_least_privilege(tmp_path):
    resources = _synth(tmp_path)
    (machine,) = [v for v in resources.values() if v["Type"] == "AWS::StepFunctions::StateMachine"]
    props = machine["Properties"]
    assert props["StateMachineName"] == "openclaw-cron-runner" and props["StateMachineType"] == "STANDARD"
    assert props["LoggingConfiguration"]["IncludeExecutionData"] is False
    parts = props["DefinitionString"]["Fn::Join"][1]
    steps = json.loads("".join(p if isinstance(p, str) else "ARN" for p in parts))["States"]
    assert steps["WaitBeforePoll"] == {"Type": "Wait", "Seconds": 60, "Next": "Poll"}
    for step in ("Start", "Poll", "Deliver"):
        assert "Parameters" not in steps[step]
    assert "Catch" not in steps["Deliver"]
    env = _cron_fn(resources)["Properties"]["Environment"]["Variables"]
    assert str(env["CRON_ASYNC_STATE_MACHINE_ARN"]).endswith(":stateMachine:openclaw-cron-runner")


def test_rollback_flag_turns_async_off_in_one_deploy(tmp_path):
    env = _cron_fn(_synth(tmp_path, cron_async="false"))["Properties"]["Environment"]["Variables"]
    assert env["CRON_ASYNC_STATE_MACHINE_ARN"] == ""
