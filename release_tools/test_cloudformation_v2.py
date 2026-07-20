from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from release_tools.cloudformation_v2 import (
    CloudFormationMutationAmbiguous,
    CloudFormationMutationDispatcher,
    CloudFormationMutationError,
    CloudFormationOperationV2,
    VerifiedCloudFormationPreflightV2,
    _observed_request_projection_digest,
    _planned_observed_parameters,
    _reviewed_template,
    _template_parameter_digest,
    minimal_bootstrap_template_body,
    validate_cloudformation_preflight,
)
from release_tools.contracts import (
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    write_new_private_mutation_envelope,
)
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.test_transaction import (
    AGENTCORE_STACK_ID,
    _advance_v2_until_phase,
    _consumer_change_set_id,
    _consumer_stack_id,
    _create_v2,
    _resolved_mutation_request,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
TEMPLATE_ASSET_ID = "c" * 64
REVIEWED_TEMPLATE_BODY = canonical_json_bytes(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Reviewed Personal Operator stack",
        "Resources": {},
    }
).decode("utf-8")
TEMPLATE_CONTENT_SHA256 = hashlib.sha256(
    REVIEWED_TEMPLATE_BODY.encode("utf-8")
).hexdigest()
ENDPOINT_REVIEWED_TEMPLATE_BODY = canonical_json_bytes(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Reviewed Personal Operator stack",
        "Parameters": {
            "HardenedRuntimeArn": {"Type": "String"},
            "HardenedRuntimeId": {"Type": "String"},
            "HardenedRuntimeVersion": {"Type": "String"},
        },
        "Resources": {},
    }
).decode("utf-8")
TEMPLATE_URL = (
    f"https://cdk-hnb659fds-assets-{ACCOUNT}-{REGION}.s3.{REGION}.amazonaws.com/"
    f"{TEMPLATE_ASSET_ID}.json"
)


def _operation(
    kind: str = "STACK_CREATE",
    *,
    stack: str = "OpenClawVpc",
    account: str = ACCOUNT,
    region: str = REGION,
    source_commit: str = COMMIT,
    source_tree: str = TREE,
) -> dict[str, object]:
    change_set = kind in {"CHANGESET_CREATE", "CHANGESET_EXECUTE"}
    execute = kind == "CHANGESET_EXECUTE"
    value: dict[str, object] = {
        "schema": CloudFormationOperationV2.SCHEMA,
        "kind": kind,
        "account": account,
        "region": region,
        "sourceCommit": source_commit,
        "sourceTree": source_tree,
        "stackName": stack,
        "changeSetName": f"release-{source_commit}" if change_set else "",
        "templateBody": "",
        "templateUrl": "" if execute else (
            f"https://cdk-hnb659fds-assets-{account}-{region}.s3.{region}."
            f"amazonaws.com/{TEMPLATE_ASSET_ID}.json"
        ),
        "reviewedTemplateBody": "" if execute else REVIEWED_TEMPLATE_BODY,
        "templateAssetId": "" if execute else TEMPLATE_ASSET_ID,
        "templateContentSha256": "" if execute else TEMPLATE_CONTENT_SHA256,
        "expectedTemplateParameterSha256": "",
        "expectedObservedRequestSha256": "0" * 64,
        "parameters": [],
        "capabilities": (
            [] if execute else ["CAPABILITY_NAMED_IAM"]
        ),
        "tags": [] if execute else [
            {"Key": "SourceCommit", "Value": source_commit},
            {"Key": "SourceTree", "Value": source_tree},
            {"Key": "TransactionId", "Value": f"release_{source_commit}"},
        ],
    }
    parameters: tuple[tuple[str, str], ...] = ()
    tags = tuple(
        (str(item["Key"]), str(item["Value"]))
        for item in value["tags"]  # type: ignore[union-attr]
    )
    capabilities = tuple(str(item) for item in value["capabilities"])  # type: ignore[union-attr]
    reviewed = None if execute else _reviewed_template(REVIEWED_TEMPLATE_BODY)
    if kind in {"STACK_CREATE", "CHANGESET_CREATE"}:
        value["expectedTemplateParameterSha256"] = _template_parameter_digest(
            reviewed or {}, parameters
        )
    value["expectedObservedRequestSha256"] = (
        _observed_request_projection_digest(
            kind=kind,
            stack_name=stack,
            change_set_name=str(value["changeSetName"]),
            template=reviewed,
            capabilities=capabilities,
            tags=tags,
        )
    )
    return value


