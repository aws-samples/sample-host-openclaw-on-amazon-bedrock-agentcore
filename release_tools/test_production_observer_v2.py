from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from typing import Iterator

import pytest

from release_tools import test_image_publication as image_test
from release_tools.asset_publication_v2 import AssetPublicationV2
from release_tools.cloudformation_v2 import (
    CloudFormationOperationV2,
    _observed_request_projection_digest,
    _reviewed_template,
    _template_parameter_digest,
    validate_cloudformation_preflight,
)
from release_tools.contracts import ReleasePlanV2, canonical_json_bytes
from release_tools.production_observer_v2 import (
    CanonicalReadObservationV2,
    ProductionObserverV2,
    ProductionObserverV2Ambiguous,
    ProductionObserverV2Error,
    _new_observation,
)
from release_tools.test_asset_publication_v2 import (
    ASSET_ID,
    COMMIT,
    PAYLOAD,
    REGION,
    TREE,
    _artifact,
    _verified_asset,
)
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_cloudformation_v2 import (
    ENDPOINT_REVIEWED_TEMPLATE_BODY,
    REVIEWED_TEMPLATE_BODY,
    _operation,
    _plan_for_operation,
    _replace_reviewed_template,
    _verified_operation,
)
from release_tools.test_image_publication import _prepare
from release_tools.test_transaction import (
    AGENTCORE_STACK_ID,
    _advance_v2_until_phase,
    _create_v2,
    _foundation_inputs,
)
from release_tools.transaction import ObservationDisposition


ACCOUNT = "123456789012"
RUNTIME_ID = "Runtime-ABCDEFGHIJ"
RUNTIME_VERSION = "7"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agent/"
    f"12345678-1234-1234-1234-123456789abc:{RUNTIME_VERSION}"
)
ENDPOINT_ID = "Endpoint-ABCDEFGHIJ"
ENDPOINT_NAME = f"release_{COMMIT}"
ENDPOINT_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:agentEndpoint/"
    "87654321-4321-4321-4321-cba987654321"
)
ENDPOINT_RESOURCE_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME_ID}/"
    f"runtime-endpoint/{ENDPOINT_ID}"
)
WORKLOAD_IDENTITY_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "workload-identity-directory/default/workload-identity/"
    "personal_operator_bridge-0123456789"
)


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeService:
    def __init__(self, service: str) -> None:
        self.service = service
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, list[object]] = {}
        self.meta = SimpleNamespace(
            region_name=REGION,
            service_model=SimpleNamespace(service_name=service),
            config=SimpleNamespace(
                region_name=REGION,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries={"mode": "standard", "total_max_attempts": 1}
            ),
        )

    def queue(self, method: str, *responses: object) -> None:
        self.responses.setdefault(method, []).extend(responses)

    def close(self) -> None:
        return None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(**kwargs: object) -> object:
            self.calls.append((name, kwargs))
            queued = self.responses.get(name, [])
            if not queued:
                raise AssertionError(f"unexpected {self.service}.{name}")
            result = queued.pop(0)
            if isinstance(result, BaseException):
                raise result
            return deepcopy(result)

        return invoke


@contextmanager
def _observer(
    *,
    services: dict[str, FakeService] | None = None,
) -> Iterator[tuple[ProductionObserverV2, dict[str, FakeService]]]:
    fakes = services or {
        service: FakeService(service)
        for service in (
            "s3",
            "cloudformation",
            "ecr",
            "bedrock-agentcore-control",
            "signer",
            "cloudtrail",
        )
    }
    with ExitStack() as stack:
        clients = {
            service: stack.enter_context(
                attested_test_client(fake, service=service)
            )
            for service, fake in fakes.items()
        }
        yield (
            ProductionObserverV2(
                account=ACCOUNT,
                region=REGION,
                s3=clients["s3"],
                cloudformation=clients["cloudformation"],
                ecr=clients["ecr"],
                agentcore=clients["bedrock-agentcore-control"],
                signer=clients["signer"],
                cloudtrail=clients["cloudtrail"],
            ),
            fakes,
        )


def _asset_metadata() -> AssetPublicationV2:
    raw = _artifact()
    magic_size = len(b"PO-CDK-ASSET-V2\x00")
    header_size = int.from_bytes(raw[magic_size : magic_size + 4], "big")
    return AssetPublicationV2.from_header_bytes(
        raw[magic_size + 4 : magic_size + 4 + header_size]
    )


def _s3_head() -> dict[str, object]:
    metadata = _asset_metadata()
    return {
        "ContentLength": metadata.content_size,
        "ContentType": metadata.content_type,
        "ChecksumSHA256": base64.b64encode(
            bytes.fromhex(metadata.content_sha256)
        ).decode("ascii"),
        "ServerSideEncryption": "aws:kms",
        "BucketKeyEnabled": True,
        "SSEKMSKeyId": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/" + "1" * 36,
        "VersionId": "3LgTQepXxE",
        "Metadata": {
            "content-sha256": metadata.content_sha256,
            "asset-id": metadata.asset_id,
            "source-commit": metadata.source_commit,
            "source-tree": metadata.source_tree,
        },
    }


def test_raw_clients_are_rejected() -> None:
    raw = FakeService("s3")
    with pytest.raises(ProductionObserverV2Error, match="attested"):
        ProductionObserverV2(
            account=ACCOUNT,
            region=REGION,
            s3=raw,
            cloudformation=raw,
            ecr=raw,
            agentcore=raw,
            signer=raw,
            cloudtrail=raw,
        )


def test_canonical_observation_is_not_directly_constructible() -> None:
    assert not hasattr(CanonicalReadObservationV2, "create")
    with pytest.raises(ProductionObserverV2Error, match="constructible"):
        CanonicalReadObservationV2(
            service="s3",
            operation="head_object",
            subject="cdk:asset:" + "a" * 64,
            disposition=ObservationDisposition.PRESENT,
            provider_status="PRESENT",
            projection_bytes=b"{}\n",
        )


@pytest.mark.parametrize(
    "operation",
    (
        "describe_stack_drift_detection_status",
        "describe_stack_resource_drifts",
    ),
)
def test_canonical_observation_factory_allows_only_drift_reads(
    operation: str,
) -> None:
    observed = _new_observation(
        service="cloudformation",
        operation=operation,
        subject=(
            f"cfn:{ACCOUNT}:{REGION}:stack:CDKToolkit:release:"
            f"{'a' * 40}:drift"
        ),
        disposition=ObservationDisposition.PENDING,
        provider_status="TEST_PENDING",
        projection={"driftDetectionId": "12345678-1234-1234-1234-123456789abc"},
    )

    assert observed.operation == operation

    with pytest.raises(ProductionObserverV2Error, match="identity"):
        _new_observation(
            service="cloudformation",
            operation="detect_stack_drift",
            subject=observed.subject,
            disposition=ObservationDisposition.PENDING,
            provider_status="TEST_PENDING",
            projection={},
        )


def test_s3_asset_present_is_stable_exact_and_plan_bound(tmp_path) -> None:
    with _verified_asset(
        tmp_path,
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue("head_object", _s3_head(), _s3_head())
            result = observer.observe_asset(verified)

    assert isinstance(result, CanonicalReadObservationV2)
    assert result.disposition is ObservationDisposition.PRESENT
    assert result.projection()["contentSha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert [call for call in fakes["s3"].calls] == [
        (
            "head_object",
            {
                "Bucket": f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}",
                "Key": f"{ASSET_ID}.json",
                "ExpectedBucketOwner": ACCOUNT,
                "ChecksumMode": "ENABLED",
            },
        ),
        (
            "head_object",
            {
                "Bucket": f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}",
                "Key": f"{ASSET_ID}.json",
                "ExpectedBucketOwner": ACCOUNT,
                "ChecksumMode": "ENABLED",
            },
        ),
    ]


