"""Cron Stack — EventBridge Scheduler + Cron Executor Lambda.

Deploys the EventBridge Scheduler group and cron executor Lambda that fires
on user-defined schedules, invokes per-user AgentCore Runtime sessions, and delivers
responses back to the originating channel (Telegram/Slack/Feishu/WhatsApp).

Also grants the AgentCore Runtime execution role permissions to create/manage
EventBridge schedules and write cron records to the identity DynamoDB table.
"""

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_scheduler as scheduler,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
import cdk_nag
from constructs import Construct

from stacks import DeploymentNamer, retention_days


class CronStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_name: str,
        identity_table_name: str,
        identity_table_arn: str,
        telegram_token_secret_name: str,
        slack_token_secret_name: str,
        feishu_token_secret_name: str,
        cmk_arn: str,
        agentcore_execution_role: iam.IRole,
        user_files_bucket_name: str,
        user_files_bucket_arn: str,
        whatsapp_token_secret_name: str = "",
        line_token_secret_name: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        lambda_timeout = int(self.node.try_get_context("cron_lambda_timeout_seconds") or "600")
        lambda_memory = int(self.node.try_get_context("cron_lambda_memory_mb") or "256")
        # Async cron (default on): a schedule's Lambda invocation starts the
        # CronRunner state machine, which runs the turn in the container and polls
        # it, so a brief is not bound by the Lambda's 900 s limit. Rollback: deploy
        # this stack with -c cron_async=false. Schedules keep targeting the Lambda
        # either way, so no schedule changes in either direction.
        cron_async = str(self.node.try_get_context("cron_async") if self.node.try_get_context("cron_async")
                         is not None else "true").lower() not in ("false", "0", "no", "off")
        namer = DeploymentNamer.from_scope(self)
        schedule_group_name = namer.name("openclaw-cron")
        scheduler_role_name = namer.name(f"openclaw-cron-scheduler-role-{region}")
        # Fixed name, so the Lambda can be given the ARN without a circular reference.
        state_machine_name = namer.name("openclaw-cron-runner")
        state_machine_arn = Stack.of(self).format_arn(
            service="states", resource="stateMachine", resource_name=state_machine_name,
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

        # --- EventBridge Scheduler Group ---
        self.schedule_group = scheduler.CfnScheduleGroup(
            self,
            "CronScheduleGroup",
            name=schedule_group_name,
        )

        # --- Scheduler IAM Role (assumed by EventBridge Scheduler to invoke Lambda) ---
        self.scheduler_role = iam.Role(
            self,
            "CronSchedulerRole",
            role_name=scheduler_role_name,
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Role assumed by EventBridge Scheduler to invoke the cron executor Lambda",
        )

        # --- CloudWatch Log Group ---
        cron_log_group = logs.LogGroup(
            self,
            "CronLogGroup",
            log_group_name=namer.name("/openclaw/lambda/cron"),
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Cron Executor Lambda ---
        self.cron_fn = _lambda.Function(
            self,
            "CronExecutorFn",
            function_name=namer.name("openclaw-cron-executor"),
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/cron"),
            layers=[_lambda.LayerVersion(
                self, "RuntimeRoutingLayer",
                code=_lambda.Code.from_asset("lambda/shared", exclude=["**/__pycache__/**", "**/*.pyc"]),
                compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
                description="Shared per-user AgentCore Runtime route resolution and admission",
            )],
            timeout=Duration.seconds(lambda_timeout),
            # EventBridge Scheduler invokes asynchronously. Lambda's default two
            # async retries re-run a brief that timed out or crashed, so the user
            # gets a duplicate turn (and its "already ran" reply) instead of the brief.
            retry_attempts=0,
            memory_size=lambda_memory,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_name,
                "IDENTITY_TABLE_NAME": identity_table_name,
                "TELEGRAM_TOKEN_SECRET_ID": telegram_token_secret_name,
                "SLACK_TOKEN_SECRET_ID": slack_token_secret_name,
                "FEISHU_TOKEN_SECRET_ID": feishu_token_secret_name,
                "WHATSAPP_TOKEN_SECRET_ID": whatsapp_token_secret_name,
                "LINE_TOKEN_SECRET_ID": line_token_secret_name,
                "LAMBDA_TIMEOUT_SECONDS": str(lambda_timeout),
                "USER_FILES_BUCKET": user_files_bucket_name,
                "CRON_ASYNC_STATE_MACHINE_ARN": state_machine_arn if cron_async else "",
            },
            log_group=cron_log_group,
        )

        # --- Async cron state machine ---
        # launch (Lambda, from the schedule) -> Start -> (Wait 60 s -> Poll)* -> Deliver.
        # Every step is the same Lambda; the state's cronPhase selects the step and
        # its "next" field the branch. Delivery code is the Lambda's own.
        def lambda_step(step_id):
            return tasks.LambdaInvoke(
                self, step_id, lambda_function=self.cron_fn,
                # No payload: the whole state is the event, and the step's output is the
                # Lambda's return value (the next state).
                payload_response_only=True,
                retry_on_service_exceptions=True,
            )

        start_step, poll_step, deliver_step = lambda_step("Start"), lambda_step("Poll"), lambda_step("Deliver")
        done = sfn.Succeed(self, "Done")
        wait = sfn.Wait(self, "WaitBeforePoll", time=sfn.WaitTime.duration(Duration.seconds(60)))
        # A Start or Poll step that errors (after the service retries) still sends
        # the user the fixed failure notice. A failing Deliver is not retried, so
        # nothing is sent twice.
        # The catch keeps the step's input (plus $.caught); this only switches it to
        # the Deliver step, which reports a caught error as step_failed.
        notice = sfn.Pass(self, "StepFailed", result=sfn.Result.from_string("deliver"), result_path="$.cronPhase")
        notice.next(deliver_step)
        start_step.add_catch(notice, result_path="$.caught")
        poll_step.add_catch(notice, result_path="$.caught")
        route = (
            sfn.Choice(self, "NextStep")
            .when(sfn.Condition.string_equals("$.next", "wait"), wait)
            .when(sfn.Condition.string_equals("$.next", "deliver"), deliver_step)
            .otherwise(done)
        )
        start_step.next(route)
        wait.next(poll_step)
        poll_step.next(route)
        deliver_step.next(done)

        runner_log_group = logs.LogGroup(
            self, "CronRunnerLogGroup",
            log_group_name=namer.name("/openclaw/states/cron-runner"),
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.cron_runner = sfn.StateMachine(
            self, "CronRunner",
            state_machine_name=state_machine_name,
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(start_step),
            # A job runs at most 55 min (bridge/cron-jobs.js), plus warm-up, the
            # 5 min poll grace and delivery.
            timeout=Duration.minutes(75),
            tracing_enabled=True,
            # Execution data (brief text, schedule message) stays out of the logs.
            logs=sfn.LogOptions(destination=runner_log_group, level=sfn.LogLevel.ALL,
                                include_execution_data=False),
        )
        # The Lambda starts the runner (schedule invocation -> execution named
        # <scheduleId>-<fire time>, which makes a repeated fire a no-op).
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["states:StartExecution"], resources=[state_machine_arn])
        )

        # Grant scheduler role permission to invoke the Lambda (IAM policy)
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self.cron_fn.function_arn],
            )
        )

        # Grant EventBridge Scheduler service permission via Lambda resource-based policy
        # Required in addition to the IAM role policy above
        self.cron_fn.add_permission(
            "AllowEventBridgeScheduler",
            principal=iam.ServicePrincipal("scheduler.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=f"arn:aws:scheduler:{region}:{account}:schedule/{schedule_group_name}/*",
        )

        # --- Cron Lambda IAM Permissions ---

        # AgentCore Runtime invocation
        if runtime_arn:
            self.cron_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[runtime_arn, f"{runtime_arn}/*"],
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

        # Secrets Manager read: only the channel-token secrets the Lambda
        # delivers through (the same names it gets as *_SECRET_ID env vars).
        # The Cognito password secret, gateway token and per-user secrets
        # under openclaw/user/ are deliberately not readable here.
        cron_secret_names = [
            telegram_token_secret_name,
            slack_token_secret_name,
            feishu_token_secret_name,
            whatsapp_token_secret_name,
            line_token_secret_name,
        ]
        # "-??????" is the 6-character suffix Secrets Manager appends to every
        # secret ARN; it matches exactly one secret per name.
        cron_secret_resources = [
            f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-??????"
            for name in cron_secret_names
            if name
        ]
        if cron_secret_resources:
            self.cron_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=cron_secret_resources,
                )
            )

        # KMS decrypt for secrets (and for the user-files bucket, which uses the same CMK)
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # S3 read of brief audio (BRIEF_AUDIO marker -> Telegram sendAudio).
        # Read-only and mp3-only. IAM can't scope this per user, so the Lambda
        # checks that the key sits under the run's own namespace before reading.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{user_files_bucket_arn}/*/*.mp3"],
            )
        )
        # List the bucket to find a brief mp3 a run uploaded without a BRIEF_AUDIO
        # line. Keys only; the Lambda lists just the run's own namespace prefix.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[user_files_bucket_arn],
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
                    f"arn:aws:scheduler:{region}:{account}:schedule/{schedule_group_name}/*",
                ],
            )
        )

        # Allow the container to pass the scheduler role to EventBridge
        # Use deterministic ARN to avoid cross-stack cyclic dependency
        scheduler_role_arn = f"arn:aws:iam::{account}:role/{scheduler_role_name}"
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
        CfnOutput(self, "CronRunnerArn", value=self.cron_runner.state_machine_arn)

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
                    "DynamoDB index wildcard needed for query operations. S3 "
                    "GetObject limited to */*.mp3 brief audio; the per-user "
                    "namespace check is enforced in code.",
                    applies_to=[
                        *([f"Resource::{runtime_arn}/*"] if runtime_arn else []),
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*/*.mp3",
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
            self.cron_runner,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Generated by CDK for the state machine role: lambda:InvokeFunction on the cron "
                    "executor's versions (<arn>:*), and X-Ray / CloudWatch Logs delivery actions, which "
                    "do not support resource-level permissions.",
                    applies_to=[
                        "Resource::<CronExecutorFn28CAB097.Arn>:*",
                        "Resource::*",
                    ],
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
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/{schedule_group_name}/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