def _replace_reviewed_template(
    raw: dict[str, object],
    body: str,
) -> None:
    raw["reviewedTemplateBody"] = body
    raw["templateContentSha256"] = hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()


def _bootstrap() -> dict[str, object]:
    value = _operation("BOOTSTRAP_STACK", stack="CDKToolkit")
    body = minimal_bootstrap_template_body(
        account=ACCOUNT,
        region=REGION,
        source_commit=COMMIT,
        source_tree=TREE,
    )
    value.update(
        {
            "templateBody": body,
            "templateUrl": "",
            "reviewedTemplateBody": body,
            "templateAssetId": "",
            "templateContentSha256": hashlib.sha256(body.encode()).hexdigest(),
            "expectedTemplateParameterSha256": _template_parameter_digest(
                _reviewed_template(body), ()
            ),
            "capabilities": [],
        }
    )
    value["expectedObservedRequestSha256"] = (
        _observed_request_projection_digest(
            kind="BOOTSTRAP_STACK",
            stack_name="CDKToolkit",
            change_set_name="",
            template=_reviewed_template(body),
            capabilities=(),
            tags=tuple(
                (str(item["Key"]), str(item["Value"]))
                for item in value["tags"]  # type: ignore[union-attr]
            ),
        )
    )
    return value


def _plan_for_operation(
    raw_artifact: bytes,
    *,
    phase: str,
    kind: str,
    stack: str,
    expected_template_parameter_sha256: str,
    expected_observed_request_sha256: str,
    expected_template_sha256: str | None = None,
) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    expected_subject = (
        f"cfn:{ACCOUNT}:{REGION}:stack:{stack}:release:{COMMIT}"
    )
    step = next(
        item
        for item in steps
        if item["phase"] == phase
        and item["kind"] == kind
        and item["subject"] == expected_subject
    )
    artifact = next(
        item for item in artifacts if item["path"] == step["requestArtifact"]
    )
    request_sha256 = hashlib.sha256(raw_artifact).hexdigest()
    artifact.update(size=len(raw_artifact), sha256=request_sha256)
    step.update(
        requestSha256=request_sha256,
        expectedRequestSha256=request_sha256,
        expectedTemplateParameterSha256=expected_template_parameter_sha256,
        expectedObservedRequestSha256=expected_observed_request_sha256,
    )
    operation = CloudFormationOperationV2.from_bytes(raw_artifact)
    if "expectedTemplateSha256" in step:
        step["expectedTemplateSha256"] = (
            (
                operation.template_content_sha256
                if expected_template_sha256 is None
                else expected_template_sha256
            )
            if operation.kind == "STACK_UPDATE"
            else ""
        )
    if operation.template_asset_id:
        asset_step = next(
            item for item in steps if item["kind"] == "ASSET_PUBLISH"
        )
        asset_step["subject"] = f"cdk:asset:{operation.template_asset_id}"
        asset_step["expectedContentSha256"] = (
            operation.template_content_sha256
        )
    return ReleasePlanV2.from_mapping(value)


