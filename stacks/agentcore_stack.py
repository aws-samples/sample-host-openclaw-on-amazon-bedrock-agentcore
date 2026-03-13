"""AgentCore Stack — Hosts OpenClaw on AgentCore Runtime.

Deploys the OpenClaw messaging bridge as a container on AgentCore Runtime.
The container runs an AgentCore contract server on port 8080, a Bedrock
proxy on port 18790, and OpenClaw gateway on port 18789 (started lazily
per user session).
"""

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct

# Regions where AgentCore Browser (CfnBrowserCustom) is confirmed available.
BROWSER_SUPPORTED_REGIONS = {"us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1", "ap-southeast-2"}


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
        cognito_issuer_url: str,
        cognito_client_id: str,
        cognito_user_pool_id: str,
        cognito_password_secret_name: str,
        gateway_token_secret_name: str,
        guardrail_id: str = "",
        guardrail_version: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

        # --- ECR Repository for OpenClaw bridge image -------------------------
        self.bridge_repo = ecr.Repository(
            self,
            "BridgeRepo",
            repository_name="openclaw-bridge",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,
        )

        # --- Security Group for AgentCore Runtime containers ------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=False,
        )
        self.agent_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS to VPC endpoints and internet (web_fetch/web_search tools)",
        )
        self.agent_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC",
        )

        # --- Execution Role (what the container can do) -----------------------
        execution_role_name = "openclaw-agentcore-execution-role"
        # Deterministic ARN avoids CDK circular dependency when the role
        # references itself in its trust policy and inline policy.
        execution_role_arn_str = f"arn:aws:iam::{account}:role/{execution_role_name}"
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
            role_name=execution_role_name,
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
        )

        # Bedrock model invocation
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                ],
            )
        )

        # Bedrock Guardrails — ApplyGuardrail permission (only when guardrails enabled)
        if guardrail_id:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{region}:{account}:guardrail/*",
                    ],
                )
            )

        # Secrets Manager — scoped to the 2 secrets the container actually needs
        # (gateway token for WebSocket auth, Cognito secret for identity derivation)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/gateway-token-*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/cognito-password-secret-*",
                ],
            )
        )
        # Secrets Manager — per-user API key storage (manage_secret tool).
        # Session policy further restricts to openclaw/user/{namespace}/* per user.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/user/*",
                ],
            )
        )
        # ListSecrets does not support resource-level restrictions (AWS API limitation).
        # Results filtered by prefix in application code (executeManageSecret).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:ListSecrets"],
                resources=["*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # Cognito admin operations for auto-provisioning identities
        # Scoped to specific user pool
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/{cognito_user_pool_id}",
                ],
            )
        )

        # STS self-assume for per-user scoped S3 credentials
        # The container assumes its own role with a session policy that restricts
        # S3 access to the user's namespace prefix, preventing cross-user access.
        # Two parts required:
        #   1. IAM permission to call sts:AssumeRole (inline policy)
        #   2. Trust policy entry allowing the role to assume itself
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[execution_role_arn_str],
            )
        )
        self.execution_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                principals=[iam.ArnPrincipal(execution_role_arn_str)],
                conditions={
                    "StringLike": {
                        "sts:RoleSessionName": "scoped-*"
                    }
                },
            )
        )

        # CloudWatch Logs — scoped to /openclaw/ log group prefix
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                ],
            )
        )

        # CloudWatch Metrics — namespace condition prevents alarm falsification
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": [
                            "OpenClaw/AgentCore",
                            "OpenClaw/TokenUsage",
                        ]
                    }
                },
            )
        )

        # X-Ray tracing
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

        # ECR pull
        self.bridge_repo.grant_pull(self.execution_role)

        # --- S3 Bucket for Per-User File Storage ------------------------------
        user_files_ttl_days = int(
            self.node.try_get_context("user_files_ttl_days") or "365"
        )
        user_files_cmk = kms.Key.from_key_arn(self, "UserFilesCmk", cmk_arn)
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"openclaw-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=user_files_cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-user-files",
                    expiration=Duration.days(user_files_ttl_days),
                ),
            ],
            enforce_ssl=True,
            versioned=True,
        )

        # S3 per-user file storage permissions
        self.user_files_bucket.grant_read_write(self.execution_role)

        # --- AgentCore WorkloadIdentity ---------------------------------------
        self.workload_identity = agentcore.CfnWorkloadIdentity(
            self,
            "WorkloadIdentity",
            name="openclaw_identity",
        )

        # --- Default Bedrock model ID -----------------------------------------
        default_model_id = self.node.try_get_context("default_model_id") or "minimax.minimax-m2.1"
        subagent_model_id = self.node.try_get_context("subagent_model_id") or ""
        image_version = str(self.node.try_get_context("image_version") or "1")

        # --- AgentCore Runtime (hosts OpenClaw container) ---------------------
        self.runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name="openclaw_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-bridge:v{image_version}"
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=private_subnet_ids,
                    security_groups=[self.agent_sg.security_group_id],
                ),
            ),
            # QW1/QW2: workload_identity_details and request_header_configuration
            # are not yet available in the installed CDK L1 construct version.
            # Uncomment when aws-cdk-lib is updated with these CfnRuntime properties:
            # workload_identity_details=agentcore.CfnRuntime.WorkloadIdentityDetailsProperty(
            #     workload_identity_arn=self.workload_identity.attr_workload_identity_arn,
            # ),
            # request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
            #     request_header_allowlist=[
            #         "x-bedrock-agentcore-runtime-session-id",
            #         "Authorization",
            #     ]
            # ),
            role_arn=self.execution_role.role_arn,
            environment_variables={
                "AWS_REGION": region,
                "BEDROCK_MODEL_ID": default_model_id,
                "GATEWAY_TOKEN_SECRET_ID": gateway_token_secret_name,
                "COGNITO_USER_POOL_ID": cognito_user_pool_id,
                "COGNITO_CLIENT_ID": cognito_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
                "S3_USER_FILES_BUCKET": self.user_files_bucket.bucket_name,
                "WORKSPACE_SYNC_INTERVAL_MS": str(
                    int(self.node.try_get_context("workspace_sync_interval_seconds") or "300") * 1000
                ),
                "IMAGE_VERSION": image_version,  # bump in cdk.json to force container redeploy
                # Per-user S3 credential scoping — STS AssumeRole with session policy
                "EXECUTION_ROLE_ARN": execution_role_arn_str,
                "CMK_ARN": cmk_arn,
                # EventBridge cron scheduling — deterministic names to avoid circular deps
                "EVENTBRIDGE_SCHEDULE_GROUP": "openclaw-cron",
                "CRON_LAMBDA_ARN": f"arn:aws:lambda:{region}:{account}:function:openclaw-cron-executor",
                "EVENTBRIDGE_ROLE_ARN": f"arn:aws:iam::{account}:role/openclaw-cron-scheduler-role",
                "IDENTITY_TABLE_NAME": "openclaw-identity",
                "CRON_LEAD_TIME_MINUTES": str(
                    self.node.try_get_context("cron_lead_time_minutes") or "5"
                ),
                # Sub-agent model: empty = use same as default_model_id.
                # SUBAGENT_BEDROCK_MODEL_ID is forwarded by the contract server
                # to the proxy for Bedrock model routing.
                "SUBAGENT_BEDROCK_MODEL_ID": subagent_model_id,
                # Bedrock Guardrails — content filtering (empty = disabled)
                "BEDROCK_GUARDRAIL_ID": guardrail_id,
                "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
            },
            description="OpenClaw messaging bridge on AgentCore Runtime (per-user sessions)",
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                # Per-user sessions: idle timeout allows natural termination when
                # user stops chatting. Container returns Healthy (not HealthyBusy).
                idle_runtime_session_timeout=int(
                    self.node.try_get_context("session_idle_timeout") or "1800"
                ),
                max_lifetime=int(
                    self.node.try_get_context("session_max_lifetime") or "28800"
                ),
            ),
        )

        # --- AgentCore Runtime Endpoint ---------------------------------------
        self.runtime_endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "AgentRuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name="openclaw_agent_live",
            description="Production endpoint for OpenClaw on AgentCore",
            agent_runtime_version=self.runtime.attr_agent_runtime_version,
        )
        self.runtime_endpoint.add_dependency(self.runtime)

        # --- AgentCore Browser (optional) -------------------------------------
        enable_browser = str(self.node.try_get_context("enable_browser") or "false").lower() == "true"
        self.browser = None
        if enable_browser:
            if region not in BROWSER_SUPPORTED_REGIONS:
                Annotations.of(self).add_warning(
                    f"enable_browser=true but region {region} is not in "
                    f"BROWSER_SUPPORTED_REGIONS {BROWSER_SUPPORTED_REGIONS}. "
                    f"Browser resource will NOT be deployed."
                )
            else:
                self.browser = agentcore.CfnBrowserCustom(
                    self,
                    "BrowserCustom",
                    name="openclaw_browser",
                    network_configuration=agentcore.CfnBrowserCustom.BrowserNetworkConfigurationProperty(
                        network_mode="VPC",
                        vpc_config=agentcore.CfnBrowserCustom.VpcConfigProperty(
                            subnets=private_subnet_ids,
                            security_groups=[self.agent_sg.security_group_id],
                        ),
                    ),
                    execution_role_arn=self.execution_role.role_arn,
                    recording_config=agentcore.CfnBrowserCustom.RecordingConfigProperty(
                        enabled=False,
                    ),
                    description="AgentCore Browser for OpenClaw (per-user browsing sessions)",
                )

                self.execution_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:StartBrowserSession",
                            "bedrock-agentcore:StopBrowserSession",
                            "bedrock-agentcore:GetBrowserSession",
                            "bedrock-agentcore:UpdateBrowserStream",
                            "bedrock-agentcore:ConnectBrowserAutomationStream",
                        ],
                        resources=[self.browser.attr_browser_arn],
                    )
                )

                # Inject browser identifier into Runtime environment variables.
                # CfnRuntime.environment_variables is a map — override the property
                # to include the new key (CDK L1 constructs expose cfn_options).
                self.runtime.add_property_override(
                    "EnvironmentVariables.BROWSER_IDENTIFIER",
                    self.browser.attr_browser_id,
                )

        # --- Outputs ----------------------------------------------------------
        self.runtime_id = self.runtime.attr_agent_runtime_id
        self.runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}"
        self.runtime_endpoint_id = self.runtime_endpoint.attr_id

        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.attr_id)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
        CfnOutput(self, "WorkloadIdentityArn", value=self.workload_identity.attr_workload_identity_arn)
        CfnOutput(
            self,
            "RuntimeArn",
            value=f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}",
        )
        if self.browser:
            CfnOutput(
                self,
                "BrowserIdentifier",
                value=self.browser.attr_browser_id,
                description="AgentCore Browser identifier",
            )

        # --- cdk-nag suppressions ---------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Bedrock foundation model ARNs require wildcard for model ID. "
                    "Bedrock guardrail ARNs require wildcard for guardrail version. "
                    "Logs, Metrics, X-Ray, and Secrets Manager APIs are scoped to "
                    "project prefix (openclaw/*) or do not support resource-level "
                    "permissions. Cognito scoped to specific user pool.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/gateway-token-*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/cognito-password-secret-*",
                        "Resource::*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                        # S3 per-user file storage bucket (grant_read_write wildcards)
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*",
                        # EventBridge cron scheduling (added by CronStack)
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/openclaw-identity/index/*",
                        # Per-user API key storage in Secrets Manager (manage_secret tool)
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/user/*",
                        # Bedrock Guardrails (wildcard for guardrail version changes)
                        f"Resource::arn:aws:bedrock:{region}:{account}:guardrail/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_files_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not required for user file storage — "
                    "CloudTrail S3 data events provide sufficient audit trail.",
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_files_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not required for user file storage — "
                    "CloudTrail S3 data events provide sufficient audit trail.",
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.agent_sg,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-EC23",
                    reason="Ingress uses VPC CIDR; not open to 0.0.0.0/0.",
                ),
                cdk_nag.NagPackSuppression(
                    id="CdkNagValidationFailure",
                    reason="Security group rule uses Fn::GetAtt for VPC CIDR which "
                    "cannot be validated at synth time.",
                ),
            ],
        )
