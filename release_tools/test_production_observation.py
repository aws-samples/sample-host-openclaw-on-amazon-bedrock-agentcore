from __future__ import annotations

from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from release_tools.ecr import (
    EcrEvidenceError,
    EcrImageAbsent,
    EcrRepositoryAbsent,
)
from release_tools.agentcore import (
    AgentCoreEndpointAbsent,
    AgentCoreEvidenceError,
    AgentCoreRuntimeAbsent,
)
from release_tools.contracts import (
    ProductionObservationConfigV1,
    RuntimeContextV3,
    RuntimeImageEvidence,
    StagingTransactionV1,
    canonical_json_bytes,
)
from release_tools.production_observation import (
    CONSUMER_STACKS,
    FOUNDATION_STACKS,
    CloudFormationEvidenceAdapter,
    HttpsArtifactBlobReader,
    ProductionEvidenceComposer,
    ProductionObservationError,
    compose_production_evidence,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
RUNTIME_ID = "Runtime-ABCDEFGHIJ"
VERSION = "7"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/openclaw-agentcore-execution-role-eu-west-1"
BUILDER_INPUT = "sha256:" + "f" * 64
IMAGE_URI = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"personal-operator/bridge@{DIGEST}"
)
SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
SECURITY_GROUP_IDS = ("sg-00000000000000001",)
RUNTIME_ENVIRONMENT = {
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "CAPABILITY_GATEWAY_FUNCTION_ARN": (
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
        "personal-operator-capability-gateway"
    ),
    "DISABLE_ADOT_OBSERVABILITY": "true",
    "S3_USER_FILES_BUCKET": "personal-operator-user-files-123456789012",
    "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": "workspace-credential-broker",
    "WORKSPACE_SYNC_INTERVAL_MS": "300000",
}
ROLLBACK = f"rollback:v1:{ACCOUNT}:{REGION}:{COMMIT}:sha256:" + "9" * 64


def _stack_template(name: str) -> dict[str, object]:
    return {"Resources": {name: {}}}


def _stack_digest(name: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {"parameters": [], "template": _stack_template(name)}
        )
    ).hexdigest()


def _stack_request(name: str) -> dict[str, object]:
    return {
        "capabilities": ["CAPABILITY_IAM"],
        "deploymentConfig": {},
        "description": "",
        "disableRollback": False,
        "enableTerminationProtection": False,
        "notificationArns": [],
        "retainExceptOnCreate": False,
        "roleArn": "",
        "rollbackConfiguration": {},
        "stackName": name,
        "tags": [],
        "timeoutInMinutes": 0,
    }


def _stack_request_digest(name: str) -> str:
    return hashlib.sha256(canonical_json_bytes(_stack_request(name))).hexdigest()


def _change_set_content(name: str) -> dict[str, object]:
    return {
        "capabilities": ["CAPABILITY_IAM"],
        "changeSetName": f"release-{COMMIT}",
        "changeSetType": "UPDATE",
        "changes": [],
        "deploymentConfig": {},
        "deploymentMode": "",
        "description": "",
        "importExistingResources": False,
        "includeNestedStacks": False,
        "notificationArns": [],
        "onStackFailure": "",
        "parameters": [],
        "rollbackConfiguration": {},
        "stackName": name,
        "tags": [],
        "templateParameterDigest": _stack_digest(name),
    }


def _change_set_digest(name: str) -> str:
    return hashlib.sha256(canonical_json_bytes(_change_set_content(name))).hexdigest()


def _config() -> ProductionObservationConfigV1:
    return ProductionObservationConfigV1.from_mapping(
        {
            "schema": ProductionObservationConfigV1.SCHEMA,
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "buildContext": "bridge",
            "builderId": (
                "https://github.com/example/personal-operator/"
                ".github/workflows/release.yml"
            ),
            "builderInputs": [BUILDER_INPUT],
            "runtimeSubnetIds": list(SUBNET_IDS),
            "runtimeSecurityGroupIds": list(SECURITY_GROUP_IDS),
            "runtimeEnvironmentVariables": dict(RUNTIME_ENVIRONMENT),
            "runtimeIdleSessionTimeout": 1800,
            "runtimeMaxLifetime": 28800,
            "foundationStackTemplateParameterDigests": {
                name: _stack_digest(name)
                for name in FOUNDATION_STACKS
            },
            "runtimeStackTemplateParameterDigest": _stack_digest(
                "OpenClawAgentCore"
            ),
            "consumerStackTemplateParameterDigests": {
                name: _stack_digest(name)
                for name in CONSUMER_STACKS
            },
            "consumerChangeSetContentDigests": {
                name: _change_set_digest(name)
                for name in CONSUMER_STACKS
            },
            "foundationStackRequestDigests": {
                name: _stack_request_digest(name)
                for name in FOUNDATION_STACKS
            },
            "runtimeStackRequestDigest": _stack_request_digest(
                "OpenClawAgentCore"
            ),
            "consumerStackRequestDigests": {
                name: _stack_request_digest(name)
                for name in CONSUMER_STACKS
            },
            "evidenceRuntimeSha256": "9" * 64,
        }
    )


