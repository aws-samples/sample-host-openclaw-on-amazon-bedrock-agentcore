"""Synthesized least-authority boundary for the capability gateway."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
FUNCTION_NAME = "personal-operator-capability-gateway"
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FUNCTION_NAME}"
RELEASE_COMMIT = "a" * 40
CATALOG_DIGEST = "b" * 64
CMK_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-capability-key"
TABLE_NAME = "personal-operator-capability-state"


def _synth_capability_stack(region: str = REGION):
    from stacks.capability_stack import CapabilityStack

    app = cdk.App()
    stack = CapabilityStack(
        app,
        "PersonalOperatorCapabilities",
        trusted_code_asset_root=str(ROOT / "lambda"),
        cmk_arn=CMK_ARN,
        release_commit=RELEASE_COMMIT,
        catalog_digest=CATALOG_DIGEST,
        env=cdk.Environment(account=ACCOUNT, region=region),
    )
    return stack, Template.from_stack(stack).to_json()


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template.get("Resources", {}).values()
        if resource["Type"] == resource_type
    ]


def _actions(template: dict) -> set[str]:
    result: set[str] = set()
    for policy in _resources(template, "AWS::IAM::Policy"):
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            result.update([actions] if isinstance(actions, str) else actions)
    return result


def _reference_dynamodb_cmk_data_actions() -> set[str]:
    app = cdk.App()
    stack = cdk.Stack(
        app,
        "ReferenceDynamoCmk",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    key = kms.Key(stack, "ReferenceKey")
    table = dynamodb.Table(
        stack,
        "ReferenceTable",
        partition_key=dynamodb.Attribute(
            name="PK",
            type=dynamodb.AttributeType.STRING,
        ),
        encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=key,
    )
    role = iam.Role(
        stack,
        "ReferenceRole",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
    )
    table.grant_read_write_data(role)
    template = Template.from_stack(stack).to_json()
    return {action for action in _actions(template) if action.startswith("kms:")}


def test_capability_stack_is_one_fail_closed_gateway_with_exact_identity():
    stack, template = _synth_capability_stack()
    functions = _resources(template, "AWS::Lambda::Function")

    assert stack.gateway_function_arn == FUNCTION_ARN
    assert len(functions) == 1
    assert functions[0]["Properties"] == {
        "Architectures": ["arm64"],
        "Code": functions[0]["Properties"]["Code"],
        "FunctionName": FUNCTION_NAME,
        "Handler": "capabilities.gateway.lambda_handler",
        "LoggingConfig": functions[0]["Properties"]["LoggingConfig"],
        "MemorySize": 256,
        "Role": functions[0]["Properties"]["Role"],
        "Runtime": "python3.13",
        "Timeout": 15,
        "Environment": {
            "Variables": {
                "CAPABILITY_ALLOWED_CALLER_ARN": (
                    f"arn:aws:iam::{ACCOUNT}:role/"
                    "openclaw-agentcore-execution-role-eu-west-1"
                ),
                "CAPABILITY_CATALOG_DIGEST": CATALOG_DIGEST,
                "CAPABILITY_RELEASE_COMMIT": RELEASE_COMMIT,
                "CAPABILITY_STATE_TABLE_NAME": TABLE_NAME,
            }
        },
    }


def test_gateway_role_has_only_exact_logs_dynamo_and_kms_authority():
    _, template = _synth_capability_stack()
    actions = _actions(template)
    statements = [
        statement
        for policy in _resources(template, "AWS::IAM::Policy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    assert actions == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert len(statements) == 3
    tables = _resources(template, "AWS::DynamoDB::Table")
    assert len(tables) == 1
    assert tables[0]["Properties"] == {
        "AttributeDefinitions": [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "DeletionProtectionEnabled": True,
        "KeySchema": [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "PointInTimeRecoverySpecification": {
            "PointInTimeRecoveryEnabled": True,
        },
        "SSESpecification": {
            "KMSMasterKeyId": CMK_ARN,
            "SSEEnabled": True,
            "SSEType": "KMS",
        },
        "TableName": TABLE_NAME,
    }
    assert tables[0]["DeletionPolicy"] == "Retain"
    assert tables[0]["UpdateReplacePolicy"] == "Retain"
    assert _resources(template, "AWS::SecretsManager::Secret") == []
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []
    assert _resources(template, "AWS::Scheduler::Schedule") == []
    serialized = str(template).casefold()
    for forbidden in (
        "provider",
        "browser",
        "scheduler:",
        "secretsmanager:",
        "execute-api:",
        "mcp",
    ):
        assert forbidden not in serialized


def test_gateway_dynamo_and_kms_statements_are_resource_and_condition_bounded():
    _, template = _synth_capability_stack()
    statements = [
        statement
        for policy in _resources(template, "AWS::IAM::Policy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    dynamo = next(
        statement
        for statement in statements
        if "dynamodb:GetItem" in statement.get("Action", [])
    )
    kms = next(
        statement
        for statement in statements
        if "kms:Decrypt" in statement.get("Action", [])
    )

    assert dynamo["Resource"] == {
        "Fn::GetAtt": [
            next(
                logical_id
                for logical_id, resource in template["Resources"].items()
                if resource["Type"] == "AWS::DynamoDB::Table"
            ),
            "Arn",
        ]
    }
    assert kms["Resource"] == CMK_ARN
    assert kms["Condition"] == {
        "StringEquals": {
            "kms:CallerAccount": ACCOUNT,
            "kms:ViaService": f"dynamodb.{REGION}.amazonaws.com",
        }
    }


def test_gateway_cmk_actions_match_cdk_dynamodb_data_plane_reference():
    _, template = _synth_capability_stack()
    statements = [
        statement
        for policy in _resources(template, "AWS::IAM::Policy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    production = next(
        statement
        for statement in statements
        if "kms:Decrypt" in statement.get("Action", [])
    )
    reference = _reference_dynamodb_cmk_data_actions()

    assert reference == {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
    }
    assert set(production["Action"]) == reference
    assert "kms:CreateGrant" not in production["Action"]
    assert production["Resource"] == CMK_ARN
    assert production["Condition"]["StringEquals"] == {
        "kms:CallerAccount": ACCOUNT,
        "kms:ViaService": f"dynamodb.{REGION}.amazonaws.com",
    }


def test_capability_stack_rejects_every_noncanonical_region():
    with pytest.raises(ValueError, match="eu-west-1"):
        _synth_capability_stack("us-east-1")


def test_agentcore_source_has_no_runtime_owned_browser_escape_hatch():
    source = (ROOT / "stacks" / "agentcore_stack.py").read_text(encoding="utf-8")

    assert "CfnBrowserCustom" not in source
    assert "StartBrowserSession" not in source
    assert "ConnectBrowserAutomationStream" not in source
    assert "trusted Browser Gateway" in source


def test_app_and_both_deploy_phases_bind_the_exact_capability_release_catalog():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    deploy_source = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    context = (ROOT / "cdk.json").read_text(encoding="utf-8")

    assert 'try_get_context("capability_release_commit")' in app_source
    assert "compile_catalog(" in app_source
    assert "cmk_arn=security_stack.cmk.key_arn" in app_source
    assert "release_commit=capability_release_commit" in app_source
    assert "catalog_digest=capability_catalog.catalog_digest" in app_source
    assert (
        deploy_source.count(
            '-c "capability_release_commit=$PERSONAL_OPERATOR_DEPLOY_COMMIT"'
        )
        == 2
    )
    assert '"capability_release_commit": ""' in context
