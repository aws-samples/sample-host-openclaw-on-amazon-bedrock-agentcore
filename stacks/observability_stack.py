"""Metadata-only operational dashboards, alarms, and SNS notifications."""

from aws_cdk import (
    Stack,
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_kms as kms,
    aws_sns as sns,
    CfnOutput,
)
import cdk_nag
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- SNS Topic for alarms -----------------------------------------
        alarm_cmk = kms.Key.from_key_arn(self, "AlarmTopicCmk", cmk_arn)
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name="openclaw-alarms",
            display_name="OpenClaw Alarms",
            master_key=alarm_cmk,
        )

        # --- Operations Dashboard -----------------------------------------
        dashboard = cw.Dashboard(
            self,
            "OperationsDashboard",
            dashboard_name="OpenClaw-Operations",
        )

        # Bedrock metrics
        bedrock_invocations = cw.Metric(
            namespace="AWS/Bedrock",
            metric_name="Invocations",
            period=Duration.minutes(5),
            statistic="Sum",
        )
        bedrock_latency = cw.Metric(
            namespace="AWS/Bedrock",
            metric_name="InvocationLatency",
            period=Duration.minutes(5),
            statistic="p99",
        )
        bedrock_throttles = cw.Metric(
            namespace="AWS/Bedrock",
            metric_name="InvocationThrottles",
            period=Duration.minutes(5),
            statistic="Sum",
        )
        bedrock_errors = cw.Metric(
            namespace="AWS/Bedrock",
            metric_name="InvocationServerErrors",
            period=Duration.minutes(5),
            statistic="Sum",
        )

        # AgentCore Runtime metrics
        agentcore_invocations = cw.Metric(
            namespace="AWS/BedrockAgentCore",
            metric_name="Invocations",
            period=Duration.minutes(5),
            statistic="Sum",
        )
        agentcore_latency = cw.Metric(
            namespace="AWS/BedrockAgentCore",
            metric_name="InvocationLatency",
            period=Duration.minutes(5),
            statistic="p99",
        )
        agentcore_errors = cw.Metric(
            namespace="AWS/BedrockAgentCore",
            metric_name="InvocationErrors",
            period=Duration.minutes(5),
            statistic="Sum",
        )

        # Router Lambda metrics
        router_invocations = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Invocations",
            dimensions_map={"FunctionName": "openclaw-router"},
            period=Duration.minutes(5),
            statistic="Sum",
        )
        router_errors = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Errors",
            dimensions_map={"FunctionName": "openclaw-router"},
            period=Duration.minutes(5),
            statistic="Sum",
        )
        router_duration = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Duration",
            dimensions_map={"FunctionName": "openclaw-router"},
            period=Duration.minutes(5),
            statistic="p99",
        )
        router_throttles = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Throttles",
            dimensions_map={"FunctionName": "openclaw-router"},
            period=Duration.minutes(5),
            statistic="Sum",
        )

        dashboard.add_widgets(
            cw.TextWidget(markdown="# OpenClaw Operations Dashboard (AgentCore)", width=24, height=1),
            cw.GraphWidget(
                title="Bedrock Invocations & Errors",
                left=[bedrock_invocations],
                right=[bedrock_errors, bedrock_throttles],
                width=12,
            ),
            cw.GraphWidget(
                title="Bedrock Latency (p99)",
                left=[bedrock_latency],
                width=12,
            ),
            cw.GraphWidget(
                title="AgentCore Runtime Invocations & Errors",
                left=[agentcore_invocations],
                right=[agentcore_errors],
                width=12,
            ),
            cw.GraphWidget(
                title="AgentCore Runtime Latency (p99)",
                left=[agentcore_latency],
                width=12,
            ),
            cw.GraphWidget(
                title="Router Lambda Invocations & Errors",
                left=[router_invocations],
                right=[router_errors, router_throttles],
                width=12,
            ),
            cw.GraphWidget(
                title="Router Lambda Duration (p99)",
                left=[router_duration],
                width=12,
            ),
        )

        # --- Alarms -------------------------------------------------------
        # Error rate > 5%
        bedrock_errors.create_alarm(
            self,
            "BedrockErrorAlarm",
            alarm_name="openclaw-bedrock-errors",
            threshold=5,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        # P99 latency > 10s
        bedrock_latency.create_alarm(
            self,
            "BedrockLatencyAlarm",
            alarm_name="openclaw-bedrock-latency",
            threshold=10000,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        # Router Lambda errors
        router_errors.create_alarm(
            self,
            "RouterLambdaErrorAlarm",
            alarm_name="openclaw-router-errors",
            threshold=5,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        # Throttle rate
        bedrock_throttles.create_alarm(
            self,
            "BedrockThrottleAlarm",
            alarm_name="openclaw-bedrock-throttles",
            threshold=1,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        CfnOutput(
            self,
            "AlarmTopicArn",
            value=self.alarm_topic.topic_arn,
            description="SNS topic ARN for alarm notifications",
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.alarm_topic,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-SNS3",
                    reason="This SNS topic is used exclusively for CloudWatch alarm "
                    "notifications. Publishers are AWS services (CloudWatch Alarms) "
                    "which use internal AWS service endpoints. SSL enforcement via "
                    "topic policy is not required for service-to-service communication.",
                ),
            ],
        )