def _transaction(phase: str) -> StagingTransactionV1:
    ordered = (
        "foundation",
        "image",
        "runtime",
        "endpoint",
        "context",
        "consumer-changesets",
        "consumers",
        "verify",
    )
    states = (
        "PREFLIGHTED",
        "FOUNDATION_READY",
        "IMAGE_PUBLISHED",
        "RUNTIME_READY",
        "ENDPOINT_READY",
        "CONTEXT_WRITTEN",
        "CONSUMER_CHANGESETS_READY",
        "CONSUMERS_APPLIED",
        "VERIFIED",
    )
    if phase == "rollback":
        stable = "VERIFIED"
        uncertain = "ROLLBACK"
        index = len(ordered)
    else:
        index = ordered.index(phase)
        stable = states[index]
        uncertain = states[index + 1]
    return StagingTransactionV1.from_mapping(
        {
            "schema": StagingTransactionV1.SCHEMA,
            "transactionId": f"release_{COMMIT}",
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "state": "UNCERTAIN",
            "lastStableState": stable,
            "revision": index + 2,
            "runtimeImageDigest": DIGEST if index >= 2 else "",
            "runtimeId": RUNTIME_ID if index >= 3 else "",
            "runtimeVersion": VERSION if index >= 3 else "",
            "runtimeEndpointName": f"release_{COMMIT}",
            "runtimeContextSha256": (
                hashlib.sha256(_context().to_bytes()).hexdigest()
                if index >= 5
                else ""
            ),
            "consumerChangesetsSha256": "1" * 64 if index >= 6 else "",
            "consumerApplicationSha256": "2" * 64 if index >= 7 else "",
            "verificationSha256": "3" * 64 if index >= 8 else "",
            "rollbackReference": ROLLBACK,
            "uncertainPhase": uncertain,
            "uncertainOperationSha256": "sha256:" + "8" * 64,
        }
    )


def _runtime_configuration() -> dict[str, object]:
    return {
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": IMAGE_URI}
        },
        "authorizerConfiguration": {},
        "environmentVariables": dict(RUNTIME_ENVIRONMENT),
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 28800,
        },
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": list(SECURITY_GROUP_IDS),
                "subnets": list(SUBNET_IDS),
            },
        },
        "metadataConfiguration": {"requireMMDSV2": True},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "requestHeaderConfiguration": {},
    }


def _runtime_configuration_sha256() -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "executionRoleArn": ROLE_ARN,
                "runtimeConfiguration": _runtime_configuration(),
            }
        )
    ).hexdigest()


def _image() -> RuntimeImageEvidence:
    return RuntimeImageEvidence.from_mapping(
        {
            "schema": RuntimeImageEvidence.SCHEMA,
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "repositoryName": "personal-operator/bridge",
            "commitTag": f"commit-{COMMIT}",
            "imageDigest": DIGEST,
            "imageUri": IMAGE_URI,
            "imageSizeBytes": 123,
            "scanStatus": "COMPLETE",
            "criticalFindings": 0,
            "highFindings": 0,
            "sbomSha256": "d" * 64,
            "provenanceSha256": "e" * 64,
            "signingProfileArn": (
                f"arn:aws:signer:{REGION}:{ACCOUNT}:/"
                "signing-profiles/personal_operator_bridge"
            ),
            "signatureStatus": "SIGNED",
        }
    )


def _context() -> RuntimeContextV3:
    return RuntimeContextV3.from_mapping(
        {
            "schema": RuntimeContextV3.SCHEMA,
            "sourceCommit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtimeId": RUNTIME_ID,
            "runtimeEndpointId": "ReleaseEndpoint-ABCDEFGHIJ",
            "runtimeEndpointName": f"release_{COMMIT}",
            "runtimeArn": (
                f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
                f"agent/12345678-1234-1234-1234-123456789abc:{VERSION}"
            ),
            "runtimeVersion": VERSION,
            "runtimeImageUri": IMAGE_URI,
            "executionRoleArn": ROLE_ARN,
            "runtimeConfiguration": _runtime_configuration(),
            "runtimeConfigurationSha256": _runtime_configuration_sha256(),
        }
    )


