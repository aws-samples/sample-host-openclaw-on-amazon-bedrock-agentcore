"""AgentCore release foundation and Runtime support resources.

Creates the supporting resources that the AgentCore Runtime needs:
  - Execution Role (Bedrock/log/telemetry plus exact broker invocation)
  - Trusted Credential Broker Role (the sole workspace-role assumer)
  - Workspace Session Role (S3/KMS base authority narrowed by the broker)
  - Security Group (VPC networking for the container)
  - S3 Bucket (per-user file storage and workspace sync)

The stack owns the immutable ECR and signing boundary. Runtime resources are
added only for an exact digest-bound release input.
"""

import json
import re
from dataclasses import dataclass

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Duration,
    Stack,
    RemovalPolicy,
    Token,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
    aws_signer as signer,
)
import cdk_nag
from constructs import Construct

REQUIRED_REGION = "eu-west-1"
S3_PREFIX_LIST_ID_EU_WEST_1 = "pl-6da54004"
BEDROCK_INFERENCE_PROFILE_ID = "eu.anthropic.claude-sonnet-4-6"
BEDROCK_FOUNDATION_MODEL_ID = "anthropic.claude-sonnet-4-6"
BEDROCK_DESTINATION_REGIONS = (
    "eu-central-1",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "eu-west-1",
    "eu-west-3",
)
_RUNTIME_ID_ALLOWED_PATTERN = (
    r"^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$"
)
_RUNTIME_VERSION_ALLOWED_PATTERN = r"^[1-9][0-9]{0,4}$"


def _runtime_arn_allowed_pattern(*, account: str, region: str) -> str:
    return (
        rf"^arn:aws:bedrock-agentcore:{region}:{account}:agent/"
        r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
        r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}$"
    )


