from __future__ import annotations

import json

from aws_cdk import App, Environment, Stack, aws_ec2 as ec2
from aws_cdk.assertions import Template

from stacks.agentcore_stack import AgentCoreStack


ACCOUNT = "123456789012"
REGION = "eu-west-1"
REPOSITORY_NAME = "personal-operator/bridge"
SOURCE_COMMIT = "a" * 40
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY_NAME}"
    "@sha256:" + "b" * 64
)
RUNTIME_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$"
RUNTIME_VERSION_PATTERN = r"^[1-9][0-9]{0,4}$"
RUNTIME_ARN_PATTERN = (
    rf"^arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}$"
)


def _stack(context_overrides: dict[str, str] | None = None) -> AgentCoreStack:
    context = {
        "runtime_source_commit": "",
        "agentcore_release_stage": "foundation",
        "runtime_id": "",
        "runtime_endpoint_id": "",
        "runtime_endpoint_name": "",
        "runtime_version": "",
        "runtime_arn": "",
        "runtime_image_uri": "",
        "user_files_ttl_days": "30",
        "session_idle_timeout": "1800",
        "session_max_lifetime": "28800",
        "workspace_sync_interval_seconds": "300",
        "default_model_id": "eu.anthropic.claude-sonnet-4-6",
        "subagent_model_id": "",
        "enable_browser": "false",
    }
    context.update(context_overrides or {})
    app = App(
        context=context
    )
    env = Environment(account=ACCOUNT, region=REGION)
    network = Stack(app, "Network", env=env)
    vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=0)
    trusted_endpoint_sg = ec2.SecurityGroup(
        network,
        "TrustedEndpointSecurityGroup",
        vpc=vpc,
        allow_all_outbound=False,
    )
    return AgentCoreStack(
        app,
        "AgentCore",
        cmk_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key",
        vpc=vpc,
        private_subnet_ids=["subnet-00000000000000001"],
        trusted_endpoint_security_group=trusted_endpoint_sg,
        s3_prefix_list_id="pl-6da54004",
        workspace_capability_secret_name=(
            "personal-operator/workspace-capability"
        ),
        capability_gateway_function_arn=(
            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
            "personal-operator-capability-gateway"
        ),
        env=env,
    )


def _foundation_template() -> dict:
    stack = _stack()
    return Template.from_stack(stack).to_json()


def _runtime_template() -> dict:
    stack = _stack(
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        }
    )
    return Template.from_stack(stack).to_json()


def _endpoint_template(
    context_overrides: dict[str, str] | None = None,
) -> dict:
    context = {
        "agentcore_release_stage": "endpoint",
        "runtime_source_commit": SOURCE_COMMIT,
        "runtime_image_uri": IMAGE_URI,
    }
    context.update(context_overrides or {})
    stack = _stack(context)
    return Template.from_stack(stack).to_json()


def _statements(template: dict) -> list[dict]:
    statements: list[dict] = []
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        statements.extend(
            resource["Properties"]["PolicyDocument"]["Statement"]
        )
    return statements


def _render_join(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        separator, fragments = value["Fn::Join"]
        return separator.join(_render_join(fragment) for fragment in fragments)
    return "EXACT_TOKEN"


def _string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {
            item
            for nested in value.values()
            for item in _string_values(nested)
        }
    if isinstance(value, list):
        return {item for nested in value for item in _string_values(nested)}
    return set()


def _canonical_template_bytes(template: dict) -> bytes:
    return json.dumps(template, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def test_foundation_owns_one_retained_private_immutable_ecr_repository() -> None:
    template = _foundation_template()
    repositories = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::ECR::Repository"
    }

    assert len(repositories) == 1
    repository = next(iter(repositories.values()))
    properties = repository["Properties"]
    assert properties["RepositoryName"] == REPOSITORY_NAME
    assert properties["ImageTagMutability"] == "IMMUTABLE"
    assert properties["ImageScanningConfiguration"] == {"ScanOnPush": True}
    assert properties["EncryptionConfiguration"]["EncryptionType"] == "KMS"
    assert properties["EncryptionConfiguration"]["KmsKey"]["Fn::GetAtt"][1] == "Arn"
    assert repository["DeletionPolicy"] == "Retain"
    assert repository["UpdateReplacePolicy"] == "Retain"

    lifecycle = json.loads(properties["LifecyclePolicy"]["LifecyclePolicyText"])
    assert lifecycle == {
        "rules": [
            {
                "action": {"type": "expire"},
                "description": "Expire untagged images after 30 days",
                "rulePriority": 1,
                "selection": {
                    "countNumber": 30,
                    "countType": "sinceImagePushed",
                    "countUnit": "days",
                    "tagStatus": "untagged",
                },
            }
        ]
    }

    keys = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::KMS::Key"
    ]
    assert len(keys) == 1
    assert keys[0]["Properties"]["EnableKeyRotation"] is True
    assert keys[0]["DeletionPolicy"] == "Retain"
    assert keys[0]["UpdateReplacePolicy"] == "Retain"


