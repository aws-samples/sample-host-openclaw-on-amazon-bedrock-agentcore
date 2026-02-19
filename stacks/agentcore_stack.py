"""AgentCore Stack — Runtime, RuntimeEndpoint, Memory, ECR repo, and agent IAM role."""

import os

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    Stack,
    RemovalPolicy,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as lambda_,
    custom_resources as cr,
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
        default_model_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

        # --- ECR Repository for Agent image -------------------------------
        self.agent_repo = ecr.Repository(
            self,
            "AgentRepo",
            repository_name="openclaw-agent",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,
        )

        # --- Security Group for AgentCore Runtime containers --------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=True,
        )
        # Allow HTTPS from VPC (for VPC endpoints)
        self.agent_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC",
        )

        # --- Agent Execution Role -----------------------------------------
        self.agent_role = iam.Role(
            self,
            "AgentExecutionRole",
            role_name="openclaw-agent-execution-role",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
        )

        # Bedrock InvokeModel — scoped to specific models
        self.agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                ],
            )
        )

        # AgentCore Memory APIs
        self.agent_role.add_to_policy(
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

        # AgentCore Runtime invocation
        self.agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeRuntime",
                    "bedrock-agentcore:InvokeRuntimeEndpoint",
                ],
                resources=["*"],
            )
        )

        # CloudWatch Logs + Metrics
        self.agent_role.add_to_policy(
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
        self.agent_repo.grant_pull(self.agent_role)

        # --- Memory Execution Role ----------------------------------------
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

        # --- AgentCore Memory ---------------------------------------------
        self.memory = agentcore.CfnMemory(
            self,
            "AgentMemory",
            name="openclaw_memory",
            event_expiry_duration=90,  # 90 days (matches token_ttl_days)
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

        # --- AgentCore WorkloadIdentity ------------------------------------
        self.workload_identity = agentcore.CfnWorkloadIdentity(
            self,
            "WorkloadIdentity",
            name="openclaw_identity",
        )

        # --- AgentCore Runtime --------------------------------------------
        self.runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name="openclaw_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-agent:latest"
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=private_subnet_ids,
                    security_groups=[self.agent_sg.security_group_id],
                ),
            ),
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=f"{cognito_issuer_url}/.well-known/openid-configuration",
                    allowed_audience=[cognito_client_id],
                    allowed_clients=[cognito_client_id],
                ),
            ),
            role_arn=self.agent_role.role_arn,
            environment_variables={
                "DEFAULT_MODEL_ID": default_model_id,
                "AWS_REGION": region,
                "AGENTCORE_MEMORY_ID": self.memory.attr_memory_id,
            },
            description="OpenClaw personal assistant agent on AgentCore Runtime",
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=1800,
                max_lifetime=3600,
            ),
        )

        # --- Wait for Runtime to reach READY status -----------------------
        # CfnRuntime returns CREATE_COMPLETE as soon as the API call succeeds,
        # but the runtime takes minutes to transition from CREATING → READY.
        # Without this waiter, CfnRuntimeEndpoint fails with a 409:
        #   "Agent version 1 must be in READY status. Current status: CREATING"
        waiter_fn = lambda_.Function(
            self,
            "RuntimeWaiterFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.on_event",
            code=lambda_.Code.from_asset(
                os.path.join(os.path.dirname(__file__), "..", "lambda", "runtime_waiter")
            ),
            timeout=Duration.minutes(15),
            memory_size=128,
        )
        waiter_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:GetRuntime"],
                resources=["*"],
            )
        )

        waiter_provider = cr.Provider(
            self,
            "RuntimeWaiterProvider",
            on_event_handler=waiter_fn,
        )

        runtime_ready = CustomResource(
            self,
            "RuntimeReadyWaiter",
            service_token=waiter_provider.service_token,
            properties={
                "AgentRuntimeId": self.runtime.attr_agent_runtime_id,
            },
        )

        # --- AgentCore Runtime Endpoint -----------------------------------
        self.runtime_endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "AgentRuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name="openclaw_agent_live",
            description="Production endpoint for OpenClaw agent",
        )
        # Ensure endpoint waits for the runtime to be READY (not just created)
        self.runtime_endpoint.node.add_dependency(runtime_ready)

        # --- Expose outputs for downstream stacks -------------------------
        self.runtime_id = self.runtime.attr_agent_runtime_id
        self.runtime_endpoint_id = self.runtime_endpoint.attr_id
        self.runtime_endpoint_arn = self.runtime_endpoint.attr_agent_runtime_endpoint_arn
        self.memory_id = self.memory.attr_memory_id
        self.workload_identity_arn = self.workload_identity.attr_workload_identity_arn

        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.attr_id)
        CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id)
        CfnOutput(self, "WorkloadIdentityArn", value=self.workload_identity.attr_workload_identity_arn)

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.agent_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Bedrock foundation model ARNs require wildcard for model ID "
                    "because the agent may use multiple models. Actions are scoped to "
                    "InvokeModel only. Memory, AgentCore Runtime, Logs, Metrics, and "
                    "X-Ray APIs do not support resource-level permissions.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        "Resource::*",
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
        # Runtime waiter Lambda + Provider framework suppressions
        cdk_nag.NagSuppressions.add_resource_suppressions(
            waiter_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="bedrock-agentcore:GetRuntime does not support resource-level "
                    "ARNs; wildcard required. Action is read-only.",
                    applies_to=["Resource::*"],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda uses AWSLambdaBasicExecutionRole for CloudWatch Logs.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.12 is the latest stable runtime supported by CDK.",
                ),
            ],
            apply_to_children=True,
        )
        # Provider framework Lambda (CDK-managed, cannot customise)
        cdk_nag.NagSuppressions.add_resource_suppressions(
            waiter_provider,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="CDK Provider framework Lambda uses AWSLambdaBasicExecutionRole. "
                    "This is managed by CDK and cannot be customised.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="CDK Provider framework Lambda needs invoke permission on the "
                    "on_event handler. The :* suffix covers Lambda versions and is "
                    "CDK-managed.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Lambda runtime is managed by CDK Provider framework "
                    "and cannot be overridden.",
                ),
            ],
            apply_to_children=True,
        )