def test_s3_asset_exact_404_is_absent_but_transport_is_ambiguous(tmp_path) -> None:
    with _verified_asset(
        tmp_path / "absent",
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue(
                "head_object",
                ProviderError("NoSuchKey", "missing", 404),
            )
            absent = observer.observe_asset(verified)
    assert absent.disposition is ObservationDisposition.ABSENT

    with _verified_asset(
        tmp_path / "ambiguous",
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue("head_object", TimeoutError("lost response"))
            with pytest.raises(ProductionObserverV2Ambiguous):
                observer.observe_asset(verified)


def test_s3_asset_conflict_never_becomes_present(tmp_path) -> None:
    wrong = _s3_head()
    wrong["Metadata"] = {**wrong["Metadata"], "source-tree": "c" * 40}
    with _verified_asset(
        tmp_path,
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue("head_object", wrong, wrong)
            observed = observer.observe_asset(verified)
    assert observed.disposition is ObservationDisposition.FAILED_RETAINED
    assert observed.provider_status == "RETAINED_OBJECT_CONFLICT"


def test_s3_null_version_is_a_retained_subject_conflict(tmp_path) -> None:
    wrong = _s3_head()
    wrong["VersionId"] = "null"
    with _verified_asset(
        tmp_path,
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue("head_object", wrong, wrong)
            observed = observer.observe_asset(verified)
    assert observed.disposition is ObservationDisposition.FAILED_RETAINED

    malformed = _s3_head()
    malformed["ContentLength"] = "not-an-integer"
    with _verified_asset(
        tmp_path / "malformed",
        _artifact(),
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        with _observer() as (observer, fakes):
            fakes["s3"].queue("head_object", malformed, malformed)
            with pytest.raises(ProductionObserverV2Ambiguous):
                observer.observe_asset(verified)


STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/OpenClawVpc/"
    "11111111-2222-3333-4444-555555555555"
)
ROUTER_STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/OpenClawRouter/"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
ROUTER_CHANGE_SET_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/"
    f"release-{COMMIT}/11111111-aaaa-bbbb-cccc-222222222222"
)


def _cf_preflight(raw: dict[str, object], *, phase: str):
    operation = CloudFormationOperationV2.from_mapping(raw)
    payload = operation.to_bytes()
    plan = _plan_for_operation(
        payload,
        phase=phase,
        kind=operation.kind,
        stack=operation.stack_name,
        expected_template_parameter_sha256=(
            operation.expected_template_parameter_sha256
        ),
        expected_observed_request_sha256=(
            operation.expected_observed_request_sha256
        ),
    )
    return validate_cloudformation_preflight(operation, release_plan=plan)


def _stack(status: str = "CREATE_COMPLETE") -> dict[str, object]:
    created = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    return {
        "StackId": STACK_ID,
        "StackName": "OpenClawVpc",
        "Description": "Reviewed Personal Operator stack",
        "StackStatus": status,
        "CreationTime": created,
        "DisableRollback": True,
        "EnableTerminationProtection": True,
        "RetainExceptOnCreate": False,
        "RoleARN": "",
        "TimeoutInMinutes": 0,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "NotificationARNs": [],
        "Parameters": [],
        "Tags": [
            {"Key": "SourceCommit", "Value": COMMIT},
            {"Key": "SourceTree", "Value": TREE},
            {"Key": "TransactionId", "Value": f"release_{COMMIT}"},
        ],
        "RollbackConfiguration": {},
        "DeploymentConfig": {},
        "DriftInformation": {
            "StackDriftStatus": "IN_SYNC",
            "LastCheckTimestamp": datetime(
                2026, 7, 20, 9, 5, tzinfo=timezone.utc
            ),
        },
    }


def test_cloudformation_stack_present_captures_id_then_closes_by_exact_arn(
    tmp_path,
) -> None:
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    preflight = _cf_preflight(raw, phase="foundation")
    with _verified_operation(
        tmp_path,
        raw,
        phase="foundation",
    ) as (verified, helper_preflight):
        assert type(helper_preflight) is type(preflight)
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [_stack()]},
                {"Stacks": [_stack()]},
            )
            fakes["cloudformation"].queue(
                "get_template",
                {
                    "TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY),
                    "StagesAvailable": ["Original", "Processed"],
                },
                {
                    "TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY),
                    "StagesAvailable": ["Original", "Processed"],
                },
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            result = observer.observe_cloudformation(verified, preflight)

    assert result.disposition is ObservationDisposition.PRESENT
    assert result.projection()["stackId"] == STACK_ID
    describe_calls = [
        arguments
        for method, arguments in fakes["cloudformation"].calls
        if method == "describe_stacks"
    ]
    assert describe_calls == [
        {"StackName": "OpenClawVpc"},
        {"StackName": STACK_ID},
    ]
    assert all(
        call.get("StackName") == STACK_ID
        for method, call in fakes["cloudformation"].calls
        if method in {"get_template", "get_stack_policy"}
    )


def test_cloudformation_stack_projection_retains_only_reviewed_output_keys(
    tmp_path,
) -> None:
    template_body = canonical_json_bytes(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Reviewed Personal Operator stack",
            "Outputs": {
                "PrivateSubnetIds": {"Value": "provider-generated"},
                "SecurityGroupId": {"Value": "provider-generated"},
            },
            "Resources": {},
        }
    ).decode("utf-8")
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    _replace_reviewed_template(raw, template_body)
    reviewed = _reviewed_template(template_body)
    raw["expectedTemplateParameterSha256"] = _template_parameter_digest(
        reviewed,
        (),
    )
    raw["expectedObservedRequestSha256"] = _observed_request_projection_digest(
        kind="STACK_CREATE",
        stack_name="OpenClawVpc",
        change_set_name="",
        template=reviewed,
        capabilities=("CAPABILITY_NAMED_IAM",),
        tags=(
            ("SourceCommit", COMMIT),
            ("SourceTree", TREE),
            ("TransactionId", f"release_{COMMIT}"),
        ),
    )
    stack = _stack()
    stack["Outputs"] = [
        {"OutputKey": "SecurityGroupId", "OutputValue": "sg-00000000000000001"},
        {
            "OutputKey": "PrivateSubnetIds",
            "OutputValue": (
                "subnet-00000000000000001,subnet-00000000000000002"
            ),
        },
        {"OutputKey": "UnreviewedSecret", "OutputValue": "must-not-be-retained"},
    ]
    with _verified_operation(
        tmp_path,
        raw,
        phase="foundation",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [stack]},
                {"Stacks": [stack]},
            )
            live_template = {"TemplateBody": reviewed}
            fakes["cloudformation"].queue(
                "get_template",
                live_template,
                live_template,
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            observed = observer.observe_cloudformation(verified, preflight)

    assert observed.projection()["outputs"] == {
        "PrivateSubnetIds": (
            "subnet-00000000000000001,subnet-00000000000000002"
        ),
        "SecurityGroupId": "sg-00000000000000001",
    }


def test_cloudformation_stack_ignores_historical_drift_but_closes_template_and_policy(
    tmp_path,
) -> None:
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    preflight = _cf_preflight(raw, phase="foundation")
    without_drift = _stack()
    without_drift.pop("DriftInformation")
    with _verified_operation(
        tmp_path / "drift",
        raw,
        phase="foundation",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [without_drift]},
                {"Stacks": [without_drift]},
            )
            expected = {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)}
            fakes["cloudformation"].queue("get_template", expected, expected)
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            observed = observer.observe_cloudformation(verified, preflight)

    assert observed.disposition is ObservationDisposition.PRESENT
    assert "drift" not in observed.projection()

    with _verified_operation(
        tmp_path / "template",
        raw,
        phase="foundation",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [_stack()]}
            )
            fakes["cloudformation"].queue(
                "get_template",
                {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)},
                {"TemplateBody": {"Resources": {}}},
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            with pytest.raises(ProductionObserverV2Ambiguous, match="template changed"):
                observer.observe_cloudformation(verified, preflight)


def test_cloudformation_stack_known_pending_and_failed_are_not_present(
    tmp_path,
) -> None:
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    preflight = _cf_preflight(raw, phase="foundation")
    for suffix, status, expected in (
        ("pending", "CREATE_IN_PROGRESS", ObservationDisposition.PENDING),
        ("failed", "ROLLBACK_COMPLETE", ObservationDisposition.FAILED_RETAINED),
    ):
        with _verified_operation(
            tmp_path / suffix,
            raw,
            phase="foundation",
        ) as (verified, helper_preflight):
            assert type(helper_preflight) is type(preflight)
            with _observer() as (observer, fakes):
                fakes["cloudformation"].queue(
                    "describe_stacks", {"Stacks": [_stack(status)]}
                )
                observed = observer.observe_cloudformation(verified, preflight)
        assert observed.disposition is expected


def test_cloudformation_absence_requires_exact_not_found_message(tmp_path) -> None:
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    preflight = _cf_preflight(raw, phase="foundation")
    with _verified_operation(
        tmp_path / "absent",
        raw,
        phase="foundation",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                ProviderError(
                    "ValidationError",
                    "Stack with id OpenClawVpc does not exist",
                    400,
                ),
            )
            observed = observer.observe_cloudformation(verified, preflight)
    assert observed.disposition is ObservationDisposition.ABSENT

    with _verified_operation(
        tmp_path / "ambiguous",
        raw,
        phase="foundation",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                ProviderError(
                    "ValidationError",
                    "Template format error: unsupported resource",
                    400,
                ),
            )
            with pytest.raises(ProductionObserverV2Ambiguous):
                observer.observe_cloudformation(verified, preflight)


def test_cloudformation_stack_pagination_or_identity_swap_fails_closed(
    tmp_path,
) -> None:
    raw = _operation("STACK_CREATE", stack="OpenClawVpc")
    preflight = _cf_preflight(raw, phase="foundation")
    with _verified_operation(
        tmp_path / "page",
        raw,
        phase="foundation",
    ) as (verified, helper_preflight):
        assert type(helper_preflight) is type(preflight)
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [_stack()], "NextToken": "unexpected"},
            )
            with pytest.raises(ProductionObserverV2Ambiguous, match="paginated"):
                observer.observe_cloudformation(verified, preflight)

    crossed = _stack()
    crossed["StackId"] = STACK_ID.replace("11111111", "aaaaaaaa")
    with _verified_operation(
        tmp_path / "swap",
        raw,
        phase="foundation",
    ) as (verified, helper_preflight):
        assert type(helper_preflight) is type(preflight)
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [_stack()]},
                {"Stacks": [crossed]},
            )
            fakes["cloudformation"].queue(
                "get_template",
                {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)},
                {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)},
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            with pytest.raises(ProductionObserverV2Ambiguous, match="changed"):
                observer.observe_cloudformation(verified, preflight)


