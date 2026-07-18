"""Synthesized least-authority boundary for the capability gateway."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
FUNCTION_NAME = "personal-operator-capability-gateway"
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FUNCTION_NAME}"


def _synth_capability_stack(region: str = REGION):
    from stacks.capability_stack import CapabilityStack

    app = cdk.App()
    stack = CapabilityStack(
        app,
        "PersonalOperatorCapabilities",
        trusted_code_asset_root=str(ROOT / "lambda"),
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
    }
    assert "Environment" not in functions[0]["Properties"]


def test_gateway_role_has_logs_only_and_stack_has_no_adapter_authority():
    _, template = _synth_capability_stack()
    actions = _actions(template)
    statements = [
        statement
        for policy in _resources(template, "AWS::IAM::Policy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    assert actions == {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert statements == [
        {
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Effect": "Allow",
            "Resource": (
                "arn:aws:logs:eu-west-1:123456789012:log-group:"
                "/personal-operator/lambda/capability-gateway:*"
            ),
        }
    ]
    assert _resources(template, "AWS::DynamoDB::Table") == []
    assert _resources(template, "AWS::SecretsManager::Secret") == []
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []
    assert _resources(template, "AWS::Scheduler::Schedule") == []
    serialized = str(template).casefold()
    for forbidden in (
        "provider",
        "browser",
        "scheduler:",
        "dynamodb:",
        "secretsmanager:",
        "execute-api:",
        "mcp",
    ):
        assert forbidden not in serialized


def test_capability_stack_rejects_every_noncanonical_region():
    with pytest.raises(ValueError, match="eu-west-1"):
        _synth_capability_stack("us-east-1")


def test_agentcore_source_has_no_runtime_owned_browser_escape_hatch():
    source = (ROOT / "stacks" / "agentcore_stack.py").read_text(encoding="utf-8")

    assert "CfnBrowserCustom" not in source
    assert "StartBrowserSession" not in source
    assert "ConnectBrowserAutomationStream" not in source
    assert "trusted Browser Gateway" in source
