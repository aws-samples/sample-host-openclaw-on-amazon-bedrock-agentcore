import copy
import hashlib
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.session import Session
from botocore.validate import validate_parameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import runtime_deploy as deploy
from postdeploy_warmup import WarmupError, run_warmup


IMAGE = "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge@sha256:" + "a" * 64
ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/openclaw_agent_v2-1234567890"
MODEL = Session().get_service_model("bedrock-agentcore-control")


def configuration(jwt=False):
    config = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
        "roleArn": "arn:aws:iam::123456789012:role/openclaw",
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": ["subnet-12345678"], "securityGroups": ["sg-12345678"],
                "requireServiceS3Endpoint": False,
            },
        },
        "description": "Retain this description",
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 28800},
        "metadataConfiguration": {"requireMMDSV2": True},
        "environmentVariables": {"KEEP_ME": "yes", "AGENTCORE_RUNTIME_PLATFORM": "V2"},
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
        "capacityProviderConfiguration": {
            "capacityProviderArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:capacity-provider/test"
        },
        "platformVersion": "V2",
    }
    if jwt:
        config["authorizerConfiguration"] = {"customJWTAuthorizer": {
            "discoveryUrl": "https://example.com/.well-known/openid-configuration",
            "allowedClients": ["client-id"],
        }}
    return config


class FakeControl:
    def __init__(self, config=None):
        self.meta = SimpleNamespace(service_model=MODEL, region_name="us-west-2")
        self.current = dict(copy.deepcopy(config or configuration()),
                            agentRuntimeId="openclaw_agent_v2-1234567890",
                            agentRuntimeArn=ARN, agentRuntimeName="openclaw_agent_v2",
                            agentRuntimeVersion="1", status="READY")
        self.calls = []
        self.statuses = []
        self.endpoint = None
        self.endpoints = {}
        self.versions = {}

    def get_agent_runtime(self, **kwargs):
        self.calls.append(("get", kwargs))
        result = copy.deepcopy(self.current)
        if kwargs.get("agentRuntimeVersion") in self.versions:
            result = copy.deepcopy(self.versions[kwargs["agentRuntimeVersion"]])
        if self.statuses:
            result["status"] = self.statuses.pop(0)
        return result

    def create_agent_runtime(self, **kwargs):
        validate_parameters(kwargs, MODEL.operation_model("CreateAgentRuntime").input_shape)
        self.calls.append(("create", copy.deepcopy(kwargs)))
        self.current = {k: v for k, v in self.current.items()
                        if k in {"agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion", "status"}}
        self.current.update({k: copy.deepcopy(v) for k, v in kwargs.items()
                             if k not in {"clientToken", "tags"}})
        return copy.deepcopy(self.current)

    def update_agent_runtime(self, **kwargs):
        validate_parameters(kwargs, MODEL.operation_model("UpdateAgentRuntime").input_shape)
        self.calls.append(("update", copy.deepcopy(kwargs)))
        self.versions[self.current["agentRuntimeVersion"]] = copy.deepcopy(self.current)
        previous_network = self.current.get("networkConfiguration", {}).get("networkModeConfig", {})
        self.current.update({k: copy.deepcopy(v) for k, v in kwargs.items() if k != "clientToken"})
        network = self.current.get("networkConfiguration", {}).get("networkModeConfig", {})
        if "requireServiceS3Endpoint" in previous_network:
            # The service retains this readback field when an update omits it.
            network.setdefault("requireServiceS3Endpoint", previous_network["requireServiceS3Endpoint"])
        self.current["agentRuntimeVersion"] = str(int(self.current["agentRuntimeVersion"]) + 1)
        return copy.deepcopy(self.current)

    def get_agent_runtime_endpoint(self, **kwargs):
        self.calls.append(("get-endpoint", kwargs))
        endpoint = self.endpoints.get(kwargs["endpointName"])
        if self.endpoint is not None and self.endpoint.get("name") == kwargs["endpointName"]:
            endpoint = self.endpoint
        if endpoint is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntimeEndpoint")
        return copy.deepcopy(endpoint)

    def create_agent_runtime_endpoint(self, **kwargs):
        validate_parameters(kwargs, MODEL.operation_model("CreateAgentRuntimeEndpoint").input_shape)
        self.calls.append(("create-endpoint", kwargs))
        self.endpoint = {"status": "READY", "name": kwargs["name"],
                         "liveVersion": kwargs["agentRuntimeVersion"],
                         "targetVersion": kwargs["agentRuntimeVersion"], "agentRuntimeArn": ARN}
        self.endpoints[kwargs["name"]] = self.endpoint
        return copy.deepcopy(self.endpoint)

    def update_agent_runtime_endpoint(self, **kwargs):
        validate_parameters(kwargs, MODEL.operation_model("UpdateAgentRuntimeEndpoint").input_shape)
        self.calls.append(("update-endpoint", kwargs))
        self.endpoint.update(liveVersion=kwargs["agentRuntimeVersion"],
                             targetVersion=kwargs["agentRuntimeVersion"])
        return copy.deepcopy(self.endpoint)


class FakeCreateNetworkReadback(FakeControl):
    def __init__(self, network_readback):
        super().__init__()
        self.network_readback = copy.deepcopy(network_readback)

    def create_agent_runtime(self, **kwargs):
        created = super().create_agent_runtime(**kwargs)
        self.current["networkConfiguration"]["networkModeConfig"].update(self.network_readback)
        return created


class FakeEndpointReadback(FakeControl):
    def __init__(self):
        super().__init__()
        self.endpoint_readbacks = []
        self.interrupt_endpoint = None

    def get_agent_runtime_endpoint(self, **kwargs):
        if self.endpoint_readbacks:
            self.calls.append(("get-endpoint", kwargs))
            observation = self.endpoint_readbacks.pop(0)
            if isinstance(observation, Exception):
                raise observation
            self.endpoint = copy.deepcopy(observation)
            return copy.deepcopy(self.endpoint)
        return super().get_agent_runtime_endpoint(**kwargs)

    def create_agent_runtime_endpoint(self, **kwargs):
        result = super().create_agent_runtime_endpoint(**kwargs)
        if kwargs["name"] == self.interrupt_endpoint:
            self.endpoint["status"] = "CREATING"
            self.endpoint.pop("liveVersion")
            raise KeyboardInterrupt("deployment interrupted after endpoint creation")
        return result


@pytest.fixture
def endpoint_clock(monkeypatch):
    state = SimpleNamespace(now=0, sleeps=[])
    original_wait = deploy.wait_endpoint

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    def wait(*args, **kwargs):
        return original_wait(*args, **kwargs, sleep=sleep, clock=lambda: state.now)

    monkeypatch.setattr(deploy, "wait_endpoint", wait)
    monkeypatch.setattr(deploy, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    return state


class FakeInvoke:
    def __init__(self, body=None, status=200):
        self.body = body
        self.status = status
        self.calls = []
        self.stream = None

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["payload"])
        body = self.body
        if body is None:
            body = {
                "response": "ok", "runtimePlatform": "V2", "phase": "USER_READY",
                "execution": {"engine": "openclaw", "phase": "completed"},
                **{key: payload[key] for key in ("userId", "actorId", "sessionId", "runtimeSessionId")},
            }
        if callable(body):
            body = body(payload)
        self.stream = io.BytesIO(json.dumps(body).encode())
        return {"statusCode": self.status, "response": self.stream}


def test_model_gate_checks_all_required_operations():
    deploy.check_service_model(FakeControl())


def test_request_retains_all_mutable_fields_and_input_is_unchanged():
    current = configuration(jwt=True)
    before = copy.deepcopy(current)
    desired = deploy.build_configuration(FakeControl(), current, {
        "environmentVariables": {"NEW_KEY": "value"},
    }, IMAGE, "V2")
    for field in deploy.MUTABLE_FIELDS - {"environmentVariables"}:
        assert desired[field] == current[field]
    assert desired["environmentVariables"]["KEEP_ME"] == "yes"
    assert desired["environmentVariables"]["NEW_KEY"] == "value"
    assert current == before


def test_create_omits_update_only_fields():
    request = deploy.create_request(configuration(), "openclaw_agent_v2")
    assert "metadataConfiguration" not in request
    assert "requireServiceS3Endpoint" not in request["networkConfiguration"]["networkModeConfig"]
    assert request["filesystemConfigurations"] == configuration()["filesystemConfigurations"]
    assert request["platformVersion"] == "V2"
    validate_parameters(request, MODEL.operation_model("CreateAgentRuntime").input_shape)


