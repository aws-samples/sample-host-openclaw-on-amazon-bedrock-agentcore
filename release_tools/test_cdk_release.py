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


def _stack(context_overrides: dict[str, str] | None = None) -> AgentCoreStack:
    context = {
        "runtime_source_commit": "",
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


def _release_template() -> dict:
    stack = _stack(
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
        }
    )
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


def test_release_synth_owns_digest_bound_runtime_and_retained_endpoint() -> None:
    template = _release_template()
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
    assert len(endpoints) == 1
    runtime_id, runtime = next(iter(runtimes.items()))
    endpoint = next(iter(endpoints.values()))
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

    assert endpoint["Properties"] == {
        "AgentRuntimeId": {"Fn::GetAtt": [runtime_id, "AgentRuntimeId"]},
        "AgentRuntimeVersion": {
            "Fn::GetAtt": [runtime_id, "AgentRuntimeVersion"]
        },
        "Name": f"release_{SOURCE_COMMIT}",
    }
    assert endpoint["DeletionPolicy"] == "Retain"
    assert endpoint["UpdateReplacePolicy"] == "Retain"


def test_release_synth_denies_both_command_apis_on_runtime_and_endpoint() -> None:
    template = _release_template()
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
        separator, fragments = policy["Properties"]["Policy"]["Fn::Join"]
        assert separator == ""
        assert fragments[1] == subject
        rendered = json.loads(fragments[0] + "EXACT_SUBJECT" + fragments[2])
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
                    "Resource": "EXACT_SUBJECT",
                }
            ],
        }


def test_release_stack_rejects_partial_or_mutable_runtime_inputs() -> None:
    invalid = [
        {"runtime_source_commit": SOURCE_COMMIT},
        {"runtime_image_uri": IMAGE_URI},
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace("@sha256:", ":latest-"),
        },
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(
                REPOSITORY_NAME, "personal-operator/other"
            ),
        },
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(ACCOUNT, "999999999999"),
        },
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI.replace(REGION, "us-east-1"),
        },
        {
            "runtime_source_commit": SOURCE_COMMIT,
            "runtime_image_uri": IMAGE_URI,
            "runtime_endpoint_name": "release_" + "c" * 40,
        },
    ]

    for context in invalid:
        try:
            _stack(context)
        except ValueError as error:
            assert "runtime" in str(error).casefold()
        else:
            raise AssertionError(f"release stack accepted invalid inputs: {context}")
