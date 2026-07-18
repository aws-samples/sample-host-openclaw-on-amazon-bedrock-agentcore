"""Static deployment contracts for the ordered Telegram worker boundary."""

from __future__ import annotations

from tests.test_product_configuration import (
    _flatten_statement_actions,
    _synth_router_template,
)


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _function(template: dict, name: str) -> dict:
    return next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName") == name
    )


def _statements_for_function(template: dict, name: str) -> list[dict]:
    function = _function(template, name)
    role_logical_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    statements: list[dict] = []
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        if {"Ref": role_logical_id} in resource["Properties"].get("Roles", []):
            statements.extend(
                resource["Properties"]["PolicyDocument"].get("Statement", [])
            )
    return statements


def test_router_acks_through_one_encrypted_fifo_queue_and_dedicated_dlq() -> None:
    template = _synth_router_template()
    queues = _resources(template, "AWS::SQS::Queue")

    assert len(queues) == 2
    main = next(queue for queue in queues if queue["Properties"].get("RedrivePolicy"))
    dlq = next(queue for queue in queues if not queue["Properties"].get("RedrivePolicy"))
    assert main["Properties"]["FifoQueue"] is True
    assert dlq["Properties"]["FifoQueue"] is True
    assert main["Properties"]["ContentBasedDeduplication"] is False
    assert main["Properties"]["KmsMasterKeyId"]
    assert dlq["Properties"]["KmsMasterKeyId"]
    assert main["Properties"]["VisibilityTimeout"] >= 600
    assert main["Properties"]["RedrivePolicy"]["maxReceiveCount"] == 5


def test_worker_is_single_record_fifo_consumer_with_partial_failure_reporting() -> None:
    template = _synth_router_template()
    worker = _function(template, "personal-operator-telegram-worker")
    mappings = _resources(template, "AWS::Lambda::EventSourceMapping")

    assert worker["Properties"]["Handler"] == "worker.index.lambda_handler"
    assert worker["Properties"]["Environment"]["Variables"][
        "AGENTCORE_QUALIFIER"
    ] == "DEFAULT"
    assert len(mappings) == 1
    assert mappings[0]["Properties"]["BatchSize"] == 1
    assert mappings[0]["Properties"]["FunctionResponseTypes"] == [
        "ReportBatchItemFailures"
    ]


def test_router_and_worker_have_split_exact_authority() -> None:
    template = _synth_router_template()
    router_statements = _statements_for_function(template, "openclaw-router")
    worker_statements = _statements_for_function(
        template, "personal-operator-telegram-worker"
    )
    router_actions = _flatten_statement_actions(router_statements)
    worker_actions = _flatten_statement_actions(worker_statements)

    assert "sqs:SendMessage" in router_actions
    assert "lambda:InvokeFunction" not in router_actions
    assert "bedrock-agentcore:InvokeAgentRuntime" not in router_actions
    assert "secretsmanager:GetSecretValue" in router_actions
    assert "s3:PutObject" not in router_actions

    assert "bedrock-agentcore:InvokeAgentRuntime" in worker_actions
    assert "bedrock-agentcore:StopRuntimeSession" in worker_actions
    assert "secretsmanager:GetSecretValue" in worker_actions
    assert "sqs:SendMessage" not in worker_actions
    assert "lambda:InvokeFunction" not in worker_actions


def test_only_telegram_and_health_are_public_router_routes() -> None:
    template = _synth_router_template()
    routes = _resources(template, "AWS::ApiGatewayV2::Route")
    route_keys = {route["Properties"]["RouteKey"] for route in routes}

    assert route_keys == {"POST /webhook/telegram", "GET /health"}


def test_queue_failures_and_backlog_have_explicit_alarms() -> None:
    template = _synth_router_template()
    alarms = _resources(template, "AWS::CloudWatch::Alarm")
    names = {alarm["Properties"].get("AlarmName") for alarm in alarms}

    assert {
        "personal-operator-telegram-worker-errors",
        "personal-operator-telegram-worker-throttles",
        "personal-operator-telegram-dlq-visible",
        "personal-operator-telegram-oldest-message",
    }.issubset(names)


def test_message_ledger_is_encrypted_recoverable_and_event_keyed() -> None:
    template = _synth_router_template()
    table = next(
        table
        for table in _resources(template, "AWS::DynamoDB::Table")
        if table["Properties"].get("TableName") == "personal-operator-message-ledger"
    )

    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    assert table["Properties"]["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert table["Properties"]["KeySchema"] == [
        {"AttributeName": "eventId", "KeyType": "HASH"}
    ]
    assert table["Properties"]["SSESpecification"]["SSEEnabled"] is True