@pytest.mark.parametrize("require_service_s3_endpoint", [False, True])
def test_update_request_omits_unchanged_flag_without_mutating_inputs(require_service_s3_endpoint):
    current = FakeControl(configuration(jwt=True)).current
    current["agentRuntimeVersion"] = "7"
    current["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = require_service_s3_endpoint
    desired = deploy.mutable_configuration(current)
    desired["environmentVariables"]["NEW_KEY"] = "new-value"
    before_current, before_desired = copy.deepcopy(current), copy.deepcopy(desired)
    desired_hash = deploy.configuration_hash(desired)
    runtime_id = current["agentRuntimeId"]

    request = deploy.update_request(desired, current, runtime_id)

    expected = copy.deepcopy(before_desired)
    expected["networkConfiguration"]["networkModeConfig"].pop("requireServiceS3Endpoint")
    expected["agentRuntimeId"] = runtime_id
    request_hash = deploy.configuration_hash(expected)
    expected["clientToken"] = hashlib.sha256(f"{runtime_id}:7:{request_hash}".encode()).hexdigest()
    assert request == expected
    assert request["clientToken"] != hashlib.sha256(f"{runtime_id}:7:{desired_hash}".encode()).hexdigest()
    assert deploy.update_request(desired, current, runtime_id) == request
    assert deploy.configuration_hash(desired) == desired_hash
    assert desired == before_desired
    assert current == before_current
    validate_parameters(request, MODEL.operation_model("UpdateAgentRuntime").input_shape)

    request["networkConfiguration"]["networkModeConfig"]["subnets"].append("subnet-87654321")
    request["environmentVariables"]["NEW_KEY"] = "changed-request"
    assert desired == before_desired
    assert current == before_current


@pytest.mark.parametrize("current_flag,desired_flag", [(False, True), (True, False)])
def test_update_request_preserves_explicit_network_flag_change(current_flag, desired_flag):
    current = FakeControl().current
    current["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = current_flag
    desired = deploy.mutable_configuration(current)
    desired["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = desired_flag
    before_current, before_desired = copy.deepcopy(current), copy.deepcopy(desired)

    request = deploy.update_request(desired, current, current["agentRuntimeId"])

    assert request["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] is desired_flag
    assert desired == before_desired
    assert current == before_current
    validate_parameters(request, MODEL.operation_model("UpdateAgentRuntime").input_shape)


@pytest.mark.parametrize("current_flag,desired_flag", [
    (None, None), (False, None), (True, None), (None, False), (None, True),
])
def test_update_request_handles_absent_network_flag(current_flag, desired_flag):
    current = FakeControl().current
    desired = configuration()
    for config, flag in ((current, current_flag), (desired, desired_flag)):
        network = config["networkConfiguration"]["networkModeConfig"]
        if flag is None:
            network.pop("requireServiceS3Endpoint")
        else:
            network["requireServiceS3Endpoint"] = flag
    before_current, before_desired = copy.deepcopy(current), copy.deepcopy(desired)

    request = deploy.update_request(desired, current, current["agentRuntimeId"])

    assert request["networkConfiguration"] == before_desired["networkConfiguration"]
    assert desired == before_desired
    assert current == before_current
    validate_parameters(request, MODEL.operation_model("UpdateAgentRuntime").input_shape)


def test_environment_limit_counts_utf8_keys_and_values():
    assert deploy.environment_size({"X": "あ"}) == 4
    with pytest.raises(deploy.DeploymentError, match="2500"):
        deploy.build_configuration(FakeControl(), configuration(),
                                   {"environmentVariables": {"EXTRA": "x" * 2500}}, IMAGE, "V2")


def test_unknown_configuration_is_rejected():
    with pytest.raises(deploy.DeploymentError, match="unknown"):
        deploy.build_configuration(FakeControl(), configuration(), {"madeUpField": True}, IMAGE, "V2")


def test_waiter_waits_for_version_not_just_old_ready_status():
    client = FakeControl()
    client.statuses = ["UPDATING", "READY"]
    got = deploy.wait_runtime(client, client.current["agentRuntimeId"], "1",
                              timeout=3, interval=0, sleep=lambda _: None)
    assert got["status"] == "READY"
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETING", "UNKNOWN"])
def test_waiter_fails_closed(status):
    client = FakeControl()
    client.statuses = [status]
    with pytest.raises(deploy.DeploymentError):
        deploy.wait_runtime(client, "openclaw_agent_v2-1234567890", timeout=1, interval=0)


def test_readback_mismatch_rejected_without_secret_value():
    desired = configuration()
    current = copy.deepcopy(desired)
    current["environmentVariables"]["KEEP_ME"] = "sensitive-value"
    with pytest.raises(deploy.DeploymentError, match="environmentVariables") as err:
        deploy.verify_configuration(current, desired)
    assert "sensitive-value" not in str(err.value)


def test_endpoint_uses_name_and_never_repoints_live_version():
    client = FakeControl()
    deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "release_1", "1",
                           timeout=1, interval=0)
    assert client.endpoint["name"] == "release_1"
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "release_1", "2",
                               timeout=1, interval=0)
    assert client.endpoint["liveVersion"] == "1"
    assert not any(op == "update-endpoint" for op, _ in client.calls)


def test_warmup_real_chat_is_isolated_and_response_stream_closed():
    client = FakeInvoke()
    result = run_warmup(client, ARN, "release_1")
    assert result["status"] == "succeeded"
    request = client.calls[0]
    payload = json.loads(request["payload"])
    assert payload["action"] == "chat"
    assert payload["actorId"].startswith("deployment-warmup:")
    assert payload["channel"] == "deployment-warmup"
    assert len(request["runtimeSessionId"]) >= 33
    assert request["qualifier"] == "release_1"
    assert client.stream.closed


@pytest.mark.parametrize("body,status", [
    ({"response": "ok", "status": "error"}, 200),
    ({"response": "ok", "status": "initializing"}, 200),
    ({"response": "I'm starting up — please try again."}, 200),
    ({"error": "failed"}, 200), ({"response": ""}, 200), ({"response": "ok"}, 500),
])
def test_warmup_rejects_failure_without_retry(body, status):
    client = FakeInvoke(body, status)
    with pytest.raises(WarmupError):
        run_warmup(client, ARN, "release_1")
    assert len(client.calls) == 1
    assert client.stream.closed


def test_warmup_rejects_channel_delivery_without_retry():
    def body(payload):
        return {
            "response": "ok", "runtimePlatform": "V2", "phase": "USER_READY",
            "execution": {"engine": "openclaw", "phase": "completed"},
            **{key: payload[key] for key in ("userId", "actorId", "sessionId", "runtimeSessionId")},
            "streamed": True,
        }

    client = FakeInvoke(body)
    with pytest.raises(WarmupError, match="unexpectedly delivered a channel message"):
        run_warmup(client, ARN, "release_1")
    assert len(client.calls) == 1
    assert client.stream.closed


@pytest.mark.parametrize("raw,message", [
    pytest.param(b"invalid-json", "invalid JSON", id="malformed-json"),
    pytest.param(b"[]", "not an object", id="json-array"),
    pytest.param(b"x" * (1024 * 1024 + 1), "exceeds 1 MiB", id="over-1-mib"),
])
def test_warmup_rejects_invalid_response_body_without_retry(raw, message):
    class RawInvoke(FakeInvoke):
        def invoke_agent_runtime(self, **kwargs):
            result = super().invoke_agent_runtime(**kwargs)
            self.stream.close()
            self.stream = io.BytesIO(raw)
            return {**result, "response": self.stream}

    client = RawInvoke()
    with pytest.raises(WarmupError, match=message):
        run_warmup(client, ARN, "release_1")
    assert len(client.calls) == 1
    assert client.stream.closed


def perform_deploy(tmp_path, client, invoke=None, **kwargs):
    options = {
        "manifest_path": tmp_path / "runtime-v2.json", "region": "us-west-2",
        "platform": "V2", "name": "openclaw_agent_v2", "endpoint_name": "release_1",
        "image_uri": IMAGE, "image_digest": "sha256:" + "a" * 64,
        "overrides": configuration(), "timeout": 1,
    }
    options.update(kwargs)
    return deploy.deploy_runtime(client, invoke or FakeInvoke(), **options)


def test_create_preserves_metadata_via_ready_then_update_and_warmup(tmp_path):
    client = FakeControl()
    invoke = FakeInvoke()
    manifest = perform_deploy(tmp_path, client, invoke)
    operations = [op for op, _ in client.calls]
    assert operations.index("create") < operations.index("get") < operations.index("update")
    assert client.current["metadataConfiguration"] == {"requireMMDSV2": True}
    assert client.current["agentRuntimeVersion"] == "2"
    assert manifest["warmup"]["status"] == "succeeded"
    assert manifest["configurationHash"] == deploy.configuration_hash(client.current)
    assert manifest["endpointName"] == "release_1"
    assert "KEEP_ME" not in (tmp_path / "runtime-v2.json").read_text()
    assert (tmp_path / "runtime-v2.json").stat().st_mode & 0o777 == 0o600
    assert len(invoke.calls) == 1


@pytest.fixture
def fresh_create_configuration():
    config = configuration()
    config.pop("metadataConfiguration")
    config["networkConfiguration"]["networkModeConfig"].pop("requireServiceS3Endpoint")
    return config


@pytest.mark.parametrize("require_service_s3_endpoint", [False, True])
def test_create_accepts_service_populated_network_flag_without_update(
        tmp_path, fresh_create_configuration, require_service_s3_endpoint):
    client = FakeCreateNetworkReadback({"requireServiceS3Endpoint": require_service_s3_endpoint})
    invoke = FakeInvoke()

    manifest = perform_deploy(tmp_path, client, invoke, overrides=fresh_create_configuration)

    creates = [args for op, args in client.calls if op == "create"]
    assert len(creates) == 1
    assert creates[0]["networkConfiguration"] == fresh_create_configuration["networkConfiguration"]
    assert "requireServiceS3Endpoint" not in creates[0]["networkConfiguration"]["networkModeConfig"]
    assert not any(op == "update" for op, _ in client.calls)
    assert manifest["agentRuntimeVersion"] == "1"
    assert manifest["warmup"]["status"] == "succeeded"
    assert client.endpoint["liveVersion"] == "1"
    assert len(invoke.calls) == 1

    expected = copy.deepcopy(fresh_create_configuration)
    expected["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = require_service_s3_endpoint
    assert client.current["networkConfiguration"] == expected["networkConfiguration"]
    assert manifest["configurationHash"] == deploy.configuration_hash(expected)
    assert manifest["configurationHash"] != deploy.configuration_hash(fresh_create_configuration)
    saved = deploy.load_manifest(tmp_path / "runtime-v2.json")
    assert saved == manifest
    deploy.verify_manifest(client, saved, "us-west-2")


@pytest.mark.parametrize("field,value", [
    ("subnets", ["subnet-87654321"]),
    ("securityGroups", ["sg-87654321"]),
])
def test_create_rejects_network_drift_alongside_service_populated_flag(
        tmp_path, fresh_create_configuration, field, value):
    client = FakeCreateNetworkReadback({"requireServiceS3Endpoint": False, field: value})
    invoke = FakeInvoke()

    with pytest.raises(deploy.DeploymentError, match="networkConfiguration"):
        perform_deploy(tmp_path, client, invoke, overrides=fresh_create_configuration)

    assert any(op == "create" for op, _ in client.calls)
    assert not any(op in {"update", "create-endpoint", "update-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert deploy.load_manifest(tmp_path / "runtime-v2.json")["warmup"]["status"] == "failed"


def test_same_deployment_is_idempotent_without_second_warmup(tmp_path):
    client = FakeControl()
    invoke = FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    before = len([op for op, _ in client.calls if op in {"create", "update", "create-endpoint", "update-endpoint"}])
    perform_deploy(tmp_path, client, invoke)
    after = len([op for op, _ in client.calls if op in {"create", "update", "create-endpoint", "update-endpoint"}])
    assert before == after
    assert len(invoke.calls) == 1


@pytest.mark.parametrize("operation", ["create", "update", "endpoint"])
@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "READY"])
def test_interrupted_endpoint_retry_resumes_same_release(
        tmp_path, monkeypatch, endpoint_clock, operation, status):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    endpoint_name = "release_1" if operation == "create" else "release_2"
    overrides = {
        "create": configuration(), "update": {"description": "second release"}, "endpoint": {},
    }[operation]
    if operation != "create":
        perform_deploy(tmp_path, client, invoke)
    old_endpoints = copy.deepcopy(client.endpoints)
    invoke.calls.clear()
    client.interrupt_endpoint = endpoint_name
    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        perform_deploy(tmp_path, client, invoke, endpoint_name=endpoint_name, overrides=overrides)

    path = tmp_path / "runtime-v2.json"
    pending = deploy.load_manifest(path)
    assert pending["warmup"]["status"] == "pending"
    assert not invoke.calls
    version = pending["agentRuntimeVersion"]
    ready = dict(client.endpoint, status="READY", liveVersion=version)
    observation = dict(ready, status=status)
    if status != "READY":
        observation.pop("liveVersion")
    client.endpoint_readbacks = [observation, observation, ready]
    client.calls.clear()
    original_write = deploy.write_manifest

    def write_after_ready(path, manifest):
        assert client.endpoint["status"] == "READY"
        original_write(path, manifest)

    monkeypatch.setattr(deploy, "write_manifest", write_after_ready)
    resumed = perform_deploy(tmp_path, client, invoke, endpoint_name=None,
                             overrides=overrides, timeout=20)
    assert resumed["endpointName"] == endpoint_name
    assert resumed["agentRuntimeVersion"] == version
    assert resumed["configurationHash"] == pending["configurationHash"]
    assert resumed["warmup"]["status"] == "succeeded"
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert len(invoke.calls) == 1
    assert invoke.calls[0]["qualifier"] == endpoint_name
    for name, old_endpoint in old_endpoints.items():
        assert client.endpoints[name] == old_endpoint
    deploy.verify_manifest(client, resumed, "us-west-2", require_enrollment_ready=True)


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "READY"])
@pytest.mark.parametrize("conflict", ["name", "targetVersion", "liveVersion", "configuration"])
def test_endpoint_retry_rejects_conflicting_release_before_writes(
        tmp_path, endpoint_clock, status, conflict):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    original = (tmp_path / "runtime-v2.json").read_bytes()
    observation = dict(client.endpoint, status=status)
    overrides = {}
    if conflict == "configuration":
        overrides = {"environmentVariables": {"KEEP_ME": "changed-secret"}}
    else:
        observation[conflict] = "other_release" if conflict == "name" else "999"
    client.endpoint_readbacks = [observation]
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="immutable") as error:
        perform_deploy(tmp_path, client, invoke, overrides=overrides)
    assert "changed-secret" not in str(error.value)
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert not endpoint_clock.sleeps
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETED", "UNKNOWN"])
@pytest.mark.parametrize("during_wait", [False, True])
def test_endpoint_retry_failure_preserves_previous_manifest(
        tmp_path, endpoint_clock, status, during_wait):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    original = (tmp_path / "runtime-v2.json").read_bytes()
    failed = dict(client.endpoint, status=status)
    client.endpoint_readbacks = ([dict(client.endpoint, status="CREATING")] if during_wait else []) + [failed]
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(deploy.DeploymentError, match=status):
        perform_deploy(tmp_path, client, invoke, overrides={})
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "READY"])
def test_endpoint_retry_timeout_preserves_previous_manifest(tmp_path, endpoint_clock, status):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    original = (tmp_path / "runtime-v2.json").read_bytes()
    client.endpoint["status"] = status
    client.endpoint.pop("liveVersion")
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="timed out"):
        perform_deploy(tmp_path, client, invoke, overrides={}, timeout=1)
    assert endpoint_clock.sleeps == [1]
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


