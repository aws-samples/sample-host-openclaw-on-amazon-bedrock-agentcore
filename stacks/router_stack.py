"""Router Stack — API Gateway HTTP API for Telegram/Slack/Feishu webhook ingestion.

Deploys the Router Lambda behind an API Gateway HTTP API with explicit
routes for each webhook path. Webhook secret validation (Telegram
secret_token header, Slack HMAC signature, Feishu X-Lark-Signature)
is enforced inside the Lambda. Also creates the DynamoDB identity table
for user resolution and cross-channel binding.
"""

import os
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from aws_cdk import (
    Annotations,
    CfnParameter,
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    custom_resources as cr,
)
import cdk_nag
from constructs import Construct

from stacks import DeploymentNamer, retention_days, stateful_removal_policy


class RouterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_id: str,
        gateway_token_secret_name: str,
        gateway_token_secret_arn: str,
        telegram_token_secret_name: str,
        telegram_token_secret_arn: str,
        slack_token_secret_name: str,
        slack_token_secret_arn: str,
        feishu_token_secret_name: str,
        feishu_token_secret_arn: str,
        webhook_secret_name: str,
        webhook_secret_arn: str,
        cmk_arn: str,
        user_files_bucket_name: str,
        user_files_bucket_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namer = DeploymentNamer.from_scope(self)
        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        lambda_timeout = int(self.node.try_get_context("router_lambda_timeout_seconds") or "300")
        lambda_memory = int(self.node.try_get_context("router_lambda_memory_mb") or "256")
        registration_open = str(self.node.try_get_context("registration_open") or "false").lower()
        telegram_admin_user_id = os.environ.get("TELEGRAM_ADMIN_USER_ID", "").strip()
        router_function_name = namer.name("openclaw-router")
        router_api_name = namer.name("openclaw-router")
        router_log_group_name = namer.name("/openclaw/lambda/router")
        api_access_log_group_name = namer.name("/openclaw/api-access")
        identity_table_name = namer.name("openclaw-identity")
        identity_table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}"
        secret_resource_arns = [
            gateway_token_secret_arn,
            telegram_token_secret_arn,
            slack_token_secret_arn,
            feishu_token_secret_arn,
            webhook_secret_arn,
        ]
        telegram_bot_token_parameter = CfnParameter(
            self,
            "TelegramBotToken",
            type="String",
            default="",
            no_echo=True,
            description="Optional Telegram bot token for deploy-time bootstrap via custom resource.",
        )
        telegram_admin_user_id_parameter = CfnParameter(
            self,
            "TelegramAdminUserId",
            type="String",
            default=telegram_admin_user_id,
            description="Optional Telegram numeric user ID to bootstrap into the allowlist.",
        )

        # --- DynamoDB Identity Table ---
        dynamodb_client = boto3.client("dynamodb", region_name=region)
        try:
            table_description = dynamodb_client.describe_table(
                TableName=identity_table_name
            )["Table"]
            reuse_identity_table = True
            identity_table_kms_arn = (
                table_description.get("SSEDescription", {}).get("KMSMasterKeyArn")
                or cmk_arn
            )
            Annotations.of(self).add_info(
                f"Reusing existing identity table: {identity_table_name}"
            )
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            if error_code == "ResourceNotFoundException":
                reuse_identity_table = False
                identity_table_kms_arn = cmk_arn
            else:
                raise ValueError(
                    "Failed to determine whether the identity table already exists. "
                    f"Table={identity_table_name}. Fix the DynamoDB lookup error: {error_code}"
                ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the identity table already exists because "
                "AWS credentials or the DynamoDB endpoint are unavailable."
            ) from err

        identity_cmk = kms.Key.from_key_arn(self, "IdentityTableCmk", cmk_arn)
        if reuse_identity_table:
            self.identity_table = dynamodb.Table.from_table_arn(
                self,
                "IdentityTable",
                table_arn=identity_table_arn,
            )
        else:
            self.identity_table = dynamodb.Table(
                self,
                "IdentityTable",
                table_name=identity_table_name,
                partition_key=dynamodb.Attribute(
                    name="PK", type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(
                    name="SK", type=dynamodb.AttributeType.STRING
                ),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                removal_policy=stateful_removal_policy(self),
                time_to_live_attribute="ttl",
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True,
                ),
                encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
                encryption_key=identity_cmk,
            )

        logs_client = boto3.client("logs", region_name=region)
        try:
            logs_client.describe_log_groups(logGroupNamePrefix=router_log_group_name)
            router_log_group_exists = any(
                group.get("logGroupName") == router_log_group_name
                for group in logs_client.describe_log_groups(
                    logGroupNamePrefix=router_log_group_name
                ).get("logGroups", [])
            )
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            raise ValueError(
                "Failed to determine whether the router Lambda log group already exists. "
                f"LogGroup={router_log_group_name}. Fix the CloudWatch Logs lookup error: {error_code}"
            ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the router Lambda log group already exists because "
                "AWS credentials or the CloudWatch Logs endpoint are unavailable."
            ) from err

        if router_log_group_exists:
            Annotations.of(self).add_info(
                f"Reusing existing router Lambda log group: {router_log_group_name}"
            )
            router_log_group = logs.LogGroup.from_log_group_name(
                self,
                "RouterLogGroup",
                log_group_name=router_log_group_name,
            )
        else:
            router_log_group = logs.LogGroup(
                self,
                "RouterLogGroup",
                log_group_name=router_log_group_name,
                retention=retention_days(log_retention),
                removal_policy=RemovalPolicy.DESTROY,
            )

        try:
            api_access_log_group_exists = any(
                group.get("logGroupName") == api_access_log_group_name
                for group in logs_client.describe_log_groups(
                    logGroupNamePrefix=api_access_log_group_name
                ).get("logGroups", [])
            )
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            raise ValueError(
                "Failed to determine whether the API access log group already exists. "
                f"LogGroup={api_access_log_group_name}. Fix the CloudWatch Logs lookup error: {error_code}"
            ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the API access log group already exists because "
                "AWS credentials or the CloudWatch Logs endpoint are unavailable."
            ) from err

        if api_access_log_group_exists:
            Annotations.of(self).add_info(
                f"Reusing existing API access log group: {api_access_log_group_name}"
            )
            access_log_group = logs.LogGroup.from_log_group_name(
                self,
                "ApiAccessLogGroup",
                log_group_name=api_access_log_group_name,
            )
        else:
            access_log_group = logs.LogGroup(
                self,
                "ApiAccessLogGroup",
                log_group_name=api_access_log_group_name,
                retention=retention_days(log_retention),
                removal_policy=stateful_removal_policy(self),
            )

        # --- Lambda Function ---
        self.router_fn = _lambda.Function(
            self,
            "RouterFn",
            function_name=router_function_name,
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/router"),
            timeout=Duration.seconds(lambda_timeout),
            memory_size=lambda_memory,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_id,
                "IDENTITY_TABLE_NAME": self.identity_table.table_name,
                "TELEGRAM_TOKEN_SECRET_ID": telegram_token_secret_name,
                "SLACK_TOKEN_SECRET_ID": slack_token_secret_name,
                "FEISHU_TOKEN_SECRET_ID": feishu_token_secret_name,
                "WEBHOOK_SECRET_ID": webhook_secret_name,
                "REGISTRATION_OPEN": registration_open,
                "USER_FILES_BUCKET": user_files_bucket_name,
                "LAMBDA_TIMEOUT_SECONDS": str(lambda_timeout),
            },
            log_group=router_log_group,
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
            api_name=router_api_name,
            description="OpenClaw webhook ingestion API (explicit routes only)",
        )

        # Explicit routes — only these paths are reachable
        self.http_api.add_routes(
            path="/webhook/telegram",
            methods=[apigwv2.HttpMethod.POST],
            integration=lambda_integration,
        )
        self.http_api.add_routes(
            path="/webhook/slack",
            methods=[apigwv2.HttpMethod.POST],
            integration=lambda_integration,
        )
        self.http_api.add_routes(
            path="/webhook/feishu",
            methods=[apigwv2.HttpMethod.POST],
            integration=lambda_integration,
        )
        self.http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=lambda_integration,
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

        # --- IAM Permissions ---

        # AgentCore Runtime invocation — scoped to specific runtime and its endpoints
        # IAM evaluates against runtime/{id}/runtime-endpoint/{endpoint-id}
        self.router_fn.add_to_role_policy(
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

        # DynamoDB read/write
        self.identity_table.grant_read_write_data(self.router_fn)

        # Lambda self-invoke (for async dispatch)
        # Use constructed ARN to avoid circular dependency with Function URL
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{region}:{account}:function:{router_function_name}",
                ],
            )
        )

        # Secrets Manager (channel tokens)
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=secret_resource_arns,
            )
        )

        # KMS decrypt for secrets
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # S3 PutObject for image uploads (scoped to _uploads/ prefix)
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{user_files_bucket_arn}/*/_uploads/*"],
            )
        )

        # KMS GenerateDataKey for S3 bucket encryption (bucket uses KMS CMK)
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey"],
                resources=[cmk_arn],
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[identity_table_kms_arn],
            )
        )

        bootstrap_fn = _lambda.Function(
            self,
            "BootstrapFn",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.on_event",
            code=_lambda.Code.from_asset("lambda/bootstrap"),
            timeout=Duration.seconds(60),
            memory_size=256,
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:UpdateSecret",
                ],
                resources=[
                    telegram_token_secret_arn,
                    webhook_secret_arn,
                ],
            )
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                ],
                resources=[cmk_arn],
            )
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[identity_table_kms_arn],
            )
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                ],
                resources=[identity_table_arn],
            )
        )

        bootstrap_provider = cr.Provider(
            self,
            "BootstrapProvider",
            on_event_handler=bootstrap_fn,
        )

        bootstrap_resource = CustomResource(
            self,
            "TelegramBootstrap",
            service_token=bootstrap_provider.service_token,
            properties={
                "PhysicalResourceId": namer.name("telegram-bootstrap"),
                "TelegramBotToken": telegram_bot_token_parameter.value_as_string,
                "TelegramAdminUserId": telegram_admin_user_id_parameter.value_as_string,
                "TelegramTokenSecretId": telegram_token_secret_name,
                "WebhookSecretId": webhook_secret_name,
                "IdentityTableName": self.identity_table.table_name,
                "ApiUrl": self.http_api.url or "",
            },
        )
        bootstrap_resource.node.add_dependency(self.http_api)
        bootstrap_resource.node.add_dependency(self.identity_table)

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

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            [self.router_fn, bootstrap_fn],
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
                    reason="Router permissions are scoped to the deployed runtime, "
                    "specific secrets, the identity table, and upload prefix. "
                    "CDK-generated DynamoDB/KMS permissions and the runtime endpoint "
                    "sub-resource path require documented wildcards.",
                    applies_to=[
                        "Resource::<Runtime99E3DDFA.AgentRuntimeArn>/*",
                        *[f"Resource::{secret_arn}" for secret_arn in secret_resource_arns],
                        f"Resource::{self.identity_table.table_arn}/index/*",
                        f"Resource::arn:aws:s3:::{user_files_bucket_name}/*/_uploads/*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*/_uploads/*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Bootstrap custom resource updates the specific Telegram "
                    "secret, reads the specific webhook secret, and writes a single "
                    "allowlist row into the environment-scoped identity table.",
                    applies_to=[
                        f"Resource::{telegram_token_secret_arn}",
                        f"Resource::{webhook_secret_arn}",
                        f"Resource::{identity_table_arn}",
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
            bootstrap_provider,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="CDK custom resource provider framework uses the standard "
                    "AWSLambdaBasicExecutionRole managed policy for its helper Lambda.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="CDK custom resource provider framework invokes only the "
                    "stack-defined bootstrap Lambda and requires the standard Lambda "
                    "function-version wildcard on that function ARN.",
                    applies_to=[
                        "Resource::<BootstrapFn2732AD89.Arn>:*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="CDK custom resource provider framework manages its own helper Lambda runtime.",
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.http_api,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason="External webhooks (Telegram, Slack) cannot use IAM/JWT auth. "
                    "Webhook secret validation is enforced in the Lambda handler: "
                    "Telegram X-Telegram-Bot-Api-Secret-Token header and Slack "
                    "X-Slack-Signature HMAC verification. API Gateway throttling "
                    "provides rate limiting. Only explicit POST routes are exposed.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG1",
                    reason="Access logging IS configured via L1 escape hatch "
                    "(CfnStage.access_log_settings) to the API access log group. "
                    "cdk-nag cannot detect L1-level access log configuration.",
                ),
            ],
            apply_to_children=True,
        )
