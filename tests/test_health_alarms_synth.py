"""Synth-time tests for the health alarms (no AWS calls).

16 Oct 2026: a user's session storage returned EREMOTEIO for hours and every
scheduled brief then failed, and nothing alarmed. Session storage alarms live in
OpenClawObservability (phase 1); scheduled-task alarms filter
/openclaw/lambda/cron, so they live in OpenClawTokenMonitoring (phase 3, after
OpenClawCron).

    pytest tests/test_health_alarms_synth.py -v
"""

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.observability_stack import ObservabilityStack
from stacks.security_stack import SecurityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

_ACCOUNT = "123456789012"
_REGION = "us-west-2"


def _synth():
    app = cdk.App(context={"account": _ACCOUNT, "region": _REGION,
                           "availability_zones": [f"{_REGION}a", f"{_REGION}b"]})
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    security = SecurityStack(app, "OpenClawSecurity", env=env)
    observability = ObservabilityStack(app, "OpenClawObservability", cmk_arn=security.cmk.key_arn, env=env)
    token = TokenMonitoringStack(app, "OpenClawTokenMonitoring",
                                 invocation_log_group=observability.invocation_log_group,
                                 alarm_topic=observability.alarm_topic, cmk_arn=security.cmk.key_arn, env=env)
    return Template.from_stack(observability).to_json(), Template.from_stack(token).to_json()


def _of(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def _filters(template):
    return {v["Properties"]["MetricTransformations"][0]["MetricName"]: v["Properties"]
            for v in _of(template, "AWS::Logs::MetricFilter").values()}


def _alarms(template):
    return {v["Properties"]["AlarmName"]: v["Properties"] for v in _of(template, "AWS::CloudWatch::Alarm").values()}


def test_session_storage_alarms_are_in_observability():
    obs, token = _synth()
    f = _filters(obs)
    assert set(f) == {"SessionStorageWriteErrors", "SessionStorageBudgetSkips"}
    assert f["SessionStorageWriteErrors"]["LogGroupName"] == "/openclaw/container"
    assert f["SessionStorageWriteErrors"]["FilterPattern"] == '?"MOUNT_WRITE_FAILED" ?"EREMOTEIO" ?"ENOSPC" ?"EDQUOT"'
    assert f["SessionStorageBudgetSkips"]["FilterPattern"] == '"MOUNT_BUDGET"'
    a = _alarms(obs)
    for name in ("openclaw-session-storage-write-errors", "openclaw-session-storage-budget"):
        assert a[name]["Threshold"] == 1 and a[name]["Period"] == 300 and a[name]["AlarmActions"]
        assert a[name]["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert a[name]["TreatMissingData"] == "notBreaching"


def test_scheduled_task_alarms_are_in_token_monitoring():
    obs, token = _synth()
    f = _filters(token)
    assert set(f) == {"ScheduledTaskFailures", "RuntimeSessionUnstartable"}
    assert f["ScheduledTaskFailures"]["LogGroupName"] == "/openclaw/lambda/cron"
    assert f["ScheduledTaskFailures"]["FilterPattern"] == '?"did not complete" ?"Warmup timed out" ?"Status: timeout"'
    assert f["RuntimeSessionUnstartable"]["FilterPattern"] == '"could not start runtime session"'
    a = _alarms(token)
    for name in ("openclaw-scheduled-task-failures", "openclaw-runtime-session-unstartable"):
        assert a[name]["Threshold"] == 1 and a[name]["Period"] == 300 and a[name]["AlarmActions"]


def test_container_log_group_is_created_only_if_missing_and_never_deleted():
    obs, _ = _synth()
    ensure = [v for v in obs["Resources"].values()
              if v["Type"] == "Custom::AWS" and "createLogGroup" in str(v["Properties"].get("Create"))]
    assert len(ensure) == 1
    assert "ResourceAlreadyExistsException" in str(ensure[0]["Properties"]["Create"])
    assert "Delete" not in ensure[0]["Properties"]
    for key, v in _of(obs, "AWS::Logs::MetricFilter").items():
        assert v.get("DependsOn"), f"{key} may be created before /openclaw/container exists"
