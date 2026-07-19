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
CONTROL_FUNCTION_NAME = "personal-operator-scheduler-control"
CATALOG_DIGEST = "a" * 64
CAPABILITY_TABLE_NAME = "personal-operator-capability-state"
CAPABILITY_TABLE_ARN = (
    f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{CAPABILITY_TABLE_NAME}"
)


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
        catalog_digest=CATALOG_DIGEST,
        capability_state_table_name=CAPABILITY_TABLE_NAME,
        capability_state_table_arn=CAPABILITY_TABLE_ARN,
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
    assert props["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }
    assert {
        index["IndexName"] for index in props["GlobalSecondaryIndexes"]
    } == {"schedule-user-index-v1", "proposal-user-index-v1"}
    assert tables[0]["DeletionPolicy"] == "Retain"
    assert tables[0]["UpdateReplacePolicy"] == "Retain"


def test_scheduler_stack_creates_no_static_schedule_or_forbidden_resource():
    _, template = _synth()
    # Live schedules are created at runtime by the trusted service, not baked
    # into the template.
    assert _resources(template, "AWS::Scheduler::Schedule") == []
    groups = _resources(template, "AWS::Scheduler::ScheduleGroup")
    assert len(groups) == 1
    assert groups[0]["Properties"]["Name"] == "personal-operator-v1"
    assert _resources(template, "AWS::SecretsManager::Secret") == []
    assert _resources(template, "AWS::BedrockAgentCore::BrowserCustom") == []
    functions = _resources(template, "AWS::Lambda::Function")
    assert len(functions) == 2
    assert {function["Properties"]["Handler"] for function in functions} == {
        "scheduler.ingress.lambda_handler",
        "scheduler.control.lambda_handler",
    }


def test_control_role_has_exact_schedule_apply_authority_and_passrole_condition():
    _, template = _synth()
    control_role = _role_logical_id(template, "scheduler-control")
    actions = _role_actions(template, control_role)

    assert actions == {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem",
        "dynamodb:TransactWriteItems",
        "scheduler:CreateSchedule",
        "scheduler:GetSchedule",
        "scheduler:DeleteSchedule",
        "iam:PassRole",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    policies = _resources(template, "AWS::IAM::Policy")
    statements = [
        statement
        for policy in policies
        if {ref.get("Ref") for ref in policy["Properties"].get("Roles", [])}
        == {control_role}
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    passrole = next(
        statement
        for statement in statements
        if statement.get("Action") == "iam:PassRole"
        or "iam:PassRole" in statement.get("Action", [])
    )
    assert passrole["Condition"] == {
        "StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}
    }
    scheduler_role = _role_logical_id(template, "scheduler-invoke")
    assert passrole["Resource"] == {"Fn::GetAtt": [scheduler_role, "Arn"]}
    serialized = str(statements)
    assert "schedule-group/personal-operator-v1" in serialized
    assert "schedule/personal-operator-v1/po-*" in serialized
    assert CAPABILITY_TABLE_ARN in serialized
    for forbidden in (
        "bedrock-agentcore",
        "secretsmanager:",
        "execute-api:",
        "browser",
        "gmail",
        "sqs:",
    ):
        assert forbidden not in serialized.casefold()


def test_control_lambda_environment_binds_catalog_table_group_and_exact_target():
    _, template = _synth()
    function = next(
        function
        for function in _resources(template, "AWS::Lambda::Function")
        if function["Properties"]["FunctionName"] == CONTROL_FUNCTION_NAME
    )
    environment = function["Properties"]["Environment"]["Variables"]
    assert environment["AWS_REGION_LOCK"] == REGION
    assert environment["CAPABILITY_CATALOG_DIGEST"] == CATALOG_DIGEST
    assert environment["CAPABILITY_STATE_TABLE_NAME"] == CAPABILITY_TABLE_NAME
    assert environment["SCHEDULER_CONTROL_TABLE_NAME"] == (
        "personal-operator-scheduler-control"
    )
    assert environment["SCHEDULER_GROUP_NAME"] == "personal-operator-v1"
    assert environment["SCHEDULER_INGRESS_FUNCTION_ARN"].endswith(
        f":function:{INGRESS_FUNCTION_NAME}"
    )


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
        catalog_digest=CATALOG_DIGEST,
        capability_state_table_name=CAPABILITY_TABLE_NAME,
        capability_state_table_arn=CAPABILITY_TABLE_ARN,
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