@contextmanager
def _verified_operation(
    tmp_path: Path,
    raw: dict[str, object],
    *,
    phase: str,
    plan_expected_template_parameter_sha256: str | None = None,
    plan_expected_observed_request_sha256: str | None = None,
    plan_expected_template_sha256: str | None = None,
) -> Iterator[
    tuple[VerifiedPrivateMutationV2, VerifiedCloudFormationPreflightV2]
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw_artifact = canonical_json_bytes(raw)
    kind = str(raw["kind"])
    stack = str(raw["stackName"])
    plan = _plan_for_operation(
        raw_artifact,
        phase=phase,
        kind=kind,
        stack=stack,
        expected_template_parameter_sha256=str(
            raw["expectedTemplateParameterSha256"]
            if plan_expected_template_parameter_sha256 is None
            else plan_expected_template_parameter_sha256
        ),
        expected_observed_request_sha256=str(
            raw["expectedObservedRequestSha256"]
            if plan_expected_observed_request_sha256 is None
            else plan_expected_observed_request_sha256
        ),
        expected_template_sha256=plan_expected_template_sha256,
    )
    preflight = validate_cloudformation_preflight(
        CloudFormationOperationV2.from_bytes(raw_artifact),
        release_plan=plan,
    )
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, f"{phase}:{kind}")
    journal.begin_step()
    request_path = tmp_path / "cloudformation-request.json"
    request_path.write_bytes(raw_artifact)
    envelope_path = tmp_path / "private-mutation.bin"
    write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=_resolved_mutation_request(
            journal,
            request_artifact_size=len(raw_artifact),
        ),
        request_artifact_path=request_path,
        plan=plan,
        transaction=journal.current,
    )
    with PrivateMutationEnvelopeV2.open_verified(
        envelope_path,
        plan=plan,
        transaction=journal.current,
        scratch_dir=tmp_path / "scratch",
    ) as verified:
        yield verified, preflight