class FakeEcrAdapter:
    def __init__(self, result: RuntimeImageEvidence) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def collect(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeAgentCoreAdapter:
    def __init__(self, result: RuntimeContextV3) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def collect_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def collect_runtime_identity(self, **kwargs):
        self.calls.append(kwargs)
        return (self.result.runtime_id, self.result.runtime_version)

    def observe_retained_disposition(self, **kwargs):
        self.calls.append(kwargs)
        return (
            "PRESENT",
            hashlib.sha256(self.result.to_bytes()).hexdigest(),
        )

    def assert_runtime_name_absent(self, **kwargs):
        self.calls.append(kwargs)


class FakeDeploymentAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.foundation: bool | None = True
        self.runtime: tuple[str, str] | None = (RUNTIME_ID, VERSION)
        self.changesets: str | None = "4" * 64
        self.consumers: str | None = "5" * 64
        self.verification: str | None = "6" * 64
        self.rollback: bool | None = True

    def observe_foundation(self, transaction):
        self.calls.append(("foundation", transaction.transaction_id))
        return self.foundation

    def observe_runtime_identity(self, transaction):
        self.calls.append(("runtime", transaction.transaction_id))
        return self.runtime

    def observe_consumer_changesets(self, transaction):
        self.calls.append(("consumer-changesets", transaction.transaction_id))
        return self.changesets

    def observe_consumers(self, transaction):
        self.calls.append(("consumers", transaction.transaction_id))
        return self.consumers

    def observe_verification(self, transaction, *, image, context):
        self.calls.append(
            (
                "verify",
                (transaction.transaction_id, image.image_digest, context.runtime_id),
            )
        )
        return self.verification

    def observe_rollback(self, transaction):
        self.calls.append(("rollback", transaction.transaction_id))
        return self.rollback


def _composer(
    ecr: FakeEcrAdapter,
    agentcore: FakeAgentCoreAdapter,
    deployment: FakeDeploymentAdapter | None = None,
) -> ProductionEvidenceComposer:
    return ProductionEvidenceComposer(
        ecr=ecr,
        agentcore=agentcore,
        deployment=deployment or FakeDeploymentAdapter(),
        config=_config(),
    )


def test_construction_is_credential_lazy_and_image_calls_exact_ecr_adapter() -> None:
    ecr = FakeEcrAdapter(_image())
    agentcore = FakeAgentCoreAdapter(_context())

    composer = _composer(ecr, agentcore)

    assert ecr.calls == []
    assert agentcore.calls == []
    evidence = composer.image_evidence()
    assert evidence == {"runtime_image_evidence": _image().to_mapping()}
    assert ecr.calls == [
        {
            "source_commit": COMMIT,
            "source_tree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "build_context": "bridge",
            "builder_id": (
                "https://github.com/example/personal-operator/"
                ".github/workflows/release.yml"
            ),
            "builder_inputs": (BUILDER_INPUT,),
        }
    ]
    assert agentcore.calls == []


def test_production_factory_wires_exact_adapters_without_touching_clients() -> None:
    class PoisonClient:
        def __getattr__(self, name):
            raise AssertionError(f"client accessed during construction: {name}")

    class PoisonBlobReader:
        def read(self, url: str, *, maximum_bytes: int) -> bytes:
            raise AssertionError("blob reader accessed during construction")

    composer = compose_production_evidence(
        ecr_client=PoisonClient(),
        artifact_blob_reader=PoisonBlobReader(),
        agentcore_client=PoisonClient(),
        cloudformation_client=PoisonClient(),
        config=_config(),
    )

    assert isinstance(composer, ProductionEvidenceComposer)


def test_endpoint_and_context_observations_bind_exact_live_bytes() -> None:
    ecr = FakeEcrAdapter(_image())
    agentcore = FakeAgentCoreAdapter(_context())
    composer = _composer(ecr, agentcore)

    endpoint = composer.endpoint_evidence(
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_digest=DIGEST,
    )
    context = composer.context_evidence(
        runtime_id=RUNTIME_ID,
        runtime_version=VERSION,
        runtime_image_digest=DIGEST,
    )

    assert endpoint == {"runtime_context": _context().to_mapping()}
    assert context == {
        "runtime_context": _context().to_mapping(),
        "runtime_context_sha256": hashlib.sha256(_context().to_bytes()).hexdigest(),
    }
    assert agentcore.calls == [
        {
            "source_commit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtime_id": RUNTIME_ID,
            "runtime_version": VERSION,
            "runtime_image_uri": IMAGE_URI,
            "expected_subnet_ids": SUBNET_IDS,
            "expected_security_group_ids": SECURITY_GROUP_IDS,
            "expected_environment_variables": RUNTIME_ENVIRONMENT,
            "expected_idle_runtime_session_timeout": 1800,
            "expected_max_lifetime": 28800,
        },
        {
            "source_commit": COMMIT,
            "account": ACCOUNT,
            "region": REGION,
            "runtime_id": RUNTIME_ID,
            "runtime_version": VERSION,
            "runtime_image_uri": IMAGE_URI,
            "expected_subnet_ids": SUBNET_IDS,
            "expected_security_group_ids": SECURITY_GROUP_IDS,
            "expected_environment_variables": RUNTIME_ENVIRONMENT,
            "expected_idle_runtime_session_timeout": 1800,
            "expected_max_lifetime": 28800,
        },
    ]


@pytest.mark.parametrize(
    ("adapter", "replacement"),
    [
        ("ecr", replace(_image(), source_tree="0" * 40)),
        ("agentcore", replace(_context(), runtime_version="8")),
    ],
)
def test_composer_rejects_injected_adapter_identity_drift(adapter, replacement) -> None:
    ecr = FakeEcrAdapter(replacement if adapter == "ecr" else _image())
    agentcore = FakeAgentCoreAdapter(
        replacement if adapter == "agentcore" else _context()
    )
    composer = _composer(ecr, agentcore)

    with pytest.raises(ProductionObservationError, match="identity"):
        if adapter == "ecr":
            composer.image_evidence()
        else:
            composer.endpoint_evidence(
                runtime_id=RUNTIME_ID,
                runtime_version=VERSION,
                runtime_image_digest=DIGEST,
            )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("foundation", (True, {})),
        ("image", (True, {"runtime_image_digest": DIGEST})),
        (
            "runtime",
            (
                True,
                {"runtime_id": RUNTIME_ID, "runtime_version": VERSION},
            ),
        ),
        ("endpoint", (True, {})),
        (
            "context",
            (
                True,
                {
                    "runtime_context_sha256": hashlib.sha256(
                        _context().to_bytes()
                    ).hexdigest()
                },
            ),
        ),
        (
            "consumer-changesets",
            (True, {"consumer_changesets_sha256": "4" * 64}),
        ),
        (
            "consumers",
            (True, {"consumer_application_sha256": "5" * 64}),
        ),
        ("verify", (True, {"verification_sha256": "6" * 64})),
        ("rollback", (True, {})),
    ],
)
def test_every_phase_uses_the_in_package_live_authority(phase, expected) -> None:
    composer = _composer(FakeEcrAdapter(_image()), FakeAgentCoreAdapter(_context()))

    assert composer.observe_phase(phase, _transaction(phase)) == expected


