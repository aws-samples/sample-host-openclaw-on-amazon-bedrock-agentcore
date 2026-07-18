from __future__ import annotations

import json

from aws_cdk import App, Environment, Stack, aws_ec2 as ec2
from aws_cdk.assertions import Template

from stacks.agentcore_stack import AgentCoreStack


ACCOUNT = "123456789012"
REGION = "eu-west-1"
REPOSITORY_NAME = "personal-operator/bridge"


def _foundation_template() -> dict:
    app = App(
        context={
            "runtime_source_commit": "",
            "runtime_id": "",
            "runtime_endpoint_id": "",
            "runtime_endpoint_name": "",
            "runtime_version": "",
            "runtime_arn": "",
            "runtime_image_uri": "",
            "user_files_ttl_days": "30",
            "enable_browser": "false",
        }
    )
    env = Environment(account=ACCOUNT, region=REGION)
    network = Stack(app, "Network", env=env)
    vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=0)
    stack = AgentCoreStack(
        app,
        "AgentCore",
        cmk_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key",
        vpc=vpc,
        private_subnet_ids=["subnet-00000000000000001"],
        workspace_capability_secret_name=(
            "personal-operator/workspace-capability"
        ),
        env=env,
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


def test_runtime_pull_role_is_scoped_to_the_exact_release_repository() -> None:
    template = _foundation_template()
    statements = _statements(template)
    pull_actions = {
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:BatchCheckLayerAvailability",
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
