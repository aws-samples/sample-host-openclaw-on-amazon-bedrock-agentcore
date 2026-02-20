"""AgentCore Stack — Hosts OpenClaw on AgentCore Runtime.

Deploys the OpenClaw messaging bridge as a container on AgentCore Runtime,
replacing Fargate. The container runs OpenClaw (Telegram/Discord/Slack),
a Bedrock proxy, and an AgentCore contract server on port 8080.

Also provisions AgentCore Memory for conversation persistence.
"""

from aws_cdk import (
    CfnOutput,
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

        # --- S3 Bucket for workspace persistence -------------------------------
        self.workspace_bucket = s3.Bucket(
            self,
            "WorkspaceBucket",
            bucket_name=f"openclaw-workspace-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=kms.Key.from_key_arn(self, "ImportedCmk", cmk_arn),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )

        # --- Security Group for AgentCore Runtime containers ------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=True,
        )
        self.agent_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC",
        )

        # --- Execution Role (what the container can do) -----------------------
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
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

        # Secrets Manager (gateway token, channel tokens, Cognito secret)
        self.execution_role.add_to_policy(
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
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # S3 workspace persistence
        self.workspace_bucket.grant_read_write(self.execution_role)

        # Cognito admin operations for auto-provisioning identities
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/*",
                ],
            )
        )

        # AgentCore Memory APIs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:CreateMemory",
                    "bedrock:GetMemory",
                    "bedrock:ListMemories",
                    "bedrock:DeleteMemory",
                    "bedrock:CreateMemoryEvent",
                    "bedrock:ListMemoryEvents",
                    "bedrock:RetrieveMemories",
                ],
                resources=["*"],
            )
        )

        # CloudWatch Logs + Metrics + X-Ray
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

        # ECR pull
        self.bridge_repo.grant_pull(self.execution_role)

        # --- Memory Execution Role --------------------------------------------
        self.memory_role = iam.Role(
            self,
            "MemoryExecutionRole",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
        )
        self.memory_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{region}::foundation-model/*"],
            )
        )

        # --- AgentCore Memory -------------------------------------------------
        self.memory = agentcore.CfnMemory(
            self,
            "AgentMemory",
            name="openclaw_memory",
            event_expiry_duration=90,
            description="OpenClaw conversation memory",
            encryption_key_arn=cmk_arn,
            memory_execution_role_arn=self.memory_role.role_arn,
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="openclaw_semantic",
                        description="Extract factual knowledge from conversations",
                    )
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="openclaw_user_prefs",
                        description="Track user preferences across sessions",
                    )
                ),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=agentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name="openclaw_summary",
                        description="Summarize conversation sessions",
                    )
                ),
            ],
        )

        # --- AgentCore WorkloadIdentity ---------------------------------------
        self.workload_identity = agentcore.CfnWorkloadIdentity(
            self,
            "WorkloadIdentity",
            name="openclaw_identity",
        )

        # --- Default Bedrock model ID -----------------------------------------
        default_model_id = self.node.try_get_context("default_model_id") or "us.anthropic.claude-sonnet-4-6"

        # --- AgentCore Runtime (hosts OpenClaw container) ---------------------
        self.runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name="openclaw_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-bridge:latest"
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=private_subnet_ids,
                    security_groups=[self.agent_sg.security_group_id],
                ),
            ),
            role_arn=self.execution_role.role_arn,
            environment_variables={
                "AWS_REGION": region,
                "BEDROCK_MODEL_ID": default_model_id,
                "GATEWAY_TOKEN_SECRET_ID": gateway_token_secret_name,
                "COGNITO_USER_POOL_ID": cognito_user_pool_id,
                "COGNITO_CLIENT_ID": cognito_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
                "AGENTCORE_MEMORY_ID": self.memory.attr_memory_id,
                "WORKSPACE_BUCKET": self.workspace_bucket.bucket_name,
                "IMAGE_VERSION": "10",  # bump to force container redeploy
            },
            description="OpenClaw messaging bridge on AgentCore Runtime",
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                # Max values to keep the container running as long as possible.
                # HealthyBusy ping status prevents idle termination.
                idle_runtime_session_timeout=28800,  # 8 hours
                max_lifetime=28800,  # 8 hours
            ),
        )

        # --- AgentCore Runtime Endpoint ---------------------------------------
        self.runtime_endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "AgentRuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name="openclaw_agent_live",
            description="Production endpoint for OpenClaw on AgentCore",
        )
        self.runtime_endpoint.add_dependency(self.runtime)

        # --- Outputs ----------------------------------------------------------
        self.runtime_id = self.runtime.attr_agent_runtime_id
        self.runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}"
        self.runtime_endpoint_id = self.runtime_endpoint.attr_id
        self.memory_id = self.memory.attr_memory_id

        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.attr_id)
        CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id)
        CfnOutput(self, "WorkspaceBucketName", value=self.workspace_bucket.bucket_name)
        CfnOutput(self, "WorkloadIdentityArn", value=self.workload_identity.attr_workload_identity_arn)
        CfnOutput(
            self,
            "RuntimeArn",
            value=f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}",
        )

        # --- cdk-nag suppressions ---------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Bedrock foundation model ARNs require wildcard for model ID. "
                    "Memory, Logs, Metrics, X-Ray, and Secrets Manager APIs are scoped "
                    "to project prefix (openclaw/*) or do not support resource-level "
                    "permissions. Cognito userpool/* is scoped to this account/region. "
                    "S3 workspace bucket uses /* for object-level read/write.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                        f"Resource::arn:aws:cognito-idp:{region}:{account}:userpool/*",
                        "Resource::*",
                        "Resource::<WorkspaceBucket53E30B92.Arn>/*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.memory_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Memory execution role needs InvokeModel on foundation models "
                    "for memory extraction. Wildcard required for model ID.",
                    applies_to=[
                        f"Resource::arn:aws:bedrock:{region}::foundation-model/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Workspace bucket stores agent workspace files (markdown). "
                    "Access logging not required for non-sensitive operational data.",
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