def test_authoritative_absence_is_distinct_from_ambiguous_live_evidence() -> None:
    deployment = FakeDeploymentAdapter()
    deployment.foundation = None
    composer = _composer(
        FakeEcrAdapter(_image()),
        FakeAgentCoreAdapter(_context()),
        deployment,
    )

    assert composer.observe_phase("foundation", _transaction("foundation")) == (
        False,
        {},
    )

    class AmbiguousDeployment(FakeDeploymentAdapter):
        def observe_foundation(self, transaction):
            raise ProductionObservationError("live state is ambiguous")

    ambiguous = _composer(
        FakeEcrAdapter(_image()),
        FakeAgentCoreAdapter(_context()),
        AmbiguousDeployment(),
    )
    with pytest.raises(ProductionObservationError, match="ambiguous"):
        ambiguous.observe_phase("foundation", _transaction("foundation"))


def test_image_absence_is_authoritative_but_other_ecr_errors_fail_closed() -> None:
    class AbsentEcr(FakeEcrAdapter):
        def collect(self, **kwargs):
            raise EcrImageAbsent("image does not exist")

    composer = _composer(AbsentEcr(_image()), FakeAgentCoreAdapter(_context()))

    assert composer.observe_phase("image", _transaction("image")) == (False, {})

    class AmbiguousEcr(FakeEcrAdapter):
        def collect(self, **kwargs):
            raise EcrEvidenceError("image evidence is ambiguous")

    ambiguous = _composer(
        AmbiguousEcr(_image()),
        FakeAgentCoreAdapter(_context()),
    )
    with pytest.raises(ProductionObservationError, match="live evidence"):
        ambiguous.observe_phase("image", _transaction("image"))


def test_absence_requires_every_last_stable_prerequisite_to_remain_exact() -> None:
    class MissingImage(FakeEcrAdapter):
        def collect(self, **kwargs):
            raise EcrImageAbsent("exact image is absent")

    class MissingRepository(FakeEcrAdapter):
        def collect(self, **kwargs):
            raise EcrRepositoryAbsent("retained repository is absent")

    class MissingRuntime(FakeAgentCoreAdapter):
        def collect_runtime_identity(self, **kwargs):
            raise AgentCoreRuntimeAbsent("retained runtime is absent")

        def collect_context(self, **kwargs):
            raise AgentCoreRuntimeAbsent("retained runtime is absent")

    class MissingEndpoint(FakeAgentCoreAdapter):
        def collect_context(self, **kwargs):
            raise AgentCoreEndpointAbsent("phase-owned endpoint is absent")

    deployment = FakeDeploymentAdapter()
    assert _composer(
        MissingImage(_image()),
        FakeAgentCoreAdapter(_context()),
        deployment,
    ).observe_phase("image", _transaction("image")) == (False, {})

    deployment = FakeDeploymentAdapter()
    deployment.foundation = None
    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            MissingImage(_image()),
            FakeAgentCoreAdapter(_context()),
            deployment,
        ).observe_phase("image", _transaction("image"))

    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            MissingRepository(_image()),
            FakeAgentCoreAdapter(_context()),
        ).observe_phase("image", _transaction("image"))

    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            FakeEcrAdapter(_image()),
            MissingRuntime(_context()),
        ).observe_phase("endpoint", _transaction("endpoint"))

    assert _composer(
        FakeEcrAdapter(_image()),
        MissingEndpoint(_context()),
    ).observe_phase("endpoint", _transaction("endpoint")) == (False, {})

    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            FakeEcrAdapter(_image()),
            MissingEndpoint(_context()),
        ).observe_phase("context", _transaction("context"))

    deployment = FakeDeploymentAdapter()
    deployment.changesets = None
    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            FakeEcrAdapter(_image()),
            MissingRuntime(_context()),
            deployment,
        ).observe_phase(
            "consumer-changesets",
            _transaction("consumer-changesets"),
        )

    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _composer(
            MissingImage(_image()),
            FakeAgentCoreAdapter(_context()),
        ).observe_phase("verify", _transaction("verify"))

    class OrphanRuntime(FakeAgentCoreAdapter):
        def assert_runtime_name_absent(self, **kwargs):
            raise AgentCoreEvidenceError("runtime still exists")

    deployment = FakeDeploymentAdapter()
    deployment.runtime = None
    with pytest.raises(ProductionObservationError, match="runtime"):
        _composer(
            FakeEcrAdapter(_image()),
            OrphanRuntime(_context()),
            deployment,
        ).observe_phase("runtime", _transaction("runtime"))