def test_foundation_configures_notation_signing_for_only_the_bridge_repo() -> None:
    template = _foundation_template()
    profiles = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Signer::SigningProfile"
    ]
    configurations = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::ECR::SigningConfiguration"
    ]

    assert len(profiles) == 1
    assert profiles[0]["Properties"] == {
        "PlatformId": "Notation-OCI-SHA384-ECDSA",
        "ProfileName": "personal_operator_bridge",
        "SignatureValidityPeriod": {"Type": "DAYS", "Value": 3650},
    }
    assert profiles[0]["DeletionPolicy"] == "Retain"
    assert profiles[0]["UpdateReplacePolicy"] == "Retain"

    assert len(configurations) == 1
    rules = configurations[0]["Properties"]["Rules"]
    assert len(rules) == 1
    assert rules[0]["RepositoryFilters"] == [
        {"Filter": REPOSITORY_NAME, "FilterType": "WILDCARD_MATCH"}
    ]
    assert rules[0]["SigningProfileArn"]["Fn::GetAtt"][1] == "Arn"
    assert configurations[0]["DeletionPolicy"] == "Retain"
    assert configurations[0]["UpdateReplacePolicy"] == "Retain"


def test_runtime_pull_role_is_scoped_to_the_exact_release_repository() -> None:
    template = _foundation_template()
    statements = _statements(template)
    pull_actions = {
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
    }
    pull = [
        statement
        for statement in statements
        if set(statement.get("Action", [])) == pull_actions
    ]
    authorization = [
        statement
        for statement in statements
        if statement.get("Action") == "ecr:GetAuthorizationToken"
    ]

    assert len(pull) == 1
    assert pull[0]["Resource"] == (
        f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{REPOSITORY_NAME}"
    )
    assert authorization == [
        {
            "Action": "ecr:GetAuthorizationToken",
            "Effect": "Allow",
            "Resource": "*",
        }
    ]

    serialized = json.dumps(template)
    assert "openclaw-bridge*" not in serialized
    assert "openclaw_agent*" not in serialized
    assert "bedrock-agentcore-*" not in serialized


def test_foundation_synth_omits_runtime_and_endpoint_resources() -> None:
    template = _foundation_template()
    types = [resource["Type"] for resource in template["Resources"].values()]

    assert "AWS::BedrockAgentCore::Runtime" not in types
    assert "AWS::BedrockAgentCore::RuntimeEndpoint" not in types


def test_runtime_stage_owns_digest_bound_runtime_without_an_endpoint() -> None:
    template = _runtime_template()
    runtimes = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    }
    endpoints = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::BedrockAgentCore::RuntimeEndpoint"
    }

    assert len(runtimes) == 1
    assert endpoints == {}
    runtime_id, runtime = next(iter(runtimes.items()))
    security_group_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::EC2::SecurityGroup"
    )
    properties = runtime["Properties"]
    assert properties["AgentRuntimeArtifact"] == {
        "ContainerConfiguration": {"ContainerUri": IMAGE_URI}
    }
    assert properties["AgentRuntimeName"] == "personal_operator_bridge"
    assert properties["NetworkConfiguration"] == {
        "NetworkMode": "VPC",
        "NetworkModeConfig": {
            "SecurityGroups": [
                {
                    "Fn::GetAtt": [
                        security_group_id,
                        "GroupId",
                    ]
                }
            ],
            "Subnets": ["subnet-00000000000000001"],
        },
    }
    assert properties["FilesystemConfigurations"] == [
        {"SessionStorage": {"MountPath": "/mnt/workspace"}}
    ]
    assert properties["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 1800,
        "MaxLifetime": 28800,
    }
    assert properties["ProtocolConfiguration"] == "HTTP"
    assert properties["EnvironmentVariables"] == {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
        "CAPABILITY_GATEWAY_FUNCTION_ARN": (
            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
            "personal-operator-capability-gateway"
        ),
        "DISABLE_ADOT_OBSERVABILITY": "true",
        "S3_USER_FILES_BUCKET": {
            "Ref": "UserFilesBucketCFDFD8C0"
        },
        "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
            "personal-operator-workspace-credential-broker"
        ),
        "WORKSPACE_SYNC_INTERVAL_MS": "300000",
    }
    assert not any(
        name.startswith("OTEL_") for name in properties["EnvironmentVariables"]
    )

    assert "MetadataConfiguration" not in properties


