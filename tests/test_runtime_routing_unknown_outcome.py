import copy
import io
import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ReadTimeoutError

from tests.test_runtime_routing import (
    ACTOR, USER, FakeRuntime, FakeTable, candidate, cron_event, cron_table,
    legacy, load_lambda, resolve, routing,
)
from runtime_invocation import InvocationTransportError


class Runtime:
    def __init__(self, body=None, error=None, status_code=200):
        self.body, self.error = body, error
        self.status_code = status_code
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"statusCode": self.status_code, "response": self.body}


class Body:
    def __init__(self, content=b"", read_error=None, close_error=False):
        self.stream = io.BytesIO(content)
        self.read_error = read_error
        self.close_error = close_error
        self.close_calls = 0

    def read(self, size):
        if self.read_error:
            raise self.read_error
        return self.stream.read(size)

    @property
    def closed(self):
        return self.stream.closed

    def close(self):
        self.close_calls += 1
        self.stream.close()
        if self.close_error:
            raise OSError("close failed")


UNKNOWN_METADATA = [
    {"status": "unknown_outcome", "execution": {"engine": "openclaw", "phase": "unknown_outcome"}},
    {"status": "unknown_outcome"},
    {"execution": {"engine": "openclaw", "phase": "unknown_outcome"}},
    {"status": "completed", "execution": {"engine": "openclaw", "phase": "unknown_outcome"}},
    {"status": "unknown_outcome", "execution": {"phase": "completed"}},
    {"status": "unknown_outcome", "execution": ["future", "metadata"]},
]


def unknown_response(metadata=None, streamed=True):
    return {
        "response": "The request outcome is unknown. It has not been retried.",
        "streamed": streamed,
        "additionalMetadata": {"preserve": True},
        **copy.deepcopy(metadata if metadata is not None else UNKNOWN_METADATA[0]),
    }


def invoke(module, caller, route):
    if caller == "router":
        return module.invoke_agent_runtime(route, USER, ACTOR, "telegram", "hi")
    return module.invoke_agentcore(route, "cron", USER, ACTOR, "telegram", "hi")


def assert_retained(table, route):
    session = table.items[(f"USER#{USER}", "SESSION")]
    assert session["inFlight"] == 1
    assert len(session["activeAdmissions"]) == 1
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, route)


@pytest.mark.parametrize("caller", ["router", "cron"])
@pytest.mark.parametrize("record", [
    legacy(),
    {**candidate(), "platformVersion": "V1", "generation": 0, "endpointName": "DEFAULT"},
    {**candidate(), "endpointName": "DEFAULT"},
])
def test_legacy_or_mutable_route_cannot_start_an_invocation_or_create_a_replacement(caller, record):
    module = load_lambda(caller)
    module.identity_table = FakeTable(record)
    module.agentcore_client = FakeRuntime([unknown_response()])
    before = copy.deepcopy(module.identity_table.items)
    with pytest.raises(routing.RouteUnavailable, match="reconciliation"):
        module.get_or_create_session(USER)
    assert not module.agentcore_client.calls
    assert module.identity_table.items == before


@pytest.mark.parametrize("caller", ["router", "cron"])
@pytest.mark.parametrize("record", [candidate], ids=["v2"])
@pytest.mark.parametrize("metadata", UNKNOWN_METADATA)
def test_http_unknown_response_is_preserved_and_retains_its_admission(caller, record, metadata):
    module = load_lambda(caller)
    table = module.identity_table = FakeTable(record())
    response = unknown_response(metadata)
    runtime = module.agentcore_client = FakeRuntime([response])
    route = resolve(table)
    assert invoke(module, caller, route) == response
    assert len(runtime.calls) == 1
    assert_retained(table, route)
    assert not any(params["ExpressionAttributeValues"].get(":delta") == -1
                   for operation, params in table.calls if operation == "update")


@pytest.mark.parametrize("caller", ["router", "cron"])
@pytest.mark.parametrize("record", [candidate], ids=["v2"])
@pytest.mark.parametrize("response", [
    {"response": "done"},
    {"response": "done", "status": "completed", "execution": {"engine": "openclaw", "phase": "completed"}},
])
def test_completed_v2_response_still_releases(caller, record, response):
    module = load_lambda(caller)
    table = module.identity_table = FakeTable(record())
    runtime = module.agentcore_client = FakeRuntime([response])
    route = resolve(table)
    assert invoke(module, caller, route) == response
    assert len(runtime.calls) == 1
    session = table.items[(f"USER#{USER}", "SESSION")]
    assert session["inFlight"] == 0 and "activeAdmissions" not in session
    routing.freeze_route(table, route)