def _change_set(
    *,
    status: str = "CREATE_COMPLETE",
    execution_status: str = "AVAILABLE",
    next_token: str | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "ChangeSetId": ROUTER_CHANGE_SET_ID,
        "ChangeSetName": f"release-{COMMIT}",
        "StackId": ROUTER_STACK_ID,
        "StackName": "OpenClawRouter",
        "Description": f"Personal Operator release {COMMIT}",
        "CreationTime": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "Status": status,
        "ExecutionStatus": execution_status,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "NotificationARNs": [],
        "Parameters": [],
        "Tags": [
            {"Key": "SourceCommit", "Value": COMMIT},
            {"Key": "SourceTree", "Value": TREE},
            {"Key": "TransactionId", "Value": f"release_{COMMIT}"},
        ],
        "RollbackConfiguration": {},
        "DeploymentConfig": {},
        "IncludeNestedStacks": False,
        "OnStackFailure": "DO_NOTHING",
        "ImportExistingResources": False,
        "Changes": [],
    }
    if next_token is not None:
        response["NextToken"] = next_token
    return response


def _cloudtrail_create_change_set(operation_sha256: str) -> dict[str, object]:
    event_id = "11111111-2222-3333-4444-555555555555"
    event = {
        "eventVersion": "1.11",
        "eventID": event_id,
        "eventName": "CreateChangeSet",
        "eventSource": "cloudformation.amazonaws.com",
        "awsRegion": REGION,
        "recipientAccountId": ACCOUNT,
        "readOnly": False,
        "userIdentity": {"accountId": ACCOUNT},
        "requestParameters": {
            "stackName": "OpenClawRouter",
            "changeSetName": f"release-{COMMIT}",
            "changeSetType": "CREATE",
            "description": f"Personal Operator release {COMMIT}",
            "templateURL": (
                f"https://cdk-hnb659fds-assets-{ACCOUNT}-{REGION}.s3."
                f"{REGION}.amazonaws.com/" + "c" * 64 + ".json"
            ),
            "parameters": [],
            "capabilities": ["CAPABILITY_NAMED_IAM"],
            "notificationARNs": [],
            "tags": [
                {"key": "SourceCommit", "value": COMMIT},
                {"key": "SourceTree", "value": TREE},
                {"key": "TransactionId", "value": f"release_{COMMIT}"},
            ],
            "clientToken": "po-" + operation_sha256.removeprefix("sha256:"),
            "includeNestedStacks": False,
            "onStackFailure": "DO_NOTHING",
            "importExistingResources": False,
        },
        "responseElements": {
            "id": ROUTER_CHANGE_SET_ID,
            "stackId": ROUTER_STACK_ID,
        },
    }
    return {
        "EventId": event_id,
        "EventName": "CreateChangeSet",
        "ReadOnly": "false",
        "CloudTrailEvent": json.dumps(event, sort_keys=True),
    }


def _cloudtrail_execute_change_set(
    operation_sha256: str,
    *,
    stack_id: str,
    change_set_id: str,
) -> dict[str, object]:
    event_id = "66666666-7777-8888-9999-000000000000"
    event = {
        "eventVersion": "1.11",
        "eventID": event_id,
        "eventName": "ExecuteChangeSet",
        "eventSource": "cloudformation.amazonaws.com",
        "awsRegion": REGION,
        "recipientAccountId": ACCOUNT,
        "readOnly": False,
        "userIdentity": {"accountId": ACCOUNT},
        "requestParameters": {
            "stackName": stack_id,
            "changeSetName": change_set_id,
            "clientRequestToken": (
                "po-" + operation_sha256.removeprefix("sha256:")
            ),
        },
        "responseElements": None,
    }
    return {
        "EventId": event_id,
        "EventName": "ExecuteChangeSet",
        "ReadOnly": "false",
        "CloudTrailEvent": json.dumps(event, sort_keys=True),
    }


def _executed_stack(stack_id: str, *, status: str) -> dict[str, object]:
    created = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    return {
        "StackId": stack_id,
        "StackName": "OpenClawRouter",
        "StackStatus": status,
        "CreationTime": created,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Parameters": [],
        "Tags": [
            {"Key": "SourceCommit", "Value": COMMIT},
            {"Key": "SourceTree", "Value": TREE},
            {"Key": "TransactionId", "Value": f"release_{COMMIT}"},
        ],
        "DriftInformation": {
            "StackDriftStatus": "IN_SYNC",
            "LastCheckTimestamp": datetime(
                2026, 7, 20, 10, 5, tzinfo=timezone.utc
            ),
        },
    }


def test_cloudformation_change_set_captures_exact_ids_and_cloudtrail_request(
    tmp_path,
) -> None:
    raw = _operation("CHANGESET_CREATE", stack="OpenClawRouter")
    preflight = _cf_preflight(raw, phase="router-cron-cs")
    with _verified_operation(
        tmp_path,
        raw,
        phase="router-cron-cs",
    ) as (verified, helper_preflight):
        assert type(helper_preflight) is type(preflight)
        operation_sha256 = (
            verified.resolved_request.mutation_request.operation_sha256
        )
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set", _change_set(), _change_set()
            )
            fakes["cloudformation"].queue(
                "get_template",
                {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)},
                {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)},
            )
            fakes["cloudtrail"].queue(
                "lookup_events",
                {
                    "Events": [
                        _cloudtrail_create_change_set(operation_sha256)
                    ]
                },
            )
            result = observer.observe_cloudformation(verified, preflight)

    assert result.disposition is ObservationDisposition.PRESENT
    assert result.projection()["stackId"] == ROUTER_STACK_ID
    assert result.projection()["changeSetId"] == ROUTER_CHANGE_SET_ID
    calls = [
        kwargs
        for method, kwargs in fakes["cloudformation"].calls
        if method == "describe_change_set"
    ]
    assert calls[0]["StackName"] == "OpenClawRouter"
    assert calls[0]["ChangeSetName"] == f"release-{COMMIT}"
    assert calls[1]["StackName"] == ROUTER_STACK_ID
    assert calls[1]["ChangeSetName"] == ROUTER_CHANGE_SET_ID


def test_cloudformation_change_set_token_cycle_is_ambiguous(tmp_path) -> None:
    raw = _operation("CHANGESET_CREATE", stack="OpenClawRouter")
    preflight = _cf_preflight(raw, phase="router-cron-cs")
    with _verified_operation(
        tmp_path,
        raw,
        phase="router-cron-cs",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set",
                _change_set(next_token="cycle"),
                _change_set(next_token="cycle"),
            )
            with pytest.raises(ProductionObserverV2Ambiguous, match="token cycle"):
                observer.observe_cloudformation(verified, preflight)


def test_change_set_create_exact_provider_not_found_is_absent(tmp_path) -> None:
    raw = _operation("CHANGESET_CREATE", stack="OpenClawRouter")
    with _verified_operation(
        tmp_path,
        raw,
        phase="router-cron-cs",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set",
                ProviderError("ChangeSetNotFound", "not found", 404),
            )
            observed = observer.observe_cloudformation(verified, preflight)
    assert observed.disposition is ObservationDisposition.ABSENT


