"""Trusted Telegram ingress, ordered work queue, and isolated worker plane."""

import re

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Token,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
    aws_sqs as sqs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


REQUIRED_REGION = "eu-west-1"
CONTROL_FUNCTION_NAME = "personal-operator-control-command:live"


class RouterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_iam_arn: str,
        runtime_endpoint_name: str,
        telegram_token_secret_name: str,
        slack_token_secret_name: str,
        feishu_token_secret_name: str,
        webhook_secret_name: str,
        workspace_capability_secret_name: str,
        workspace_broker_role_arn: str,
        workspace_broker_function_name: str,
        workspace_session_role_arn: str,
        cmk_arn: str,
        user_files_bucket_name: str,
        user_files_bucket_arn: str,
        trusted_code_asset_root: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"RouterStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        expected_broker_function = (
            "personal-operator-workspace-credential-broker"
        )
        expected_broker_role_arn = (
            f"arn:aws:iam::{account}:role/"
            f"personal-operator-workspace-credential-broker-{region}"
        )
        expected_workspace_role_arn = (
            f"arn:aws:iam::{account}:role/"
            f"openclaw-workspace-session-role-{region}"
        )
        if workspace_broker_function_name != expected_broker_function:
            raise ValueError("workspace broker function name is not canonical")
        if workspace_broker_role_arn != expected_broker_role_arn:
            raise ValueError("workspace broker role ARN is not canonical")
        if workspace_session_role_arn != expected_workspace_role_arn:
            raise ValueError("workspace session role ARN is not canonical")
        if (
            not Token.is_unresolved(workspace_capability_secret_name)
            and workspace_capability_secret_name
            != "personal-operator/workspace-capability"
        ):
            raise ValueError("workspace capability secret name is not canonical")

        runtime_values = (runtime_arn, runtime_iam_arn, runtime_endpoint_name)
        if runtime_values == ("PLACEHOLDER", "PLACEHOLDER", "PLACEHOLDER"):
            # Foundation/offline synthesis precedes creation of the runtime.
            # Partial placeholders are rejected below so they cannot reach a
            # deployable Router template.
            pass
        else:
            if "PLACEHOLDER" in runtime_values:
                raise ValueError(
                    "runtime ARN, IAM ARN, and endpoint must be concrete together"
                )
            runtime_id_pattern = r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}"
            invocation_arn_pattern = (
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            iam_arn_pattern = (
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:runtime/{runtime_id_pattern}"
            )
            if re.fullmatch(invocation_arn_pattern, runtime_arn) is None:
                raise ValueError(
                    "runtime_arn must be the exact AgentCore invocation ARN"
                )
            if re.fullmatch(iam_arn_pattern, runtime_iam_arn) is None:
                raise ValueError(
                    "runtime_iam_arn must be the exact AgentCore IAM runtime resource"
                )
            if re.fullmatch(r"release_[0-9a-f]{40}", runtime_endpoint_name) is None:
                raise ValueError(
                    "runtime_endpoint_name must be an exact release endpoint"
                )
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        worker_timeout = int(
            self.node.try_get_context("router_lambda_timeout_seconds") or "600"
        )
        if not 60 <= worker_timeout <= 900:
            raise ValueError("worker timeout must be between 60 and 900 seconds")
        worker_memory = int(
            self.node.try_get_context("router_lambda_memory_mb") or "256"
        )
        ingress_timeout = 20
        runtime_lease_ms = (worker_timeout + 60) * 1000
        registration_open = str(
            self.node.try_get_context("registration_open") or "false"
        ).lower()
        if registration_open != "false":
            raise ValueError(
                "external pilot registration must remain closed; use one-time invites"
            )

        # --- DynamoDB Identity Table ---
        identity_cmk = kms.Key.from_key_arn(self, "IdentityTableCmk", cmk_arn)
        self.identity_table = dynamodb.Table(
            self,
            "IdentityTable",
            table_name="openclaw-identity",
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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=identity_cmk,
        )
        self.identity_table.add_global_secondary_index(
            index_name="userId-index",
            partition_key=dynamodb.Attribute(
                name="userId", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # Runtime state has a deliberately separate, single-item-per-user
        # boundary. Session mapping, lease fencing, and deletion tombstone are
        # atomically co-located; tombstones have no DynamoDB TTL.
        self.runtime_state_table = dynamodb.Table(
            self,
            "RuntimeStateTable",
            table_name="personal-operator-runtime-state",
            partition_key=dynamodb.Attribute(
                name="userId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=identity_cmk,
        )

        # One immutable event record combines the execution claim, durable
        # result, and Telegram outbox fence. The worker assigns the TTL only
        # after a record becomes terminal or quarantined, preserving active
        # recovery fences while bounding retained message/result content.
        self.message_ledger_table = dynamodb.Table(
            self,
            "MessageLedgerTable",
            table_name="personal-operator-message-ledger",
            partition_key=dynamodb.Attribute(
                name="eventId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=identity_cmk,
        )
        self.message_ledger_table.add_global_secondary_index(
            index_name="userId-index",
            partition_key=dynamodb.Attribute(
                name="userId", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        self.update_dead_letter_queue = sqs.Queue(
            self,
            "TelegramDeadLetterQueue",
            queue_name="personal-operator-telegram-dead-letter.fifo",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=identity_cmk,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.update_queue = sqs.Queue(
            self,
            "TelegramUpdateQueue",
            queue_name="personal-operator-telegram-updates.fifo",
            fifo=True,
            content_based_deduplication=False,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=identity_cmk,
            enforce_ssl=True,
            visibility_timeout=Duration.seconds(worker_timeout + 60),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.update_dead_letter_queue,
                max_receive_count=5,
            ),
        )

        # --- Log Group ---
        router_log_group = logs.LogGroup(
            self,
            "RouterLogGroup",
            log_group_name="/openclaw/lambda/router",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        worker_log_group = logs.LogGroup(
            self,
            "TelegramWorkerLogGroup",
            log_group_name="/personal-operator/lambda/telegram-worker",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )
        workspace_broker_log_group = logs.LogGroup(
            self,
            "WorkspaceCredentialBrokerLogGroup",
            log_group_name=(
                "/personal-operator/lambda/workspace-credential-broker"
            ),
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Lambda Function ---
        self.router_fn = _lambda.Function(
            self,
            "RouterFn",
            function_name="openclaw-router",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="router.index.handler",
            # Ingress and worker execute from the same reviewed, normalized
            # ARM64 asset.  Do not bypass the manifest-verified release bundle
            # with the raw source directory.
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            timeout=Duration.seconds(ingress_timeout),
            memory_size=worker_memory,
            environment={
                "IDENTITY_TABLE_NAME": self.identity_table.table_name,
                "WEBHOOK_SECRET_ID": webhook_secret_name,
                "UPDATE_QUEUE_URL": self.update_queue.queue_url,
                "REGISTRATION_OPEN": registration_open,
                "LAMBDA_TIMEOUT_SECONDS": str(ingress_timeout),
            },
            log_group=router_log_group,
        )

        self.worker_fn = _lambda.Function(
            self,
            "TelegramWorkerFn",
            function_name="personal-operator-telegram-worker",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="worker.index.lambda_handler",
            # The worker composes trusted modules from router/ and worker/;
            # package their common lambda/ root as one immutable asset.
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            timeout=Duration.seconds(worker_timeout),
            memory_size=worker_memory,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_name,
                "RUNTIME_STATE_TABLE_NAME": self.runtime_state_table.table_name,
                "MESSAGE_LEDGER_TABLE_NAME": self.message_ledger_table.table_name,
                "RUNTIME_LEASE_MS": str(runtime_lease_ms),
                "LAMBDA_TIMEOUT_SECONDS": str(worker_timeout),
                "TELEGRAM_TOKEN_SECRET_ID": telegram_token_secret_name,
                "CONTROL_FUNCTION_NAME": CONTROL_FUNCTION_NAME,
                "WORKSPACE_CAPABILITY_SECRET_ID": (
                    workspace_capability_secret_name
                ),
                "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                    workspace_broker_function_name
                ),
            },
            log_group=worker_log_group,
        )
        workspace_broker_role = iam.Role.from_role_arn(
            self,
            "ImportedWorkspaceCredentialBrokerRole",
            workspace_broker_role_arn,
            mutable=False,
        )
        self.workspace_broker_fn = _lambda.Function(
            self,
            "WorkspaceCredentialBrokerFn",
            function_name=workspace_broker_function_name,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="workspace_broker.index.lambda_handler",
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            role=workspace_broker_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "WORKSPACE_CAPABILITY_SECRET_ID": (
                    workspace_capability_secret_name
                ),
                "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                    workspace_broker_function_name
                ),
                "WORKSPACE_SESSION_ROLE_ARN": workspace_session_role_arn,
                "RUNTIME_STATE_TABLE_NAME": self.runtime_state_table.table_name,
                "S3_USER_FILES_BUCKET": user_files_bucket_name,
                "CMK_ARN": cmk_arn,
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_name,
            },
            log_group=workspace_broker_log_group,
        )
        self.worker_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.update_queue,
                batch_size=1,
                report_batch_item_failures=True,
            )
        )
        logs.MetricFilter(
            self,
            "TelegramWorkerFailedRecordMetric",
            log_group=worker_log_group,
            filter_pattern=logs.FilterPattern.literal(
                '"Telegram FIFO record failed"'
            ),
            metric_namespace="PersonalOperator/Worker",
            metric_name="FailedRecords",
            metric_value="1",
            default_value=0,
        )

        # --- API Gateway HTTP API ---
        # No default_integration — only explicit routes are exposed to reduce
        # attack surface. Unmatched paths return 404 from API Gateway itself.
        lambda_integration = apigwv2_integrations.HttpLambdaIntegration(
            "LambdaIntegration",
            handler=self.router_fn,
        )

        self.http_api = apigwv2.HttpApi(
            self,
            "RouterApi",
            api_name="openclaw-router",
            description="Personal Operator Telegram ingress (explicit routes only)",
        )

        # Explicit routes — only these paths are reachable
        self.http_api.add_routes(
            path="/webhook/telegram",
            methods=[apigwv2.HttpMethod.POST],
            integration=lambda_integration,
        )
        self.http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=lambda_integration,
        )

        # --- Access Logging ---
        access_log_group = logs.LogGroup(
            self,
            "ApiAccessLogGroup",
            log_group_name="/openclaw/api-access",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Throttling + access logging — configured on the default stage
        default_stage = self.http_api.default_stage
        if default_stage:
            cfn_stage = default_stage.node.default_child
            cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
                throttling_burst_limit=50,
                throttling_rate_limit=100,
                detailed_metrics_enabled=True,
            )
            cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
                destination_arn=access_log_group.log_group_arn,
                format='{"requestId":"$context.requestId","ip":"$context.identity.sourceIp","method":"$context.httpMethod","path":"$context.path","status":"$context.status","responseLength":"$context.responseLength","latency":"$context.responseLatency","time":"$context.requestTime"}',
            )

        # --- Split IAM permissions ---
        # Ingress can resolve an invite and durably enqueue it. It cannot call
        # AgentCore, send Telegram messages, upload files, or invoke itself.
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:TransactWriteItems"],
                resources=[self.identity_table.table_arn],
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"],
                resources=[self.update_queue.queue_arn],
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:"
                    f"{webhook_secret_name}-??????",
                ],
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"secretsmanager.{region}.amazonaws.com",
                        "kms:CallerAccount": account,
                    }
                },
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"sqs.{region}.amazonaws.com",
                        "kms:CallerAccount": account,
                    }
                },
            )
        )

        # The ordered worker alone owns runtime and Telegram authority.
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                    "bedrock-agentcore:StopRuntimeSession",
                ],
                resources=[
                    runtime_iam_arn,
                    f"{runtime_iam_arn}/runtime-endpoint/{runtime_endpoint_name}",
                ],
            )
        )
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{region}:{account}:function:{CONTROL_FUNCTION_NAME}"
                ],
            )
        )
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[
                    self.runtime_state_table.table_arn,
                    self.message_ledger_table.table_arn,
                ],
            )
        )
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:"
                    f"{telegram_token_secret_name}-??????",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:"
                    f"{workspace_capability_secret_name}-??????",
                ],
            )
        )
        self.worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                    },
                    "ForAnyValue:StringEquals": {
                        "kms:ViaService": [
                            f"secretsmanager.{region}.amazonaws.com",
                            f"sqs.{region}.amazonaws.com",
                        ]
                    },
                },
            )
        )

        cloudwatch.Alarm(
            self,
            "TelegramWorkerErrorsAlarm",
            alarm_name="personal-operator-telegram-worker-errors",
            metric=self.worker_fn.metric_errors(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "TelegramWorkerThrottlesAlarm",
            alarm_name="personal-operator-telegram-worker-throttles",
            metric=self.worker_fn.metric_throttles(
                period=Duration.minutes(5), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "TelegramWorkerFailedRecordsAlarm",
            alarm_name="personal-operator-telegram-worker-failed-records",
            metric=cloudwatch.Metric(
                namespace="PersonalOperator/Worker",
                metric_name="FailedRecords",
                period=Duration.minutes(1),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "TelegramDeadLetterVisibleAlarm",
            alarm_name="personal-operator-telegram-dlq-visible",
            metric=self.update_dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cloudwatch.Alarm(
            self,
            "TelegramOldestMessageAlarm",
            alarm_name="personal-operator-telegram-oldest-message",
            metric=self.update_queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(1), statistic="Maximum"
            ),
            threshold=300,
            evaluation_periods=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.url or "",
            description="Router API Gateway URL for webhook registration",
        )
        CfnOutput(
            self,
            "IdentityTableName",
            value=self.identity_table.table_name,
        )
        CfnOutput(
            self,
            "RuntimeStateTableName",
            value=self.runtime_state_table.table_name,
        )
        CfnOutput(
            self,
            "MessageLedgerTableName",
            value=self.message_ledger_table.table_name,
        )
        CfnOutput(
            self,
            "TelegramUpdateQueueUrl",
            value=self.update_queue.queue_url,
        )
        CfnOutput(
            self,
            "TelegramDeadLetterQueueUrl",
            value=self.update_dead_letter_queue.queue_url,
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            [self.router_fn, self.worker_fn],
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
                    reason="Secrets Manager appends an unknown six-character suffix "
                    "to each otherwise exact secret name; the exact CMK statements "
                    "use only the AWS-defined GenerateDataKey and ReEncrypt action "
                    "families constrained to this account and DynamoDB service; SQS "
                    "and Secrets Manager remain in separate service-bound statements.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.13 is the latest stable runtime supported in all regions.",
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_broker_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason=(
                        "Python 3.13 is the latest stable runtime supported in "
                        "the required region."
                    ),
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.http_api,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason="Telegram cannot use IAM/JWT auth. Its exact webhook-secret "
                    "header is validated before parsing or durable enqueue. API Gateway "
                    "throttling applies and only Telegram plus health are exposed.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG1",
                    reason="Access logging IS configured via L1 escape hatch "
                    "(CfnStage.access_log_settings) to /openclaw/api-access log group. "
                    "cdk-nag cannot detect L1-level access log configuration.",
                ),
            ],
            apply_to_children=True,
        )