class FakeCloudFormation:
    def __init__(
        self,
        *,
        response: object | None = None,
        error: Exception | None = None,
        account: str = ACCOUNT,
        region: str = REGION,
        service: str = "cloudformation",
        retries: dict[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.response = {"StackId": "ack"} if response is None else response
        self.error = error
        self._personal_operator_attested_account = account
        self.meta = SimpleNamespace(
            region_name=region,
            service_model=SimpleNamespace(service_name=service),
            config=SimpleNamespace(
                region_name=region,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries=(
                    {"mode": "standard", "total_max_attempts": 1}
                    if retries is None
                    else retries
                )
            ),
        )

    def _call(self, name: str, kwargs: dict[str, object]) -> object:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def create_stack(self, **kwargs: object) -> object:
        return self._call("create_stack", kwargs)

    def update_stack(self, **kwargs: object) -> object:
        return self._call("update_stack", kwargs)

    def create_change_set(self, **kwargs: object) -> object:
        return self._call("create_change_set", kwargs)

    def execute_change_set(self, **kwargs: object) -> object:
        return self._call("execute_change_set", kwargs)


def test_bootstrap_dispatches_exact_non_admin_same_account_request(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path, _bootstrap(), phase="foundation"
    ) as (verified, preflight):
        token = "po-" + verified.resolved_request.mutation_request.operation_sha256.removeprefix(
            "sha256:"
        )
        with attested_test_client(fake, service="cloudformation") as client:
            acknowledgement = CloudFormationMutationDispatcher(client).dispatch(
                verified, preflight
            )

    assert acknowledgement == {"dispatched": True}
    assert fake.calls == [
        (
            "create_stack",
            {
                "StackName": "CDKToolkit",
                "TemplateBody": _bootstrap()["templateBody"],
                "Parameters": [],
                "Capabilities": [],
                "Tags": _bootstrap()["tags"],
                "ClientRequestToken": token,
                "EnableTerminationProtection": True,
                "OnFailure": "DO_NOTHING",
            },
        )
    ]
    request = fake.calls[0][1]
    assert "RoleARN" not in request
    assert "AWS::IAM::Role" not in str(request["TemplateBody"])


def test_template_expectation_includes_resolved_cdk_bootstrap_default() -> None:
    template = {
        "Parameters": {
            "BootstrapVersion": {
                "Type": "AWS::SSM::Parameter::Value<String>",
                "Default": "/cdk-bootstrap/hnb659fds/version",
            }
        },
        "Resources": {},
    }

    assert _planned_observed_parameters(template, ()) == [
        {
            "ParameterKey": "BootstrapVersion",
            "ParameterValue": "/cdk-bootstrap/hnb659fds/version",
            "ResolvedValue": "6",
        }
    ]


def test_stack_create_uses_only_exact_plan_bound_template_url(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    raw = _operation()
    with _verified_operation(tmp_path, raw, phase="foundation") as (
        verified,
        preflight,
    ):
        token = "po-" + verified.resolved_request.mutation_request.operation_sha256.removeprefix(
            "sha256:"
        )
        with attested_test_client(fake, service="cloudformation") as client:
            acknowledgement = CloudFormationMutationDispatcher(client).dispatch(
                verified, preflight
            )

    assert acknowledgement == {"dispatched": True}
    assert fake.calls == [
        (
            "create_stack",
            {
                "StackName": "OpenClawVpc",
                "TemplateURL": TEMPLATE_URL,
                "Parameters": [],
                "Capabilities": ["CAPABILITY_NAMED_IAM"],
                "Tags": raw["tags"],
                "ClientRequestToken": token,
                "EnableTerminationProtection": True,
                "OnFailure": "DO_NOTHING",
            },
        )
    ]
    assert "RoleARN" not in fake.calls[0][1]


def test_runtime_update_has_no_parameters_and_endpoint_injects_only_observed_tuple(
    tmp_path: Path,
) -> None:
    runtime = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    runtime_fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path / "runtime", runtime, phase="runtime"
    ) as (verified, preflight):
        with attested_test_client(
            runtime_fake, service="cloudformation"
        ) as client:
            CloudFormationMutationDispatcher(client).dispatch(verified, preflight)
    assert runtime_fake.calls[0][1]["Parameters"] == []
    assert runtime_fake.calls[0][1]["StackName"] == AGENTCORE_STACK_ID

    endpoint = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(endpoint, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    endpoint_fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path / "endpoint", endpoint, phase="endpoint"
    ) as (verified, preflight):
        with attested_test_client(
            endpoint_fake, service="cloudformation"
        ) as client:
            CloudFormationMutationDispatcher(client).dispatch(verified, preflight)
    assert endpoint_fake.calls[0][1]["Parameters"] == [
        {
            "ParameterKey": "HardenedRuntimeArn",
            "ParameterValue": (
                "arn:aws:bedrock-agentcore:eu-west-1:123456789012:agent/"
                "12345678-1234-1234-1234-123456789abc:7"
            ),
        },
        {"ParameterKey": "HardenedRuntimeId", "ParameterValue": "Runtime-ABCDEFGHIJ"},
        {"ParameterKey": "HardenedRuntimeVersion", "ParameterValue": "7"},
    ]
    assert endpoint_fake.calls[0][1]["StackName"] == AGENTCORE_STACK_ID


@pytest.mark.parametrize("phase", ["runtime", "endpoint"])
def test_stack_update_template_cannot_self_assert_a_substituted_resource_graph(
    tmp_path: Path,
    phase: str,
) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    substituted = canonical_json_bytes(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Reviewed Personal Operator stack",
            "Resources": {
                "SubstitutedRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "AssumeRolePolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [],
                        }
                    },
                }
            },
        }
    ).decode("utf-8")
    raw["reviewedTemplateBody"] = substituted
    raw["templateContentSha256"] = hashlib.sha256(
        substituted.encode("utf-8")
    ).hexdigest()
    fake = FakeCloudFormation()

    with pytest.raises(
        CloudFormationMutationError,
        match="update template differs from the plan",
    ):
        with _verified_operation(
            tmp_path,
            raw,
            phase=phase,
            plan_expected_template_sha256=TEMPLATE_CONTENT_SHA256,
        ):
            pass

    assert fake.calls == []


