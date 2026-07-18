"""Static deployment contracts for the ordered Telegram worker boundary."""

from __future__ import annotations

from tests.test_product_configuration import (
    TEST_RUNTIME_ARN,
    TEST_RUNTIME_ENDPOINT_NAME,
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
    ] == TEST_RUNTIME_ENDPOINT_NAME
    assert len(mappings) == 1
    assert mappings[0]["Properties"]["BatchSize"] == 1
    assert mappings[0]["Properties"]["FunctionResponseTypes"] == [
        "ReportBatchItemFailures"
    ]


def test_router_and_worker_share_only_the_reviewed_arm64_asset() -> None:
    template = _synth_router_template()
    router = _function(template, "openclaw-router")
    worker = _function(template, "personal-operator-telegram-worker")

    assert router["Properties"]["Handler"] == "router.index.handler"
    assert worker["Properties"]["Handler"] == "worker.index.lambda_handler"
    assert router["Properties"]["Architectures"] == ["arm64"]
    assert worker["Properties"]["Architectures"] == ["arm64"]
    assert router["Properties"]["Code"] == worker["Properties"]["Code"]


def test_trusted_workspace_broker_and_worker_capability_issuer_are_wired() -> None:
    template = _synth_router_template()
    broker = _function(
        template, "personal-operator-workspace-credential-broker"
    )
    worker = _function(template, "personal-operator-telegram-worker")

    assert broker["Properties"]["Handler"] == "workspace_broker.index.lambda_handler"
    assert broker["Properties"]["Role"] == (
        "arn:aws:iam::123456789012:role/"
        "personal-operator-workspace-credential-broker-eu-west-1"
    )
    assert broker["Properties"]["Environment"]["Variables"] == {
        "WORKSPACE_CAPABILITY_SECRET_ID": "personal-operator/workspace-capability",
        "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
            "personal-operator-workspace-credential-broker"
        ),
        "WORKSPACE_SESSION_ROLE_ARN": (
            "arn:aws:iam::123456789012:role/"
            "openclaw-workspace-session-role-eu-west-1"
        ),
        "RUNTIME_STATE_TABLE_NAME": {
            "Ref": next(
                logical_id
                for logical_id, resource in template["Resources"].items()
                if resource["Type"] == "AWS::DynamoDB::Table"
                and resource["Properties"].get("TableName")
                == "personal-operator-runtime-state"
            )
        },
        "S3_USER_FILES_BUCKET": "openclaw-user-files-test",
        "CMK_ARN": "arn:aws:kms:eu-west-1:123456789012:key/test-key",
        "AGENTCORE_RUNTIME_ARN": TEST_RUNTIME_ARN,
        "AGENTCORE_QUALIFIER": TEST_RUNTIME_ENDPOINT_NAME,
    }
    worker_environment = worker["Properties"]["Environment"]["Variables"]
    assert worker_environment["WORKSPACE_CAPABILITY_SECRET_ID"] == (
        "personal-operator/workspace-capability"
    )
    assert worker_environment["WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME"] == (
        "personal-operator-workspace-credential-broker"
    )


def test_router_and_worker_have_separate_exact_dynamodb_cmk_authority() -> None:
    template = _synth_router_template()
    expected_actions = {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
    }
    for function_name in (
        "openclaw-router",
        "personal-operator-telegram-worker",
    ):
        statements = _statements_for_function(template, function_name)
        matches = [
            statement
            for statement in statements
            if set(
                statement["Action"]
                if isinstance(statement.get("Action"), list)
                else [statement.get("Action")]
            )
            == expected_actions
        ]
        assert len(matches) == 1
        assert matches[0]["Resource"] == (
            "arn:aws:kms:eu-west-1:123456789012:key/test-key"
        )
        assert matches[0]["Condition"] == {
            "StringEquals": {
                "kms:CallerAccount": "123456789012",
                "kms:ViaService": "dynamodb.eu-west-1.amazonaws.com",
            }
        }


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

    identity_transactions = [
        statement
        for statement in router_statements
        if "dynamodb:TransactWriteItems"
        in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    ]
    assert len(identity_transactions) == 1
    assert set(identity_transactions[0]["Action"]) == {
        "dynamodb:GetItem",
        "dynamodb:TransactWriteItems",
    }
    assert not isinstance(identity_transactions[0]["Resource"], list)
    assert "index" not in str(identity_transactions[0]["Resource"])

    assert "bedrock-agentcore:InvokeAgentRuntime" in worker_actions
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in worker_actions
    assert "bedrock-agentcore:StopRuntimeSession" in worker_actions
    assert "secretsmanager:GetSecretValue" in worker_actions
    assert "sqs:SendMessage" not in worker_actions
    assert "lambda:InvokeFunction" in worker_actions

    worker = _function(template, "personal-operator-telegram-worker")
    assert worker["Properties"]["Environment"]["Variables"][
        "CONTROL_FUNCTION_NAME"
    ] == "personal-operator-control-command:live"
    invoke = next(
        statement
        for statement in worker_statements
        if "lambda:InvokeFunction" in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    )
    assert invoke["Resource"] == (
        "arn:aws:lambda:eu-west-1:123456789012:"
        "function:personal-operator-control-command:live"
    )


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
        "personal-operator-telegram-worker-failed-records",
        "personal-operator-telegram-dlq-visible",
        "personal-operator-telegram-oldest-message",
    }.issubset(names)
    filters = _resources(template, "AWS::Logs::MetricFilter")
    assert len(filters) == 1
    assert "Telegram FIFO record failed" in filters[0]["Properties"]["FilterPattern"]
    transformation = filters[0]["Properties"]["MetricTransformations"][0]
    assert transformation["MetricNamespace"] == "PersonalOperator/Worker"
    assert transformation["MetricName"] == "FailedRecords"


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
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }
    assert table["Properties"]["SSESpecification"]["SSEEnabled"] is True


def test_identity_and_message_ledger_are_exactly_user_indexed_for_deletion() -> None:
    template = _synth_router_template()
    tables = {
        table["Properties"].get("TableName"): table
        for table in _resources(template, "AWS::DynamoDB::Table")
    }

    for table_name in ("openclaw-identity", "personal-operator-message-ledger"):
        indexes = tables[table_name]["Properties"]["GlobalSecondaryIndexes"]
        assert indexes == [
            {
                "IndexName": "userId-index",
                "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ]