def test_consumer_phases_require_the_exact_journaled_runtime_context() -> None:
    drifted = replace(
        _context(),
        runtime_endpoint_id="ReleaseEndpoint-ZYXWVUTSRQ",
    )
    with pytest.raises(ProductionObservationError, match="context prerequisite"):
        _composer(
            FakeEcrAdapter(_image()),
            FakeAgentCoreAdapter(drifted),
        ).observe_phase(
            "consumer-changesets",
            _transaction("consumer-changesets"),
        )


def test_rollback_requires_exact_agentcore_retained_disposition() -> None:
    class AmbiguousRetainedAgentCore(FakeAgentCoreAdapter):
        def observe_retained_disposition(self, **kwargs):
            raise AgentCoreRuntimeAbsent("runtime missing but endpoint unproven")

    deployment = FakeDeploymentAdapter()
    deployment.rollback = True
    with pytest.raises(ProductionObservationError, match="rollback"):
        _composer(
            FakeEcrAdapter(_image()),
            AmbiguousRetainedAgentCore(_context()),
            deployment,
        ).observe_phase("rollback", _transaction("rollback"))
    assert deployment.calls == []

    drifted = replace(
        _context(),
        runtime_endpoint_id="ReleaseEndpoint-ZYXWVUTSRQ",
    )
    exact_cf = FakeDeploymentAdapter()
    with pytest.raises(ProductionObservationError, match="journaled context"):
        _composer(
            FakeEcrAdapter(_image()),
            FakeAgentCoreAdapter(drifted),
            exact_cf,
        ).observe_phase("rollback", _transaction("rollback"))
    assert exact_cf.calls == []


def test_artifact_reader_is_https_only_and_bounded_before_returning_bytes() -> None:
    calls: list[object] = []

    class Response:
        status = 200
        headers = {"Content-Length": "3"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://artifacts.example.invalid/blob"

        def read(self, maximum):
            calls.append(maximum)
            return b"abc"

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout, dict(request.header_items())))
        return Response()

    reader = HttpsArtifactBlobReader(opener=opener, timeout_seconds=7)

    with pytest.raises(ProductionObservationError, match="HTTPS"):
        reader.read("http://artifacts.example.invalid/blob", maximum_bytes=3)
    assert calls == []
    assert reader.read(
        "https://artifacts.example.invalid/blob", maximum_bytes=3
    ) == b"abc"
    assert calls == [
        ("https://artifacts.example.invalid/blob", 7, {}),
        4,
    ]


def test_default_artifact_reader_ignores_ambient_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release_tools import production_observation as observation_module

    built_handlers: list[object] = []
    calls: list[str] = []

    class Response:
        status = 200
        headers = {"Content-Length": "2"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://artifacts.example.invalid/blob"

        def read(self, maximum):
            return b"{}"

    class ExplicitOpener:
        def open(self, request, *, timeout):
            calls.append(request.full_url)
            return Response()

    def build_opener(*handlers):
        built_handlers.extend(handlers)
        return ExplicitOpener()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8")
    monkeypatch.setattr(observation_module.urllib_request, "build_opener", build_opener)
    reader = HttpsArtifactBlobReader()

    assert reader.read(
        "https://artifacts.example.invalid/blob",
        maximum_bytes=2,
    ) == b"{}"
    proxy_handlers = [
        item
        for item in built_handlers
        if isinstance(item, observation_module.urllib_request.ProxyHandler)
    ]
    https_handlers = [
        item
        for item in built_handlers
        if isinstance(item, observation_module.urllib_request.HTTPSHandler)
    ]
    assert len(proxy_handlers) == 1 and proxy_handlers[0].proxies == {}
    assert len(https_handlers) == 1
    assert calls == ["https://artifacts.example.invalid/blob"]


def test_default_artifact_reader_accepts_only_an_explicit_absolute_ca_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release_tools import production_observation as observation_module

    contexts: list[tuple[object, str | None]] = []
    sentinel = object()

    def create_default_context(purpose, *, cafile=None):
        contexts.append((purpose, cafile))
        return sentinel

    monkeypatch.setattr(
        observation_module.ssl,
        "create_default_context",
        create_default_context,
    )
    monkeypatch.setattr(
        observation_module.urllib_request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: None),
    )

    HttpsArtifactBlobReader(ca_file="/dev/fd/17")
    assert contexts == [(observation_module.ssl.Purpose.SERVER_AUTH, "/dev/fd/17")]
    with pytest.raises(ProductionObservationError, match="CA file"):
        HttpsArtifactBlobReader(ca_file="relative-ca.pem")


