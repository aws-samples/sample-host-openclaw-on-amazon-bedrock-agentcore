"""Async cron: the schedule's Lambda invocation starts a Step Functions run that
starts the turn in the container (cron_start), polls it (cron_status) and
delivers the result with the Lambda's existing delivery code.

A brief is then not limited by the Lambda's 900 s timeout. Every way a run can
end without a result sends the fixed failure notice and logs the line the
openclaw-scheduled-task-failures alarm matches.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.test_runtime_routing import ACTOR, USER, FakeRuntime, cron_event, cron_table, load_lambda
from tests.test_runtime_routing_synth import ACCOUNT, REGION, ROOT

SM_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:openclaw-cron-runner"
READY = {"status": "ready", "phase": "USER_READY"}
OWNER = hashlib.sha256(USER.encode()).hexdigest()[:12]
JOB_ID = f"schedule123-{OWNER}-20261019T084700Z"


class Context:
    def __init__(self, seconds=900):
        self.seconds = seconds

    def get_remaining_time_in_millis(self):
        return self.seconds * 1000


def _lambda(responses=None, async_on=True):
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime(responses or [])
    module.CRON_ASYNC_STATE_MACHINE_ARN = SM_ARN if async_on else ""
    module.sfn_client = MagicMock()
    module.deliver_response = MagicMock()
    module.deliver_brief_audio = MagicMock()
    module.find_run_audio_key = MagicMock(return_value=None)
    module.warmup_and_wait.__globals__["WARMUP_POLL_INTERVAL_SECONDS"] = 0
    return module


def _payloads(module):
    return [json.loads(call["payload"]) for call in module.agentcore_client.calls]


def _state(**extra):
    return {**cron_event(), "scheduleName": "Desk",
            "jobId": JOB_ID, **extra}


def _sent(module):
    return [call.args[2] for call in module.deliver_response.call_args_list]


FAILED = "[Scheduled: Desk] " + "Your scheduled task did not return a result this time. " \
    "It will run again at the next scheduled time."


# --- execution name / jobId ----------------------------------------------------

def test_job_id_is_the_schedule_its_owner_and_its_scheduled_fire_time():
    module = _lambda()
    event = {**cron_event(), "scheduledTime": "2026-10-19T08:47:00Z"}
    assert module.async_job_id(event, USER) == JOB_ID
    # A delivery of the same fire that arrives in a later minute keeps the name.
    late = time.mktime((2026, 10, 19, 8, 49, 4, 0, 0, 0)) - time.timezone
    assert module.async_job_id(event, USER, now=late) == JOB_ID
    # Older schedules carry no scheduled time: the invocation time, to the minute.
    fired = time.mktime((2026, 10, 19, 8, 47, 4, 0, 0, 0)) - time.timezone
    assert module.async_job_id(cron_event(), USER, now=fired) == JOB_ID
    # Only characters Step Functions accepts in an execution name.
    name = module.async_job_id({"scheduleId": "a:b/c d#e", "scheduledTime": "bogus"}, USER, now=fired)
    assert name == f"abcde-{OWNER}-20261019T084700Z"
    # The longest scheduleId still fits Step Functions' 80-character name limit.
    assert len(module.async_job_id({**event, "scheduleId": "x" * 100}, USER)) <= 80
    assert len(module.async_job_id({**event, "scheduleId": "s" * 200}, "u" * 200)) <= 80


def test_the_same_schedule_id_and_fire_time_of_another_user_is_a_different_run():
    module = _lambda()
    event = {**cron_event(), "scheduledTime": "2026-10-19T08:47:00Z"}
    assert module.async_job_id(event, USER) != module.async_job_id(event, "another-user")


def test_new_schedules_pass_their_scheduled_fire_time():
    create = (ROOT / "bridge/skills/eventbridge-cron/create.js").read_text()
    update = (ROOT / "bridge/skills/eventbridge-cron/update.js").read_text()
    assert 'scheduledTime: "<aws.scheduler.scheduled-time>"' in create
    assert '"<aws.scheduler.scheduled-time>"' in update


# --- launch (the schedule's invocation) -------------------------------------------

def test_launch_starts_one_named_execution_and_does_not_run_the_turn():
    module = _lambda()
    event = {**cron_event(), "scheduleName": "Desk", "scheduledTime": "2026-10-19T08:47:00Z"}
    fired = time.mktime((2026, 10, 19, 8, 47, 4, 0, 0, 0)) - time.timezone
    with patch.object(module.time, "time", return_value=fired):
        assert module.handler(event, Context())["statusCode"] == 202
    (call,) = module.sfn_client.start_execution.call_args_list
    assert call.kwargs["stateMachineArn"] == SM_ARN
    assert call.kwargs["name"] == JOB_ID
    state = json.loads(call.kwargs["input"])
    assert state["cronPhase"] == "start" and state["jobId"] == call.kwargs["name"]
    assert state["message"] == "hi" and state["channelTarget"] == "123"
    # The state carries the server-authoritative userId, not the stale payload one.
    assert state["userId"] == USER
    assert module.agentcore_client.calls == []
    assert _sent(module) == []


def test_a_repeated_fire_from_the_same_owner_does_not_start_a_second_run():
    module = _lambda()
    module.sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution")
    # describe_execution shows the collision belongs to the same verified owner.
    module.sfn_client.describe_execution.return_value = {
        "input": json.dumps({"userId": USER, "scheduleId": "schedule123"})
    }
    assert module.handler({**cron_event(), "scheduleName": "Desk"}, Context())["statusCode"] == 200
    assert _sent(module) == []


def test_a_collision_from_a_different_owner_alarms_and_delivers_the_failure(caplog):
    module = _lambda()
    module.sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution")
    module.sfn_client.describe_execution.return_value = {
        "input": json.dumps({"userId": "someone-else", "scheduleId": "schedule123"})
    }
    with caplog.at_level(logging.ERROR):
        out = module.handler({**cron_event(), "scheduleName": "Desk"}, Context())
    assert out["statusCode"] == 409
    assert _sent(module) == [FAILED]
    assert "did not complete" in caplog.text


def test_a_failed_start_sends_the_failure_notice(caplog):
    module = _lambda()
    module.sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution")
    with caplog.at_level(logging.WARNING):
        assert module.handler({**cron_event(), "scheduleName": "Desk"}, Context())["statusCode"] == 503
    assert _sent(module) == [FAILED]
    assert "did not complete" in caplog.text


def test_launch_verifies_the_schedule_before_starting_anything():
    module = _lambda()
    module.identity_table.items.pop((f"USER#{USER}", "CRON#schedule123"))
    assert module.handler(cron_event(), Context())["statusCode"] == 403
    module.sfn_client.start_execution.assert_not_called()


def test_without_the_state_machine_the_original_synchronous_path_runs():
    module = _lambda([READY, {"response": "done"}], async_on=False)
    assert module.handler(cron_event(), Context())["statusCode"] == 200
    module.sfn_client.start_execution.assert_not_called()
    assert [p["action"] for p in _payloads(module)] == ["warmup", "cron"]
    assert _sent(module) == ["done"]


# --- start ---------------------------------------------------------------------

def test_start_warms_up_and_sends_cron_start_with_the_job_id():
    module = _lambda([READY, {"status": "accepted", "jobId": _state()["jobId"], "deadline": 123}])
    out = module.handler({**_state(), "cronPhase": "start"}, Context())
    assert out["next"] == "wait" and out["cronPhase"] == "poll"
    assert out["jobDeadline"] == 123 and isinstance(out["runStarted"], float)
    start = _payloads(module)[1]
    assert start["action"] == "cron_start" and start["jobId"] == _state()["jobId"]
    assert start["message"] == "[Scheduled task: Desk] hi"
    # No caller budget: the contract bounds the job by its own deadline.
    assert "callerBudgetMs" not in start
    assert _sent(module) == []


def test_start_of_an_already_accepted_job_keeps_polling_it():
    module = _lambda([READY, {"status": "accepted", "jobId": _state()["jobId"], "duplicate": True, "deadline": 1}])
    out = module.handler({**_state(), "cronPhase": "start", "runStarted": 5.0}, Context())
    assert out["next"] == "wait" and out["runStarted"] == 5.0


def test_start_when_the_agent_cannot_start_sends_the_start_notice():
    module = _lambda([{"status": "error"}])
    out = module.handler({**_state(), "cronPhase": "start"}, Context())
    assert out["next"] == "done"
    assert "could not run because the agent failed to start" in _sent(module)[0]


@pytest.mark.parametrize("reply", [
    {"status": "error", "response": "x"}, {"status": "unknown_outcome", "response": "x"},
    {"error": "bad"}, {"status": "accepted", "jobId": "someone-else"},
])
def test_a_refused_start_goes_to_delivery_as_a_failure(reply):
    module = _lambda([READY, reply])
    out = module.handler({**_state(), "cronPhase": "start"}, Context())
    assert out["next"] == "deliver" and out["outcome"] != "completed"


# --- poll ----------------------------------------------------------------------

def _poll(reply, **state):
    module = _lambda([reply])
    state.setdefault("jobDeadline", (time.time() + 600) * 1000)
    out = module.handler(_state(cronPhase="poll", **state), Context())
    (payload,) = _payloads(module)
    assert payload["action"] == "cron_status" and payload["jobId"] == _state()["jobId"]
    return module, out


def test_a_running_job_waits_for_the_next_poll():
    _, out = _poll({"status": "running", "jobId": _state()["jobId"]})
    assert out["next"] == "wait"


def test_a_done_job_goes_to_delivery_with_its_reply():
    _, out = _poll({"status": "done", "outcome": "completed", "response": "the brief",
                    "execution": {"phase": "completed"}})
    assert out["next"] == "deliver" and out["outcome"] == "completed" and out["response"] == "the brief"
    assert isinstance(out["runFinished"], float)


@pytest.mark.parametrize("reply, outcome", [
    ({"status": "done", "outcome": "error", "response": ""}, "error"),
    ({"status": "done", "outcome": "unknown_outcome", "response": "", "execution": {"phase": "unknown_outcome"}},
     "unknown_outcome"),
    ({"status": "unknown", "jobId": "j"}, "unknown_job"),
    ({"status": "unknown", "jobId": "j", "reason": "lost"}, "unknown_job"),
])
def test_a_failed_or_lost_job_goes_to_delivery_as_a_failure(reply, outcome):
    _, out = _poll(reply)
    assert out["next"] == "deliver" and out["outcome"] == outcome


def test_a_job_still_running_past_its_deadline_is_a_timeout():
    _, out = _poll({"status": "running"}, jobDeadline=(time.time() - 301) * 1000)
    assert out["next"] == "deliver" and out["outcome"] == "timeout"


def test_status_checks_may_fail_twice_before_the_job_is_reported():
    module = _lambda([{"status": "failed"}, {"status": "failed"}, {"status": "failed"}])
    state = _state(cronPhase="poll", jobDeadline=(time.time() + 600) * 1000)
    for expected in ("wait", "wait", "deliver"):
        state = module.handler(state, Context())
        assert state["next"] == expected
    assert state["outcome"] == "status_failed"


def test_an_unknown_outcome_status_keeps_the_route_admission():
    # The same fence as the synchronous path: the route is not released.
    module = _lambda([{"status": "done", "outcome": "unknown_outcome", "execution": {"phase": "unknown_outcome"}}])
    route = module.get_or_create_session(USER)
    with patch.object(module, "admit_route") as admit:
        admission = admit.return_value.__enter__.return_value
        module.invoke_agentcore(route, "cron_status", USER, ACTOR, "telegram", extra={"jobId": "j"})
    admission.retain.assert_called_once()


# --- deliver -------------------------------------------------------------------

def test_delivery_of_a_completed_job_uses_the_existing_delivery_and_audio_fallback():
    module = _lambda()
    out = module.handler(_state(cronPhase="deliver", outcome="completed", response="the brief\nBRIEF_AUDIO: x",
                                runStarted=100.0, runFinished=200.0), Context())
    assert out["next"] == "done" and "response" not in out
    assert _sent(module) == ["the brief"]
    module.deliver_brief_audio.assert_called_once()
    assert module.deliver_brief_audio.call_args.args[2] == "x"


def test_delivery_without_a_marker_looks_for_the_run_audio_in_the_run_window():
    module = _lambda()
    module.handler(_state(cronPhase="deliver", outcome="completed", response="the brief",
                          runStarted=100.0, runFinished=200.0), Context())
    args = module.find_run_audio_key.call_args.args
    assert args[1:3] == (100.0, 200.0) and args[4] == "hi"


@pytest.mark.parametrize("outcome", [
    "error", "unknown_outcome", "timeout", "unknown_job", "status_failed", "step_failed", "failed", None,
])
def test_every_failure_sends_the_fixed_notice_and_the_alarm_line(outcome, caplog):
    module = _lambda()
    state = _state(cronPhase="deliver", response="secret partial text")
    if outcome:
        state["outcome"] = outcome
    with caplog.at_level(logging.WARNING):
        assert module.handler(state, Context())["next"] == "done"
    assert _sent(module) == [FAILED]
    assert "Scheduled task schedule123 did not complete" in caplog.text
    assert "secret partial text" not in caplog.text


def test_a_caught_step_error_is_reported_as_step_failed(caplog):
    module = _lambda()
    state = _state(cronPhase="deliver", caught={"Error": "Lambda.Unknown", "Cause": "x"})
    with caplog.at_level(logging.WARNING):
        assert module.handler(state, Context())["next"] == "done"
    assert _sent(module) == [FAILED]
    assert "status=step_failed" in caplog.text


def test_a_step_for_an_unverified_schedule_stops_without_delivery():
    module = _lambda()
    module.identity_table.items.pop((f"USER#{USER}", "CRON#schedule123"))
    assert module.handler(_state(cronPhase="deliver", outcome="completed", response="x"), Context())["next"] == "done"
    assert _sent(module) == []


# --- CDK -----------------------------------------------------------------------

def _synth(tmp_path, **extra):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION, "availability_zones": [REGION + "a", REGION + "b"],
        "runtime_manifest": "",
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
        **extra,
    })
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({"CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(tmp_path / "cdk.out"),
                "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true"})
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "cdk.out/OpenClawCron.template.json").read_text())["Resources"]


def _one(resources, kind):
    (item,) = [(key, value) for key, value in resources.items() if value["Type"] == kind]
    return item


def _definition(machine):
    parts = machine["Properties"]["DefinitionString"]["Fn::Join"][1]
    return json.loads("".join(part if isinstance(part, str) else "ARN" for part in parts))


def _statements(resources, role_id):
    return [statement for value in resources.values() if value["Type"] == "AWS::IAM::Policy"
            and {"Ref": role_id} in value["Properties"]["Roles"]
            for statement in value["Properties"]["PolicyDocument"]["Statement"]]


@pytest.fixture(scope="module")
def cron_template(tmp_path_factory):
    return _synth(tmp_path_factory.mktemp("async"))


def test_the_runner_is_a_standard_wait_poll_loop(cron_template):
    _, machine = _one(cron_template, "AWS::StepFunctions::StateMachine")
    props = machine["Properties"]
    assert props["StateMachineName"] == "openclaw-cron-runner"
    assert props["StateMachineType"] == "STANDARD"
    assert props["LoggingConfiguration"]["IncludeExecutionData"] is False
    assert props["TracingConfiguration"]["Enabled"] is True
    states = _definition(machine)
    assert states["StartAt"] == "Start"
    steps = states["States"]
    assert steps["Start"]["Next"] == "NextStep"
    for step in ("Start", "Poll", "Deliver"):
        # The whole state is the Lambda event (a JSONPath string here would pass the literal "$").
        assert "Parameters" not in steps[step] and "Payload" not in json.dumps(steps[step])
    assert steps["WaitBeforePoll"] == {"Type": "Wait", "Seconds": 60, "Next": "Poll"}
    assert steps["Poll"]["Next"] == "NextStep"
    choices = {c["StringEquals"]: c["Next"] for c in steps["NextStep"]["Choices"]}
    assert choices == {"wait": "WaitBeforePoll", "deliver": "Deliver"}
    assert steps["NextStep"]["Default"] == "Done"
    assert steps["Deliver"]["Next"] == "Done" and "Catch" not in steps["Deliver"]
    for step in ("Start", "Poll"):
        assert steps[step]["Catch"][0]["Next"] == "StepFailed"
    assert steps["StepFailed"]["Next"] == "Deliver"
    assert steps["StepFailed"]["Result"] == "deliver" and steps["StepFailed"]["ResultPath"] == "$.cronPhase"
    # The whole run outlasts the longest job (55 min) but is bounded.
    assert 55 * 60 < states["TimeoutSeconds"] <= 90 * 60


def test_least_privilege_between_the_lambda_and_the_runner(cron_template):
    fn_id, fn = next((k, v) for k, v in cron_template.items() if v["Type"] == "AWS::Lambda::Function"
                     and v["Properties"].get("FunctionName") == "openclaw-cron-executor")
    env = fn["Properties"]["Environment"]["Variables"]
    arn = env["CRON_ASYNC_STATE_MACHINE_ARN"]
    joined = arn if isinstance(arn, str) else "".join(p if isinstance(p, str) else "X" for p in arn["Fn::Join"][1])
    assert joined.endswith(":stateMachine:openclaw-cron-runner")
    fn_role = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    starts = [s for s in _statements(cron_template, fn_role) if "states:StartExecution" in str(s["Action"])]
    assert len(starts) == 1 and starts[0]["Action"] == "states:StartExecution"
    assert starts[0]["Resource"] == arn
    _, machine = _one(cron_template, "AWS::StepFunctions::StateMachine")
    runner_role = machine["Properties"]["RoleArn"]["Fn::GetAtt"][0]
    invokes = [s for s in _statements(cron_template, runner_role) if "lambda:InvokeFunction" in str(s["Action"])]
    assert len(invokes) == 1
    targets = invokes[0]["Resource"]
    assert all(fn_id in json.dumps(target) for target in targets)


def test_schedules_keep_targeting_the_lambda(cron_template):
    # Async on or off, the scheduler role only invokes the Lambda: no schedule changes.
    role_id, _ = next((k, v) for k, v in cron_template.items() if v["Type"] == "AWS::IAM::Role"
                      and [st.get("Principal", {}).get("Service")
                           for st in v["Properties"]["AssumeRolePolicyDocument"]["Statement"]]
                      == ["scheduler.amazonaws.com"])
    actions = {s["Action"] for s in _statements(cron_template, role_id)}
    assert actions == {"lambda:InvokeFunction"}


def test_rollback_flag_turns_async_off_in_one_deploy(tmp_path):
    resources = _synth(tmp_path, cron_async="false")
    fn = next(v for v in resources.values() if v["Type"] == "AWS::Lambda::Function"
              and v["Properties"].get("FunctionName") == "openclaw-cron-executor")
    assert fn["Properties"]["Environment"]["Variables"]["CRON_ASYNC_STATE_MACHINE_ARN"] == ""