@pytest.mark.parametrize("outcome", ["ready", "conflict", "failure", "timeout"])
def test_endpoint_retry_rechecks_after_preflight_without_losing_manifest(
        tmp_path, monkeypatch, endpoint_clock, outcome):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    previous = perform_deploy(tmp_path, client, invoke)
    path = tmp_path / "runtime-v2.json"
    original = path.read_bytes()
    ready = copy.deepcopy(client.endpoint)
    pending = dict(ready, status="UPDATING")
    pending.pop("liveVersion")
    # Preflight succeeds; a subsequent observation still needs to be checked.
    client.endpoint_readbacks = [ready, pending]
    if outcome == "ready":
        client.endpoint_readbacks += [pending, ready]
    elif outcome == "conflict":
        client.endpoint_readbacks.append(dict(pending, targetVersion="999"))
    elif outcome == "failure":
        client.endpoint_readbacks.append(dict(pending, status="UPDATE_FAILED"))
    client.calls.clear()
    invoke.calls.clear()
    original_write = deploy.write_manifest

    def write_after_ready(path, manifest):
        assert client.endpoint["status"] == "READY"
        original_write(path, manifest)

    monkeypatch.setattr(deploy, "write_manifest", write_after_ready)
    if outcome == "ready":
        resumed = perform_deploy(tmp_path, client, invoke, overrides={}, timeout=20)
        assert resumed["warmup"] == previous["warmup"]
        assert resumed["agentRuntimeVersion"] == previous["agentRuntimeVersion"]
    else:
        message = {"conflict": "immutable", "failure": "UPDATE_FAILED", "timeout": "timed out"}[outcome]
        with pytest.raises(deploy.DeploymentError, match=message):
            perform_deploy(tmp_path, client, invoke, overrides={}, timeout=1)
        assert path.read_bytes() == original
        if outcome == "timeout":
            assert endpoint_clock.sleeps == [1]
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls


@pytest.mark.parametrize("field", ["targetVersion", "liveVersion"])
def test_ensure_pending_endpoint_never_waits_for_another_release(endpoint_clock, field):
    client = FakeEndpointReadback()
    client.endpoint = {"name": "release_1", "status": "CREATING",
                       "targetVersion": "2", "liveVersion": "2"}
    client.endpoint[field] = "1"
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "release_1", "2", timeout=1)
    assert not endpoint_clock.sleeps
    assert all(op == "get-endpoint" for op, _ in client.calls)


@pytest.mark.parametrize("status", ["CREATING", "UPDATING"])
def test_ensure_endpoint_waits_for_missing_version_readback(endpoint_clock, status):
    client = FakeEndpointReadback()
    ready = {"name": "release_1", "status": "READY", "targetVersion": "1", "liveVersion": "1"}
    client.endpoint_readbacks = [{"name": "release_1", "status": status}, ready]
    result = deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "release_1", "1",
                                    timeout=2, interval=1)
    assert result == ready
    assert endpoint_clock.sleeps == [1]
    assert all(op == "get-endpoint" for op, _ in client.calls)