class CloudFormationNotFound(Exception):
    response = {"Error": {"Code": "ValidationError"}}


class CloudFormationChangeSetNotFound(Exception):
    response = {"Error": {"Code": "ChangeSetNotFound"}}


class FakeCloudFormation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.stacks = {
            name: {
                "StackId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:"
                    f"stack/{name}/00000000-0000-0000-0000-000000000000"
                ),
                "StackName": name,
                "StackStatus": "CREATE_COMPLETE",
                "Parameters": [],
                "Outputs": [],
                "Capabilities": ["CAPABILITY_IAM"],
            }
            for name in (*FOUNDATION_STACKS, *CONSUMER_STACKS)
        }
        self.stacks["OpenClawAgentCore"]["Outputs"] = [
            {"OutputKey": "RuntimeId", "OutputValue": RUNTIME_ID},
            {"OutputKey": "RuntimeVersion", "OutputValue": VERSION},
            {"OutputKey": "RuntimeImageUri", "OutputValue": IMAGE_URI},
            {"OutputKey": "RuntimeSourceCommit", "OutputValue": COMMIT},
        ]
        self.templates = {
            name: _stack_template(name)
            for name in (*FOUNDATION_STACKS, *CONSUMER_STACKS)
        }
        self.change_set_templates = {
            name: _stack_template(name) for name in CONSUMER_STACKS
        }
        self.stack_policies: dict[str, str] = {}
        self.change_sets = {
            name: {
                "StackId": self.stacks[name]["StackId"],
                "StackName": name,
                "ChangeSetId": (
                    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/"
                    f"release-{COMMIT}/00000000-0000-0000-0000-000000000000"
                ),
                "ChangeSetName": f"release-{COMMIT}",
                "ChangeSetType": "UPDATE",
                "Status": "CREATE_COMPLETE",
                "ExecutionStatus": "AVAILABLE",
                "Parameters": [],
                "Capabilities": ["CAPABILITY_IAM"],
                "Changes": [],
            }
            for name in CONSUMER_STACKS
        }

    def describe_stacks(self, **kwargs):
        self.calls.append(("describe_stacks", kwargs))
        stack = self.stacks.get(kwargs["StackName"])
        if stack is None:
            raise CloudFormationNotFound("missing")
        return {"Stacks": [stack], "ResponseMetadata": {}}

    def get_template(self, **kwargs):
        self.calls.append(("get_template", kwargs))
        if "ChangeSetName" in kwargs:
            if kwargs["StackName"] not in self.change_sets:
                raise CloudFormationNotFound("missing")
        elif kwargs["StackName"] not in self.stacks:
            raise CloudFormationNotFound("missing")
        template = (
            self.change_set_templates[kwargs["StackName"]]
            if "ChangeSetName" in kwargs
            else self.templates[kwargs["StackName"]]
        )
        return {
            "TemplateBody": template,
            "StagesAvailable": ["Original", "Processed"],
            "ResponseMetadata": {},
        }

    def get_stack_policy(self, **kwargs):
        self.calls.append(("get_stack_policy", kwargs))
        if kwargs["StackName"] not in self.stacks:
            raise CloudFormationNotFound("missing")
        response: dict[str, object] = {"ResponseMetadata": {}}
        if kwargs["StackName"] in self.stack_policies:
            response["StackPolicyBody"] = self.stack_policies[kwargs["StackName"]]
        return response

    def describe_change_set(self, **kwargs):
        self.calls.append(("describe_change_set", kwargs))
        value = self.change_sets.get(kwargs["StackName"])
        if value is None:
            raise CloudFormationChangeSetNotFound("missing")
        return {**value, "ResponseMetadata": {}}


def _cloudformation_adapter(fake: FakeCloudFormation):
    return CloudFormationEvidenceAdapter(fake, config=_config())