def test_executed_change_set_requires_exact_event_and_terminal_applied_stack(
    tmp_path,
) -> None:
    raw = _operation("CHANGESET_EXECUTE", stack="OpenClawRouter")
    with _verified_operation(
        tmp_path / "present",
        raw,
        phase="router-cron",
    ) as (verified, preflight):
        resolved = verified.resolved_request
        stack_id = resolved.router_target_stack_id
        change_set_id = resolved.router_change_set_id
        operation_sha256 = resolved.mutation_request.operation_sha256
        predecessor_sha256 = resolved.router_cron_changesets_sha256
        change_set = _change_set(execution_status="EXECUTE_COMPLETE")
        change_set.update(
            StackId=stack_id,
            ChangeSetId=change_set_id,
        )
        stack = _executed_stack(stack_id, status="CREATE_COMPLETE")
        template = {"TemplateBody": json.loads(REVIEWED_TEMPLATE_BODY)}
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set", change_set, change_set
            )
            fakes["cloudtrail"].queue(
                "lookup_events",
                {
                    "Events": [
                        _cloudtrail_execute_change_set(
                            operation_sha256,
                            stack_id=stack_id,
                            change_set_id=change_set_id,
                        )
                    ]
                },
            )
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [stack]}, {"Stacks": [stack]}
            )
            fakes["cloudformation"].queue(
                "get_template", template, template, template, template
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            observed = observer.observe_cloudformation(verified, preflight)

    assert observed.disposition is ObservationDisposition.PRESENT
    projection = observed.projection()
    assert projection["stackId"] == stack_id
    assert projection["changeSetId"] == change_set_id
    assert projection["predecessorObservationSha256"] == predecessor_sha256
    assert len(
        [
            call
            for call in fakes["cloudformation"].calls
            if call[0] == "describe_stacks"
        ]
    ) == 2


def test_executed_change_set_never_promotes_in_progress_or_missing_exact_ids(
    tmp_path,
) -> None:
    raw = _operation("CHANGESET_EXECUTE", stack="OpenClawRouter")
    with _verified_operation(
        tmp_path / "available",
        raw,
        phase="router-cron",
    ) as (verified, preflight):
        resolved = verified.resolved_request
        available = _change_set(execution_status="AVAILABLE")
        available.update(
            StackId=resolved.router_target_stack_id,
            ChangeSetId=resolved.router_change_set_id,
        )
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set", available, available
            )
            observed = observer.observe_cloudformation(verified, preflight)
    assert observed.disposition is ObservationDisposition.ABSENT
    assert observed.provider_status == "AVAILABLE_NOT_EXECUTED"

    with _verified_operation(
        tmp_path / "pending",
        raw,
        phase="router-cron",
    ) as (verified, preflight):
        resolved = verified.resolved_request
        change_set = _change_set(execution_status="EXECUTE_COMPLETE")
        change_set.update(
            StackId=resolved.router_target_stack_id,
            ChangeSetId=resolved.router_change_set_id,
        )
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue("describe_change_set", change_set)
            fakes["cloudtrail"].queue("lookup_events", {"Events": []})
            observed = observer.observe_cloudformation(verified, preflight)
    assert observed.disposition is ObservationDisposition.PENDING
    assert observed.provider_status == "EXECUTE_COMPLETE_AWAITING_CLOUDTRAIL"

    with _verified_operation(
        tmp_path / "stack-pending",
        raw,
        phase="router-cron",
    ) as (verified, preflight):
        resolved = verified.resolved_request
        change_set = _change_set(execution_status="EXECUTE_COMPLETE")
        change_set.update(
            StackId=resolved.router_target_stack_id,
            ChangeSetId=resolved.router_change_set_id,
        )
        operation_sha256 = resolved.mutation_request.operation_sha256
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue("describe_change_set", change_set)
            fakes["cloudtrail"].queue(
                "lookup_events",
                {
                    "Events": [
                        _cloudtrail_execute_change_set(
                            operation_sha256,
                            stack_id=resolved.router_target_stack_id,
                            change_set_id=resolved.router_change_set_id,
                        )
                    ]
                },
            )
            fakes["cloudformation"].queue(
                "describe_stacks",
                {
                    "Stacks": [
                        _executed_stack(
                            resolved.router_target_stack_id,
                            status="CREATE_IN_PROGRESS",
                        )
                    ]
                },
            )
            observed = observer.observe_cloudformation(verified, preflight)
    assert observed.disposition is ObservationDisposition.PENDING
    assert observed.provider_status == "CREATE_IN_PROGRESS"

    with _verified_operation(
        tmp_path / "missing",
        raw,
        phase="router-cron",
    ) as (verified, preflight):
        retained = verified.resolved_request.router_change_set_id
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_change_set",
                ProviderError(
                    "ValidationError",
                    f"ChangeSet [{retained}] does not exist",
                    400,
                ),
            )
            with pytest.raises(
                ProductionObserverV2Ambiguous,
                match="retained change set",
            ):
                observer.observe_cloudformation(verified, preflight)


def _endpoint_stack() -> dict[str, object]:
    updated = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    return {
        "StackId": AGENTCORE_STACK_ID,
        "StackName": "OpenClawAgentCore",
        "Description": "Reviewed Personal Operator stack",
        "StackStatus": "UPDATE_COMPLETE",
        "LastUpdatedTime": updated,
        "DisableRollback": True,
        "EnableTerminationProtection": True,
        "RetainExceptOnCreate": False,
        "RoleARN": "",
        "TimeoutInMinutes": 0,
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "NotificationARNs": [],
        "Parameters": [
            {
                "ParameterKey": "HardenedRuntimeArn",
                "ParameterValue": RUNTIME_ARN,
            },
            {
                "ParameterKey": "HardenedRuntimeId",
                "ParameterValue": RUNTIME_ID,
            },
            {
                "ParameterKey": "HardenedRuntimeVersion",
                "ParameterValue": RUNTIME_VERSION,
            },
        ],
        "Tags": [
            {"Key": "SourceCommit", "Value": COMMIT},
            {"Key": "SourceTree", "Value": TREE},
            {"Key": "TransactionId", "Value": f"release_{COMMIT}"},
        ],
        "RollbackConfiguration": {},
        "DeploymentConfig": {},
        "DriftInformation": {
            "StackDriftStatus": "IN_SYNC",
            "LastCheckTimestamp": datetime(
                2026, 7, 20, 11, 5, tzinfo=timezone.utc
            ),
        },
        "Outputs": [
            {"OutputKey": "RuntimeId", "OutputValue": RUNTIME_ID},
            {"OutputKey": "RuntimeVersion", "OutputValue": RUNTIME_VERSION},
            {"OutputKey": "RuntimeArn", "OutputValue": RUNTIME_ARN},
            {"OutputKey": "RuntimeEndpointId", "OutputValue": ENDPOINT_ID},
            {"OutputKey": "RuntimeEndpointName", "OutputValue": ENDPOINT_NAME},
        ],
    }


RUNTIME_REVIEWED_TEMPLATE_BODY = canonical_json_bytes(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Reviewed Personal Operator stack",
        "Outputs": {
            "RuntimeArn": {"Value": "provider-generated"},
            "RuntimeId": {"Value": "provider-generated"},
            "RuntimeVersion": {"Value": "provider-generated"},
        },
        "Resources": {},
    }
).decode("utf-8")


def _runtime_stack() -> dict[str, object]:
    stack = _endpoint_stack()
    stack["Parameters"] = []
    stack["Outputs"] = [
        {"OutputKey": "RuntimeId", "OutputValue": RUNTIME_ID},
        {"OutputKey": "RuntimeVersion", "OutputValue": RUNTIME_VERSION},
        {"OutputKey": "RuntimeArn", "OutputValue": RUNTIME_ARN},
    ]
    return stack


def _runtime_response() -> dict[str, object]:
    image_uri = (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
        "personal-operator/bridge@sha256:" + "c" * 64
    )
    return {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "personal_operator_bridge",
        "agentRuntimeVersion": RUNTIME_VERSION,
        "agentRuntimeArn": RUNTIME_ARN,
        "status": "READY",
        "roleArn": (
            f"arn:aws:iam::{ACCOUNT}:role/"
            f"openclaw-agentcore-execution-role-{REGION}"
        ),
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": image_uri}
        },
        "authorizerConfiguration": {},
        "requestHeaderConfiguration": {},
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": ["sg-00000000000000001"],
                "subnets": [
                    "subnet-00000000000000001",
                    "subnet-00000000000000002",
                ],
            },
        },
        "environmentVariables": {
            "AWS_DEFAULT_REGION": REGION,
            "AWS_REGION": REGION,
            "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
            "BEDROCK_GUARDRAIL_ID": "abcdefghij",
            "BEDROCK_GUARDRAIL_VERSION": "1",
            "CAPABILITY_GATEWAY_FUNCTION_ARN": (
                f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
                "personal-operator-capability-gateway"
            ),
            "DISABLE_ADOT_OBSERVABILITY": "true",
            "S3_USER_FILES_BUCKET": (
                f"openclaw-user-files-{ACCOUNT}-{REGION}"
            ),
            "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME": (
                "personal-operator-workspace-credential-broker"
            ),
            "WORKSPACE_SYNC_INTERVAL_MS": "300000",
        },
        "filesystemConfigurations": [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}}
        ],
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": 1800,
            "maxLifetime": 28800,
        },
        "metadataConfiguration": {"requireMMDSV2": True},
        "workloadIdentityDetails": {
            "workloadIdentityArn": WORKLOAD_IDENTITY_ARN
        },
    }


def _transitional_runtime_response() -> dict[str, object]:
    runtime = _runtime_response()
    runtime["metadataConfiguration"] = {"requireMMDSV2": False}
    network = runtime["networkConfiguration"]
    assert isinstance(network, dict)
    vpc = network["networkModeConfig"]
    assert isinstance(vpc, dict)
    vpc["requireServiceS3Endpoint"] = True
    return runtime


def _endpoint_response(**changes: object) -> dict[str, object]:
    value = {
        "id": ENDPOINT_ID,
        "name": ENDPOINT_NAME,
        "status": "READY",
        "liveVersion": RUNTIME_VERSION,
        "targetVersion": RUNTIME_VERSION,
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
    }
    value.update(changes)
    return value


def _command_deny_policy(resource_arn: str) -> dict[str, object]:
    return {
        "policy": json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyRuntimeCommandExecution",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": [
                            "bedrock-agentcore:InvokeAgentRuntimeCommand",
                            "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
                        ],
                        "Resource": resource_arn,
                    }
                ],
            },
            sort_keys=True,
        )
    }