@dataclass(frozen=True, slots=True)
class AgentCoreRuntimeBinding:
    """Exact endpoint-stage producer objects trusted by consumer stacks."""

    producer_stack: "AgentCoreStack"
    account: str
    region: str
    runtime_id_parameter: CfnParameter
    runtime_version_parameter: CfnParameter
    runtime_arn_parameter: CfnParameter
    runtime_endpoint: agentcore.CfnRuntimeEndpoint
    runtime_endpoint_name: str

    @property
    def runtime_id(self) -> str:
        return self.runtime_id_parameter.value_as_string

    @property
    def runtime_version(self) -> str:
        return self.runtime_version_parameter.value_as_string

    @property
    def runtime_arn(self) -> str:
        return self.runtime_arn_parameter.value_as_string

    @property
    def runtime_endpoint_id(self) -> str:
        return self.runtime_endpoint.attr_id

    def validated_values_for(
        self,
        consumer_stack: Stack,
    ) -> tuple[str, str, str, str, str]:
        """Return values only for this exact producer object and consumer env."""

        if type(self) is not AgentCoreRuntimeBinding:
            raise ValueError("runtime binding type is not canonical")
        producer = self.producer_stack
        if not isinstance(producer, AgentCoreStack):
            raise ValueError("runtime binding producer is not canonical")
        if getattr(producer, "runtime_binding", None) is not self:
            raise ValueError("runtime binding was reconstructed or replaced")
        if producer.node.root is not consumer_stack.node.root:
            raise ValueError("runtime binding crosses its CDK application")
        producer_stack = Stack.of(producer)
        consumer = Stack.of(consumer_stack)
        if (
            self.region != REQUIRED_REGION
            or producer_stack.region != self.region
            or consumer.region != self.region
            or re.fullmatch(r"[0-9]{12}", self.account) is None
            or producer_stack.account != self.account
            or consumer.account != self.account
        ):
            raise ValueError("runtime binding crosses its canonical account or region")

        parameter_contracts = (
            (
                self.runtime_id_parameter,
                getattr(producer, "hardened_runtime_id_parameter", None),
                "HardenedRuntimeId",
                _RUNTIME_ID_ALLOWED_PATTERN,
            ),
            (
                self.runtime_version_parameter,
                getattr(producer, "hardened_runtime_version_parameter", None),
                "HardenedRuntimeVersion",
                _RUNTIME_VERSION_ALLOWED_PATTERN,
            ),
            (
                self.runtime_arn_parameter,
                getattr(producer, "hardened_runtime_arn_parameter", None),
                "HardenedRuntimeArn",
                _runtime_arn_allowed_pattern(
                    account=self.account,
                    region=self.region,
                ),
            ),
        )
        for parameter, exact_parameter, node_id, allowed_pattern in (
            parameter_contracts
        ):
            if (
                parameter is not exact_parameter
                or parameter.node.scope is not producer
                or parameter.node.id != node_id
                or parameter.type != "String"
                or parameter.default is not None
                or parameter.allowed_pattern != allowed_pattern
            ):
                raise ValueError("runtime binding parameter boundary is not canonical")

        endpoint = self.runtime_endpoint
        if (
            endpoint is not getattr(producer, "runtime_endpoint", None)
            or endpoint.node.scope is not producer
            or endpoint.node.id != "BridgeRuntimeEndpoint"
            or endpoint.agent_runtime_id != self.runtime_id
            or endpoint.agent_runtime_version != self.runtime_version
            or endpoint.name != self.runtime_endpoint_name
            or producer.runtime_id != self.runtime_id
            or producer.runtime_version != self.runtime_version
            or producer.runtime_arn != self.runtime_arn
            or producer.runtime_endpoint_id != self.runtime_endpoint_id
            or producer.runtime_endpoint_name != self.runtime_endpoint_name
            or self.runtime_endpoint_name
            != f"release_{producer.runtime_source_commit}"
        ):
            raise ValueError("runtime binding endpoint boundary is not canonical")
        return (
            self.runtime_id,
            self.runtime_version,
            self.runtime_arn,
            self.runtime_endpoint_id,
            self.runtime_endpoint_name,
        )


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
        trusted_endpoint_security_group: ec2.ISecurityGroup,
        s3_prefix_list_id: str,
        workspace_capability_secret_name: str,
        capability_gateway_function_arn: str,
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
        guardrail_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"AgentCoreStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if s3_prefix_list_id != S3_PREFIX_LIST_ID_EU_WEST_1:
            raise ValueError(
                "S3 prefix list must be the exact eu-west-1 AWS-managed subject"
            )
        if (
            not Token.is_unresolved(workspace_capability_secret_name)
            and workspace_capability_secret_name
            != "personal-operator/workspace-capability"
        ):
            raise ValueError("workspace capability secret name is not canonical")
        expected_gateway_arn = (
            f"arn:aws:lambda:{region}:{account}:function:"
            "personal-operator-capability-gateway"
        )
        if capability_gateway_function_arn != expected_gateway_arn:
            raise ValueError("capability gateway function ARN is not canonical")
        guardrail_values = (guardrail_id, guardrail_version, guardrail_arn)
        if any(guardrail_values) != all(guardrail_values):
            raise ValueError("guardrail identity, version, and ARN must be atomic")
        if guardrail_id and not Token.is_unresolved(guardrail_id):
            if re.fullmatch(r"[a-z0-9]+", guardrail_id) is None:
                raise ValueError("guardrail identity is not canonical")
        if guardrail_version and not Token.is_unresolved(guardrail_version):
            if re.fullmatch(r"(?:DRAFT|[1-9][0-9]{0,7})", guardrail_version) is None:
                raise ValueError("guardrail version is not canonical")
        if guardrail_arn and not Token.is_unresolved(guardrail_arn):
            expected_guardrail_arn = (
                f"arn:aws:bedrock:{region}:{account}:guardrail/{guardrail_id}"
            )
            if guardrail_arn != expected_guardrail_arn:
                raise ValueError("guardrail ARN does not match the configured subject")

        # --- Security Group for AgentCore Runtime containers ------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=False,
        )
        self.agent_sg.add_egress_rule(
            peer=trusted_endpoint_security_group,
            connection=ec2.Port.tcp(443),
            description=(
                "Personal Operator runtime HTTPS to trusted AWS interface endpoints only"
            ),
        )
        self.agent_sg.add_egress_rule(
            peer=ec2.Peer.prefix_list(s3_prefix_list_id),
            connection=ec2.Port.tcp(443),
            description=(
                "Personal Operator runtime HTTPS to the exact eu-west-1 S3 "
                "managed prefix list under the scoped gateway policy"
            ),
        )

        # --- Execution Role (what the container can do) -----------------------
        execution_role_name = f"openclaw-agentcore-execution-role-{region}"
        # Deterministic ARN lets the workspace role trust this exact role
        # without creating a CloudFormation reference cycle.
        execution_role_arn_str = f"arn:aws:iam::{account}:role/{execution_role_name}"
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
            role_name=execution_role_name,
            path="/",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com"
            ).with_conditions(
                {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account}:*"
                        )
                    },
                }
            ),
        )

        # The frozen EU inference profile can route to any of these six EU
        # Regions. Grant its exact source ARN separately, then bind every
        # destination model permission to invocations through that profile.
        inference_profile_arn = (
            f"arn:aws:bedrock:{region}:{account}:inference-profile/"
            f"{BEDROCK_INFERENCE_PROFILE_ID}"
        )
        bedrock_invocation_actions = [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
        ]
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=bedrock_invocation_actions,
                resources=[inference_profile_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=bedrock_invocation_actions,
                resources=[
                    f"arn:aws:bedrock:{destination_region}::foundation-model/"
                    f"{BEDROCK_FOUNDATION_MODEL_ID}"
                    for destination_region in BEDROCK_DESTINATION_REGIONS
                ],
                conditions={
                    "StringLike": {
                        "bedrock:InferenceProfileArn": inference_profile_arn,
                    }
                },
            )
        )

        # Bedrock Guardrails — ApplyGuardrail permission (only when guardrails enabled)
        if guardrail_arn:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail_arn],
                )
            )

        # The potentially compromised runtime has neither direct workspace nor
        # STS authority. It may invoke only the trusted credential broker, which
        # derives and applies the exact per-user session policy outside AgentCore.
        workspace_session_role_name = f"openclaw-workspace-session-role-{region}"
        workspace_session_role_arn_str = (
            f"arn:aws:iam::{account}:role/{workspace_session_role_name}"
        )
        workspace_broker_function_name = (
            "personal-operator-workspace-credential-broker"
        )
        workspace_broker_function_arn = (
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{workspace_broker_function_name}"
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[workspace_broker_function_arn],
            )
        )
        # The runtime may invoke exactly the release-owned capability gateway and
        # nothing else in the capability plane.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[capability_gateway_function_arn],
            )
        )

        workspace_broker_role_name = (
            f"personal-operator-workspace-credential-broker-{region}"
        )
        workspace_broker_role_arn_str = (
            f"arn:aws:iam::{account}:role/{workspace_broker_role_name}"
        )
        self.workspace_broker_function_name = workspace_broker_function_name
        self.workspace_broker_role_arn = workspace_broker_role_arn_str
        self.workspace_session_role_arn = workspace_session_role_arn_str
        self.workspace_broker_role = iam.Role(
            self,
            "WorkspaceCredentialBrokerRole",
            role_name=workspace_broker_role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Trusted boundary that validates user/session capabilities before "
                "assuming the workspace base role"
            ),
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[workspace_session_role_arn_str],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    f"arn:aws:dynamodb:{region}:{account}:"
                    "table/personal-operator-runtime-state"
                ],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:"
                    f"{workspace_capability_secret_name}-??????"
                ],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": (
                            f"secretsmanager.{region}.amazonaws.com"
                        ),
                    }
                },
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/lambda/workspace-credential-broker:*"
                ],
            )
        )

        # The reviewed bridge logger writes one fixed group and stream. Keep
        # its application-owned log authority separate from AgentCore's
        # service-managed platform telemetry authority below.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:"
                    "log-group:/openclaw/container"
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:"
                    "log-group:/openclaw/container:log-stream:runtime"
                ],
            )
        )

        # AgentCore emits its own platform logs. These are the exact runtime
        # telemetry permissions required by the service, independent of the
        # bridge's closed-schema application log stream above.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:"
                    "log-group:/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:*"
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:"
                    "log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                ],
            )
        )

        # CloudWatch Metrics — exact service namespace prevents unrelated
        # metric publication.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "bedrock-agentcore"
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
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )

        # --- Immutable runtime image repository and managed signing ----------
        # This repository is an independently retained release boundary. The
        # registry-wide signing configuration has one exact repository filter.
        self.bridge_repository_key = kms.Key(
            self,
            "BridgeRepositoryKey",
            description="KMS key for Personal Operator runtime images",
            enable_key_rotation=True,
            pending_window=Duration.days(30),
            removal_policy=RemovalPolicy.RETAIN,
        )
        lifecycle_policy = json.dumps(
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Expire untagged images after 30 days",
                        "selection": {
                            "tagStatus": "untagged",
                            "countType": "sinceImagePushed",
                            "countUnit": "days",
                            "countNumber": 30,
                        },
                        "action": {"type": "expire"},
                    }
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.bridge_repository = ecr.CfnRepository(
            self,
            "BridgeRepository",
            repository_name="personal-operator/bridge",
            image_tag_mutability="IMMUTABLE",
            image_scanning_configuration=(
                ecr.CfnRepository.ImageScanningConfigurationProperty(
                    scan_on_push=True
                )
            ),
            encryption_configuration=(
                ecr.CfnRepository.EncryptionConfigurationProperty(
                    encryption_type="KMS",
                    kms_key=self.bridge_repository_key.key_arn,
                )
            ),
            lifecycle_policy=ecr.CfnRepository.LifecyclePolicyProperty(
                lifecycle_policy_text=lifecycle_policy
            ),
        )
        self.bridge_repository.apply_removal_policy(
            RemovalPolicy.RETAIN,
            apply_to_update_replace_policy=True,
        )

        self.bridge_signing_profile = signer.CfnSigningProfile(
            self,
            "BridgeSigningProfile",
            platform_id="Notation-OCI-SHA384-ECDSA",
            profile_name="personal_operator_bridge",
            signature_validity_period=(
                signer.CfnSigningProfile.SignatureValidityPeriodProperty(
                    type="DAYS",
                    value=3650,
                )
            ),
        )
        self.bridge_signing_profile.apply_removal_policy(
            RemovalPolicy.RETAIN,
            apply_to_update_replace_policy=True,
        )
        self.bridge_signing_configuration = ecr.CfnSigningConfiguration(
            self,
            "BridgeSigningConfiguration",
            rules=[
                ecr.CfnSigningConfiguration.RuleProperty(
                    signing_profile_arn=self.bridge_signing_profile.attr_arn,
                    repository_filters=[
                        ecr.CfnSigningConfiguration.RepositoryFilterProperty(
                            filter="personal-operator/bridge",
                            filter_type="WILDCARD_MATCH",
                        )
                    ],
                )
            ],
        )
        self.bridge_signing_configuration.apply_removal_policy(
            RemovalPolicy.RETAIN,
            apply_to_update_replace_policy=True,
        )
        self.bridge_signing_configuration.add_dependency(self.bridge_repository)

        # AgentCore can pull only the exact retained release repository.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:"
                    "repository/personal-operator/bridge"
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

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
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="clean-noncurrent-workspace-versions",
                    noncurrent_version_expiration=Duration.days(
                        user_files_ttl_days
                    ),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
            enforce_ssl=True,
            versioned=True,
        )

        # Workspace access base role. Its trust is limited to the exact broker
        # role. AgentCore cannot assume it, even if runtime code omits a policy.
        self.workspace_session_role = iam.Role(
            self,
            "WorkspaceSessionRole",
            role_name=workspace_session_role_name,
            assumed_by=iam.AccountRootPrincipal().with_conditions(
                {
                    "ArnEquals": {"aws:PrincipalArn": workspace_broker_role_arn_str},
                    "StringLike": {"sts:RoleSessionName": "workspace-*"},
                }
            ),
            description="Base S3/KMS role narrowed by per-user STS session policy",
        )
        self.workspace_session_role.node.add_dependency(self.workspace_broker_role)
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[self.user_files_bucket.bucket_arn],
            )
        )
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[self.user_files_bucket.arn_for_objects("*")],
            )
        )
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": "s3.eu-west-1.amazonaws.com",
                        "kms:CallerAccount": account,
                    }
                },
            )
        )

        # --- Immutable AgentCore release resources ---------------------------
        # CloudFormation's create model cannot set requireMMDSV2. The release
        # therefore has three explicit stages: foundation, runtime-only, then
        # endpoint bound to the exact version returned by the hardening update.
        raw_release_stage = str(
            self.node.try_get_context("agentcore_release_stage") or ""
        )
        runtime_source_commit = str(
            self.node.try_get_context("runtime_source_commit") or ""
        )
        runtime_id = str(self.node.try_get_context("runtime_id") or "")
        runtime_endpoint_id = str(
            self.node.try_get_context("runtime_endpoint_id") or ""
        )
        runtime_endpoint_name = str(
            self.node.try_get_context("runtime_endpoint_name") or ""
        )
        runtime_version = str(self.node.try_get_context("runtime_version") or "")
        runtime_arn = str(self.node.try_get_context("runtime_arn") or "")
        runtime_image_uri = str(
            self.node.try_get_context("runtime_image_uri") or ""
        )
        release_inputs = (runtime_source_commit, runtime_image_uri)
        runtime_identity = (runtime_id, runtime_version, runtime_arn)
        endpoint_identity = (runtime_endpoint_id, runtime_endpoint_name)
        all_runtime_values = release_inputs + runtime_identity + endpoint_identity
        if not raw_release_stage:
            if any(all_runtime_values):
                raise ValueError(
                    "agentcore_release_stage is required for runtime release inputs"
                )
            release_stage = "foundation"
        else:
            release_stage = raw_release_stage
        if release_stage not in {"foundation", "runtime", "endpoint"}:
            raise ValueError(
                "agentcore_release_stage must be foundation, runtime, or endpoint"
            )
        self.hardened_runtime_id_parameter = None
        self.hardened_runtime_version_parameter = None
        self.hardened_runtime_arn_parameter = None
        self.runtime_binding = None

        if release_stage == "foundation":
            if any(all_runtime_values):
                raise ValueError(
                    "foundation stage cannot include runtime release inputs"
                )
            # Foundation stacks are synthesizable before any image mutation.
            # Consumer placeholders are intentionally undeployable release
            # identities; the staging transaction never deploys those stacks.
            self.runtime_source_commit = "PLACEHOLDER"
            self.runtime_id = "PLACEHOLDER"
            self.runtime_endpoint_id = "PLACEHOLDER"
            self.runtime_endpoint_name = "PLACEHOLDER"
            self.runtime_version = "PLACEHOLDER"
            self.runtime_arn = "PLACEHOLDER"
            self.runtime_image_uri = "PLACEHOLDER"
            self.runtime_iam_arn = "PLACEHOLDER"
            self.runtime = None
            self.runtime_endpoint = None
            self.runtime_command_deny_policy = None
            self.endpoint_command_deny_policy = None
        else:
            if not all(release_inputs):
                raise ValueError(
                    "runtime_source_commit and runtime_image_uri must be set together"
                )
            source_commit_pattern = r"[0-9a-f]{40}"
            runtime_version_pattern = _RUNTIME_VERSION_ALLOWED_PATTERN
            runtime_id_pattern = _RUNTIME_ID_ALLOWED_PATTERN
            runtime_arn_pattern = (
                _runtime_arn_allowed_pattern(account=account, region=region)
            )
            runtime_image_uri_pattern = (
                rf"{re.escape(account)}\.dkr\.ecr\."
                rf"{re.escape(region)}\.amazonaws\.com/"
                r"personal-operator/bridge"
                r"@sha256:[0-9a-f]{64}"
            )
            if re.fullmatch(source_commit_pattern, runtime_source_commit) is None:
                raise ValueError("runtime_source_commit must be an exact git commit")
            if re.fullmatch(runtime_image_uri_pattern, runtime_image_uri) is None:
                raise ValueError(
                    "runtime_image_uri must be the immutable personal-operator/bridge "
                    f"ECR digest in account {account} and region {region}"
                )

            if release_stage == "runtime" and any(
                runtime_identity + endpoint_identity
            ):
                raise ValueError(
                    "runtime stage cannot include persisted runtime identity"
                )
            expected_endpoint_name = f"release_{runtime_source_commit}"
            if release_stage == "endpoint":
                if any(runtime_identity) and not all(runtime_identity):
                    raise ValueError(
                        "endpoint stage literal runtime identity must be atomic"
                    )
                if all(runtime_identity):
                    if (
                        re.fullmatch(runtime_version_pattern, runtime_version)
                        is None
                    ):
                        raise ValueError("runtime_version is not canonical")
                    if re.fullmatch(runtime_id_pattern, runtime_id) is None:
                        raise ValueError(f"runtime_id is not canonical: {runtime_id}")
                    if re.fullmatch(runtime_arn_pattern, runtime_arn) is None:
                        raise ValueError(
                            "runtime_arn must be the exact AgentCore ARN returned for "
                            f"account {account} in {region}"
                        )
                    if runtime_arn.rsplit(":", 1)[-1] != runtime_version:
                        raise ValueError(
                            "runtime_version must equal the exact runtime ARN version"
                        )
                if any(endpoint_identity) and not all(endpoint_identity):
                    raise ValueError(
                        "runtime endpoint ID and name must be set together"
                    )
                if runtime_endpoint_id and (
                    re.fullmatch(runtime_id_pattern, runtime_endpoint_id) is None
                ):
                    raise ValueError(
                        f"runtime_endpoint_id is not canonical: {runtime_endpoint_id}"
                    )
                if (
                    runtime_endpoint_name
                    and runtime_endpoint_name != expected_endpoint_name
                ):
                    raise ValueError(
                        "runtime_endpoint_name must be derived from the exact "
                        "runtime_source_commit"
                    )

            hardened_runtime_id = runtime_id
            hardened_runtime_version = runtime_version
            hardened_runtime_arn = runtime_arn
            if release_stage == "endpoint":
                self.hardened_runtime_id_parameter = CfnParameter(
                    self,
                    "HardenedRuntimeId",
                    type="String",
                    allowed_pattern=_RUNTIME_ID_ALLOWED_PATTERN,
                )
                hardened_runtime_id = (
                    self.hardened_runtime_id_parameter.value_as_string
                )
                self.hardened_runtime_version_parameter = CfnParameter(
                    self,
                    "HardenedRuntimeVersion",
                    type="String",
                    allowed_pattern=_RUNTIME_VERSION_ALLOWED_PATTERN,
                )
                hardened_runtime_version = (
                    self.hardened_runtime_version_parameter.value_as_string
                )
                self.hardened_runtime_arn_parameter = CfnParameter(
                    self,
                    "HardenedRuntimeArn",
                    type="String",
                    allowed_pattern=_runtime_arn_allowed_pattern(
                        account=account,
                        region=region,
                    ),
                )
                hardened_runtime_arn = (
                    self.hardened_runtime_arn_parameter.value_as_string
                )

            idle_timeout = int(
                self.node.try_get_context("session_idle_timeout") or "1800"
            )
            max_lifetime = int(
                self.node.try_get_context("session_max_lifetime") or "28800"
            )
            workspace_sync_seconds = int(
                self.node.try_get_context("workspace_sync_interval_seconds")
                or "300"
            )
            runtime_environment = {
                "AWS_REGION": region,
                "AWS_DEFAULT_REGION": region,
                # AgentCore's default ADOT application telemetry can include
                # request/response and tool payloads. Keep that transport off;
                # the closed-schema bridge and platform operational logs remain.
                "DISABLE_ADOT_OBSERVABILITY": "true",
                "BEDROCK_MODEL_ID": str(
                    self.node.try_get_context("default_model_id")
                    or BEDROCK_INFERENCE_PROFILE_ID
                ),
                "CAPABILITY_GATEWAY_FUNCTION_ARN": (
                    capability_gateway_function_arn
                ),
                "S3_USER_FILES_BUCKET": self.user_files_bucket.bucket_name,
                "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                    workspace_broker_function_name
                ),
                "WORKSPACE_SYNC_INTERVAL_MS": str(workspace_sync_seconds * 1000),
            }
            subagent_model_id = str(
                self.node.try_get_context("subagent_model_id") or ""
            )
            if subagent_model_id:
                runtime_environment["SUBAGENT_BEDROCK_MODEL_ID"] = (
                    subagent_model_id
                )
            if guardrail_id:
                runtime_environment.update(
                    {
                        "BEDROCK_GUARDRAIL_ID": guardrail_id,
                        "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
                    }
                )

            self.runtime = agentcore.CfnRuntime(
                self,
                "BridgeRuntime",
                agent_runtime_artifact=(
                    agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                        container_configuration=(
                            agentcore.CfnRuntime.ContainerConfigurationProperty(
                                container_uri=runtime_image_uri
                            )
                        )
                    )
                ),
                agent_runtime_name="personal_operator_bridge",
                network_configuration=(
                    agentcore.CfnRuntime.NetworkConfigurationProperty(
                        network_mode="VPC",
                        network_mode_config=(
                            agentcore.CfnRuntime.VpcConfigProperty(
                                subnets=private_subnet_ids,
                                security_groups=[self.agent_sg.security_group_id],
                            )
                        ),
                    )
                ),
                role_arn=self.execution_role.role_arn,
                environment_variables=runtime_environment,
                filesystem_configurations=[
                    agentcore.CfnRuntime.FilesystemConfigurationProperty(
                        session_storage=(
                            agentcore.CfnRuntime.SessionStorageConfigurationProperty(
                                mount_path="/mnt/workspace"
                            )
                        )
                    )
                ],
                lifecycle_configuration=(
                    agentcore.CfnRuntime.LifecycleConfigurationProperty(
                        idle_runtime_session_timeout=idle_timeout,
                        max_lifetime=max_lifetime,
                    )
                ),
                protocol_configuration="HTTP",
                description=(
                    "Personal Operator immutable bridge runtime at commit "
                    f"{runtime_source_commit}"
                ),
                tags={"SourceCommit": runtime_source_commit},
            )
            self.runtime.add_dependency(self.bridge_repository)
            self.runtime.apply_removal_policy(
                RemovalPolicy.RETAIN,
                apply_to_update_replace_policy=True,
            )
            self.runtime_endpoint = None
            if release_stage == "endpoint":
                self.runtime_endpoint = agentcore.CfnRuntimeEndpoint(
                    self,
                    "BridgeRuntimeEndpoint",
                    agent_runtime_id=hardened_runtime_id,
                    agent_runtime_version=hardened_runtime_version,
                    name=expected_endpoint_name,
                )
                self.runtime_endpoint.add_dependency(self.runtime)
                self.runtime_endpoint.apply_removal_policy(
                    RemovalPolicy.RETAIN,
                    apply_to_update_replace_policy=True,
                )

            def command_deny_policy(resource_arn: str) -> str:
                return Stack.of(self).to_json_string(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "DenyRuntimeCommandExecution",
                                "Effect": "Deny",
                                "Principal": "*",
                                "Action": [
                                    (
                                        "bedrock-agentcore:"
                                        "InvokeAgentRuntimeCommand"
                                    ),
                                    (
                                        "bedrock-agentcore:"
                                        "InvokeAgentRuntimeCommandShell"
                                    ),
                                ],
                                "Resource": resource_arn,
                            }
                        ],
                    }
                )

            # AgentCore runtimes created after 2026-03-17 expose command and
            # interactive-shell APIs independently of the model tool catalog.
            # Explicit resource denies on both hierarchical subjects prevent a
            # broad caller identity policy from turning that platform surface
            # into a catalog or credential-boundary bypass.
            runtime_resource_arn = (
                f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/"
                f"{self.runtime.attr_agent_runtime_id}"
            )
            self.runtime_command_deny_policy = agentcore.CfnResourcePolicy(
                self,
                "BridgeRuntimeCommandDenyPolicy",
                resource_arn=runtime_resource_arn,
                policy=command_deny_policy(runtime_resource_arn),
            )
            self.runtime_command_deny_policy.add_dependency(self.runtime)
            self.runtime_command_deny_policy.apply_removal_policy(
                RemovalPolicy.RETAIN,
                apply_to_update_replace_policy=True,
            )
            self.endpoint_command_deny_policy = None
            if self.runtime_endpoint is not None:
                endpoint_resource_arn = (
                    f"arn:aws:bedrock-agentcore:{region}:{account}:"
                    f"runtime/{hardened_runtime_id}/runtime-endpoint/"
                    f"{self.runtime_endpoint.attr_id}"
                )
                self.endpoint_command_deny_policy = agentcore.CfnResourcePolicy(
                    self,
                    "BridgeEndpointCommandDenyPolicy",
                    resource_arn=endpoint_resource_arn,
                    policy=command_deny_policy(endpoint_resource_arn),
                )
                self.endpoint_command_deny_policy.add_dependency(
                    self.runtime_endpoint
                )
                self.endpoint_command_deny_policy.apply_removal_policy(
                    RemovalPolicy.RETAIN,
                    apply_to_update_replace_policy=True,
                )

            self.runtime_source_commit = runtime_source_commit
            self.runtime_image_uri = runtime_image_uri
            if release_stage == "endpoint":
                runtime_output_id = hardened_runtime_id
                runtime_output_version = hardened_runtime_version
                runtime_output_arn = hardened_runtime_arn
                self.runtime_id = hardened_runtime_id
                self.runtime_endpoint_id = self.runtime_endpoint.attr_id
                self.runtime_endpoint_name = expected_endpoint_name
                self.runtime_version = hardened_runtime_version
                self.runtime_arn = hardened_runtime_arn
                self.runtime_iam_arn = (
                    f"arn:aws:bedrock-agentcore:{region}:{account}:"
                    f"runtime/{hardened_runtime_id}"
                )
                self.runtime_binding = AgentCoreRuntimeBinding(
                    producer_stack=self,
                    account=account,
                    region=region,
                    runtime_id_parameter=self.hardened_runtime_id_parameter,
                    runtime_version_parameter=(
                        self.hardened_runtime_version_parameter
                    ),
                    runtime_arn_parameter=self.hardened_runtime_arn_parameter,
                    runtime_endpoint=self.runtime_endpoint,
                    runtime_endpoint_name=expected_endpoint_name,
                )
            else:
                runtime_output_id = self.runtime.attr_agent_runtime_id
                runtime_output_version = self.runtime.attr_agent_runtime_version
                runtime_output_arn = self.runtime.attr_agent_runtime_arn
                # Runtime creation is not yet a deployable consumer identity:
                # there is no hardened version-bound Endpoint. Keep the tuple
                # atomic so the full app can synthesize this stage without
                # granting Router/Web partial runtime authority.
                self.runtime_id = "PLACEHOLDER"
                self.runtime_endpoint_id = "PLACEHOLDER"
                self.runtime_endpoint_name = "PLACEHOLDER"
                self.runtime_version = "PLACEHOLDER"
                self.runtime_arn = "PLACEHOLDER"
                self.runtime_iam_arn = "PLACEHOLDER"

        # --- Browser authority is forbidden in the runtime -------------------
        # The conversational runtime never owns a browser. Curated browsing is
        # provided outside AgentCore by the separate trusted Browser Gateway
        # introduced by Task 10 (see ``stacks/browser_stack.py``), disabled by
        # default and owning ALL browser IAM in its own role. That browser role
        # is NEVER this execution role and is never passed into AgentCoreStack,
        # so no browser IAM statement is ever added to the runtime role here. A
        # runtime-owned browser escape hatch is rejected at synth time.
        enable_browser = str(
            self.node.try_get_context("enable_browser") or "false"
        ).casefold()
        if enable_browser != "false":
            raise ValueError(
                "runtime-owned browser authority is forbidden; use the separate "
                "trusted Browser Gateway introduced by Task 10"
            )

        # --- Outputs ----------------------------------------------------------
        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(
            self,
            "WorkspaceCredentialBrokerRoleArn",
            value=workspace_broker_role_arn_str,
        )
        CfnOutput(
            self,
            "WorkspaceCredentialBrokerFunctionName",
            value=workspace_broker_function_name,
        )
        CfnOutput(
            self,
            "WorkspaceSessionRoleArn",
            value=workspace_session_role_arn_str,
        )
        CfnOutput(self, "SecurityGroupId", value=self.agent_sg.security_group_id)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(private_subnet_ids),
        )
        if self.runtime is not None:
            CfnOutput(self, "RuntimeId", value=runtime_output_id)
            CfnOutput(
                self,
                "RuntimeVersion",
                value=runtime_output_version,
            )
            CfnOutput(
                self,
                "RuntimeArn",
                value=runtime_output_arn,
            )
            if self.runtime_endpoint is not None:
                CfnOutput(
                    self,
                    "RuntimeEndpointId",
                    value=self.runtime_endpoint_id,
                )
                CfnOutput(
                    self,
                    "RuntimeEndpointName",
                    value=self.runtime_endpoint_name,
                )
            CfnOutput(
                self,
                "RuntimeImageUri",
                value=self.runtime_image_uri,
            )
            CfnOutput(
                self,
                "RuntimeSourceCommit",
                value=self.runtime_source_commit,
            )

        # --- cdk-nag suppressions ---------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "AgentCore platform log groups and streams require its "
                        "documented runtime-name wildcards. CloudWatch metric, "
                        "X-Ray sampling/telemetry, and ECR authorization APIs do "
                        "not support narrower resource-level permissions."
                    ),
                    applies_to=[
                        "Resource::*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:*",
                        f"Resource::arn:aws:logs:{region}:{account}:"
                        "log-group:/aws/bedrock-agentcore/runtimes/*",
                        f"Resource::arn:aws:logs:{region}:{account}:"
                        "log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_session_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="The workspace base role is limited to this bucket; the "
                    "required per-user STS session policy further restricts the "
                    "object wildcard to one canonical namespace.",
                    applies_to=[
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_broker_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Secrets Manager appends an unknown six-character suffix "
                        "to the otherwise exact capability-secret name; the log "
                        "stream wildcard is confined to one exact broker log group."
                    ),
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