@pytest.mark.parametrize("streamed", [True, False])
def test_telegram_preserves_unknown_response_delivery_ownership(streamed):
    module = load_lambda("router")
    table = module.identity_table = cron_table()
    response = unknown_response(streamed=streamed)
    runtime = module.agentcore_client = FakeRuntime([response])
    with patch.object(module, "_get_telegram_token", return_value="local"), patch.object(
        module, "send_telegram_typing",
    ), patch.object(module, "_periodic_typing"), patch.object(module, "_send_telegram_chunks") as send:
        module.handle_telegram({"message": {"chat": {"id": 123}, "from": {"id": 123}, "text": "hi"}})
    if streamed:
        send.assert_not_called()
    else:
        send.assert_called_once_with(123, response["response"], "local")
    assert len(runtime.calls) == 1
    assert_retained(table, resolve(table))


@pytest.mark.parametrize("metadata", UNKNOWN_METADATA[:4])
def test_cron_does_not_report_or_deliver_unknown_execution_as_success(metadata):
    module = load_lambda("cron")
    table = module.identity_table = cron_table()
    runtime = module.agentcore_client = FakeRuntime([
        {"status": "ready", "phase": "USER_READY"}, unknown_response(metadata),
    ])
    with patch.object(module, "deliver_response") as deliver:
        assert module.handler(cron_event(), None)["statusCode"] == 503
    deliver.assert_not_called()
    assert [json.loads(call["payload"])["action"] for call in runtime.calls] == ["warmup", "cron"]
    assert_retained(table, resolve(table))


def test_unknown_execution_cannot_be_overridden_by_ready_warmup_status():
    module = load_lambda("cron")
    table = module.identity_table = FakeTable(candidate())
    runtime = module.agentcore_client = FakeRuntime([{
        "status": "ready", "phase": "USER_READY", "execution": {"phase": "unknown_outcome"},
    }])
    route = resolve(table)
    assert not module.warmup_and_wait(route, USER, ACTOR, "telegram")
    assert len(runtime.calls) == 1
    assert_retained(table, route)


def test_unknown_response_survives_body_cleanup_failure_without_releasing():
    module = load_lambda("router")
    table = module.identity_table = FakeTable(candidate())
    response = unknown_response()
    body = Body(json.dumps(response).encode(), close_error=True)
    runtime = module.agentcore_client = Runtime(body=body)
    route = resolve(table)
    assert invoke(module, "router", route) == response
    assert body.closed and body.close_calls == 1 and len(runtime.calls) == 1
    assert_retained(table, route)


def test_later_completed_call_only_releases_its_own_admission():
    module = load_lambda("router")
    table = module.identity_table = FakeTable(candidate())
    runtime = module.agentcore_client = FakeRuntime([unknown_response(), {"response": "done"}])
    route = resolve(table)
    invoke(module, "router", route)
    assert_retained(table, route)
    tokens = table.items[(f"USER#{USER}", "SESSION")]["activeAdmissions"].copy()
    assert invoke(module, "router", route) == {"response": "done"}
    assert len(runtime.calls) == 2
    assert_retained(table, route)
    assert table.items[(f"USER#{USER}", "SESSION")]["activeAdmissions"] == tokens


def test_explicit_retention_is_not_cleared_by_a_later_local_error():
    table = FakeTable(candidate())
    route = resolve(table)
    with pytest.raises(ValueError, match="local failure"):
        with routing.admit_route(table, route, USER, ACTOR) as admission:
            admission.retain()
            admission.retain()
            raise ValueError("local failure")
    assert_retained(table, route)


@pytest.mark.parametrize("record", [candidate], ids=["v2"])
@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("error_factory", [
    lambda: TimeoutError("response may have been accepted"),
    lambda: ReadTimeoutError(endpoint_url="https://runtime.invalid"),
], ids=["timeout", "sdk-read-timeout"])
def test_unknown_router_transport_retains_token_and_never_reinvokes(record, stage, error_factory):
    module = load_lambda("router")
    table = module.identity_table = FakeTable(record())
    error = error_factory()
    body = Body(read_error=error) if stage == "body" else None
    runtime = module.agentcore_client = Runtime(body=body, error=error if stage == "headers" else None)
    route = resolve(table)
    result = module.invoke_agent_runtime(route, USER, ACTOR, "telegram", "hi")
    assert "trouble" in result["response"]
    assert len(runtime.calls) == 1
    session = table.items[(f"USER#{USER}", "SESSION")]
    assert session["inFlight"] == 1
    assert len(session["activeAdmissions"]) == 1
    assert not any(params["ExpressionAttributeValues"].get(":delta") == -1
                   for operation, params in table.calls if operation == "update")
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, route)
    if body is not None:
        assert body.closed and body.close_calls == 1


@pytest.mark.parametrize("close_error", [False, True])
def test_known_streamed_response_survives_body_cleanup_and_releases_admission(close_error):
    module = load_lambda("router")
    table = module.identity_table = FakeTable(candidate())
    expected = {"response": "Already delivered", "streamed": True, "status": "completed"}
    body = Body(json.dumps(expected).encode(), close_error=close_error)
    runtime = module.agentcore_client = Runtime(body=body)
    route = resolve(table)
    assert module.invoke_agent_runtime(route, USER, ACTOR, "telegram", "hi") == expected
    assert body.closed and body.close_calls == 1 and len(runtime.calls) == 1
    session = table.items[(f"USER#{USER}", "SESSION")]
    assert session["inFlight"] == 0 and "activeAdmissions" not in session
    routing.freeze_route(table, route)