def test_cloudformation_foundation_requires_all_exact_complete_stacks() -> None:
    fake = FakeCloudFormation()
    adapter = _cloudformation_adapter(fake)

    assert adapter.observe_foundation(_transaction("foundation")) is True

    fake.stacks.pop(FOUNDATION_STACKS[-1])
    with pytest.raises(ProductionObservationError, match="partial"):
        adapter.observe_foundation(_transaction("foundation"))

    for name in FOUNDATION_STACKS[:-1]:
        fake.stacks.pop(name)
    assert adapter.observe_foundation(_transaction("foundation")) is None

    drifted = FakeCloudFormation()
    drifted.templates["OpenClawVpc"] = {"Resources": {"Unreviewed": {}}}
    with pytest.raises(ProductionObservationError, match="reviewed"):
        _cloudformation_adapter(drifted).observe_foundation(
            _transaction("foundation")
        )

    drifted_request = FakeCloudFormation()
    drifted_request.stacks["OpenClawVpc"]["RoleARN"] = (
        f"arn:aws:iam::{ACCOUNT}:role/unreviewed-cloudformation-role"
    )
    with pytest.raises(ProductionObservationError, match="reviewed request"):
        _cloudformation_adapter(drifted_request).observe_foundation(
            _transaction("foundation")
        )

    unreviewed_policy = FakeCloudFormation()
    unreviewed_policy.stack_policies["OpenClawVpc"] = (
        '{"Statement":[{"Effect":"Deny","Action":"Update:*",'
        '"Principal":"*","Resource":"*"}]}'
    )
    with pytest.raises(ProductionObservationError, match="stack policy"):
        _cloudformation_adapter(unreviewed_policy).observe_foundation(
            _transaction("foundation")
        )

    empty_policy = FakeCloudFormation()
    empty_policy.stack_policies["OpenClawVpc"] = ""
    assert _cloudformation_adapter(empty_policy).observe_foundation(
        _transaction("foundation")
    ) is True

    for status in ("DRIFTED", "UNKNOWN"):
        known_drift = FakeCloudFormation()
        known_drift.stacks["OpenClawVpc"]["DriftInformation"] = {
            "StackDriftStatus": status
        }
        with pytest.raises(ProductionObservationError, match="drift"):
            _cloudformation_adapter(known_drift).observe_foundation(
                _transaction("foundation")
            )


def test_cloudformation_runtime_identity_is_bound_to_exact_stack_outputs() -> None:
    fake = FakeCloudFormation()
    adapter = _cloudformation_adapter(fake)

    assert adapter.observe_runtime_identity(_transaction("runtime")) == (
        RUNTIME_ID,
        VERSION,
    )

    fake.stacks["OpenClawAgentCore"]["Outputs"][-1]["OutputValue"] = "0" * 40
    with pytest.raises(ProductionObservationError, match="outputs"):
        adapter.observe_runtime_identity(_transaction("runtime"))

    absent = FakeCloudFormation()
    absent.stacks["OpenClawAgentCore"]["Outputs"] = []
    previous = _stack_digest("OpenClawAgentCore")
    config = replace(
        _config(),
        foundation_stack_template_parameter_digests=tuple(
            (name, previous if name == "OpenClawAgentCore" else digest)
            for name, digest in _config().foundation_stack_template_parameter_digests
        ),
        runtime_stack_template_parameter_digest="f" * 64,
    )
    assert CloudFormationEvidenceAdapter(
        absent,
        config=config,
    ).observe_runtime_identity(_transaction("runtime")) is None

    absent.stacks.pop("OpenClawVpc")
    with pytest.raises(ProductionObservationError, match="prerequisite"):
        CloudFormationEvidenceAdapter(
            absent,
            config=config,
        ).observe_runtime_identity(_transaction("runtime"))


def test_cloudformation_consumer_evidence_rejects_partial_or_cross_account_sets() -> None:
    fake = FakeCloudFormation()
    adapter = _cloudformation_adapter(fake)

    digest = adapter.observe_consumer_changesets(
        _transaction("consumer-changesets")
    )
    assert isinstance(digest, str) and len(digest) == 64

    fake.change_sets[CONSUMER_STACKS[0]]["ChangeSetId"] = fake.change_sets[
        CONSUMER_STACKS[0]
    ]["ChangeSetId"].replace(ACCOUNT, "999999999999")
    with pytest.raises(ProductionObservationError, match="account"):
        adapter.observe_consumer_changesets(_transaction("consumer-changesets"))

    fake = FakeCloudFormation()
    fake.change_sets.pop(CONSUMER_STACKS[-1])
    with pytest.raises(ProductionObservationError, match="partial"):
        _cloudformation_adapter(fake).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    class InvalidChangeSetRequest(FakeCloudFormation):
        def describe_change_set(self, **kwargs):
            raise CloudFormationNotFound("invalid request")

    with pytest.raises(ProductionObservationError, match="failed"):
        _cloudformation_adapter(
            InvalidChangeSetRequest()
        ).observe_consumer_changesets(_transaction("consumer-changesets"))

    drifted = FakeCloudFormation()
    drifted.change_sets[CONSUMER_STACKS[0]]["Changes"] = [
        {"ResourceChange": {"LogicalResourceId": "Unreviewed"}}
    ]
    with pytest.raises(ProductionObservationError, match="reviewed"):
        _cloudformation_adapter(drifted).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    proposed_template_drift = FakeCloudFormation()
    proposed_template_drift.change_set_templates[CONSUMER_STACKS[0]] = {
        "Resources": {CONSUMER_STACKS[0]: {}},
        "Outputs": {"Unreviewed": {"Value": "foreign"}},
    }
    with pytest.raises(ProductionObservationError, match="proposed template"):
        _cloudformation_adapter(proposed_template_drift).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    paginated = FakeCloudFormation()
    original = paginated.describe_change_set

    def describe_change_set(**kwargs):
        return {**original(**kwargs), "NextToken": "truncated"}

    paginated.describe_change_set = describe_change_set  # type: ignore[method-assign]
    with pytest.raises(ProductionObservationError, match="paginated"):
        _cloudformation_adapter(paginated).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    truncated = FakeCloudFormation()
    truncated.change_sets[CONSUMER_STACKS[0]].pop("Changes")
    with pytest.raises(ProductionObservationError, match="complete"):
        _cloudformation_adapter(truncated).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    malformed = FakeCloudFormation()
    malformed.change_sets[CONSUMER_STACKS[0]]["NotificationARNs"] = [
        "arn:aws:sns:eu-west-1:123456789012:reviewed",
        7,
    ]
    with pytest.raises(ProductionObservationError, match="canonical"):
        _cloudformation_adapter(malformed).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    nested = FakeCloudFormation()
    nested.change_sets[CONSUMER_STACKS[0]]["ParentChangeSetId"] = (
        f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/foreign/"
        "00000000-0000-0000-0000-000000000000"
    )
    with pytest.raises(ProductionObservationError, match="nested"):
        _cloudformation_adapter(nested).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    invalid_deployment_mode = FakeCloudFormation()
    invalid_deployment_mode.change_sets[CONSUMER_STACKS[0]][
        "DeploymentMode"
    ] = {"Mode": "REVERT_DRIFT"}
    with pytest.raises(ProductionObservationError, match="scalar"):
        _cloudformation_adapter(invalid_deployment_mode).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )

    use_previous = FakeCloudFormation()
    use_previous.change_sets[CONSUMER_STACKS[0]]["Parameters"] = [
        {"ParameterKey": "Foreign", "UsePreviousValue": True}
    ]
    with pytest.raises(ProductionObservationError, match="parameter is not exact"):
        _cloudformation_adapter(use_previous).observe_consumer_changesets(
            _transaction("consumer-changesets")
        )


