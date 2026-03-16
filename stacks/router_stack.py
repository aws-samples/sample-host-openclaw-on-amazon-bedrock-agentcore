"""Router Stack — API Gateway HTTP API for Telegram/Slack/Feishu webhook ingestion.

Deploys the Router Lambda behind an API Gateway HTTP API with explicit
routes for each webhook path. Webhook secret validation (Telegram
secret_token header, Slack HMAC signature, Feishu X-Lark-Signature)
is enforced inside the Lambda. Also creates the DynamoDB identity table
for user resolution and cross-channel binding.
"""

from aws_cdk import (
    CfnOutput,
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
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class RouterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_id: str,
        gateway_token_secret_name: str,
        telegram_token_secret_name: str,
        slack_token_secret_name: str,
        feishu_token_secret_name: str,
        webhook_secret_name: str,
        cmk_arn: str,
        user_files_bucket_name: str,
        user_files_bucket_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        lambda_timeout = int(self.node.try_get_context("router_lambda_timeout_seconds") or "300")
        lambda_memory = int(self.node.try_get_context("router_lambda_memory_mb") or "256")
        registration_open = str(self.node.try_get_context("registration_open") or "false").lower()

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
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=identity_cmk,
        )

        # --- Log Group ---
        router_log_group = logs.LogGroup(
            self,
            "RouterLogGroup",
            log_group_name="/openclaw/lambda/router",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Lambda Function ---
        self.router_fn = _lambda.Function(
            self,
            "RouterFn",
            function_name="openclaw-router",
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
            api_name="openclaw-router",
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
                    f"arn:aws:lambda:{region}:{account}:function:openclaw-router",
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
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                ],
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
            self.router_fn,
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
                    "grant_read_write_data adds index wildcards and KMS wildcards "
                    "for CMK-encrypted table. S3 PutObject scoped to */_uploads/* "
                    "prefix for image uploads.",
                    applies_to=[
                        f"Resource::{runtime_arn}/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                        f"Resource::{self.identity_table.table_arn}/index/*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*/_uploads/*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
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
                    "(CfnStage.access_log_settings) to /openclaw/api-access log group. "
                    "cdk-nag cannot detect L1-level access log configuration.",
                ),
            ],
            apply_to_children=True,
        )
