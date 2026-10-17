"""Alarms for OpenClaw failures that otherwise only show up in logs.

- Session storage refusing writes (EREMOTEIO / ENOSPC / EDQUOT, or the bridge's
  own MOUNT_WRITE_FAILED). On 16 Oct 2026 this preceded an AgentCore runtime
  session that could no longer start at all, and every scheduled brief for
  that user failed.
- The bridge leaving a file to the S3 backup because it reached its byte budget
  on the 1 GiB session storage (MOUNT_BUDGET): a user's state is outgrowing it.
- A scheduled task that ended with the fixed "did not return a result" notice,
  a warm-up timeout or a cron Lambda timeout.
- The cron Lambda finding a runtime session AgentCore cannot start.

Each is a log metric filter (namespace OpenClaw/Health) plus an alarm on the
stack's SNS alarm topic that fires on one occurrence in 5 minutes.
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_logs as logs,
    aws_sns as sns,
    custom_resources as cr,
)
from constructs import Construct

NAMESPACE = "OpenClaw/Health"
CONTAINER_LOG_GROUP = "/openclaw/container"
CRON_LOG_GROUP = "/openclaw/lambda/cron"


def add_health_alarms(scope: Construct, alarm_topic: sns.ITopic) -> list:
    """Add the filters and alarms to `scope`. Returns the alarms."""
    stack = Stack.of(scope)
    # The container creates /openclaw/container itself on first boot, so it may
    # not exist yet on a fresh deployment. Create it if missing. An existing
    # group (and its events) is left as it is, also when this stack is deleted.
    ensure_container_group = cr.AwsCustomResource(
        scope,
        "EnsureContainerLogGroup",
        on_create=cr.AwsSdkCall(
            service="CloudWatchLogs",
            action="createLogGroup",
            parameters={"logGroupName": CONTAINER_LOG_GROUP},
            physical_resource_id=cr.PhysicalResourceId.of(CONTAINER_LOG_GROUP),
            ignore_error_codes_matching="ResourceAlreadyExistsException",
        ),
        policy=cr.AwsCustomResourcePolicy.from_statements([
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup"],
                resources=[
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:{CONTAINER_LOG_GROUP}",
                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:{CONTAINER_LOG_GROUP}:*",
                ],
            ),
        ]),
        install_latest_aws_sdk=False,
    )
    container_group = logs.LogGroup.from_log_group_name(scope, "ContainerLogGroup", CONTAINER_LOG_GROUP)
    # Owned by OpenClawCron; app.py makes this stack depend on it.
    cron_group = logs.LogGroup.from_log_group_name(scope, "CronLogGroup", CRON_LOG_GROUP)

    specs = [
        ("SessionStorageWriteErrors", container_group,
         logs.FilterPattern.any_term("MOUNT_WRITE_FAILED", "EREMOTEIO", "ENOSPC", "EDQUOT"),
         "openclaw-session-storage-write-errors",
         "A container's session storage (/mnt/workspace) refused a write. The mount may be full or broken; "
         "a session in this state could not be started again on 16 Oct 2026."),
        ("SessionStorageBudgetSkips", container_group,
         logs.FilterPattern.any_term("MOUNT_BUDGET"),
         "openclaw-session-storage-budget",
         "The state mirror reached its byte budget on the 1 GiB session storage and left a file to the S3 "
         "backup only. A user's state is outgrowing session storage."),
        ("ScheduledTaskFailures", cron_group,
         logs.FilterPattern.any_term("did not complete", "Warmup timed out", "Status: timeout"),
         "openclaw-scheduled-task-failures",
         "A scheduled task did not deliver a result (fixed failure notice sent, warm-up timed out, or the "
         "cron Lambda timed out)."),
        ("RuntimeSessionUnstartable", cron_group,
         logs.FilterPattern.any_term("could not start runtime session"),
         "openclaw-runtime-session-unstartable",
         "AgentCore could not start a user's runtime session (initialization time exceeded). The cron Lambda "
         "moves the user to a new session; check that user's session storage errors."),
    ]
    alarms = []
    action = cw_actions.SnsAction(alarm_topic)
    for metric_name, group, pattern, alarm_name, description in specs:
        mf = logs.MetricFilter(
            scope,
            f"{metric_name}Filter",
            log_group=group,
            filter_pattern=pattern,
            metric_namespace=NAMESPACE,
            metric_name=metric_name,
            metric_value="1",
        )
        if group is container_group:
            mf.node.add_dependency(ensure_container_group)
        alarm = mf.metric(statistic="Sum", period=Duration.minutes(5)).create_alarm(
            scope,
            f"{metric_name}Alarm",
            alarm_name=alarm_name,
            alarm_description=description,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(action)
        alarms.append(alarm)
    return alarms