def test_cloudformation_application_and_verification_are_content_bound() -> None:
    fake = FakeCloudFormation()
    for change_set in fake.change_sets.values():
        change_set["ExecutionStatus"] = "EXECUTE_COMPLETE"
    adapter = _cloudformation_adapter(fake)

    application = adapter.observe_consumers(_transaction("consumers"))
    assert isinstance(application, str) and len(application) == 64
    transaction = replace(
        _transaction("verify"),
        consumer_application_sha256=application,
    )
    verification = adapter.observe_verification(
        transaction,
        image=_image(),
        context=_context(),
    )
    assert isinstance(verification, str) and len(verification) == 64

    fake.stacks[CONSUMER_STACKS[0]]["Outputs"] = [
        {"OutputKey": "Drift", "OutputValue": "changed"}
    ]
    assert adapter.observe_consumers(_transaction("consumers")) == application
    assert adapter.observe_verification(
        transaction,
        image=_image(),
        context=_context(),
    ) == verification

    drifted = FakeCloudFormation()
    for change_set in drifted.change_sets.values():
        change_set["ExecutionStatus"] = "EXECUTE_COMPLETE"
    drifted.templates[CONSUMER_STACKS[0]] = {
        "Resources": {"UnreviewedConsumer": {}}
    }
    with pytest.raises(ProductionObservationError, match="reviewed"):
        _cloudformation_adapter(drifted).observe_consumers(
            _transaction("consumers")
        )

    drifted_request = FakeCloudFormation()
    for change_set in drifted_request.change_sets.values():
        change_set["ExecutionStatus"] = "EXECUTE_COMPLETE"
    drifted_request.stacks[CONSUMER_STACKS[0]]["Capabilities"] = [
        "CAPABILITY_NAMED_IAM"
    ]
    with pytest.raises(ProductionObservationError, match="reviewed request"):
        _cloudformation_adapter(drifted_request).observe_consumers(
            _transaction("consumers")
        )


def test_consumer_absence_requires_exact_available_change_set_prerequisites() -> None:
    absent = FakeCloudFormation()
    for name in CONSUMER_STACKS:
        absent.stacks.pop(name)
    assert _cloudformation_adapter(absent).observe_consumers(
        _transaction("consumers")
    ) is None

    missing_prerequisite = FakeCloudFormation()
    for name in CONSUMER_STACKS:
        missing_prerequisite.stacks.pop(name)
        missing_prerequisite.change_sets.pop(name)
    with pytest.raises(ProductionObservationError, match="prerequisite"):
        _cloudformation_adapter(missing_prerequisite).observe_consumers(
            _transaction("consumers")
        )

    contradictory = FakeCloudFormation()
    for name in CONSUMER_STACKS:
        contradictory.stacks.pop(name)
        contradictory.change_sets[name]["ExecutionStatus"] = "EXECUTE_COMPLETE"
    with pytest.raises(ProductionObservationError, match="contradict"):
        _cloudformation_adapter(contradictory).observe_consumers(
            _transaction("consumers")
        )