def test_stack_request_projection_matches_do_nothing_rollback_semantics() -> None:
    raw = _operation()
    expected = {
        "stackName": "OpenClawVpc",
        "description": "Reviewed Personal Operator stack",
        "roleArn": "",
        "timeoutInMinutes": 0,
        "capabilities": ["CAPABILITY_NAMED_IAM"],
        "notificationArns": [],
        "tags": raw["tags"],
        "rollbackConfiguration": {},
        "deploymentConfig": {},
        "disableRollback": True,
        "enableTerminationProtection": True,
        "retainExceptOnCreate": False,
    }

    assert raw["expectedObservedRequestSha256"] == hashlib.sha256(
        canonical_json_bytes(expected)
    ).hexdigest()


def test_change_set_projection_includes_the_persistent_deployment_mode() -> None:
    raw = _operation("CHANGESET_CREATE", stack="OpenClawRouter")
    expected = {
        "stackName": "OpenClawRouter",
        "changeSetName": f"release-{COMMIT}",
        "changeSetType": "CREATE",
        "description": f"Personal Operator release {COMMIT}",
        "roleArn": "",
        "capabilities": ["CAPABILITY_NAMED_IAM"],
        "notificationArns": [],
        "tags": raw["tags"],
        "rollbackConfiguration": {},
        "deploymentConfig": {},
        "deploymentMode": "",
        "includeNestedStacks": False,
        "onStackFailure": "DO_NOTHING",
        "importExistingResources": False,
    }

    assert raw["expectedObservedRequestSha256"] == hashlib.sha256(
        canonical_json_bytes(expected)
    ).hexdigest()


def test_change_set_create_and_execute_are_closed_and_commit_bound(
    tmp_path: Path,
) -> None:
    create = _operation("CHANGESET_CREATE", stack="OpenClawRouter")
    create_fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path / "create",
        create,
        phase="router-cron-cs",
    ) as (verified, preflight):
        token = "po-" + verified.resolved_request.mutation_request.operation_sha256.removeprefix(
            "sha256:"
        )
        with attested_test_client(
            create_fake, service="cloudformation"
        ) as client:
            CloudFormationMutationDispatcher(client).dispatch(verified, preflight)
    assert create_fake.calls == [
        (
            "create_change_set",
            {
                "StackName": "OpenClawRouter",
                "TemplateURL": TEMPLATE_URL,
                "Parameters": [],
                "Capabilities": ["CAPABILITY_NAMED_IAM"],
                "Tags": create["tags"],
                "ChangeSetName": f"release-{COMMIT}",
                "ChangeSetType": "CREATE",
                "Description": f"Personal Operator release {COMMIT}",
                "ClientToken": token,
                "IncludeNestedStacks": False,
                "ImportExistingResources": False,
                "OnStackFailure": "DO_NOTHING",
            },
        )
    ]

    execute = _operation("CHANGESET_EXECUTE", stack="OpenClawRouter")
    execute_fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path / "execute",
        execute,
        phase="router-cron",
    ) as (verified, preflight):
        token = "po-" + verified.resolved_request.mutation_request.operation_sha256.removeprefix(
            "sha256:"
        )
        with attested_test_client(
            execute_fake, service="cloudformation"
        ) as client:
            CloudFormationMutationDispatcher(client).dispatch(verified, preflight)
    assert execute_fake.calls == [
        (
            "execute_change_set",
                {
                    "StackName": _consumer_stack_id("OpenClawRouter", 1),
                    "ChangeSetName": _consumer_change_set_id(1),
                "ClientRequestToken": token,
            },
        )
    ]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra=True), "fields"),
        (lambda value: value.update(region="us-east-1"), "region"),
        (lambda value: value.update(account="999999999999"), "subject"),
        (lambda value: value.update(sourceTree="B" * 40), "source"),
        (lambda value: value.update(stackName="OtherStack"), "stack"),
        (lambda value: value.update(templateUrl="https://example.com/template"), "template URL"),
        (lambda value: value.update(templateAssetId="9" * 64), "template"),
        (lambda value: value.update(templateContentSha256="not-a-digest"), "template"),
        (lambda value: value.update(capabilities=["CAPABILITY_AUTO_EXPAND"]), "capabilities"),
        (lambda value: value.update(parameters=[{"ParameterKey": "X", "ParameterValue": "*"}]), "parameter"),
    ],
)
def test_operation_rejects_open_or_cross_subject_requests(
    mutate: object,
    match: str,
) -> None:
    value = _operation()
    assert callable(mutate)
    mutate(value)

    with pytest.raises(CloudFormationMutationError, match=match):
        CloudFormationOperationV2.from_bytes(canonical_json_bytes(value))