def test_agentcore_runtime_stack_observation_derives_exact_transitional_identity(
    tmp_path,
) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, RUNTIME_REVIEWED_TEMPLATE_BODY)
    with _verified_operation(
        tmp_path,
        raw,
        phase="runtime",
    ) as (verified, preflight):
        expected_subject = verified.resolved_request.mutation_request.subject
        with _observer() as (observer, fakes):
            stack = _runtime_stack()
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [stack]},
                {"Stacks": [stack]},
            )
            reviewed = json.loads(RUNTIME_REVIEWED_TEMPLATE_BODY)
            fakes["cloudformation"].queue(
                "get_template",
                {"TemplateBody": reviewed},
                {"TemplateBody": reviewed},
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            runtime = _transitional_runtime_response()
            fakes["bedrock-agentcore-control"].queue(
                "get_agent_runtime", runtime, runtime
            )
            runtime_resource = (
                f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
                f"{RUNTIME_ID}"
            )
            fakes["bedrock-agentcore-control"].queue(
                "get_resource_policy",
                _command_deny_policy(runtime_resource),
                _command_deny_policy(runtime_resource),
            )
            observed = observer.observe_agentcore_runtime_stack(
                verified,
                preflight,
            )

    assert observed.disposition is ObservationDisposition.PRESENT
    assert observed.subject == expected_subject
    assert observed.operation == "get_agent_runtime"
    projection = observed.projection()
    assert projection["agentCoreStackId"] == AGENTCORE_STACK_ID
    assert projection["runtimeId"] == RUNTIME_ID
    assert projection["runtimeVersion"] == RUNTIME_VERSION
    assert projection["runtimeArn"] == RUNTIME_ARN
    assert projection["workloadIdentityArn"] == WORKLOAD_IDENTITY_ARN
    assert projection["requiresMMDSV2"] is False
    assert projection["requiresServiceS3Endpoint"] is True


def test_agentcore_runtime_stack_rejects_hardened_config_drift(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, RUNTIME_REVIEWED_TEMPLATE_BODY)
    with _verified_operation(
        tmp_path,
        raw,
        phase="runtime",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            stack = _runtime_stack()
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [stack]}
            )
            drifted = _transitional_runtime_response()
            environment = drifted["environmentVariables"]
            assert isinstance(environment, dict)
            environment["CAPABILITY_GATEWAY_FUNCTION_ARN"] = (
                f"arn:aws:lambda:{REGION}:999999999999:function:"
                "personal-operator-capability-gateway"
            )
            fakes["bedrock-agentcore-control"].queue(
                "get_agent_runtime", drifted
            )
            with pytest.raises(ProductionObserverV2Error, match="configuration"):
                observer.observe_agentcore_runtime_stack(verified, preflight)


def test_agentcore_transitional_boolean_fields_are_exact() -> None:
    runtime = _transitional_runtime_response()
    runtime["metadataConfiguration"] = {"requireMMDSV2": 0}
    resolved = SimpleNamespace(
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=RUNTIME_VERSION,
        runtime_arn=RUNTIME_ARN,
        runtime_image_digest="sha256:" + "c" * 64,
        foundation_runtime_inputs=_foundation_inputs(),
    )
    with _observer() as (observer, _):
        with pytest.raises(
            ProductionObserverV2Error, match="transitional hardening"
        ):
            observer._runtime_projection(
                runtime,
                resolved,
                allow_transitional_hardening=True,
            )


def test_runtime_projection_retains_full_canonical_configuration() -> None:
    runtime = _runtime_response()
    resolved = SimpleNamespace(
        account=ACCOUNT,
        region=REGION,
        runtime_id=RUNTIME_ID,
        runtime_version=RUNTIME_VERSION,
        runtime_arn=RUNTIME_ARN,
        runtime_image_digest="sha256:" + "c" * 64,
        foundation_runtime_inputs=_foundation_inputs(),
    )

    with _observer() as (observer, _):
        projection = observer._runtime_projection(runtime, resolved)

    expected_configuration = {
        key: runtime[key]
        for key in (
            "agentRuntimeArtifact",
            "authorizerConfiguration",
            "environmentVariables",
            "filesystemConfigurations",
            "lifecycleConfiguration",
            "metadataConfiguration",
            "networkConfiguration",
            "protocolConfiguration",
            "requestHeaderConfiguration",
        )
    }
    assert projection["runtimeConfiguration"] == expected_configuration


def test_agentcore_endpoint_binds_cf_output_arn_policy_and_guardrail(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    preflight = _cf_preflight(raw, phase="endpoint")
    with _verified_operation(
        tmp_path,
        raw,
        phase="endpoint",
    ) as (verified, helper_preflight):
        assert type(helper_preflight) is type(preflight)
        expected_subject = verified.resolved_request.mutation_request.subject
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks",
                {"Stacks": [_endpoint_stack()]},
                {"Stacks": [_endpoint_stack()]},
            )
            fakes["cloudformation"].queue(
                "get_template",
                {"TemplateBody": json.loads(ENDPOINT_REVIEWED_TEMPLATE_BODY)},
                {"TemplateBody": json.loads(ENDPOINT_REVIEWED_TEMPLATE_BODY)},
            )
            fakes["cloudformation"].queue("get_stack_policy", {}, {})
            fakes["bedrock-agentcore-control"].queue(
                "list_agent_runtime_endpoints",
                {"runtimeEndpoints": [_endpoint_response()]},
                {"runtimeEndpoints": [_endpoint_response()]},
            )
            fakes["bedrock-agentcore-control"].queue(
                "get_agent_runtime",
                _runtime_response(),
                _runtime_response(),
            )
            fakes["bedrock-agentcore-control"].queue(
                "get_agent_runtime_endpoint",
                _endpoint_response(),
                _endpoint_response(),
            )
            fakes["bedrock-agentcore-control"].queue(
                "get_resource_policy",
                _command_deny_policy(
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
                    f"{RUNTIME_ID}"
                ),
                _command_deny_policy(ENDPOINT_RESOURCE_ARN),
                _command_deny_policy(
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/"
                    f"{RUNTIME_ID}"
                ),
                _command_deny_policy(ENDPOINT_RESOURCE_ARN),
            )
            observed = observer.observe_agentcore_endpoint(
                verified,
                preflight,
            )

    assert observed.disposition is ObservationDisposition.PRESENT
    projection = observed.projection()
    assert projection["endpointId"] == ENDPOINT_ID
    assert projection["endpointArn"] == ENDPOINT_ARN
    assert projection["workloadIdentityArn"] == WORKLOAD_IDENTITY_ARN
    assert projection["guardrailId"] == "abcdefghij"
    runtime = _runtime_response()
    expected_configuration = {
        key: runtime[key]
        for key in (
            "agentRuntimeArtifact",
            "authorizerConfiguration",
            "environmentVariables",
            "filesystemConfigurations",
            "lifecycleConfiguration",
            "metadataConfiguration",
            "networkConfiguration",
            "protocolConfiguration",
            "requestHeaderConfiguration",
        )
    }
    assert projection["runtimeConfiguration"] == expected_configuration
    assert projection["runtimeConfigurationSha256"] == hashlib.sha256(
        canonical_json_bytes(
            {
                "executionRoleArn": (
                    f"arn:aws:iam::{ACCOUNT}:role/"
                    f"openclaw-agentcore-execution-role-{REGION}"
                ),
                "runtimeConfiguration": expected_configuration,
            }
        )
    ).hexdigest()
    assert observed.subject == expected_subject
    assert len(
        [
            call
            for call in fakes["bedrock-agentcore-control"].calls
            if call[0] == "list_agent_runtime_endpoints"
        ]
    ) == 2
    assert len(
        [
            call
            for call in fakes["bedrock-agentcore-control"].calls
            if call[0] == "get_resource_policy"
        ]
    ) == 4


