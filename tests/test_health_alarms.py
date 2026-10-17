"""Health alarms: session storage write errors / budget, failed scheduled tasks, unstartable sessions.

16 Oct 2026: a user's session storage returned EREMOTEIO for hours and every
scheduled brief then failed, and nothing alarmed. These alarms make each of
those log lines page the alarm topic. All IDs are placeholders.
"""

import pytest

from tests.cron_v1_helpers import run_app


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    return run_app(tmp_path_factory.mktemp("synth"))


def _resources(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == kind}


def test_each_failure_line_has_a_metric_filter_and_an_alarm(templates):
    obs = templates["OpenClawObservability.template.json"]
    filters = {v["Properties"]["MetricTransformations"][0]["MetricName"]: v["Properties"]
               for v in _resources(obs, "AWS::Logs::MetricFilter").values()
               if v["Properties"]["LogGroupName"] != "/openclaw/lambda/router"}
    assert filters["SessionStorageWriteErrors"]["LogGroupName"] == "/openclaw/container"
    assert filters["SessionStorageWriteErrors"]["FilterPattern"] == \
        '?"MOUNT_WRITE_FAILED" ?"EREMOTEIO" ?"ENOSPC" ?"EDQUOT"'
    assert filters["SessionStorageBudgetSkips"]["FilterPattern"] == '"MOUNT_BUDGET"'
    assert filters["ScheduledTaskFailures"]["LogGroupName"] == "/openclaw/lambda/cron"
    assert filters["ScheduledTaskFailures"]["FilterPattern"] == \
        '?"did not complete" ?"Warmup timed out" ?"Status: timeout"'
    assert filters["RuntimeSessionUnstartable"]["FilterPattern"] == '"could not start runtime session"'
    router = [v["Properties"] for v in _resources(obs, "AWS::Logs::MetricFilter").values()
              if v["Properties"]["LogGroupName"] == "/openclaw/lambda/router"]
    assert len(router) == 1 and router[0]["MetricTransformations"][0]["MetricName"] == "RuntimeSessionUnstartable"
    for props in filters.values():
        t = props["MetricTransformations"][0]
        assert t["MetricNamespace"] == "OpenClaw/Health" and t["MetricValue"] == "1"

    alarms = {v["Properties"]["AlarmName"]: v["Properties"] for v in _resources(obs, "AWS::CloudWatch::Alarm").values()}
    for name in ("openclaw-session-storage-write-errors", "openclaw-session-storage-budget",
                 "openclaw-scheduled-task-failures", "openclaw-runtime-session-unstartable"):
        a = alarms[name]
        assert a["Threshold"] == 1 and a["Period"] == 300 and a["EvaluationPeriods"] == 1
        assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert a["TreatMissingData"] == "notBreaching"
        assert a["AlarmActions"], f"{name} notifies nobody"


def test_container_log_group_is_created_only_if_missing_and_never_deleted(templates):
    obs = templates["OpenClawObservability.template.json"]
    ensure = [v for v in obs["Resources"].values()
              if v["Type"] == "Custom::AWS" and "createLogGroup" in str(v["Properties"].get("Create"))]
    assert len(ensure) == 1
    create = str(ensure[0]["Properties"]["Create"])
    assert "ResourceAlreadyExistsException" in create and "/openclaw/container" in create
    assert "Delete" not in ensure[0]["Properties"]
    container_filters = [k for k, v in _resources(obs, "AWS::Logs::MetricFilter").items()
                         if v["Properties"]["LogGroupName"] == "/openclaw/container"]
    for key in container_filters:
        assert obs["Resources"][key].get("DependsOn"), f"{key} may be created before the log group"


def test_no_other_stack_changes_for_the_alarms(templates):
    """The alarms live in OpenClawObservability only; the cron log group stays owned by OpenClawCron."""
    cron = templates["OpenClawCron.template.json"]
    assert any(v["Properties"].get("LogGroupName") == "/openclaw/lambda/cron"
               for v in _resources(cron, "AWS::Logs::LogGroup").values())
    assert not _resources(cron, "AWS::Logs::MetricFilter")