def test_ensure_endpoint_state_changes_share_one_deadline(endpoint_clock):
    client = FakeEndpointReadback()
    endpoint = {"name": "release_1", "targetVersion": "1", "liveVersion": "1"}
    client.endpoint_readbacks = [
        dict(endpoint, status=status) for status in ("CREATING", "UPDATING", "CREATING", "READY")
    ]
    with pytest.raises(deploy.DeploymentError, match="timed out"):
        deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "release_1", "1",
                               timeout=2, interval=1)
    assert endpoint_clock.sleeps == [1, 1]
    assert client.endpoint_readbacks == [dict(endpoint, status="READY")]
    assert all(op == "get-endpoint" for op, _ in client.calls)


@pytest.mark.parametrize("failure", ["runtime_network", "endpoint_updating"])
def test_endpoint_retry_final_readback_failure_preserves_cached_warmup(
        tmp_path, monkeypatch, endpoint_clock, failure):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    path = tmp_path / "runtime-v2.json"
    original = path.read_bytes()
    if failure == "runtime_network":
        original_get = client.get_agent_runtime

        def fail_final_readback(**kwargs):
            if "agentRuntimeVersion" in kwargs:
                client.calls.append(("get", kwargs))
                raise ReadTimeoutError(endpoint_url="https://example.invalid")
            return original_get(**kwargs)

        monkeypatch.setattr(client, "get_agent_runtime", fail_final_readback)
        expected_error = ReadTimeoutError
    else:
        ready = copy.deepcopy(client.endpoint)
        client.endpoint_readbacks = [ready, ready, dict(ready, status="UPDATING")]
        expected_error = deploy.DeploymentError
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(expected_error):
        perform_deploy(tmp_path, client, invoke, overrides={})
    assert path.read_bytes() == original
    assert not invoke.calls
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)


def test_endpoint_retry_previously_observed_endpoint_is_never_recreated(tmp_path, endpoint_clock):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    path = tmp_path / "runtime-v2.json"
    original = path.read_bytes()
    client.endpoint_readbacks = [
        copy.deepcopy(client.endpoint),
        ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntimeEndpoint"),
    ]
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="previously observed endpoint"):
        perform_deploy(tmp_path, client, invoke, overrides={})
    assert path.read_bytes() == original
    assert not invoke.calls
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)


def test_endpoint_retry_preflight_and_ensure_share_deadline(tmp_path, endpoint_clock):
    client, invoke = FakeEndpointReadback(), FakeInvoke()
    perform_deploy(tmp_path, client, invoke)
    path = tmp_path / "runtime-v2.json"
    original = path.read_bytes()
    ready = copy.deepcopy(client.endpoint)
    client.endpoint_readbacks = [
        dict(ready, status="CREATING"), ready, dict(ready, status="UPDATING"), ready,
    ]
    client.calls.clear()
    invoke.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="timed out"):
        perform_deploy(tmp_path, client, invoke, overrides={}, timeout=8)
    assert endpoint_clock.now == 8
    assert endpoint_clock.sleeps == [5, 3]
    assert path.read_bytes() == original
    assert not invoke.calls
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)


@pytest.mark.parametrize("entrypoint", ["wait", "preflight", "ensure"])
def test_endpoint_deadline_rejects_ready_from_slow_read(monkeypatch, endpoint_clock, entrypoint):
    client = FakeEndpointReadback()
    client.endpoint = {"name": "release_1", "status": "READY", "liveVersion": "1", "targetVersion": "1"}
    original_get = client.get_agent_runtime_endpoint

    def slow_read(**kwargs):
        endpoint_clock.now += 2
        return original_get(**kwargs)

    monkeypatch.setattr(client, "get_agent_runtime_endpoint", slow_read)
    args = (client, client.current["agentRuntimeId"], "release_1")
    with pytest.raises(deploy.DeploymentError, match="timed out"):
        if entrypoint == "wait":
            deploy.wait_endpoint(*args, "1", timeout=1)
        elif entrypoint == "preflight":
            deploy.check_release_endpoint(*args, client.current, configuration(), timeout=1)
        else:
            deploy.ensure_endpoint(*args, "1", timeout=1)
    assert len(client.calls) == 1
    assert not endpoint_clock.sleeps
    assert all(op == "get-endpoint" for op, _ in client.calls)


def test_manifest_verification_keeps_zero_timeout_single_read(tmp_path, monkeypatch, endpoint_clock):
    client = FakeEndpointReadback()
    manifest = perform_deploy(tmp_path, client)
    client.calls.clear()
    original_get = client.get_agent_runtime_endpoint

    def slow_read(**kwargs):
        endpoint_clock.now += 2
        return original_get(**kwargs)

    monkeypatch.setattr(client, "get_agent_runtime_endpoint", slow_read)
    assert deploy.verify_manifest(client, manifest, "us-west-2")["status"] == "READY"
    assert len([op for op, _ in client.calls if op == "get-endpoint"]) == 1
    assert not endpoint_clock.sleeps