def test_agentcore_stack_update_cannot_bypass_composite_observer(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    with _verified_operation(
        tmp_path,
        raw,
        phase="endpoint",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            with pytest.raises(ProductionObserverV2Error, match="composite"):
                observer.observe_cloudformation(verified, preflight)
    assert not fakes["cloudformation"].calls


def test_endpoint_inventory_must_independently_be_ready(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    with _verified_operation(
        tmp_path,
        raw,
        phase="endpoint",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [_endpoint_stack()]}
            )
            fakes["bedrock-agentcore-control"].queue(
                "list_agent_runtime_endpoints",
                {"runtimeEndpoints": [_endpoint_response(status="UPDATE_FAILED")]},
            )
            with pytest.raises(ProductionObserverV2Error, match="not READY"):
                observer.observe_agentcore_endpoint(verified, preflight)


def test_endpoint_stack_failure_preserves_cloudformation_provider(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    failed_stack = _endpoint_stack()
    failed_stack["StackStatus"] = "UPDATE_FAILED"
    with _verified_operation(
        tmp_path,
        raw,
        phase="endpoint",
    ) as (verified, preflight):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [failed_stack]}
            )
            observed = observer.observe_agentcore_endpoint(verified, preflight)
    assert observed.disposition is ObservationDisposition.FAILED_RETAINED
    assert observed.service == "cloudformation"
    assert observed.operation == "describe_stacks"


def test_agentcore_endpoint_wrong_returned_arn_or_guardrail_fails_closed(
    tmp_path,
) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    preflight = _cf_preflight(raw, phase="endpoint")
    for suffix, endpoint, runtime, message in (
        (
            "resource-arn-is-not-api-arn",
            _endpoint_response(
                agentRuntimeEndpointArn=ENDPOINT_RESOURCE_ARN
            ),
            _runtime_response(),
            "endpoint ARN",
        ),
        (
            "arn",
            _endpoint_response(
                agentRuntimeEndpointArn=ENDPOINT_ARN.replace(
                    ACCOUNT, "999999999999"
                )
            ),
            _runtime_response(),
            "endpoint ARN",
        ),
        (
            "guardrail",
            _endpoint_response(),
            {
                **_runtime_response(),
                "environmentVariables": {
                    **_runtime_response()["environmentVariables"],
                    "BEDROCK_GUARDRAIL_VERSION": "2",
                },
            },
            "guardrail",
        ),
        (
            "missing-workload-identity",
            _endpoint_response(),
            {
                key: value
                for key, value in _runtime_response().items()
                if key != "workloadIdentityDetails"
            },
            "workload identity",
        ),
        (
            "crossed-workload-identity",
            _endpoint_response(),
            {
                **_runtime_response(),
                "workloadIdentityDetails": {
                    "workloadIdentityArn": WORKLOAD_IDENTITY_ARN.replace(
                        ACCOUNT, "999999999999"
                    )
                },
            },
            "workload identity",
        ),
        (
            "ready-with-failure-reason",
            _endpoint_response(),
            {
                **_runtime_response(),
                "failureReason": "provider contradiction",
            },
            "failure reason",
        ),
    ):
        with _verified_operation(
            tmp_path / suffix,
            raw,
            phase="endpoint",
        ) as (verified, _):
            with _observer() as (observer, fakes):
                fakes["cloudformation"].queue(
                    "describe_stacks", {"Stacks": [_endpoint_stack()]}
                )
                fakes["bedrock-agentcore-control"].queue(
                    "list_agent_runtime_endpoints",
                    {"runtimeEndpoints": [endpoint]},
                )
                fakes["bedrock-agentcore-control"].queue(
                    "get_agent_runtime", runtime
                )
                fakes["bedrock-agentcore-control"].queue(
                    "get_agent_runtime_endpoint", endpoint
                )
                with pytest.raises(ProductionObserverV2Error, match=message):
                    observer.observe_agentcore_endpoint(verified, preflight)


def test_agentcore_endpoint_pagination_token_cycle_is_ambiguous(tmp_path) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    preflight = _cf_preflight(raw, phase="endpoint")
    with _verified_operation(
        tmp_path,
        raw,
        phase="endpoint",
    ) as (verified, _):
        with _observer() as (observer, fakes):
            fakes["cloudformation"].queue(
                "describe_stacks", {"Stacks": [_endpoint_stack()]}
            )
            fakes["bedrock-agentcore-control"].queue(
                "list_agent_runtime_endpoints",
                {"runtimeEndpoints": [], "nextToken": "cycle"},
                {"runtimeEndpoints": [], "nextToken": "cycle"},
            )
            with pytest.raises(ProductionObserverV2Ambiguous, match="token cycle"):
                observer.observe_agentcore_endpoint(verified, preflight)


def test_agentcore_endpoint_retained_partial_or_failed_api_state_is_ambiguous(
    tmp_path,
) -> None:
    raw = _operation("STACK_UPDATE", stack="OpenClawAgentCore")
    _replace_reviewed_template(raw, ENDPOINT_REVIEWED_TEMPLATE_BODY)
    for suffix, endpoint_response in (
        (
            "missing",
            ProviderError("ResourceNotFoundException", "missing", 404),
        ),
        ("failed", _endpoint_response(status="CREATE_FAILED")),
    ):
        with _verified_operation(
            tmp_path / suffix,
            raw,
            phase="endpoint",
        ) as (verified, preflight):
            with _observer() as (observer, fakes):
                fakes["cloudformation"].queue(
                    "describe_stacks", {"Stacks": [_endpoint_stack()]}
                )
                fakes["bedrock-agentcore-control"].queue(
                    "list_agent_runtime_endpoints",
                    {"runtimeEndpoints": [_endpoint_response()]},
                )
                fakes["bedrock-agentcore-control"].queue(
                    "get_agent_runtime", _runtime_response()
                )
                fakes["bedrock-agentcore-control"].queue(
                    "get_agent_runtime_endpoint", endpoint_response
                )
                with pytest.raises(ProductionObserverV2Ambiguous):
                    observer.observe_agentcore_endpoint(verified, preflight)


def _release_plan_for_image_current(bundle) -> ReleasePlanV2:
    """Mirror the image fixture while contracts and its owner change in parallel."""

    effects = bundle.publication_effects(expected_plan_sha256=bundle.plan_sha256)
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list) and isinstance(artifacts, list)
    prior = [step for step in steps if step["phase"] == "image"]
    prior_paths = {step["requestArtifact"] for step in prior}
    insertion = next(i for i, step in enumerate(steps) if step["phase"] == "image")
    steps[:] = [step for step in steps if step["phase"] != "image"]
    artifacts[:] = [
        artifact
        for artifact in artifacts
        if artifact["path"] not in prior_paths
    ]
    image_steps: list[dict[str, object]] = []
    for index, effect in enumerate(effects):
        raw = effect.to_private_bytes()
        request_sha256 = hashlib.sha256(raw).hexdigest()
        path = f"build/image-effects/{index:02d}-{effect.effect_id}.private"
        image_steps.append(
            {
                "id": f"image-{index:02d}-{effect.effect_id}",
                "phase": "image",
                "ordinal": 0,
                "kind": "IMAGE_PUBLISH",
                "subject": effect.provider_subject,
                "mutation": True,
                "requestArtifact": path,
                "requestSha256": request_sha256,
                "expectedTemplateSha256": "",
                "expectedTemplateParameterSha256": "",
                "expectedRequestSha256": request_sha256,
                "expectedObservedRequestSha256": "",
                "expectedContentSha256": effect.digest.removeprefix("sha256:"),
            }
        )
        artifacts.append(
            {"path": path, "size": len(raw), "sha256": request_sha256}
        )
    observe_payload = bundle.plan.to_bytes()
    observe_sha256 = hashlib.sha256(observe_payload).hexdigest()
    observe_path = "build/image-publication-plan.json"
    image_steps.append(
        {
            "id": "image-observe-publication-plan",
            "phase": "image",
            "ordinal": 0,
            "kind": "IMAGE_OBSERVE",
            "subject": (
                f"ecr:{ACCOUNT}:{REGION}:repository:personal-operator/bridge:"
                f"release:{COMMIT}"
            ),
            "mutation": False,
            "requestArtifact": observe_path,
            "requestSha256": observe_sha256,
            "expectedTemplateSha256": "",
            "expectedTemplateParameterSha256": "",
            "expectedRequestSha256": observe_sha256,
            "expectedObservedRequestSha256": "",
            "expectedContentSha256": bundle.plan.subject.digest.removeprefix(
                "sha256:"
            ),
        }
    )
    artifacts.append(
        {
            "path": observe_path,
            "size": len(observe_payload),
            "sha256": observe_sha256,
        }
    )
    steps[insertion:insertion] = image_steps
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal
    value["runtimeImageDigest"] = bundle.plan.subject.digest
    value["runtimeImageUri"] = (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
        f"personal-operator/bridge@{bundle.plan.subject.digest}"
    )
    artifacts.sort(key=lambda artifact: artifact["path"])
    return ReleasePlanV2.from_mapping(value)


@contextmanager
def _verified_image_effect_current(bundle, index: int):
    original = image_test._release_plan_for_image
    image_test._release_plan_for_image = _release_plan_for_image_current
    try:
        with image_test._verified_image_effect(bundle, index) as result:
            yield result
    finally:
        image_test._release_plan_for_image = original


@contextmanager
def _verified_image_observe_current(tmp_path, bundle):
    original = image_test._release_plan_for_image
    image_test._release_plan_for_image = _release_plan_for_image_current
    try:
        effects, release_plan, _, authority = image_test._preflight(bundle)
    finally:
        image_test._release_plan_for_image = original
    journal = _create_v2(tmp_path, release_plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "image:IMAGE_OBSERVE")
    yield authority.bind_current_observe(
        release_plan=release_plan,
        transaction=journal.current,
    )