def test_dispatch_rejects_a_free_operation_without_both_authorities() -> None:
    operation = CloudFormationOperationV2.from_bytes(
        canonical_json_bytes(_operation())
    )
    fake = FakeCloudFormation()

    with pytest.raises(CloudFormationMutationError, match="preflight"):
        CloudFormationMutationDispatcher(fake).dispatch(operation)  # type: ignore[arg-type]

    assert fake.calls == []

    with pytest.raises(CloudFormationMutationError, match="constructible"):
        VerifiedCloudFormationPreflightV2(
            release_plan_sha256="1" * 64,
            request_sha256="2" * 64,
            operation=operation,
        )


@pytest.mark.parametrize("mutation", ["missing", "content"])
def test_cloudformation_preflight_binds_template_key_to_planned_asset_content(
    mutation: str,
) -> None:
    raw = _operation()
    payload = canonical_json_bytes(raw)
    operation = CloudFormationOperationV2.from_bytes(payload)
    plan = _plan_for_operation(
        payload,
        phase="foundation",
        kind="STACK_CREATE",
        stack="OpenClawVpc",
        expected_template_parameter_sha256=str(
            raw["expectedTemplateParameterSha256"]
        ),
        expected_observed_request_sha256=str(
            raw["expectedObservedRequestSha256"]
        ),
    )
    value = plan.to_mapping()
    asset_step = next(
        step for step in value["steps"] if step["kind"] == "ASSET_PUBLISH"
    )
    if mutation == "missing":
        asset_step["subject"] = "cdk:asset:" + "9" * 64
    else:
        asset_step["expectedContentSha256"] = "8" * 64
    mismatched = ReleasePlanV2.from_mapping(value)

    with pytest.raises(CloudFormationMutationError, match="template asset"):
        validate_cloudformation_preflight(operation, release_plan=mismatched)


def test_raw_client_with_forgeable_account_marker_is_rejected(
    tmp_path: Path,
) -> None:
    fake = FakeCloudFormation()
    with _verified_operation(
        tmp_path, _operation(), phase="foundation"
    ) as (verified, preflight):
        with pytest.raises(CloudFormationMutationError, match="attested"):
            CloudFormationMutationDispatcher(fake).dispatch(verified, preflight)
    assert fake.calls == []


def test_resolved_identity_drift_is_rejected_before_provider_call(
    tmp_path: Path,
) -> None:
    raw = _operation(source_tree="d" * 40)
    fake = FakeCloudFormation()
    with pytest.raises(
        CloudFormationMutationError,
        match="release-plan identity",
    ):
        with _verified_operation(tmp_path, raw, phase="foundation"):
            pass
    assert fake.calls == []


@pytest.mark.parametrize(
    ("template_expectation", "request_expectation", "match"),
    [
        ("8" * 64, None, "template expectation"),
        (None, "7" * 64, "request expectation"),
    ],
)
def test_raw_operation_must_equal_plan_observer_expectations_before_call(
    tmp_path: Path,
    template_expectation: str | None,
    request_expectation: str | None,
    match: str,
) -> None:
    fake = FakeCloudFormation()
    with pytest.raises(
        CloudFormationMutationError,
        match="exact plan step",
    ):
        with _verified_operation(
            tmp_path,
            _operation(),
            phase="foundation",
            plan_expected_template_parameter_sha256=template_expectation,
            plan_expected_observed_request_sha256=request_expectation,
        ):
            pass
    assert fake.calls == []


