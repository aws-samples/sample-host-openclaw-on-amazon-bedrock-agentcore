"""Cron Stack — EventBridge Scheduler + Cron Executor Lambda.

Deploys the EventBridge Scheduler group and cron executor Lambda that fires
on user-defined schedules, invokes per-user AgentCore sessions, and delivers
responses back to the originating channel (Telegram/Slack).

Also grants the AgentCore execution role permissions to create/manage
EventBridge schedules and write cron records to the identity DynamoDB table.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_scheduler as scheduler,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class CronStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_id: str,
        identity_table_name: str,
        identity_table_arn: str,
        telegram_token_secret_name: str,
        slack_token_secret_name: str,
        feishu_token_secret_name: str,
        cmk_arn: str,
        agentcore_execution_role: iam.IRole,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        lambda_timeout = int(self.node.try_get_context("cron_lambda_timeout_seconds") or "600")
        lambda_memory = int(self.node.try_get_context("cron_lambda_memory_mb") or "256")

        # --- EventBridge Scheduler Group ---
        self.schedule_group = scheduler.CfnScheduleGroup(
            self,
            "CronScheduleGroup",
            name="openclaw-cron",
        )

        # --- Scheduler IAM Role (assumed by EventBridge Scheduler to invoke Lambda) ---
        self.scheduler_role = iam.Role(
            self,
            "CronSchedulerRole",
            role_name=f"openclaw-cron-scheduler-role-{region}",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Role assumed by EventBridge Scheduler to invoke the cron executor Lambda",
        )

        # --- CloudWatch Log Group ---
        cron_log_group = logs.LogGroup(
            self,
            "CronLogGroup",
            log_group_name="/openclaw/lambda/cron",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Cron Executor Lambda ---
        self.cron_fn = _lambda.Function(
            self,
            "CronExecutorFn",
            function_name="openclaw-cron-executor",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/cron"),
            timeout=Duration.seconds(lambda_timeout),
            memory_size=lambda_memory,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_id,
                "IDENTITY_TABLE_NAME": identity_table_name,
                "TELEGRAM_TOKEN_SECRET_ID": telegram_token_secret_name,
                "SLACK_TOKEN_SECRET_ID": slack_token_secret_name,
                "FEISHU_TOKEN_SECRET_ID": feishu_token_secret_name,
                "LAMBDA_TIMEOUT_SECONDS": str(lambda_timeout),
            },
            log_group=cron_log_group,
        )

        # Grant scheduler role permission to invoke the Lambda
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self.cron_fn.function_arn],
            )
        )

        # --- Cron Lambda IAM Permissions ---

        # AgentCore Runtime invocation
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[
                    runtime_arn,
                    f"{runtime_arn}/*",
                ],
            )
        )

        # DynamoDB read (identity table — for session lookup)
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                resources=[
                    identity_table_arn,
                    f"{identity_table_arn}/index/*",
                ],
            )
        )

        # Secrets Manager read (channel tokens)
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                ],
            )
        )

        # KMS decrypt for secrets
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # --- AgentCore Execution Role Additions ---
        # Allow the container to create/manage EventBridge schedules

        agentcore_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                    "scheduler:ListSchedules",
                ],
                resources=[
                    f"arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*",
                ],
            )
        )

        # Allow the container to pass the scheduler role to EventBridge
        # Use deterministic ARN to avoid cross-stack cyclic dependency
        scheduler_role_arn = f"arn:aws:iam::{account}:role/openclaw-cron-scheduler-role-{region}"
        agentcore_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[scheduler_role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "scheduler.amazonaws.com",
                    },
                },
            )
        )

        # Allow the container to write CRON# records to the identity table
        agentcore_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                ],
                resources=[
                    identity_table_arn,
                    f"{identity_table_arn}/index/*",
                ],
            )
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "CronLambdaArn",
            value=self.cron_fn.function_arn,
        )
        CfnOutput(
            self,
            "SchedulerRoleArn",
            value=self.scheduler_role.role_arn,
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.cron_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda basic execution role is AWS-recommended for CloudWatch Logs.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="AgentCore InvokeAgentRuntime IAM resource must include "
                    "runtime-endpoint sub-resource path (runtime/{id}/*). "
                    "Secrets Manager scoped to openclaw/* prefix. DynamoDB "
                    "index wildcard needed for query operations.",
                    applies_to=[
                        f"Resource::{runtime_arn}/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.13 is the latest stable runtime supported in all regions.",
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            agentcore_execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="EventBridge Scheduler actions scoped to openclaw-cron/* group. "
                    "DynamoDB actions scoped to identity table for CRON# records. "
                    "iam:PassRole restricted to scheduler.amazonaws.com service.",
                    applies_to=[
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
