"""Gateway Stack — AgentCore Gateway exposing the in-house skills as MCP tools (prototype).

Opt-in via cdk.json context ``enable_gateway`` (default false). app.py only
instantiates this stack when the flag is on, so with the flag off the synth
output of every existing stack is unchanged.

What it creates
  * An AgentCore Gateway (MCP protocol) with a CUSTOM_JWT inbound authorizer
    that trusts the existing Cognito user pool (OpenClawSecurity) and only the
    ``openclaw-proxy`` app client — the same client whose ID token the Bedrock
    proxy already mints per user (bridge/agentcore-proxy.js getCognitoToken).
  * A REQUEST interceptor Lambda (passRequestHeaders=true) that copies the
    caller's bearer JWT into the reserved ``__caller_token`` tool argument.
    This is the documented way for verified identity to reach a Lambda target:
    the Lambda-target contract carries no claims and ``Authorization`` cannot be
    allowlisted for header propagation.
      https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html
      https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-configuration.html
      https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html
  * Two Lambda MCP targets, ``user-files`` and ``schedules``, whose functions
    re-verify the JWT (RS256 against the pool's JWKS) and derive the user
    namespace from it — never from a model-supplied argument.
  * Least-privilege IAM per Lambda, reusing the deterministic resource names the
    exec skills use today (openclaw-user-files-<acct>-<region> bucket,
    openclaw-cron schedule group, openclaw-cron-scheduler-role-<region>,
    openclaw-identity table, openclaw-cron-executor function).

Outputs consumed by scripts/deploy.sh (exact OutputKey match): GatewayUrl, GatewayId.
"""

import json
import pathlib

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrockagentcore as agentcore,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "lambda" / "gateway_tools"
_SCHEMA_FILE = _TOOLS_DIR / "tool-schemas.json"

GATEWAY_NAME = "openclaw-tools"
TARGET_USER_FILES = "user-files"
TARGET_SCHEDULES = "schedules"


def load_tool_schemas() -> dict:
    """Read lambda/gateway_tools/tool-schemas.json (single source of truth)."""
    with _SCHEMA_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)
    data.pop("_comment", None)
    return data


def _schema_property(prop: dict):
    """Translate one JSON-schema-ish property into the CfnGatewayTarget SchemaDefinitionProperty."""
    kwargs = {"type": prop["type"]}
    if "description" in prop:
        kwargs["description"] = prop["description"]
    if prop["type"] == "array" and "items" in prop:
        kwargs["items"] = _schema_property(prop["items"])
    if prop["type"] == "object":
        if "properties" in prop:
            kwargs["properties"] = {k: _schema_property(v) for k, v in prop["properties"].items()}
        if "required" in prop:
            kwargs["required"] = list(prop["required"])
    return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(**kwargs)


def _tool_definitions(tools: list) -> list:
    return [
        agentcore.CfnGatewayTarget.ToolDefinitionProperty(
            name=t["name"],
            description=t["description"],
            input_schema=_schema_property(t["inputSchema"]),
        )
        for t in tools
    ]


class GatewayStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cognito_issuer_url: str,
        cognito_client_id: str,
        cmk_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # Deterministic names shared with the exec skills / other stacks. Using
        # strings (not cross-stack references) mirrors app.py's identity-table
        # handling and keeps this stack deployable in Phase 1 alongside
        # OpenClawSecurity, before the runtime exists.
        user_files_bucket_arn = f"arn:aws:s3:::openclaw-user-files-{account}-{region}"
        identity_table_arn = f"arn:aws:dynamodb:{region}:{account}:table/openclaw-identity"
        cron_lambda_arn = f"arn:aws:lambda:{region}:{account}:function:openclaw-cron-executor"
        scheduler_role_arn = f"arn:aws:iam::{account}:role/openclaw-cron-scheduler-role-{region}"
        schedule_arn_pattern = f"arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*"

        # One asset for all three functions so lib/ is shared; tests excluded.
        code = _lambda.Code.from_asset(
            str(_TOOLS_DIR),
            exclude=["*.test.js", "test-helpers.js", "node_modules", "README*"],
        )
        common_env = {
            "COGNITO_ISSUER_URL": cognito_issuer_url,
            "COGNITO_CLIENT_ID": cognito_client_id,
            "NODE_OPTIONS": "--enable-source-maps",
        }

        def _fn(name: str, handler: str, env: dict, description: str) -> _lambda.Function:
            log_group = logs.LogGroup(
                self,
                f"{name}LogGroup",
                log_group_name=f"/openclaw/lambda/gateway-{name.lower()}",
                retention=retention_days(log_retention),
                removal_policy=RemovalPolicy.DESTROY,
            )
            return _lambda.Function(
                self,
                f"{name}Fn",
                function_name=f"openclaw-gateway-{name.lower()}",
                runtime=_lambda.Runtime.NODEJS_22_X,
                handler=handler,
                code=code,
                timeout=Duration.seconds(30),
                memory_size=256,
                environment=env,
                log_group=log_group,
                description=description,
            )

        # --- REQUEST interceptor -------------------------------------------
        self.interceptor_fn = _fn(
            "Interceptor",
            "interceptor/index.handler",
            {},
            "AgentCore Gateway REQUEST interceptor: copies the verified bearer JWT into __caller_token",
        )

        # --- user_files tool Lambda ------------------------------------------
        self.user_files_fn = _fn(
            "UserFiles",
            "s3_user_files/index.handler",
            {**common_env, "S3_USER_FILES_BUCKET": f"openclaw-user-files-{account}-{region}"},
            "Gateway MCP target: per-user S3 files (list/read/write/delete)",
        )
        self.user_files_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[user_files_bucket_arn],
            )
        )
        self.user_files_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[f"{user_files_bucket_arn}/*"],
            )
        )
        # Bucket objects are SSE-KMS encrypted with the security CMK.
        self.user_files_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
            )
        )

        # --- schedules tool Lambda -------------------------------------------
        self.schedules_fn = _fn(
            "Schedules",
            "eventbridge_cron/index.handler",
            {
                **common_env,
                "EVENTBRIDGE_SCHEDULE_GROUP": "openclaw-cron",
                "CRON_LAMBDA_ARN": cron_lambda_arn,
                "EVENTBRIDGE_ROLE_ARN": scheduler_role_arn,
                "IDENTITY_TABLE_NAME": "openclaw-identity",
            },
            "Gateway MCP target: per-user EventBridge schedules (create/list/update/delete)",
        )
        self.schedules_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                ],
                resources=[schedule_arn_pattern],
            )
        )
        self.schedules_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[scheduler_role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        self.schedules_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                ],
                resources=[identity_table_arn],
            )
        )

        # --- Gateway service role ---------------------------------------------
        # Trust policy per the Gateway prerequisites doc; SourceArn is added
        # after creation there, SourceAccount is known up front.
        #   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-prerequisites-permissions.html
        self.gateway_role = iam.Role(
            self,
            "GatewayServiceRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"},
                },
            ),
            description="Assumed by AgentCore Gateway to invoke the OpenClaw tool and interceptor Lambdas",
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    self.interceptor_fn.function_arn,
                    self.user_files_fn.function_arn,
                    self.schedules_fn.function_arn,
                ],
            )
        )
        # The Gateway encrypts its target configuration with the CMK using this
        # role (the live create failed with "GenesisMCPTargetTargetEncryption is
        # not authorized to perform: kms:GenerateDataKey" without it). Actions and
        # conditions follow the customer-managed-key doc; the encryption context
        # is the gateway ARN, which is not known before creation, hence the
        # wildcard on the gateway id.
        #   https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-encryption.html
        gateway_arn_pattern = f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"
        via_service = {"kms:ViaService": f"bedrock-agentcore.{region}.amazonaws.com"}
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayCmkDescribe",
                actions=["kms:DescribeKey"],
                resources=[cmk_arn],
                conditions={"StringEquals": via_service},
            )
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayCmkDataKeys",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": via_service,
                    "StringLike": {"kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn": gateway_arn_pattern},
                },
            )
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayCmkGrant",
                actions=["kms:CreateGrant"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {**via_service, "kms:GrantConstraintType": "EncryptionContextSubset"},
                    "ForAllValues:StringEquals": {"kms:GrantOperations": ["Decrypt", "GenerateDataKey"]},
                    "StringLike": {"kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn": gateway_arn_pattern},
                },
            )
        )

        # --- Gateway -----------------------------------------------------------
        discovery_url = f"{cognito_issuer_url}/.well-known/openid-configuration"
        self.gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name=GATEWAY_NAME,
            description="OpenClaw in-house skills as MCP tools (prototype)",
            protocol_type="MCP",
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    instructions=(
                        "Tools operate on the calling user's own files and schedules. "
                        "The user namespace is fixed by the caller's identity and cannot be chosen."
                    ),
                    search_type="SEMANTIC",
                ),
            ),
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_clients=[cognito_client_id],
                ),
            ),
            role_arn=self.gateway_role.role_arn,
            kms_key_arn=cmk_arn,
            exception_level="DEBUG",
            interceptor_configurations=[
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self.interceptor_fn.function_arn,
                        ),
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True,
                    ),
                ),
            ],
        )
        self.gateway.node.add_dependency(self.gateway_role)

        # --- Lambda MCP targets ----------------------------------------------
        schemas = load_tool_schemas()
        self.targets = {}
        for target_name, fn in (
            (TARGET_USER_FILES, self.user_files_fn),
            (TARGET_SCHEDULES, self.schedules_fn),
        ):
            target = agentcore.CfnGatewayTarget(
                self,
                f"Target{target_name.title().replace('_', '')}",
                name=target_name,
                description=schemas[target_name]["description"],
                gateway_identifier=self.gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE",
                    )
                ],
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=_tool_definitions(schemas[target_name]["tools"]),
                            ),
                        ),
                    ),
                ),
            )
            target.node.add_dependency(self.gateway)
            self.targets[target_name] = target

        # Targets are created sequentially; the Gateway API rejects concurrent
        # target mutations on one gateway.
        self.targets[TARGET_SCHEDULES].node.add_dependency(self.targets[TARGET_USER_FILES])

        self.gateway_url = self.gateway.attr_gateway_url

        # --- Outputs (exact OutputKeys used by scripts/deploy.sh) ---------------
        CfnOutput(self, "GatewayUrl", value=self.gateway_url)
        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "GatewayServiceRoleArn", value=self.gateway_role.role_arn)

        # --- cdk-nag suppressions ------------------------------------------------
        for fn in (self.interceptor_fn, self.user_files_fn, self.schedules_fn):
            cdk_nag.NagSuppressions.add_resource_suppressions(
                fn,
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
                        reason="Object-level S3 access is scoped to the openclaw-user-files bucket; "
                        "per-user isolation is enforced in code from the verified JWT namespace. "
                        "Scheduler actions are scoped to the openclaw-cron group, as for the runtime role.",
                        applies_to=[
                            f"Resource::{user_files_bucket_arn}/*",
                            f"Resource::{schedule_arn_pattern}",
                        ],
                    ),
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-L1",
                        reason="Node.js 22 is the latest LTS Lambda runtime with the bundled AWS SDK v3.",
                    ),
                ],
                apply_to_children=True,
            )
