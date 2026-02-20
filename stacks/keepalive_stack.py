"""Keepalive Stack — Lambda + EventBridge to keep AgentCore Runtime session alive.

Invokes the AgentCore Runtime every 5 minutes to:
1. Start the OpenClaw container session on first invocation
2. Keep the session alive by sending keepalive pings
3. Restart the session after 8-hour max lifetime termination
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    RemovalPolicy,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class KeepaliveStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # Explicit log group avoids CDK's auto-created LogRetention Lambda
        keepalive_log_group = logs.LogGroup(
            self,
            "KeepaliveLogGroup",
            log_group_name="/openclaw/lambda/keepalive",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Lambda function --------------------------------------------------
        self.keepalive_fn = _lambda.Function(
            self,
            "KeepaliveFn",
            function_name="openclaw-keepalive",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/keepalive"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_id,
                "SESSION_ID": "openclaw-telegram-session-primary-keepalive-001",
            },
            log_group=keepalive_log_group,
        )

        # Grant the Lambda permission to invoke AgentCore Runtime
        self.keepalive_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=["*"],
            )
        )

        # --- EventBridge Rule (every 5 minutes) ------------------------------
        self.rule = events.Rule(
            self,
            "KeepaliveRule",
            rule_name="openclaw-keepalive",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            description="Invoke OpenClaw AgentCore Runtime every 5 minutes to keep session alive",
        )
        self.rule.add_target(targets.LambdaFunction(self.keepalive_fn))

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.keepalive_fn,
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
                    reason="bedrock-agentcore:InvokeAgentRuntime does not support resource-level ARNs.",
                    applies_to=["Resource::*"],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.13 is the latest stable runtime supported in all regions. "
                    "Will upgrade to 3.14 when broadly available in Lambda.",
                ),
            ],
            apply_to_children=True,
        )