def test_body_cleanup_failure_cannot_replace_unknown_read_error_or_release_token(caplog):
    module = load_lambda("router")
    table = module.identity_table = FakeTable(candidate())
    body = Body(read_error=TimeoutError("unknown read outcome"), close_error=True)
    runtime = module.agentcore_client = Runtime(body=body)
    route = resolve(table)
    module.invoke_agent_runtime(route, USER, ACTOR, "telegram", "hi")
    assert body.closed and body.close_calls == 1 and len(runtime.calls) == 1
    session = table.items[(f"USER#{USER}", "SESSION")]
    assert session["inFlight"] == 1 and len(session["activeAdmissions"]) == 1
    failure = next(record.exc_info[1] for record in caplog.records
                   if record.getMessage().startswith("AgentCore invocation failed:"))
    assert isinstance(failure, InvocationTransportError)
    assert failure.__cause__ is body.read_error
    assert "AgentCore response cleanup failed" in caplog.text
    with pytest.raises(routing.RouteUnavailable):
        routing.freeze_route(table, route)


INVALID_RESPONSES = [
    pytest.param(json.dumps({
        "response": "unconfirmed-output-" + "x" * 500_000,
        "status": "unknown_outcome", "streamed": True,
    }).encode(), id="large-unknown-streamed-json"),
    pytest.param(b'{"response":"unconfirmed-output"}' + b" " * 500_000, id="oversize-valid-json"),
    pytest.param(b'{"response":"unconfirmed-output",broken}', id="malformed-json"),
    pytest.param(b'{"response":"unconfirmed-output","streamed":tru', id="truncated-json"),
    pytest.param(b"", id="empty-body"),
    pytest.param(b'{"response":"unconfirmed-output-\xff"}', id="invalid-utf8"),
    pytest.param(b'["unconfirmed-output"]', id="non-object-json"),
]


@pytest.mark.parametrize("caller", ["router", "cron"])
@pytest.mark.parametrize("content", INVALID_RESPONSES)
@pytest.mark.parametrize("representation", ["stream", "bytes", "text"])
def test_unconfirmed_response_retains_admission_without_returning_raw_output(caller, content, representation):
    if representation == "text":
        if b"\xff" in content:
            pytest.skip("Invalid UTF-8 applies to byte responses")
        body = content.decode()
    else:
        body = Body(content) if representation == "stream" else content
    module = load_lambda(caller)
    table = module.identity_table = FakeTable(candidate())
    runtime = module.agentcore_client = Runtime(body=body)
    route = resolve(table)
    result = invoke(module, caller, route)
    assert "unconfirmed-output" not in json.dumps(result)
    if caller == "router":
        assert result == {"response": "Sorry, I'm having trouble right now. Please try again later."}
    else:
        assert result["status"] == "failed"
    assert len(runtime.calls) == 1
    assert_retained(table, route)
    assert resolve(table) == route
    if representation == "stream":
        assert body.closed and body.close_calls == 1


@pytest.mark.parametrize("caller", ["router", "cron"])
@pytest.mark.parametrize("content", [None, *[case.values[0] for case in INVALID_RESPONSES[:4]]])
def test_handler_never_delivers_unconfirmed_body_or_replays(caller, content):
    module = load_lambda(caller)
    table = module.identity_table = cron_table()
    body = Body(content) if content is not None else None
    runtime = module.agentcore_client = Runtime(body=body)
    route = resolve(table)
    if caller == "router":
        with patch.object(module, "_get_telegram_token", return_value="local"), patch.object(
            module, "send_telegram_typing",
        ), patch.object(module, "_periodic_typing"), patch.object(module, "_send_telegram_chunks") as send:
            module.handle_telegram({"message": {"chat": {"id": 123}, "from": {"id": 123}, "text": "hi"}})
        send.assert_called_once_with(
            123, "Sorry, I'm having trouble right now. Please try again later.", "local",
        )
    else:
        with patch.object(module, "warmup_and_wait", return_value=True), patch.object(
            module, "deliver_response",
        ) as send:
            assert module.handler(cron_event(), None)["statusCode"] == 503
        send.assert_not_called()
    assert len(runtime.calls) == 1
    assert_retained(table, route)
    assert resolve(table) == route


def test_cron_http_error_without_body_does_not_confirm_execution_outcome():
    module = load_lambda("cron")
    table = module.identity_table = FakeTable(candidate())
    runtime = module.agentcore_client = Runtime(status_code=502)
    route = resolve(table)
    assert invoke(module, "cron", route)["status"] == "failed"
    assert len(runtime.calls) == 1
    assert_retained(table, route)