def test_template_bytes_cannot_self_assert_the_plan_expectations(
    tmp_path: Path,
) -> None:
    raw = _operation()
    substituted = canonical_json_bytes(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Substituted retained template",
            "Resources": {
                "Admin": {
                    "Type": "AWS::IAM::ManagedPolicy",
                    "Properties": {"PolicyDocument": {"Statement": []}},
                }
            },
        }
    ).decode("utf-8")
    substituted_asset_id = "6" * 64
    raw.update(
        {
            "reviewedTemplateBody": substituted,
            "templateContentSha256": hashlib.sha256(
                substituted.encode("utf-8")
            ).hexdigest(),
            "templateAssetId": substituted_asset_id,
            "templateUrl": (
                f"https://cdk-hnb659fds-assets-{ACCOUNT}-{REGION}.s3."
                f"{REGION}.amazonaws.com/{substituted_asset_id}.json"
            ),
        }
    )
    fake = FakeCloudFormation()

    with pytest.raises(
        CloudFormationMutationError,
        match="reviewed template and parameter",
    ):
        with _verified_operation(tmp_path, raw, phase="foundation"):
            pass

    assert fake.calls == []


def test_bootstrap_rejects_arbitrary_self_hashed_admin_template() -> None:
    value = _bootstrap()
    body = canonical_json_bytes(
        {
            "Resources": {
                "AdministratorRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "ManagedPolicyArns": [
                            "arn:aws:iam::aws:policy/AdministratorAccess"
                        ]
                    },
                }
            }
        }
    ).decode("utf-8")
    value["templateBody"] = body
    value["templateContentSha256"] = hashlib.sha256(body.encode()).hexdigest()

    with pytest.raises(CloudFormationMutationError, match="bootstrap template"):
        CloudFormationOperationV2.from_bytes(canonical_json_bytes(value))


@pytest.mark.parametrize(
    "client",
    [
        FakeCloudFormation(account="999999999999"),
        FakeCloudFormation(region="us-east-1"),
        FakeCloudFormation(service="s3"),
        FakeCloudFormation(retries={"mode": "standard", "total_max_attempts": 2}),
    ],
)
def test_dispatch_rejects_wrong_account_service_region_or_retrying_client(
    tmp_path: Path,
    client: FakeCloudFormation,
) -> None:
    with _verified_operation(
        tmp_path, _operation(), phase="foundation"
    ) as (verified, preflight):
        with pytest.raises(CloudFormationMutationError, match="client"):
            CloudFormationMutationDispatcher(client).dispatch(verified, preflight)
    assert client.calls == []


@pytest.mark.parametrize(
    "fake",
    [
        FakeCloudFormation(error=RuntimeError("unknown effect")),
        FakeCloudFormation(response=[]),
    ],
)
def test_provider_exception_or_malformed_acknowledgement_is_ambiguous(
    tmp_path: Path,
    fake: FakeCloudFormation,
) -> None:
    with _verified_operation(
        tmp_path, _operation(), phase="foundation"
    ) as (verified, preflight):
        with attested_test_client(fake, service="cloudformation") as client:
            with pytest.raises(
                CloudFormationMutationAmbiguous, match="reconciliation"
            ):
                CloudFormationMutationDispatcher(client).dispatch(
                    verified, preflight
                )
    assert len(fake.calls) == 1


def test_module_has_no_sdk_credentials_journal_or_process_authority() -> None:
    source = (Path(__file__).parent / "cloudformation_v2.py").read_text(
        encoding="utf-8"
    )
    assert "boto3" not in source
    assert "botocore" not in source
    assert "subprocess" not in source
    assert "journal" not in source.casefold()
    assert "Path(" not in source
