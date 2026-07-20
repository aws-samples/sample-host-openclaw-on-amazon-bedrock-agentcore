"""Synthesized isolation boundary for the trusted Browser Gateway (Task 10).

Proves: (a) all browser IAM lives ONLY in browser_stack; (b) the browser
gateway is disabled by default (no browser resources synthesized); (c) the
AgentCore execution-role template contains zero browser actions and the
browser role is never the runtime execution role.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"


def _synth_browser_stack(region: str = REGION, *, enable_browser: str = "false"):
    from stacks.browser_stack import BrowserStack

    app = cdk.App(context={"enable_browser": enable_browser})
    stack = BrowserStack(
        app,
        "PersonalOperatorBrowser",
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
    for role in _resources(template, "AWS::IAM::Role"):
        for policy in role["Properties"].get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                result.update([actions] if isinstance(actions, str) else actions)
    return result


def test_browser_stack_is_disabled_by_default_synthesizes_no_browser_resources():
    _, template = _synth_browser_stack()
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []
    # Disabled by default: no browser IAM authority is granted.
    actions = _actions(template)
    assert not any("browser" in action.casefold() for action in actions)


def test_browser_stack_rejects_noncanonical_region():
    with pytest.raises(ValueError, match="eu-west-1"):
        _synth_browser_stack("us-east-1")


def test_agentcore_execution_role_has_zero_browser_actions():
    from tests.test_product_configuration import _synth_agentcore_template

    template = _synth_agentcore_template()
    actions = _actions(template)
    assert not any("browser" in action.casefold() for action in actions)
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []


def test_agentcore_source_still_forbids_a_runtime_owned_browser():
    source = (ROOT / "stacks" / "agentcore_stack.py").read_text(encoding="utf-8")
    assert "CfnBrowserCustom" not in source
    assert "StartBrowserSession" not in source
    assert "ConnectBrowserAutomationStream" not in source
    assert "trusted Browser Gateway" in source


def test_browser_role_is_never_the_runtime_execution_role():
    # The browser stack owns its own role; its name is not the runtime role.
    from stacks.browser_stack import BROWSER_ROLE_NAME

    assert BROWSER_ROLE_NAME != f"openclaw-agentcore-execution-role-{REGION}"
    assert "browser" in BROWSER_ROLE_NAME