def _queue_aggregate_effect_reads(
    fake: FakeService,
    observe,
    *,
    sweeps: int = 2,
    missing_final_effect_id: str | None = None,
) -> None:
    for sweep in range(sweeps):
        for effect in observe.ordered_effects:
            missing = (
                sweep == sweeps - 1
                and effect.effect_id == missing_final_effect_id
            )
            if effect.effect_kind == "ECR_BLOB_PUT":
                response = (
                    {
                        "layers": [],
                        "failures": [
                            {
                                "layerDigest": effect.digest,
                                "failureCode": "MissingLayerDigest",
                            }
                        ],
                    }
                    if missing
                    else {
                        "layers": [
                            {
                                "layerDigest": effect.digest,
                                "layerAvailability": "AVAILABLE",
                                "layerSize": effect.size,
                                "mediaType": effect.media_type,
                            }
                        ],
                        "failures": [],
                    }
                )
                fake.queue(
                    "batch_check_layer_availability", response, response
                )
                continue
            response = {
                "images": [
                    {
                        "registryId": ACCOUNT,
                        "repositoryName": "personal-operator/bridge",
                        "imageId": {
                            "imageDigest": effect.digest,
                            **({"imageTag": effect.tag} if effect.tag else {}),
                        },
                        "imageManifest": effect.payload.decode("utf-8"),
                        "imageManifestMediaType": effect.media_type,
                    }
                ],
                "failures": [],
            }
            if missing:
                response = {
                    "images": [],
                    "failures": [
                        {
                            "imageId": {"imageDigest": effect.digest},
                            "failureCode": "ImageNotFound",
                        }
                    ],
                }
            reads = 6 if effect.tag else 3
            fake.queue("batch_get_image", *([response] * reads))


def _queue_aggregate_release_reads(
    fake: FakeService,
    signer: FakeService,
    observe,
    *,
    scan_status: str = "COMPLETE",
    critical: int = 0,
    high: int = 0,
    signature_status: str = "COMPLETE",
    final_scan_status: str | None = None,
    profile_version: str = "ABCDEFGHIJ",
) -> None:
    digest = observe.publication_plan.subject.digest
    tag = observe.publication_plan.commit_tag
    repository = {
        "repositories": [
            {
                "registryId": ACCOUNT,
                "repositoryName": "personal-operator/bridge",
                "repositoryArn": (
                    f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/"
                    "personal-operator/bridge"
                ),
                "repositoryUri": (
                    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
                    "personal-operator/bridge"
                ),
                "imageTagMutability": "IMMUTABLE",
                "imageScanningConfiguration": {"scanOnPush": True},
                "encryptionConfiguration": {
                    "encryptionType": "KMS",
                    "kmsKey": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/" + "1" * 36,
                },
            }
        ]
    }
    image = {
        "imageDetails": [
            {
                "registryId": ACCOUNT,
                "repositoryName": "personal-operator/bridge",
                "imageDigest": digest,
                "imageTags": [tag],
                "imageSizeInBytes": 123456,
            }
        ]
    }
    scan = {
        "registryId": ACCOUNT,
        "repositoryName": "personal-operator/bridge",
        "imageId": {"imageDigest": digest},
        "imageScanStatus": {"status": scan_status},
        "imageScanFindings": {
            "findingSeverityCounts": {"CRITICAL": critical, "HIGH": high}
        },
    }
    profile = (
        f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
        "personal_operator_bridge"
    )
    signing_configuration = {
        "registryId": ACCOUNT,
        "signingConfiguration": {
            "rules": [
                {
                    "signingProfileArn": profile,
                    "repositoryFilters": [
                        {
                            "filter": "personal-operator/bridge",
                            "filterType": "WILDCARD_MATCH",
                        }
                    ],
                }
            ]
        },
    }
    signing_status = {
        "registryId": ACCOUNT,
        "repositoryName": "personal-operator/bridge",
        "imageId": {"imageDigest": digest},
        "signingStatuses": [
            {"signingProfileArn": profile, "status": signature_status}
        ],
    }
    signing_profile = {
        "profileName": "personal_operator_bridge",
        "profileVersion": profile_version,
        "profileVersionArn": profile + "/" + profile_version,
        "platformId": "Notation-OCI-SHA384-ECDSA",
        "signatureValidityPeriod": {"value": 3650, "type": "DAYS"},
        "status": "Active",
        "arn": profile,
    }
    fake.queue("describe_repositories", repository, repository)
    fake.queue("describe_images", image, image)
    final_scan = deepcopy(scan)
    if final_scan_status is not None:
        final_scan["imageScanStatus"]["status"] = final_scan_status
    fake.queue("describe_image_scan_findings", scan, scan, final_scan)
    fake.queue(
        "get_signing_configuration",
        signing_configuration,
        signing_configuration,
    )
    fake.queue(
        "describe_image_signing_status", signing_status, signing_status
    )
    signer.queue("get_signing_profile", signing_profile, signing_profile)


@pytest.fixture(scope="module")
def aggregate_image_capability(tmp_path_factory):
    bundle = _prepare()
    with _verified_image_observe_current(
        tmp_path_factory.mktemp("aggregate-image-observe"), bundle
    ) as observe:
        yield bundle, observe


def test_ecr_blob_observation_distinguishes_exact_present_and_absent() -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(expected_plan_sha256=bundle.plan_sha256)
    index = next(
        index
        for index, effect in enumerate(effects)
        if effect.effect_kind == "ECR_BLOB_PUT"
    )
    effect = effects[index]
    present = {
        "layers": [
            {
                "layerDigest": effect.digest,
                "layerAvailability": "AVAILABLE",
                "layerSize": effect.size,
                "mediaType": effect.media_type,
            }
        ],
        "failures": [],
    }
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue(
                "batch_check_layer_availability", present, present
            )
            observed = observer.observe_image_effect(verified, preflight)
    assert observed.disposition is ObservationDisposition.PRESENT

    missing = {
        "layers": [],
        "failures": [
            {"layerDigest": effect.digest, "failureCode": "MissingLayerDigest"}
        ],
    }
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue(
                "batch_check_layer_availability", missing, missing
            )
            observed = observer.observe_image_effect(verified, preflight)
    assert observed.disposition is ObservationDisposition.ABSENT

    conflict = deepcopy(present)
    conflict["layers"][0]["layerSize"] = effect.size + 1
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue("batch_check_layer_availability", conflict)
            with pytest.raises(ProductionObserverV2Ambiguous):
                observer.observe_image_effect(verified, preflight)


def test_ecr_manifest_requires_exact_payload_digest_and_stable_double_read() -> None:
    bundle = _prepare()
    effects = bundle.publication_effects(expected_plan_sha256=bundle.plan_sha256)
    index = next(
        index
        for index, effect in enumerate(effects)
        if effect.effect_kind == "ECR_SUBJECT_MANIFEST_PUT"
    )
    effect = effects[index]
    response = {
        "images": [
            {
                "registryId": ACCOUNT,
                "repositoryName": "personal-operator/bridge",
                "imageId": {"imageDigest": effect.digest, "imageTag": effect.tag},
                "imageManifest": effect.payload.decode("utf-8"),
                "imageManifestMediaType": effect.media_type,
            }
        ],
        "failures": [],
    }
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue(
                "batch_get_image",
                response,
                response,
                response,
                response,
                response,
                response,
            )
            observed = observer.observe_image_effect(verified, preflight)
    assert observed.disposition is ObservationDisposition.PRESENT
    image_ids = [
        kwargs["imageIds"][0]
        for method, kwargs in fakes["ecr"].calls
        if method == "batch_get_image"
    ]
    assert image_ids == [
        {"imageDigest": effect.digest},
        {"imageDigest": effect.digest},
        {"imageTag": effect.tag},
        {"imageTag": effect.tag},
        {"imageDigest": effect.digest},
        {"imageTag": effect.tag},
    ]

    drifted = deepcopy(response)
    drifted["images"][0]["imageManifest"] = json.dumps({"schemaVersion": 2})
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue("batch_get_image", drifted)
            with pytest.raises(ProductionObserverV2Ambiguous):
                observer.observe_image_effect(verified, preflight)

    collision = deepcopy(response)
    collision["images"][0]["imageId"]["imageDigest"] = "sha256:" + "f" * 64
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue(
                "batch_get_image",
                response,
                response,
                collision,
                collision,
                response,
                collision,
            )
            observed = observer.observe_image_effect(verified, preflight)
    assert observed.disposition is ObservationDisposition.FAILED_RETAINED
    assert observed.provider_status == "IMMUTABLE_SUBJECT_CONFLICT"

    tag_missing = {
        "images": [],
        "failures": [
            {
                "imageId": {"imageTag": effect.tag},
                "failureCode": "ImageNotFound",
            }
        ],
    }
    with _verified_image_effect_current(bundle, index) as (_, verified, preflight):
        with _observer() as (observer, fakes):
            fakes["ecr"].queue(
                "batch_get_image",
                response,
                response,
                tag_missing,
                tag_missing,
                response,
                tag_missing,
            )
            observed = observer.observe_image_effect(verified, preflight)
    assert observed.disposition is ObservationDisposition.ABSENT
    assert observed.provider_status == "TAG_NOT_BOUND"


def test_aggregate_image_observation_closes_exact_release_scan_and_signing(
    aggregate_image_capability,
) -> None:
    bundle, observe = aggregate_image_capability
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"], fakes["signer"], observe
        )
        observed = observer.observe_image_release(observe)

    assert observed.disposition is ObservationDisposition.PRESENT
    assert observed.subject == observe.subject
    assert observed.operation == "describe_image_scan_findings"
    assert observed.projection() == {
        "commitTag": f"commit-{COMMIT}",
        "criticalFindings": 0,
        "highFindings": 0,
        "imageUri": (
            f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
            f"personal-operator/bridge@{bundle.plan.subject.digest}"
        ),
        "provenanceManifestDigest": bundle.plan.provenance_manifest.digest,
        "repositoryName": "personal-operator/bridge",
        "runtimeImageDigest": bundle.plan.subject.digest,
        "sbomManifestDigest": bundle.plan.sbom_manifest.digest,
        "scanStatus": "COMPLETE",
        "signatureStatus": "SIGNED",
        "signingProfileArn": (
            f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
            "personal_operator_bridge"
        ),
    }


