"""Synthesized least-authority boundary for the trusted read-only scheduler."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CMK_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-scheduler-key"
UPDATE_QUEUE_ARN = (
    f"arn:aws:sqs:{REGION}:{ACCOUNT}:personal-operator-telegram-updates.fifo"
)
UPDATE_QUEUE_URL = (
    f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/"
    "personal-operator-telegram-updates.fifo"
)
INGRESS_FUNCTION_NAME = "personal-operator-scheduler-ingress"


def _synth(region: str = REGION):
    from stacks.scheduler_stack import SchedulerStack

    app = cdk.App()
    stack = SchedulerStack(
        app,
        "PersonalOperatorScheduler",
        trusted_code_asset_root=str(ROOT / "lambda"),
        cmk_arn=CMK_ARN,
        update_queue_arn=UPDATE_QUEUE_ARN,
        update_queue_url=UPDATE_QUEUE_URL,
        env=cdk.Environment(account=ACCOUNT, region=region),
    )
    return stack, Template.from_stack(stack).to_json()


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template.get("Resources", {}).values()
        if resource["Type"] == resource_type
    ]


def _role_actions(template: dict, role_logical_id: str) -> set[str]:
    result: set[str] = set()
    for policy in _resources(template, "AWS::IAM::Policy"):
        roles = policy["Properties"].get("Roles", [])
        role_refs = {r.get("Ref") for r in roles if isinstance(r, dict)}
        if role_logical_id not in role_refs:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            result.update([actions] if isinstance(actions, str) else actions)
    return result


def _all_actions(template: dict) -> set[str]:
    result: set[str] = set()
    for policy in _resources(template, "AWS::IAM::Policy"):
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            result.update([actions] if isinstance(actions, str) else actions)
    return result


def _role_logical_id(template: dict, role_name_contains: str) -> str:
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        role_name = resource["Properties"].get("RoleName", "")
        if isinstance(role_name, str) and role_name_contains in role_name:
            return logical_id
    raise AssertionError(f"no role matching {role_name_contains}")


def test_eventbridge_schedule_role_has_only_invoke_ingress_and_no_connector_browser_provider_authority():
    _, template = _synth()
    scheduler_role = _role_logical_id(template, "scheduler-invoke")
    actions = _role_actions(template, scheduler_role)

    assert actions == {"lambda:InvokeFunction"}
    # The scheduler role holds no AgentCore/connector/browser/provider authority.
    serialized = str(template).casefold()
    for forbidden in (
        "bedrock-agentcore",
        "invokeagentruntime",
        "browser",
        "secretsmanager:",
        "execute-api:",
        "gmail",
    ):
        assert forbidden not in serialized


def test_ingress_role_has_only_control_table_read_fifo_send_and_scoped_kms():
    _, template = _synth()
    ingress_role = _role_logical_id(template, "scheduler-ingress")
    actions = _role_actions(template, ingress_role)

    # Strong-read on the control table + FIFO send + scoped KMS + log writes.
    assert actions == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem",
        "sqs:SendMessage",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    # No dispatch/effect authority anywhere in the ingress role.
    assert "bedrock-agentcore:InvokeAgentRuntime" not in actions
    assert not any(action.startswith("lambda:") for action in actions)


def test_scheduler_control_table_is_cmk_pitr_and_retained():
    _, template = _synth()
    tables = _resources(template, "AWS::DynamoDB::Table")
    assert len(tables) == 1
    props = tables[0]["Properties"]
    assert props["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert props["SSESpecification"]["KMSMasterKeyId"] == CMK_ARN
    assert tables[0]["DeletionPolicy"] == "Retain"
    assert tables[0]["UpdateReplacePolicy"] == "Retain"


def test_scheduler_stack_creates_no_static_schedule_or_forbidden_resource():
    _, template = _synth()
    # Live schedules are created at runtime by the trusted service, not baked
    # into the template.
    assert _resources(template, "AWS::Scheduler::Schedule") == []
    assert _resources(template, "AWS::SecretsManager::Secret") == []
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []
    functions = _resources(template, "AWS::Lambda::Function")
    assert len(functions) == 1
    assert functions[0]["Properties"]["Handler"] == "scheduler.ingress.lambda_handler"


def test_scheduler_stack_synthesizes_in_eu_west_1_and_passes_cdk_nag():
    import cdk_nag
    from aws_cdk.assertions import Annotations, Match
    from stacks.scheduler_stack import SchedulerStack

    app = cdk.App()
    stack = SchedulerStack(
        app,
        "PersonalOperatorScheduler",
        trusted_code_asset_root=str(ROOT / "lambda"),
        cmk_arn=CMK_ARN,
        update_queue_arn=UPDATE_QUEUE_ARN,
        update_queue_url=UPDATE_QUEUE_URL,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))
    template = Template.from_stack(stack)  # forces synth
    assert template is not None
    errors = Annotations.from_stack(stack).find_error(
        "*", Match.string_like_regexp("AwsSolutions-.*")
    )
    assert errors == [], [e.entry.data for e in errors]


def test_scheduler_stack_rejects_every_noncanonical_region():
    with pytest.raises(ValueError, match="eu-west-1"):
        _synth("us-east-1")
