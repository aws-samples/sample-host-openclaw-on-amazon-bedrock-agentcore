"""Warm Pool Stack — Maintains pre-warmed AgentCore sessions.

Deploys a Lambda that runs on a schedule to maintain a pool of pre-warmed
AgentCore sessions in DynamoDB. Pre-warmed sessions have completed Phase 1
init (secrets + OpenClaw startup) so new users only wait for Phase 2 (~15s).
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class WarmPoolStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_id: str,
        identity_table_name: str,
        identity_table_arn: str,
        cmk_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        pool_size = int(self.node.try_get_context("warm_pool_size") or "1")
        check_interval = int(self.node.try_get_context("warm_pool_check_interval_seconds") or "60")
        session_ttl = int(self.node.try_get_context("warm_pool_session_ttl_minutes") or "25")

        # --- CloudWatch Log Group ---
        warmpool_log_group = logs.LogGroup(
            self,
            "WarmPoolLogGroup",
            log_group_name="/openclaw/lambda/warmpool",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Warm Pool Maintainer Lambda ---
        self.warmpool_fn = _lambda.Function(
            self,
            "WarmPoolFn",
            function_name="openclaw-warmpool-maintainer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/warmpool"),
            timeout=Duration.seconds(300),
            memory_size=256,
            environment={
                "IDENTITY_TABLE_NAME": identity_table_name,
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_id,
                "TARGET_POOL_SIZE": str(pool_size),
                "SESSION_TTL_MINUTES": str(session_ttl),
            },
            log_group=warmpool_log_group,
        )

        # --- EventBridge Rule (periodic trigger) ---
        events.Rule(
            self,
            "WarmPoolSchedule",
            rule_name="openclaw-warmpool-check",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[events_targets.LambdaFunction(self.warmpool_fn)],
        )

        # --- IAM Permissions ---

        # AgentCore Runtime invocation (for warmup requests)
        self.warmpool_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )

        # DynamoDB read/write (warm pool records in identity table)
        self.warmpool_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                ],
                resources=[identity_table_arn],
            )
        )

        # KMS — needed when DynamoDB table uses CMK encryption
        if cmk_arn:
            self.warmpool_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    resources=[cmk_arn],
                )
            )

        # --- Outputs ---
        CfnOutput(self, "WarmPoolLambdaArn", value=self.warmpool_fn.function_arn)

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.warmpool_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda basic execution role is AWS managed — acceptable for logging.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="AgentCore runtime ARN requires wildcard for qualifier. "
                    "DynamoDB scoped to identity table.",
                    applies_to=[
                        f"Resource::arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_arn.split('/')[-1]}/*",
                        f"Resource::<AgentRuntime.AgentRuntimeId>/*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.12 is the latest stable runtime with full boto3 "
                    "bedrock-agentcore support.",
                ),
            ],
            apply_to_children=True,
        )