@pytest.mark.parametrize(
    ("scan_status", "critical", "high"),
    (
        ("FAILED", 0, 0),
        ("COMPLETE", 1, 0),
        ("COMPLETE", 0, 1),
    ),
)
def test_aggregate_image_terminal_scan_failure_is_retained(
    aggregate_image_capability,
    scan_status: str,
    critical: int,
    high: int,
) -> None:
    _, observe = aggregate_image_capability
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"],
            fakes["signer"],
            observe,
            scan_status=scan_status,
            critical=critical,
            high=high,
        )
        observed = observer.observe_image_release(observe)

    assert observed.disposition is ObservationDisposition.FAILED_RETAINED
    assert observed.provider_status == "SCAN_POLICY_FAILED"
    assert observed.operation == "describe_image_scan_findings"


def test_aggregate_image_pending_and_signing_failure_never_become_present(
    aggregate_image_capability,
) -> None:
    _, observe = aggregate_image_capability
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"],
            fakes["signer"],
            observe,
            scan_status="IN_PROGRESS",
        )
        pending = observer.observe_image_release(observe)
    assert pending.disposition is ObservationDisposition.PENDING
    assert pending.provider_status == "IN_PROGRESS"

    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"],
            fakes["signer"],
            observe,
            signature_status="FAILED",
        )
        failed = observer.observe_image_release(observe)
    assert failed.disposition is ObservationDisposition.FAILED_RETAINED
    assert failed.provider_status == "SIGNATURE_VERIFICATION_FAILED"
    assert failed.operation == "describe_image_signing_status"


def test_aggregate_image_observation_rejects_public_or_crossed_inputs(
    aggregate_image_capability,
) -> None:
    with _observer() as (observer, fakes):
        with pytest.raises(ProductionObserverV2Error, match="capability"):
            observer.observe_image_release(object())
    assert not fakes["ecr"].calls

    _, observe = aggregate_image_capability
    crossed = ProductionObserverV2.__new__(ProductionObserverV2)
    crossed._account = "999999999999"
    crossed._region = REGION
    crossed._clients = {}
    with pytest.raises(ProductionObserverV2Error, match="authority"):
        crossed.observe_image_release(observe)


@pytest.mark.parametrize(
    ("status", "disposition", "provider_status"),
    (
        ("IN_PROGRESS", ObservationDisposition.PENDING, "IN_PROGRESS"),
        (
            "UNSUPPORTED_IMAGE",
            ObservationDisposition.FAILED_RETAINED,
            "SCAN_POLICY_FAILED",
        ),
    ),
)
def test_image_scan_noncomplete_status_does_not_require_findings(
    status: str,
    disposition: ObservationDisposition,
    provider_status: str,
) -> None:
    response = {
        "registryId": ACCOUNT,
        "repositoryName": "personal-operator/bridge",
        "imageId": {"imageDigest": "sha256:" + "c" * 64},
        "imageScanStatus": {"status": status},
    }
    with _observer() as (observer, fakes):
        fakes["ecr"].queue("describe_image_scan_findings", response)
        observed, observed_status, projection = observer._image_scan_projection(
            account=ACCOUNT,
            digest="sha256:" + "c" * 64,
        )
    assert observed is disposition
    assert observed_status == provider_status
    assert projection["criticalFindings"] == 0
    assert projection["highFindings"] == 0


def test_aggregate_image_malformed_tag_inventory_fails_in_domain() -> None:
    digest = "sha256:" + "c" * 64
    response = {
        "imageDetails": [
            {
                "registryId": ACCOUNT,
                "repositoryName": "personal-operator/bridge",
                "imageDigest": digest,
                "imageTags": ["commit-" + COMMIT, 7],
                "imageSizeInBytes": 123,
            }
        ]
    }
    with _observer() as (observer, fakes):
        fakes["ecr"].queue("describe_images", response)
        with pytest.raises(ProductionObserverV2Error, match="image identity"):
            observer._image_detail_projection(
                account=ACCOUNT,
                digest=digest,
                tag="commit-" + COMMIT,
            )


def test_aggregate_image_requires_exact_active_notation_signing_profile() -> None:
    profile = (
        f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
        "personal_operator_bridge"
    )
    response = {
        "profileName": "personal_operator_bridge",
        "profileVersion": "ABCDEFGHIJ",
        "profileVersionArn": profile + "/ABCDEFGHIJ",
        "platformId": "Notation-OCI-SHA384-ECDSA",
        "signatureValidityPeriod": {"value": 3650, "type": "DAYS"},
        "status": "Active",
        "arn": profile,
    }
    with _observer() as (observer, fakes):
        fakes["signer"].queue("get_signing_profile", response)
        assert observer._image_signing_profile(
            account=ACCOUNT,
            profile=profile,
        ) == {
            "signingProfileArn": profile,
            "signingProfileVersion": "ABCDEFGHIJ",
            "signingProfileVersionArn": profile + "/ABCDEFGHIJ",
        }


def test_aggregate_image_does_not_attribute_an_unbound_profile_version(
    aggregate_image_capability,
) -> None:
    _, observe = aggregate_image_capability
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"],
            fakes["signer"],
            observe,
            profile_version="KLMNOPQRST",
        )
        observed = observer.observe_image_release(observe)

    assert observed.disposition is ObservationDisposition.PRESENT
    assert observed.projection()["signingProfileArn"].endswith(
        "/personal_operator_bridge"
    )
    assert "signingProfileVersion" not in observed.projection()
    assert "signingProfileVersionArn" not in observed.projection()


def test_aggregate_image_requires_exactly_one_reviewed_signing_rule() -> None:
    profile = (
        f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
        "personal_operator_bridge"
    )
    response = {
        "registryId": ACCOUNT,
        "signingConfiguration": {
            "rules": [
                {
                    "signingProfileArn": profile,
                    "repositoryFilters": [
                        {
                            "filter": "personal-operator/bridge",
                            "filterType": "WILDCARD_MATCH",
                        }
                    ],
                },
                {
                    "signingProfileArn": profile,
                    "repositoryFilters": [
                        {"filter": "*", "filterType": "WILDCARD_MATCH"}
                    ],
                },
            ]
        },
    }
    with _observer() as (observer, fakes):
        fakes["ecr"].queue("get_signing_configuration", response)
        with pytest.raises(ProductionObserverV2Error, match="configuration"):
            observer._image_signing_configuration(
                account=ACCOUNT,
                profile=profile,
            )


@pytest.mark.parametrize(
    ("status", "failure_fields"),
    (
        ("COMPLETE", {"failureCode": "InternalError"}),
        ("IN_PROGRESS", {"failureReason": "provider contradiction"}),
    ),
)
def test_image_signing_nonfailure_status_rejects_failure_details(
    status: str,
    failure_fields: dict[str, str],
) -> None:
    digest = "sha256:" + "c" * 64
    profile = (
        f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
        "personal_operator_bridge"
    )
    response = {
        "registryId": ACCOUNT,
        "repositoryName": "personal-operator/bridge",
        "imageId": {"imageDigest": digest},
        "signingStatuses": [
            {
                "signingProfileArn": profile,
                "status": status,
                **failure_fields,
            }
        ],
    }
    with _observer() as (observer, fakes):
        fakes["ecr"].queue("describe_image_signing_status", response)
        with pytest.raises(ProductionObserverV2Ambiguous, match="failure"):
            observer._image_signing_projection(
                account=ACCOUNT,
                digest=digest,
                profile=profile,
            )


def test_aggregate_image_final_sweep_detects_referrer_loss_after_signing(
    aggregate_image_capability,
) -> None:
    _, observe = aggregate_image_capability
    referrer = next(
        effect
        for effect in observe.ordered_effects
        if effect.effect_kind == "ECR_SBOM_REFERRER_PUT"
    )
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(
            fakes["ecr"],
            observe,
            missing_final_effect_id=referrer.effect_id,
        )
        _queue_aggregate_release_reads(
            fakes["ecr"], fakes["signer"], observe
        )
        with pytest.raises(ProductionObserverV2Ambiguous, match="closure"):
            observer.observe_image_release(observe)


def test_aggregate_image_final_sweep_detects_scan_drift_after_signing(
    aggregate_image_capability,
) -> None:
    _, observe = aggregate_image_capability
    with _observer() as (observer, fakes):
        _queue_aggregate_effect_reads(fakes["ecr"], observe)
        _queue_aggregate_release_reads(
            fakes["ecr"],
            fakes["signer"],
            observe,
            final_scan_status="FAILED",
        )
        with pytest.raises(ProductionObserverV2Ambiguous, match="scan"):
            observer.observe_image_release(observe)