def test_endpoint_stage_preserves_runtime_and_binds_required_parameters() -> None:
    runtime_template = _runtime_template()
    template = _endpoint_template()
    runtimes = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    }
    endpoints = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::RuntimeEndpoint"
    ]

    assert runtimes == {
        logical_id: resource
        for logical_id, resource in runtime_template["Resources"].items()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    }
    runtime_policy = next(
        resource
        for resource in runtime_template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::ResourcePolicy"
    )
    endpoint_runtime_policy = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::ResourcePolicy"
        and "AgentRuntimeId" in json.dumps(resource["Properties"]["ResourceArn"])
    )
    assert endpoint_runtime_policy == runtime_policy
    assert len(endpoints) == 1
    assert endpoints[0]["Properties"] == {
        "AgentRuntimeId": {"Ref": "HardenedRuntimeId"},
        "AgentRuntimeVersion": {"Ref": "HardenedRuntimeVersion"},
        "Name": f"release_{SOURCE_COMMIT}",
    }
    assert endpoints[0]["DeletionPolicy"] == "Retain"
    assert endpoints[0]["UpdateReplacePolicy"] == "Retain"


def test_endpoint_stage_has_exact_no_default_runtime_parameter_schema() -> None:
    template = _endpoint_template()
    identity_parameters = {
        name: parameter
        for name, parameter in template["Parameters"].items()
        if name.startswith("HardenedRuntime")
    }

    assert identity_parameters == {
        "HardenedRuntimeId": {
            "Type": "String",
            "AllowedPattern": RUNTIME_ID_PATTERN,
        },
        "HardenedRuntimeVersion": {
            "Type": "String",
            "AllowedPattern": RUNTIME_VERSION_PATTERN,
        },
        "HardenedRuntimeArn": {
            "Type": "String",
            "AllowedPattern": RUNTIME_ARN_PATTERN,
        },
    }
    assert all(
        "Default" not in parameter
        for parameter in identity_parameters.values()
    )
    assert template["Outputs"]["RuntimeId"]["Value"] == {
        "Ref": "HardenedRuntimeId"
    }
    assert template["Outputs"]["RuntimeVersion"]["Value"] == {
        "Ref": "HardenedRuntimeVersion"
    }
    assert template["Outputs"]["RuntimeArn"]["Value"] == {
        "Ref": "HardenedRuntimeArn"
    }


def test_endpoint_stage_exposes_only_its_exact_immutable_consumer_binding() -> None:
    stack = _stack(
        {
            "agentcore_release_stage": "endpoint",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        }
    )
    binding = stack.runtime_binding

    assert binding is not None
    assert binding.producer_stack is stack
    assert binding.account == ACCOUNT
    assert binding.region == REGION
    assert binding.runtime_id_parameter is stack.hardened_runtime_id_parameter
    assert (
        binding.runtime_version_parameter
        is stack.hardened_runtime_version_parameter
    )
    assert binding.runtime_arn_parameter is stack.hardened_runtime_arn_parameter
    assert binding.runtime_endpoint is stack.runtime_endpoint
    assert binding.runtime_id == stack.runtime_id
    assert binding.runtime_version == stack.runtime_version
    assert binding.runtime_arn == stack.runtime_arn
    assert binding.runtime_endpoint_id == stack.runtime_endpoint_id
    assert binding.runtime_endpoint_name == f"release_{SOURCE_COMMIT}"

    assert _stack().runtime_binding is None
    runtime_stack = _stack(
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        }
    )
    assert runtime_stack.runtime_binding is None