def test_update_preserves_every_unspecified_field_and_unset_env(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    before = copy.deepcopy(client.current)
    result = perform_deploy(tmp_path, client, overrides={"environmentVariables": {"NEW": "new"}},
                            unset_env=["KEEP_ME"], endpoint_name="release_2")
    for key in deploy.MUTABLE_FIELDS - {"environmentVariables"}:
        assert client.current.get(key) == before.get(key)
    assert client.current["environmentVariables"] == {"AGENTCORE_RUNTIME_PLATFORM": "V2", "NEW": "new"}
    assert result["agentRuntimeVersion"] == "3"


@pytest.mark.parametrize("require_service_s3_endpoint", [False, True])
def test_update_omits_unchanged_network_flag_and_verifies_service_readback(
        tmp_path, require_service_s3_endpoint):
    config = configuration()
    config["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = require_service_s3_endpoint
    client, invoke = FakeControl(config), FakeInvoke()
    runtime_id = client.current["agentRuntimeId"]

    manifest = perform_deploy(tmp_path, client, invoke, runtime_id=runtime_id,
                              overrides={"description": "Updated release"})

    updates = [args for op, args in client.calls if op == "update"]
    assert len(updates) == 1
    assert "requireServiceS3Endpoint" not in updates[0]["networkConfiguration"]["networkModeConfig"]
    assert updates[0]["description"] == "Updated release"
    expected = {**config, "description": "Updated release"}
    readback = client.get_agent_runtime(agentRuntimeId=runtime_id)
    assert readback["networkConfiguration"] == expected["networkConfiguration"]
    deploy.verify_configuration(readback, expected)
    assert manifest["configurationHash"] == deploy.configuration_hash(expected)
    assert manifest["agentRuntimeVersion"] == "2"
    assert manifest["warmup"]["status"] == "succeeded"
    assert len(invoke.calls) == 1
    deploy.verify_manifest(client, deploy.load_manifest(tmp_path / "runtime-v2.json"), "us-west-2")


def test_update_rejects_service_network_flag_drift_before_endpoint_or_warmup(tmp_path):
    class DriftingControl(FakeControl):
        def update_agent_runtime(self, **kwargs):
            updated = super().update_agent_runtime(**kwargs)
            self.current["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] = True
            return updated

    client, invoke = DriftingControl(), FakeInvoke()
    with pytest.raises(deploy.DeploymentError, match="networkConfiguration"):
        perform_deploy(tmp_path, client, invoke, runtime_id=client.current["agentRuntimeId"],
                       overrides={"description": "Updated release"})

    updates = [args for op, args in client.calls if op == "update"]
    assert len(updates) == 1
    assert "requireServiceS3Endpoint" not in updates[0]["networkConfiguration"]["networkModeConfig"]
    assert not any(op in {"create", "create-endpoint", "update-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert deploy.load_manifest(tmp_path / "runtime-v2.json")["warmup"]["status"] == "failed"


def test_warmup_failure_marks_manifest_failed_and_blocks_verification(tmp_path):
    client = FakeControl()
    with pytest.raises(WarmupError):
        perform_deploy(tmp_path, client, FakeInvoke({"response": "ok", "status": "error"}))
    manifest = deploy.load_manifest(tmp_path / "runtime-v2.json")
    assert manifest["warmup"]["status"] == "failed"
    with pytest.raises(deploy.DeploymentError, match="successful chat warmup"):
        deploy.verify_manifest(client, manifest, "us-west-2")


def test_explicit_skip_is_not_success(tmp_path):
    client = FakeControl()
    invoke = FakeInvoke()
    manifest = perform_deploy(tmp_path, client, invoke, skip_warmup=True)
    assert manifest["warmup"]["status"] == "skipped"
    assert not invoke.calls
    with pytest.raises(deploy.DeploymentError):
        deploy.verify_manifest(client, manifest, "us-west-2")


def test_external_v2_validation_helper_arguments_remain_supported(tmp_path):
    client = FakeControl()
    manifest = perform_deploy(tmp_path, client, skip_warmup=True)
    endpoint = deploy.verify_manifest(client, manifest, "us-west-2",
                                      require_warmup=False, expected_platform="V2")
    assert endpoint["liveVersion"] == manifest["agentRuntimeVersion"]
    invoke = FakeInvoke()
    warmup = run_warmup(invoke, ARN, manifest["endpointName"], expected_platform="V2",
                        runtime_version=manifest["agentRuntimeVersion"])
    assert warmup["agentRuntimeVersion"] == manifest["agentRuntimeVersion"]
    assert warmup["runtimePlatform"] == "V2"
    assert deploy.IMAGE_PATTERN.fullmatch(IMAGE)
    assert deploy.SDK_VERSIONS == {"boto3": "1.43.95", "botocore": "1.43.95"}
    assert "us-west-2" in deploy.V2_REGIONS
    deploy.check_sdk_versions()


@pytest.mark.parametrize("platform", ["V1", None, ""])
def test_verification_compatibility_argument_rejects_non_v2_before_readback(platform):
    client = FakeControl()
    with pytest.raises(deploy.DeploymentError, match="must be V2"):
        deploy.verify_manifest(client, {"platformVersion": "V2"}, "us-west-2",
                               expected_platform=platform, require_warmup=False)
    assert not client.calls


@pytest.mark.parametrize("platform", ["V1", None, ""])
def test_existing_non_v2_runtime_is_rejected_without_mutation(tmp_path, platform):
    client, invoke = FakeControl(), FakeInvoke()
    client.current["platformVersion"] = platform
    original = copy.deepcopy(client.current)
    with pytest.raises(deploy.DeploymentError, match="existing runtime platformVersion must be V2"):
        perform_deploy(tmp_path, client, invoke, runtime_id=client.current["agentRuntimeId"])
    assert [op for op, _ in client.calls] == ["get"]
    assert client.current == original
    assert not invoke.calls
    assert not (tmp_path / "runtime-v2.json").exists()


def test_readback_drift_blocks_manifest_verification(tmp_path):
    client = FakeControl()
    manifest = perform_deploy(tmp_path, client)
    client.current["lifecycleConfiguration"]["maxLifetime"] = 3600
    with pytest.raises(deploy.DeploymentError, match="configurationHash"):
        deploy.verify_manifest(client, manifest, "us-west-2")


def test_waiter_deadline_even_if_ready_version_is_stale():
    client = FakeControl()
    times = iter([0, 0, 1])
    with pytest.raises(deploy.DeploymentError, match="timed out"):
        deploy.wait_runtime(client, "openclaw_agent_v2-1234567890", "2", timeout=1, interval=0,
                            clock=lambda: next(times), sleep=lambda _: None)


def test_endpoint_version_conflict_fails_before_waiting():
    client = FakeControl()
    client.endpoint = {"name": "release_1", "status": "READY", "liveVersion": "1", "targetVersion": "2"}
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.wait_endpoint(client, "openclaw_agent_v2-1234567890", "release_1", "2", timeout=0)


def test_model_gate_rejects_unknown_update_field():
    operation = MODEL.operation_model("UpdateAgentRuntime")
    model = SimpleNamespace(operation_model=lambda name: SimpleNamespace(
        input_shape=SimpleNamespace(members={**operation.input_shape.members, "newOption": object()})))
    client = SimpleNamespace(meta=SimpleNamespace(service_model=model))
    with pytest.raises(deploy.DeploymentError, match="preservation contract"):
        deploy.check_service_model(client)


def test_resolve_image_uses_ecr_digest_and_rejects_region_mismatch():
    calls = []
    client = SimpleNamespace(describe_images=lambda **kw: calls.append(kw) or {
        "imageDetails": [{"imageDigest": "sha256:" + "a" * 64}],
    })
    resolved = deploy.resolve_image(client, IMAGE.split("@")[0] + ":v1", "us-west-2", "123456789012")
    assert resolved == (IMAGE, "sha256:" + "a" * 64)
    assert calls[0]["imageIds"] == [{"imageTag": "v1"}]
    with pytest.raises(deploy.DeploymentError, match="region"):
        deploy.resolve_image(client, IMAGE, "us-east-1", "123456789012")


@pytest.mark.parametrize("image", [
    IMAGE.replace("123456789012", "999999999999"),
    IMAGE.replace("openclaw-bridge", "my-openclaw"),
    IMAGE.replace("amazonaws.com/", "amazonaws.com.cn/"),
])
def test_image_without_execution_role_pull_scope_is_rejected_before_ecr(image):
    client = SimpleNamespace(describe_images=lambda **kwargs: pytest.fail("must not query ECR"))
    with pytest.raises(deploy.DeploymentError):
        deploy.resolve_image(client, image, "us-west-2", "123456789012")


@pytest.mark.parametrize("existing", ["absent", "same", "legacy", "other", "unreadable"])
def test_existing_callers_preserve_routes_or_block_cutover(existing):
    calls = []

    def read(**kwargs):
        calls.append(kwargs["FunctionName"])
        if existing == "absent":
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunctionConfiguration")
        if existing == "unreadable":
            return {"Environment": {"Error": {"ErrorCode": "AccessDeniedException"}}}
        arn = ARN if existing != "other" else ARN.replace("openclaw_agent_v2", "other_v2")
        variables = {"AGENTCORE_RUNTIME_ARN": arn}
        if existing != "legacy":
            variables["RUNTIME_TARGET"] = json.dumps({
                "platformVersion": "V2", "runtimeArn": arn, "endpointName": "release_1",
            })
        return {"Environment": {"Variables": variables}}

    client = SimpleNamespace(get_function_configuration=read)
    manifest = {"agentRuntimeArn": ARN, "endpointName": "release_2"}
    if existing in {"absent", "same"}:
        deploy.verify_existing_callers(client, manifest)
        assert calls == ["openclaw-router", "openclaw-cron-executor"]
    else:
        with pytest.raises(deploy.DeploymentError):
            deploy.verify_existing_callers(client, manifest)
        assert calls == ["openclaw-router"]


def test_existing_caller_lookup_error_does_not_allow_deployment():
    def deny(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetFunctionConfiguration")
    with pytest.raises(ClientError):
        deploy.verify_existing_callers(SimpleNamespace(get_function_configuration=deny),
                                       {"agentRuntimeArn": ARN})


def test_explicit_v1_deployment_is_rejected_before_any_call(tmp_path):
    client = FakeControl()
    with pytest.raises(deploy.DeploymentError, match="platform must be V2"):
        perform_deploy(tmp_path, client, platform="V1")
    assert not client.calls
    assert not (tmp_path / "runtime-v2.json").exists()


def test_endpoint_invalid_name_fails_before_mutation(tmp_path):
    client = FakeControl()
    with pytest.raises(deploy.DeploymentError, match="endpoint names"):
        perform_deploy(tmp_path, client, endpoint_name="arn:aws:bedrock-agentcore:example")
    assert not client.calls


def test_update_api_failure_invalidates_previous_warmup_manifest(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    def fail_update(**kwargs):
        raise ClientError({"Error": {"Code": "ValidationException", "Message": "sensitive-value"}}, "UpdateAgentRuntime")
    client.update_agent_runtime = fail_update
    with pytest.raises(ClientError):
        perform_deploy(tmp_path, client, overrides={"description": "new"}, endpoint_name="release_2")
    manifest = deploy.load_manifest(tmp_path / "runtime-v2.json")
    assert manifest["warmup"]["status"] == "failed"
    assert manifest["agentRuntimeId"] == "openclaw_agent_v2-1234567890"
    assert "sensitive-value" not in (tmp_path / "runtime-v2.json").read_text()


@pytest.mark.parametrize("field", ["platformVersion", "AGENTCORE_RUNTIME_PLATFORM"])
@pytest.mark.parametrize("source", ["current", "overrides"])
def test_v1_configuration_is_rejected_instead_of_converted(field, source):
    current, overrides = configuration(), {}
    target = current if source == "current" else overrides
    if field == "platformVersion":
        target[field] = "V1"
    else:
        target.setdefault("environmentVariables", {})[field] = "V1"
    original = copy.deepcopy((current, overrides))
    with pytest.raises(deploy.DeploymentError, match="must be V2"):
        deploy.build_configuration(FakeControl(), current, overrides, IMAGE)
    assert (current, overrides) == original


def test_default_endpoint_is_rejected_without_readback_or_mutation():
    client = FakeControl()
    with pytest.raises(deploy.DeploymentError, match="custom immutable endpoint"):
        deploy.ensure_endpoint(client, client.current["agentRuntimeId"], "DEFAULT", "1")
    assert not client.calls


def test_environment_exact_limit_is_accepted():
    client = FakeControl()
    current = configuration()
    current["environmentVariables"] = {"AGENTCORE_RUNTIME_PLATFORM": "V2"}
    remaining = deploy.V2_ENV_LIMIT - deploy.environment_size(current["environmentVariables"]) - len("FILLER")
    desired = deploy.build_configuration(client, current, {
        "environmentVariables": {"FILLER": "x" * remaining},
    }, IMAGE, "V2")
    assert deploy.environment_size(desired["environmentVariables"]) == 2500


def test_foundation_environment_keeps_all_optional_features():
    from runtime_config import foundation_configuration
    values = {
        "REGION": "us-west-2", "ACCOUNT": "123456789012",
        "EXECUTION_ROLE_ARN": "arn:aws:iam::123456789012:role/openclaw",
        "SECURITY_GROUP_ID": "sg-12345678", "PRIVATE_SUBNET_IDS": "subnet-12345678,subnet-12345679",
        "USER_FILES_BUCKET": "user-files", "COGNITO_USER_POOL_ID": "pool",
        "COGNITO_CLIENT_ID": "client", "CMK_ARN": "arn:aws:kms:us-west-2:123456789012:key/key",
        "SESSION_IDLE": "1800", "SESSION_MAX": "28800",
        "GUARDRAIL_ID": "guardrail", "GUARDRAIL_VERSION": "1", "BROWSER_IDENTIFIER": "browser",
        "AGENTCORE_GATEWAY_URL": "https://example.com/mcp", "SUBAGENT_MODEL_ID": "model",
        "DEFAULT_MODEL_ID": "global.anthropic.claude-opus-5-5",
        "FALLBACK_MODEL_ID": "global.anthropic.claude-sonnet-5-5",
    }
    desired = foundation_configuration(values)
    env = desired["environmentVariables"]
    assert env["BEDROCK_GUARDRAIL_ID"] == "guardrail"
    assert env["BROWSER_IDENTIFIER"] == "browser"
    assert env["AGENTCORE_GATEWAY_URL"] == "https://example.com/mcp"
    assert env["SUBAGENT_BEDROCK_MODEL_ID"] == "model"
    assert env["BEDROCK_MODEL_ID"] == "global.anthropic.claude-opus-5-5"
    assert env["BEDROCK_FALLBACK_MODEL_ID"] == "global.anthropic.claude-sonnet-5-5"
    assert desired["networkConfiguration"]["networkModeConfig"]["subnets"] == ["subnet-12345678", "subnet-12345679"]


@pytest.mark.parametrize("total_bytes", [2500, 2501, 2560, 2561, 4096])
def test_final_environment_boundary_reports_exact_bytes(total_bytes):
    client = FakeControl()
    current = configuration()
    current["environmentVariables"] = {"AGENTCORE_RUNTIME_PLATFORM": "V2"}
    remaining = total_bytes - deploy.environment_size(current["environmentVariables"]) - len("INHERITED")
    current["environmentVariables"]["INHERITED"] = "x" * remaining
    if total_bytes <= deploy.V2_ENV_LIMIT:
        desired = deploy.build_configuration(client, current, {}, IMAGE, "V2")
        assert deploy.environment_size(desired["environmentVariables"]) == total_bytes
    else:
        with pytest.raises(deploy.DeploymentError, match=f"bytes {total_bytes} exceeds 2500"):
            deploy.build_configuration(client, current, {}, IMAGE, "V2")
    assert not client.calls


@pytest.mark.parametrize("exists,race", [(True, False), (False, False), (False, True)])
def test_repository_creation_is_guarded_and_race_safe(exists, race):
    from runtime_image import ensure_repository
    repository = {"repositoryName": "openclaw-bridge", "repositoryUri": IMAGE.split("@")[0]}
    calls = []
    def describe(**kwargs):
        calls.append(("describe", kwargs))
        if not exists and len(calls) == 1:
            raise ClientError({"Error": {"Code": "RepositoryNotFoundException"}}, "DescribeRepositories")
        return {"repositories": [repository]}
    def create(**kwargs):
        calls.append(("create", kwargs))
        if race:
            raise ClientError({"Error": {"Code": "RepositoryAlreadyExistsException"}}, "CreateRepository")
        return {"repository": repository}
    client = SimpleNamespace(describe_repositories=describe, create_repository=create)
    assert ensure_repository(client, "openclaw-bridge") == repository["repositoryUri"]
    assert len([op for op, _ in calls if op == "create"]) == (0 if exists else 1)
    assert all(args.get("repositoryName", "openclaw-bridge") == "openclaw-bridge" for _, args in calls)


def test_repository_access_error_is_not_treated_as_missing():
    from runtime_image import ensure_repository
    def deny(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeRepositories")
    client = SimpleNamespace(describe_repositories=deny)
    with pytest.raises(ClientError):
        ensure_repository(client, "openclaw-bridge")


def test_repository_outside_pull_scope_is_not_created():
    from runtime_image import ensure_repository
    with pytest.raises(deploy.DeploymentError, match="execution-role prefix"):
        ensure_repository(SimpleNamespace(), "my-openclaw")


@pytest.mark.parametrize("option", ["--platform", "--new-runtime", "--active-runtime-id",
                                    "--source-runtime-id", "--source-endpoint-name"])
def test_cli_rejects_removed_options_before_aws(option, monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("must not initialize AWS"))
    with pytest.raises(SystemExit) as error:
        deploy.main(["deploy", "--region", "us-west-2", option, "V1"])
    assert error.value.code == 2


def test_warmup_rejects_plain_shim_and_v1_requests():
    with pytest.raises(WarmupError, match="gateway evidence"):
        run_warmup(FakeInvoke({"response": "ok"}), ARN, "release_1")
    invoke = FakeInvoke({"response": "ok"})
    with pytest.raises(WarmupError, match="only V2"):
        run_warmup(invoke, ARN, "release_1", expected_platform="V1")
    assert not invoke.calls


@pytest.mark.parametrize("field,value", [
    ("runtimePlatform", "V1"), ("phase", "BINDING"),
    ("execution", {"engine": "shim", "phase": "completed"}),
    ("execution", {"engine": "openclaw", "phase": "unknown_outcome"}),
    ("userId", "another-user"), ("actorId", "another:actor"),
    ("runtimeSessionId", "another-runtime-session"), ("sessionId", "another-session"),
])
def test_v2_warmup_rejects_false_completion_evidence(field, value):
    def body(payload):
        result = {
            "response": "ok", "runtimePlatform": "V2", "phase": "USER_READY",
            "execution": {"engine": "openclaw", "phase": "completed"},
            **{key: payload[key] for key in ("userId", "actorId", "sessionId", "runtimeSessionId")},
        }
        result[field] = value
        return result
    client = FakeInvoke(body)
    with pytest.raises(WarmupError):
        run_warmup(client, ARN, "release_1")
    assert len(client.calls) == 1
    assert client.stream.closed


def test_legacy_success_manifest_requires_new_v2_evidence(tmp_path):
    client = FakeControl()
    invoke = FakeInvoke()
    manifest = perform_deploy(tmp_path, client, invoke)
    manifest["warmup"] = {"mode": "chat", "status": "succeeded"}
    deploy.write_manifest(tmp_path / "runtime-v2.json", manifest)
    with pytest.raises(deploy.DeploymentError, match="V2 gateway"):
        deploy.verify_manifest(client, manifest, "us-west-2")
    perform_deploy(tmp_path, client, invoke)
    assert len(invoke.calls) == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_managed_feature_toggle_clears_only_explicit_fields(enabled):
    from runtime_config import foundation_configuration
    values = {
        "REGION": "us-west-2", "ACCOUNT": "123456789012", "EXECUTION_ROLE_ARN": configuration()["roleArn"],
        "SECURITY_GROUP_ID": "sg-12345678", "PRIVATE_SUBNET_IDS": "subnet-12345678",
        "USER_FILES_BUCKET": "bucket", "COGNITO_USER_POOL_ID": "pool", "COGNITO_CLIENT_ID": "client",
        "CMK_ARN": "arn:aws:kms:us-west-2:123456789012:key/key", "SESSION_IDLE": "1800", "SESSION_MAX": "28800",
    }
    managed = {
        "GUARDRAIL_ID": "BEDROCK_GUARDRAIL_ID", "GUARDRAIL_VERSION": "BEDROCK_GUARDRAIL_VERSION",
        "BROWSER_IDENTIFIER": "BROWSER_IDENTIFIER", "AGENTCORE_GATEWAY_URL": "AGENTCORE_GATEWAY_URL",
        "SUBAGENT_MODEL_ID": "SUBAGENT_BEDROCK_MODEL_ID",
        "FALLBACK_MODEL_ID": "BEDROCK_FALLBACK_MODEL_ID",
    }
    values.update({key: "enabled-value" if enabled else "" for key in managed})
    current = configuration()
    current["environmentVariables"].update({key: "inherited" for key in managed.values()})
    current["environmentVariables"].update(UNKNOWN_FEATURE="keep", BEDROCK_MODEL_ID="unspecified-model")
    desired = deploy.build_configuration(FakeControl(), current, foundation_configuration(values), IMAGE, "V2")
    environment = desired["environmentVariables"]
    for key in managed.values():
        if enabled:
            assert environment[key] == "enabled-value"
        else:
            assert key not in environment
    assert environment["UNKNOWN_FEATURE"] == "keep"
    assert environment["BEDROCK_MODEL_ID"] == "unspecified-model"


@pytest.mark.parametrize("platform", ["V1", None, ""])
def test_non_v2_manifest_never_becomes_a_new_v2_runtime(tmp_path, platform):
    client = FakeControl()
    path = tmp_path / "runtime-v2.json"
    path.write_text(json.dumps({"schemaVersion": 1, "platformVersion": platform,
                               "agentRuntimeId": client.current["agentRuntimeId"]}))
    original = path.read_bytes()
    with pytest.raises(deploy.DeploymentError, match="manifest platformVersion must be V2"):
        perform_deploy(tmp_path, client)
    assert not client.calls
    assert path.read_bytes() == original


def test_missing_existing_runtime_never_falls_back_to_create(tmp_path):
    client = FakeControl()
    def missing(**kwargs):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetAgentRuntime")
    client.get_agent_runtime = missing
    with pytest.raises(ClientError):
        perform_deploy(tmp_path, client, runtime_id="missing-1234567890")
    assert not any(op == "create" for op, _ in client.calls)


@pytest.mark.parametrize("skip_warmup", [False, True])
def test_jwt_is_preserved_but_rejected_before_runtime_mutation(tmp_path, skip_warmup):
    client = FakeControl(configuration(jwt=True))
    original = copy.deepcopy(client.current)
    with pytest.raises(deploy.DeploymentError, match="supports IAM warmup only"):
        perform_deploy(tmp_path, client, overrides=configuration(jwt=True), skip_warmup=skip_warmup)
    assert not client.calls
    assert client.current == original


def test_existing_jwt_runtime_is_not_downgraded_or_updated(tmp_path):
    client = FakeControl(configuration(jwt=True))
    original = copy.deepcopy(client.current)
    with pytest.raises(deploy.DeploymentError, match="supports IAM warmup only"):
        perform_deploy(tmp_path, client, runtime_id="openclaw_agent_v2-1234567890", overrides={})
    assert [op for op, _ in client.calls] == ["get"]
    assert client.current == original


def test_runtime_id_cannot_override_existing_manifest(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    original = (tmp_path / "runtime-v2.json").read_bytes()
    client.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="runtime ID conflicts"):
        perform_deploy(tmp_path, client, runtime_id="another-1234567890")
    assert not client.calls
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


@pytest.mark.parametrize("entrypoint", ["deploy", "verify"])
def test_cli_rejects_v1_manifest_before_aws_clients(tmp_path, monkeypatch, entrypoint):
    import boto3
    path = tmp_path / "runtime-v2.json"
    path.write_text(json.dumps({"schemaVersion": 1, "platformVersion": "V1"}))
    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("must not initialize AWS"))
    with pytest.raises(deploy.DeploymentError, match="manifest platformVersion must be V2"):
        deploy.main([entrypoint, "--region", "us-west-2", "--manifest", str(path)])


def test_v2_existing_endpoint_blocks_update_before_mutation_even_without_enrollment_flag(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    original = (tmp_path / "runtime-v2.json").read_bytes()
    client.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="new --endpoint-name"):
        perform_deploy(tmp_path, client, overrides={"description": "new release"})
    assert not any(op in {"update", "create", "update-endpoint"} for op, _ in client.calls)
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


def test_new_release_endpoint_keeps_enrolled_version_and_old_manifest_verifiable(tmp_path):
    client = FakeControl()
    old_manifest = perform_deploy(tmp_path, client)
    old_endpoint = copy.deepcopy(client.endpoints["release_1"])
    client.calls.clear()
    new_manifest = perform_deploy(tmp_path, client, overrides={"description": "new release"},
                                  endpoint_name="release_2")
    assert client.endpoints["release_1"] == old_endpoint
    assert new_manifest["agentRuntimeVersion"] != old_manifest["agentRuntimeVersion"]
    assert new_manifest["endpointPolicy"] == "IMMUTABLE"
    assert not any(op == "update-endpoint" for op, _ in client.calls)
    deploy.verify_manifest(client, old_manifest, "us-west-2", require_enrollment_ready=True)
    deploy.verify_manifest(client, new_manifest, "us-west-2", require_enrollment_ready=True)
    reads = [args for op, args in client.calls if op == "get" and "agentRuntimeVersion" in args]
    assert any(args["agentRuntimeVersion"] == old_manifest["agentRuntimeVersion"] for args in reads)


def test_immutable_endpoint_guard_cannot_be_bypassed_with_new_manifest(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    client.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        perform_deploy(tmp_path, client, manifest_path=tmp_path / "another.json",
                       runtime_id="openclaw_agent_v2-1234567890", overrides={"description": "new release"})
    assert not any(op in {"update", "update-endpoint"} for op, _ in client.calls)


def test_immutable_endpoint_never_repoints_even_if_it_changes_after_preflight():
    client = FakeControl()
    client.endpoint = {"name": "release_2", "status": "READY", "liveVersion": "1", "targetVersion": "1"}
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.ensure_endpoint(client, "openclaw_agent_v2-1234567890", "release_2", "2")
    assert not any(op == "update-endpoint" for op, _ in client.calls)


@pytest.mark.parametrize("policy,endpoint,skip", [
    (None, "release_1", False), ("IMMUTABLE", "DEFAULT", False), ("IMMUTABLE", "release_1", True),
])
def test_enrollment_requires_immutable_custom_endpoint_and_warmup(tmp_path, policy, endpoint, skip):
    client = FakeControl()
    manifest = perform_deploy(tmp_path, client, skip_warmup=skip)
    manifest["endpointPolicy"] = policy
    manifest["endpointName"] = endpoint
    client.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="enrollment requires|immutable custom V2 endpoint"):
        deploy.verify_manifest(client, manifest, "us-west-2", require_enrollment_ready=True,
                               require_warmup=not skip)
    assert not client.calls


def test_v2_rejects_default_endpoint_before_runtime_mutation(tmp_path):
    client = FakeControl()
    with pytest.raises(deploy.DeploymentError, match="custom immutable endpoint"):
        perform_deploy(tmp_path, client, endpoint_name="DEFAULT")
    assert not client.calls


@pytest.mark.parametrize("key,value", [
    ("contractVersion", True), ("contractVersion", None),
    ("execution", {"engine": "lightweight", "phase": "completed"}),
    ("actorId", "another-actor"), ("phase", "INITIALIZING"),
])
def test_enrollment_rejects_invalid_shared_warmup_contract_before_aws_readback(tmp_path, key, value):
    client = FakeControl()
    manifest = perform_deploy(tmp_path, client)
    manifest["warmup"][key] = value
    client.calls.clear()
    with pytest.raises(deploy.DeploymentError, match="enrollment requires|immutable custom V2 endpoint"):
        deploy.verify_manifest(client, manifest, "us-west-2", require_enrollment_ready=True)
    assert not client.calls


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
def test_omitted_endpoint_resumes_manifest_release_without_mutation_or_warmup(tmp_path, monkeypatch, entrypoint):
    import boto3
    client, invoke = FakeControl(), FakeInvoke()
    initial = configuration()
    initial.pop("metadataConfiguration")
    first = perform_deploy(tmp_path, client, invoke, overrides=initial)
    latest = perform_deploy(tmp_path, client, invoke, overrides={"description": "release 2"},
                            endpoint_name="release_2")
    assert first["agentRuntimeVersion"] == "1"
    assert latest["agentRuntimeVersion"] == "2"
    client.calls.clear()
    invoke.calls.clear()
    if entrypoint == "cli":
        clients = {
            "bedrock-agentcore-control": client, "bedrock-agentcore": invoke,
            "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012"}),
            "ecr": SimpleNamespace(describe_images=lambda **kwargs: {
                "imageDetails": [{"imageDigest": "sha256:" + "a" * 64}],
            }),
        }
        monkeypatch.setattr(boto3, "client", lambda service, **kwargs: clients[service])
        deploy.main(["deploy", "--region", "us-west-2", "--manifest", str(tmp_path / "runtime-v2.json"),
                     "--image-uri", IMAGE])
        resumed = deploy.load_manifest(tmp_path / "runtime-v2.json")
    else:
        resumed = perform_deploy(tmp_path, client, invoke, overrides={}, endpoint_name=None)
    assert resumed["endpointName"] == "release_2"
    assert resumed["agentRuntimeVersion"] == "2"
    assert resumed["warmup"] == latest["warmup"]
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    client.calls.clear()
    original = (tmp_path / "runtime-v2.json").read_bytes()
    with pytest.raises(deploy.DeploymentError, match="new --endpoint-name"):
        perform_deploy(tmp_path, client, invoke, overrides={"description": "release 3"}, endpoint_name=None)
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert not invoke.calls
    assert (tmp_path / "runtime-v2.json").read_bytes() == original


def test_explicit_endpoint_overrides_reused_manifest_endpoint(tmp_path):
    client = FakeControl()
    perform_deploy(tmp_path, client)
    manifest = perform_deploy(tmp_path, client, overrides={}, endpoint_name="release_2")
    assert manifest["endpointName"] == "release_2"
    assert {"release_1", "release_2"} <= client.endpoints.keys()


@pytest.mark.parametrize("response_fields,reason", [
    ({"status": "error"}, "warmup did not complete successfully"),
    ({}, "warmup did not return the requested acknowledgement"),
    ({"response": "ok", "runtimePlatform": "V1"},
     "V2 warmup lacks USER_READY and completed gateway evidence"),
])
def test_warmup_validation_failure_saves_safe_reason_and_pending_identity_on_retry(
        tmp_path, fresh_create_configuration, response_fields, reason):
    client = FakeControl()
    initial = perform_deploy(tmp_path, client, overrides=fresh_create_configuration,
                             endpoint_name="release_1", skip_warmup=True)
    client.calls.clear()
    path = tmp_path / "runtime-v2.json"
    snapshots = []
    secret = "SENTINEL-warmup-response-secret"
    body = {"response": secret, "diagnostic": secret, **response_fields}

    class ObservingInvoke(FakeInvoke):
        def invoke_agent_runtime(self, **kwargs):
            snapshots.append(json.loads(path.read_text())["warmup"])
            return super().invoke_agent_runtime(**kwargs)

    invoke = ObservingInvoke(body)
    with pytest.raises(WarmupError) as error:
        perform_deploy(tmp_path, client, invoke, overrides={}, endpoint_name=None)

    assert str(error.value) == reason
    assert len(snapshots) == len(invoke.calls) == 1
    pending = snapshots[0]
    request = invoke.calls[0]
    payload = json.loads(request["payload"])
    assert pending["status"] == "pending"
    assert pending["agentRuntimeArn"] == request["agentRuntimeArn"] == initial["agentRuntimeArn"]
    assert pending["agentRuntimeVersion"] == initial["agentRuntimeVersion"] == "1"
    assert pending["endpointName"] == request["qualifier"] == "release_1"
    for key in ("userId", "actorId", "sessionId", "runtimeSessionId"):
        assert pending[key] == payload[key]
    assert pending["runtimeSessionId"] == request["runtimeSessionId"]
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)
    assert invoke.stream.closed

    saved = deploy.load_manifest(path)
    assert saved["warmup"] == {
        **pending, "status": "failed", "errorType": "WarmupError", "failureReason": reason,
    }
    assert secret not in path.read_text()
    assert json.dumps(body) not in path.read_text()


@pytest.mark.parametrize("failure", ["invoke", "read_timeout", "read_error"])
def test_warmup_transport_failure_retains_preinvoke_identity(tmp_path, failure):
    client = FakeControl()
    snapshots = []
    path = tmp_path / "runtime-v2.json"
    error_type = OSError if failure == "read_error" else ReadTimeoutError

    def fail():
        if error_type is OSError:
            raise OSError("response read failed; do not persist this message")
        raise ReadTimeoutError(endpoint_url="https://offline.invalid")

    class BrokenStream(io.BytesIO):
        def read(self, *args):
            fail()

    class FailingInvoke(FakeInvoke):
        def invoke_agent_runtime(self, **kwargs):
            snapshots.append(json.loads(path.read_text()))
            result = super().invoke_agent_runtime(**kwargs)
            if failure == "invoke":
                fail()
            self.stream = BrokenStream()
            result["response"] = self.stream
            return result

    invoke = FailingInvoke()
    with pytest.raises(error_type):
        perform_deploy(tmp_path, client, invoke)
    assert len(invoke.calls) == 1
    request = invoke.calls[0]
    payload = json.loads(request["payload"])
    pending = snapshots[0]["warmup"]
    assert pending["status"] == "pending"
    assert pending["runtimeSessionId"] == request["runtimeSessionId"]
    assert pending["actorId"] == payload["actorId"]
    assert pending["userId"] == payload["userId"]
    assert pending["sessionId"] == payload["sessionId"]
    assert pending["agentRuntimeArn"] == request["agentRuntimeArn"]
    assert pending["agentRuntimeVersion"] == snapshots[0]["agentRuntimeVersion"]
    assert pending["endpointName"] == request["qualifier"]
    assert datetime.fromisoformat(pending["startedAt"]).utcoffset().total_seconds() == 0
    saved = deploy.load_manifest(path)
    assert saved["warmup"]["status"] == "unknown"
    assert saved["warmup"]["errorType"] == error_type.__name__
    assert "failureReason" not in saved["warmup"]
    for key in pending.keys() - {"status"}:
        assert saved["warmup"][key] == pending[key]
    assert "message" not in saved["warmup"]
    assert "payload" not in saved["warmup"]
    assert "do not persist" not in path.read_text()
    assert "offline.invalid" not in path.read_text()
    if failure != "invoke":
        assert invoke.stream.closed
    with pytest.raises(deploy.DeploymentError, match="successful chat warmup"):
        deploy.verify_manifest(client, saved, "us-west-2")
    with pytest.raises(ValueError):
        deploy.validate_v2_enrollment(saved)


@pytest.mark.parametrize("failure,reported", [
    ("warmup", "warmup did not complete successfully"),
    ("transport", "ReadTimeoutError"),
    ("aws", "ClientError"),
])
def test_cli_reports_safe_warmup_reason_but_hides_transport_and_aws_details(
        tmp_path, fresh_create_configuration, monkeypatch, capsys, failure, reported):
    import boto3
    import runpy

    secret = "SENTINEL-cli-response-secret"
    invoke = FakeInvoke({"response": secret, "status": "error"})
    if failure != "warmup":
        def fail_invoke(**kwargs):
            if failure == "transport":
                raise ReadTimeoutError(endpoint_url=f"https://offline.invalid/{secret}")
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": secret}},
                              "InvokeAgentRuntime")
        monkeypatch.setattr(invoke, "invoke_agent_runtime", fail_invoke)
    clients = {
        "bedrock-agentcore-control": FakeControl(), "bedrock-agentcore": invoke,
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012"}),
        "ecr": SimpleNamespace(describe_images=lambda **kwargs: {
            "imageDetails": [{"imageDigest": "sha256:" + "a" * 64}],
        }),
    }
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: clients[service])
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(fresh_create_configuration))
    monkeypatch.setattr(sys, "argv", [
        str(deploy.__file__), "deploy", "--region", "us-west-2",
        "--manifest", str(tmp_path / "runtime-v2.json"), "--config", str(config_path),
        "--image-uri", IMAGE,
    ])

    with pytest.raises(SystemExit) as error:
        runpy.run_path(deploy.__file__, run_name="__main__")

    assert error.value.code == 1
    captured = capsys.readouterr()
    assert f"ERROR: {reported}\n" in captured.err
    assert secret not in captured.err
    assert secret not in captured.out


def test_warmup_pending_persist_failure_prevents_invoke(tmp_path, monkeypatch):
    client, invoke = FakeControl(), FakeInvoke()
    original_write = deploy.write_manifest

    def fail_pending(path, manifest):
        if manifest["warmup"].get("status") == "pending" and manifest["warmup"].get("runtimeSessionId"):
            raise OSError("pending manifest cannot be persisted")
        original_write(path, manifest)

    monkeypatch.setattr(deploy, "write_manifest", fail_pending)
    with pytest.raises(OSError, match="pending manifest"):
        perform_deploy(tmp_path, client, invoke)
    assert not invoke.calls
    assert deploy.load_manifest(tmp_path / "runtime-v2.json")["warmup"]["status"] == "failed"


def test_first_deployment_uses_v2_release_endpoint_default(tmp_path):
    client = FakeControl()
    initial = configuration()
    initial.pop("metadataConfiguration")
    manifest = perform_deploy(tmp_path, client, endpoint_name=None, overrides=initial)
    assert manifest["endpointName"] == "release_1"
    assert manifest["platformVersion"] == "V2"


def test_success_retains_pending_identity_and_accepts_valid_success_cache(tmp_path):
    client, pending = FakeControl(), []
    path = tmp_path / "runtime-v2.json"

    class ObservingInvoke(FakeInvoke):
        def invoke_agent_runtime(self, **kwargs):
            pending.append(json.loads(path.read_text())["warmup"])
            return super().invoke_agent_runtime(**kwargs)

    invoke = ObservingInvoke()
    manifest = perform_deploy(tmp_path, client, invoke)
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert manifest["warmup"]["status"] == "succeeded"
    for key in pending[0].keys() - {"status"}:
        assert manifest["warmup"][key] == pending[0][key]
    for key in ("agentRuntimeArn", "agentRuntimeVersion", "endpointName", "startedAt"):
        del manifest["warmup"][key]
    deploy.write_manifest(path, manifest)
    client.calls.clear()
    resumed = perform_deploy(tmp_path, client, invoke, endpoint_name=None, overrides={})
    assert resumed["warmup"] == manifest["warmup"]
    assert len(invoke.calls) == 1
    assert all(op in {"get", "get-endpoint"} for op, _ in client.calls)


def test_warmup_callback_retains_identity_after_response_validation_failure():
    attempts = []
    invoke = FakeInvoke({"response": "ok", "status": "error"})
    with pytest.raises(WarmupError):
        run_warmup(invoke, ARN, "release_2", runtime_version="2", before_invoke=attempts.append)
    assert len(attempts) == len(invoke.calls) == 1
    assert attempts[0]["runtimeSessionId"] == invoke.calls[0]["runtimeSessionId"]
    assert attempts[0]["endpointName"] == "release_2"
    assert attempts[0]["agentRuntimeVersion"] == "2"
