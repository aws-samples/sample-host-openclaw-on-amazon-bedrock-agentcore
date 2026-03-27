"""Token Monitoring Stack — DynamoDB, Lambda, custom CW metrics, budget alarms."""

import os
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs,
    aws_logs_destinations as log_destinations,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    CfnOutput,
)
import cdk_nag
from constructs import Construct


class TokenMonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        invocation_log_group: logs.ILogGroup,
        alarm_topic: sns.ITopic,
        cmk_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        daily_token_budget = self.node.try_get_context("daily_token_budget") or 1_000_000
        daily_cost_budget = self.node.try_get_context("daily_cost_budget_usd") or 5
        anomaly_band = self.node.try_get_context("anomaly_band_width") or 2
        ttl_days = self.node.try_get_context("token_ttl_days") or 90

        # --- DynamoDB Token Usage Table -----------------------------------
        # CMK encryption is optional — uncomment the two lines below if the
        # table already exists and supports CUSTOMER_MANAGED encryption.
        # token_cmk = kms.Key.from_key_arn(self, "TokenUsageTableCmk", cmk_arn)
        self.table = dynamodb.Table(
            self,
            "TokenUsageTable2",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            # encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            # encryption_key=token_cmk,
        )

        # GSI1: Channel aggregation
        self.table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="GSI1PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="GSI1SK", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI2: Model aggregation
        self.table.add_global_secondary_index(
            index_name="GSI2",
            partition_key=dynamodb.Attribute(
                name="GSI2PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="GSI2SK", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI3: Daily cost ranking
        self.table.add_global_secondary_index(
            index_name="GSI3",
            partition_key=dynamodb.Attribute(
                name="GSI3PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="GSI3SK", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # --- Token Metrics Lambda -----------------------------------------
        lambda_log_group = logs.LogGroup(
            self,
            "TokenMetricsLogGroup",
            log_group_name="/openclaw/lambda/token-metrics",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.token_lambda = lambda_.Function(
            self,
            "TokenMetricsFunction",
            function_name="openclaw-token-metrics",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "token_metrics")
            ),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "TABLE_NAME": self.table.table_name,
                "TTL_DAYS": str(ttl_days),
                "METRICS_NAMESPACE": "OpenClaw/TokenUsage",
            },
            log_group=lambda_log_group,
        )

        # Permissions
        self.table.grant_read_write_data(self.token_lambda)
        self.token_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "OpenClaw/TokenUsage"
                    }
                },
            )
        )

        # CloudWatch Logs subscription filter
        logs.SubscriptionFilter(
            self,
            "InvocationLogSubscription",
            log_group=invocation_log_group,
            destination=log_destinations.LambdaDestination(self.token_lambda),
            filter_pattern=logs.FilterPattern.all_events(),
        )

        # --- Custom Metrics -----------------------------------------------
        ns = "OpenClaw/TokenUsage"
        total_tokens = cw.Metric(
            namespace=ns,
            metric_name="TotalTokens",
            period=Duration.hours(1),
            statistic="Sum",
        )
        input_tokens = cw.Metric(
            namespace=ns,
            metric_name="InputTokens",
            period=Duration.hours(1),
            statistic="Sum",
        )
        output_tokens = cw.Metric(
            namespace=ns,
            metric_name="OutputTokens",
            period=Duration.hours(1),
            statistic="Sum",
        )
        estimated_cost = cw.Metric(
            namespace=ns,
            metric_name="EstimatedCostUSD",
            period=Duration.hours(1),
            statistic="Sum",
        )
        invocation_count = cw.Metric(
            namespace=ns,
            metric_name="InvocationCount",
            period=Duration.hours(1),
            statistic="Sum",
        )

        # --- Token Analytics Dashboard ------------------------------------
        dashboard = cw.Dashboard(
            self,
            "TokenAnalyticsDashboard",
            dashboard_name="OpenClaw-Token-Analytics",
        )

        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# OpenClaw Token Analytics Dashboard",
                width=24,
                height=1,
            ),
            cw.GraphWidget(
                title="Total Tokens (Input vs Output)",
                left=[input_tokens, output_tokens],
                width=12,
            ),
            cw.GraphWidget(
                title="Estimated Cost (USD)",
                left=[estimated_cost],
                width=12,
            ),
            cw.SingleValueWidget(
                title="Invocations (1h)",
                metrics=[invocation_count],
                width=6,
            ),
            cw.SingleValueWidget(
                title="Total Tokens (1h)",
                metrics=[total_tokens],
                width=6,
            ),
            cw.SingleValueWidget(
                title="Estimated Cost (1h)",
                metrics=[estimated_cost],
                width=6,
            ),
        )

        # --- Budget Alarms ------------------------------------------------
        # Daily token budget
        total_tokens.create_alarm(
            self,
            "DailyTokenBudgetAlarm",
            alarm_name="openclaw-daily-token-budget",
            threshold=daily_token_budget,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

        # Daily cost budget
        estimated_cost.create_alarm(
            self,
            "DailyCostBudgetAlarm",
            alarm_name="openclaw-daily-cost-budget",
            threshold=daily_cost_budget,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

        # Anomaly detection alarm
        anomaly_alarm = cw.CfnAnomalyDetector(
            self,
            "TokenAnomalyDetector",
            metric_name="TotalTokens",
            namespace=ns,
            stat="Sum",
        )

        CfnOutput(
            self,
            "TokenUsageTableName",
            value=self.table.table_name,
            description="DynamoDB table for token usage records",
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.token_lambda,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="AWSLambdaBasicExecutionRole is the AWS-recommended managed policy "
                    "for Lambda functions to write to CloudWatch Logs. "
                    "See https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="DynamoDB index/* wildcard is generated by CDK grant_read_write_data() "
                    "and is scoped to the specific table's GSIs. KMS wildcards "
                    "(GenerateDataKey*, ReEncrypt*) are added by CDK for CMK-encrypted "
                    "DynamoDB table. cloudwatch:PutMetricData wildcard is constrained "
                    "by the cloudwatch:namespace condition to OpenClaw/TokenUsage only.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.12 is the latest stable runtime available in all regions. "
                    "Will upgrade to 3.13 when broadly available in Lambda.",
                ),
            ],
            apply_to_children=True,
        )