def test_endpoint_template_is_identity_independent_before_runtime_creation() -> None:
    first_identity = {
        "runtime_id": "personal_operator_bridge-0123456789",
        "runtime_endpoint_id": "release_endpoint-0123456789",
        "runtime_endpoint_name": f"release_{SOURCE_COMMIT}",
        "runtime_version": "8",
        "runtime_arn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            "agent/12345678-1234-1234-1234-123456789abc:8"
        ),
    }
    second_identity = {
        "runtime_id": "personal_operator_bridge-9876543210",
        "runtime_endpoint_id": "release_endpoint-9876543210",
        "runtime_endpoint_name": f"release_{SOURCE_COMMIT}",
        "runtime_version": "9",
        "runtime_arn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
            "agent/abcdefab-cdef-abcd-efab-cdefabcdefab:9"
        ),
    }

    baseline = _endpoint_template()
    first = _endpoint_template(first_identity)
    second = _endpoint_template(second_identity)

    assert first == baseline == second
    assert (
        _canonical_template_bytes(first)
        == _canonical_template_bytes(baseline)
        == _canonical_template_bytes(second)
    )
    strings = _string_values(baseline)
    for identity in (first_identity, second_identity):
        assert identity["runtime_id"] not in strings
        assert identity["runtime_endpoint_id"] not in strings
        assert identity["runtime_arn"] not in strings


def test_runtime_and_endpoint_stages_use_canonical_id_based_policy_subjects() -> None:
    runtime_template = _runtime_template()
    runtime_policies = [
        resource
        for resource in runtime_template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::ResourcePolicy"
    ]
    assert len(runtime_policies) == 1
    runtime_subject = runtime_policies[0]["Properties"]["ResourceArn"]
    serialized_runtime_subject = json.dumps(runtime_subject)
    assert "runtime/" in serialized_runtime_subject
    assert "AgentRuntimeId" in serialized_runtime_subject
    assert "AgentRuntimeArn" not in serialized_runtime_subject

    template = _endpoint_template()
    policies = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::ResourcePolicy"
    ]

    assert len(policies) == 2
    assert all(policy["DeletionPolicy"] == "Retain" for policy in policies)
    assert all(policy["UpdateReplacePolicy"] == "Retain" for policy in policies)
    for policy in policies:
        subject = policy["Properties"]["ResourceArn"]
        rendered = json.loads(_render_join(policy["Properties"]["Policy"]))
        assert rendered == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyRuntimeCommandExecution",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": [
                        "bedrock-agentcore:InvokeAgentRuntimeCommand",
                        "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
                    ],
                    "Resource": _render_join(subject),
                }
            ],
        }

    subjects = [
        json.dumps(policy["Properties"]["ResourceArn"])
        for policy in policies
    ]
    assert any("AgentRuntimeId" in subject for subject in subjects)
    assert any(
        "HardenedRuntimeId" in subject
        and "runtime-endpoint" in subject
        and "AgentRuntimeEndpointArn" not in subject
        and "Id" in subject
        for subject in subjects
    )


def test_release_stack_rejects_partial_or_mutable_runtime_inputs() -> None:
    invalid = [
        {"agentcore_release_stage": "unknown"},
        {
            "agentcore_release_stage": "",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        },
        {
            "agentcore_release_stage": "foundation",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        },
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
        },
        {"agentcore_release_stage": "runtime", "runtime_image_uri": IMAGE_URI},
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace("@sha256:", ":latest-"),
        },
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(
                REPOSITORY_NAME, "personal-operator/other"
            ),
        },
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(ACCOUNT, "999999999999"),
        },
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(REGION, "us-east-1"),
        },
        {
            "agentcore_release_stage": "endpoint",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
            "runtime_id": "personal_operator_bridge-0123456789",
            "runtime_version": "8",
        },
        {
            "agentcore_release_stage": "runtime",
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
            "runtime_id": "personal_operator_bridge-0123456789",
            "runtime_version": "8",
            "runtime_arn": (
                f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                "agent/12345678-1234-1234-1234-123456789abc:8"
            ),
        },
    ]

    for context in invalid:
        try:
            _stack(context)
        except ValueError as error:
            assert "runtime" in str(error).casefold()
        else:
            raise AssertionError(f"release stack accepted invalid inputs: {context}")
