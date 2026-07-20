from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from typing import Iterator
from urllib.parse import quote

import pytest

from release_tools.aws_authority_v2 import AttestedAwsClientV2
from release_tools.contracts import FoundationRuntimeInputsV1, canonical_json_bytes
from release_tools.runtime_iam_observer_v2 import (
    CanonicalRuntimeIamObservationV1,
    RuntimeIamObservationRequestV1,
    RuntimeIamObserverV2,
    RuntimeIamObserverV2Ambiguous,
    RuntimeIamObserverV2Error,
    exact_operation_tags,
)
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.transaction import ObservationDisposition
from tests.test_runtime_iam_template import ROLE_NAME, _template


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
STACK_UUID = "01234567-89ab-cdef-0123-456789abcdef"
STACK_ID = (
    f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/"
    f"OpenClawAgentCore/{STACK_UUID}"
)
GUARDRAIL_ID = "guardrail123"
GUARDRAIL_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/{GUARDRAIL_ID}"
GUARDRAIL_EXPORT = (
    "OpenClawGuardrails:"
    "ExportsOutputFnGetAttContentGuardrailGuardrailArnB39948C5"
)


def _reviewed_template() -> dict[str, object]:
    template = _template()
    resources = template["Resources"]
    role_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Type") == "AWS::IAM::Role"
        and resource.get("Properties", {}).get("RoleName") == ROLE_NAME
    )
    policy = next(
        resource
        for resource in resources.values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    policy["Properties"]["PolicyDocument"]["Statement"].append(
        {
            "Action": "bedrock:ApplyGuardrail",
            "Effect": "Allow",
            "Resource": {"Fn::ImportValue": GUARDRAIL_EXPORT},
        }
    )
    return template


def _foundation_mapping(
    *,
    account: str = ACCOUNT,
    region: str = REGION,
    commit: str = COMMIT,
    tree: str = TREE,
    stack_id: str = STACK_ID,
    guardrail_id: str = GUARDRAIL_ID,
) -> dict[str, object]:
    guardrail_arn = (
        f"arn:aws:bedrock:{region}:{account}:guardrail/{guardrail_id}"
        if guardrail_id
        else ""
    )
    return {
        "schema": "personal-operator.foundation-runtime-inputs.v1",
        "sourceCommit": commit,
        "sourceTree": tree,
        "account": account,
        "region": region,
        "releasePlanSha256": "c" * 64,
        "derivationVersion": "foundation-runtime-inputs-v1",
        "privateSubnetIds": [
            "subnet-00000000000000001",
            "subnet-00000000000000002",
        ],
        "runtimeSecurityGroupIds": ["sg-00000000000000001"],
        "userFilesBucketName": f"openclaw-user-files-{account}-{region}",
        "capabilityGatewayFunctionArn": (
            f"arn:aws:lambda:{region}:{account}:function:"
            "personal-operator-capability-gateway"
        ),
        "workspaceBrokerFunctionName": (
            "personal-operator-workspace-credential-broker"
        ),
        "agentCoreStackId": stack_id,
        "guardrailId": guardrail_id,
        "guardrailVersion": "1" if guardrail_id else "",
        "guardrailArn": guardrail_arn,
        "foundationSnapshotSha256": "d" * 64,
    }


def _template_and_role(
    template: dict[str, object] | None = None,
) -> tuple[dict[str, object], str, str, dict[str, object]]:
    template = deepcopy(template) if template is not None else _reviewed_template()
    resources = template["Resources"]
    role_id, role = next(
        (logical_id, resource)
        for logical_id, resource in resources.items()
        if resource.get("Type") == "AWS::IAM::Role"
        and resource.get("Properties", {}).get("RoleName") == ROLE_NAME
    )
    policy = next(
        resource
        for resource in resources.values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    return template, role_id, policy["Properties"]["PolicyName"], policy


def _request_mapping(
    *,
    template: dict[str, object] | None = None,
    account: str = ACCOUNT,
    region: str = REGION,
    commit: str = COMMIT,
    tree: str = TREE,
    stack_id: str = STACK_ID,
    logical_role_id: str | None = None,
    foundation: dict[str, object] | None = None,
) -> dict[str, object]:
    retained = deepcopy(template) if template is not None else _reviewed_template()
    body = canonical_json_bytes(retained).decode("utf-8")
    _, actual_role_id, _, _ = _template_and_role()
    tags = exact_operation_tags(source_commit=commit, source_tree=tree)
    foundation_value = deepcopy(foundation) if foundation is not None else (
        _foundation_mapping(
            account=account,
            region=region,
            commit=commit,
            tree=tree,
            stack_id=stack_id,
        )
    )
    foundation_inputs = FoundationRuntimeInputsV1.from_mapping(foundation_value)
    return {
        "schema": "personal-operator.runtime-iam-observation-request.v1",
        "account": account,
        "region": region,
        "sourceCommit": commit,
        "sourceTree": tree,
        "stackId": stack_id,
        "logicalRoleId": logical_role_id or actual_role_id,
        "reviewedTemplateBody": body,
        "reviewedTemplateSha256": hashlib.sha256(body.encode()).hexdigest(),
        "foundationRuntimeInputs": foundation_inputs.to_mapping(),
        "foundationInputsSha256": foundation_inputs.digest(),
        "operationTagsSha256": hashlib.sha256(
            canonical_json_bytes({"tags": tags})
        ).hexdigest(),
    }


def _request(**kwargs: object) -> RuntimeIamObservationRequestV1:
    return RuntimeIamObservationRequestV1.from_mapping(
        _request_mapping(**kwargs)
    )


def _expected_live_tags(request: RuntimeIamObservationRequestV1) -> list[dict[str, str]]:
    return sorted(
        [
            *exact_operation_tags(
                source_commit=request.source_commit,
                source_tree=request.source_tree,
            ),
            {
                "Key": "aws:cloudformation:logical-id",
                "Value": request.logical_role_id,
            },
            {
                "Key": "aws:cloudformation:stack-id",
                "Value": request.stack_id,
            },
            {
                "Key": "aws:cloudformation:stack-name",
                "Value": "OpenClawAgentCore",
            },
        ],
        key=lambda item: item["Key"],
    )


class FakeIam:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, list[object]] = {
            "get_role": [],
            "list_role_policies": [],
            "get_role_policy": [],
            "list_attached_role_policies": [],
            "list_role_tags": [],
        }
        self.meta = SimpleNamespace(
            region_name="aws-global",
            endpoint_url="https://iam.amazonaws.com",
            service_model=SimpleNamespace(service_name="iam"),
            config=SimpleNamespace(
                region_name="aws-global",
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )

    def queue(self, method: str, *responses: object) -> None:
        self.responses[method].extend(responses)

    def close(self) -> None:
        return None

    def _call(self, method: str, kwargs: dict[str, object]) -> object:
        self.calls.append((method, kwargs))
        if not self.responses[method]:
            raise AssertionError(f"unexpected iam.{method}")
        response = self.responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)

    def get_role(self, **kwargs: object) -> object:
        return self._call("get_role", kwargs)

    def list_role_policies(self, **kwargs: object) -> object:
        return self._call("list_role_policies", kwargs)

    def get_role_policy(self, **kwargs: object) -> object:
        return self._call("get_role_policy", kwargs)

    def list_attached_role_policies(self, **kwargs: object) -> object:
        return self._call("list_attached_role_policies", kwargs)

    def list_role_tags(self, **kwargs: object) -> object:
        return self._call("list_role_tags", kwargs)


def _exact_responses(
    request: RuntimeIamObservationRequestV1,
) -> dict[str, object]:
    return {
        "get_role": {
            "Role": {
                "Path": "/",
                "RoleName": ROLE_NAME,
                "RoleId": "AROAEXACTRUNTIME12345",
                "Arn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}",
                "AssumeRolePolicyDocument": request.expected_trust,
                "MaxSessionDuration": 3600,
            }
        },
        "list_role_policies": {
            "PolicyNames": [request.expected_inline_policy_name],
            "IsTruncated": False,
        },
        "get_role_policy": {
            "RoleName": ROLE_NAME,
            "PolicyName": request.expected_inline_policy_name,
            "PolicyDocument": quote(
                canonical_json_bytes(request.expected_inline_policy).decode(
                    "utf-8"
                ),
                safe="~",
            ),
        },
        "list_attached_role_policies": {
            "AttachedPolicies": [],
            "IsTruncated": False,
        },
        "list_role_tags": {
            "Tags": _expected_live_tags(request),
            "IsTruncated": False,
        },
    }


def _queue_sweeps(
    fake: FakeIam,
    request: RuntimeIamObservationRequestV1,
    *,
    first: dict[str, object] | None = None,
    second: dict[str, object] | None = None,
) -> None:
    baseline = _exact_responses(request)
    for overrides in (first or {}, second or {}):
        values = {**baseline, **overrides}
        for method in (
            "get_role",
            "list_role_policies",
            "get_role_policy",
            "list_attached_role_policies",
            "list_role_tags",
        ):
            if method == "get_role_policy" and not values["list_role_policies"].get(
                "PolicyNames"
            ):
                continue
            fake.queue(method, values[method])


@contextmanager
def _observer(fake: FakeIam) -> Iterator[RuntimeIamObserverV2]:
    with attested_test_client(fake, service="iam") as client:
        assert isinstance(client, AttestedAwsClientV2)
        yield RuntimeIamObserverV2(
            account=ACCOUNT,
            region=REGION,
            iam=client,
        )


def test_request_is_canonical_template_and_subject_bound() -> None:
    request = _request()

    assert request.to_bytes() == canonical_json_bytes(request.to_mapping())
    assert request.digest() == hashlib.sha256(request.to_bytes()).hexdigest()
    assert RuntimeIamObservationRequestV1.from_bytes(
        request.to_bytes()
    ).to_mapping() == request.to_mapping()
    assert request.expected_role_name == ROLE_NAME
    assert request.expected_role_arn == f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
    assert request.expected_path == "/"
    assert request.expected_max_session_duration == 3600
    assert request.foundation_inputs_sha256 == (
        request.foundation_runtime_inputs.digest()
    )
    assert request.foundation_runtime_inputs.guardrail_arn == GUARDRAIL_ARN
    assert request.expected_inline_policy["Statement"][-1] == {
        "Action": "bedrock:ApplyGuardrail",
        "Effect": "Allow",
        "Resource": GUARDRAIL_ARN,
    }
    assert request.expected_inline_policy_name.startswith(
        "OpenClawExecutionRoleDefaultPolicy"
    )

    for field, value in (
        ("account", "999999999999"),
        ("region", "us-east-1"),
        ("sourceCommit", "A" * 40),
        ("sourceTree", "b" * 39),
        (
            "stackId",
            STACK_ID.replace("eu-west-1", "us-east-1"),
        ),
        ("logicalRoleId", "AttackerRole"),
        ("operationTagsSha256", "0" * 64),
        ("reviewedTemplateSha256", "0" * 64),
        ("foundationInputsSha256", "0" * 64),
    ):
        raw = _request_mapping()
        raw[field] = value
        with pytest.raises(RuntimeIamObserverV2Error):
            RuntimeIamObservationRequestV1.from_mapping(raw)

    extra = _request_mapping()
    extra["inlinePolicy"] = {"Statement": [{"Effect": "Allow"}]}
    with pytest.raises(RuntimeIamObserverV2Error, match="fields"):
        RuntimeIamObservationRequestV1.from_mapping(extra)


def test_template_must_be_canonical_static_and_exactly_closed() -> None:
    baseline, role_id, _, _ = _template_and_role()
    mutations: list[dict[str, object]] = []

    for field, value in (
        ("Path", "/wider/"),
        ("MaxSessionDuration", 43200),
        ("Description", "caller selected"),
        (
            "PermissionsBoundary",
            "arn:aws:iam::aws:policy/AdministratorAccess",
        ),
        ("RoleName", {"Fn::Sub": ROLE_NAME}),
    ):
        candidate = deepcopy(baseline)
        candidate["Resources"][role_id]["Properties"][field] = value
        mutations.append(candidate)

    candidate = deepcopy(baseline)
    candidate["Resources"][role_id]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"][0]["Principal"]["Service"] = "lambda.amazonaws.com"
    mutations.append(candidate)

    candidate = deepcopy(baseline)
    candidate["Resources"][role_id]["Properties"]["Tags"] = [
        {"Key": "Attacker", "Value": "widened"}
    ]
    mutations.append(candidate)

    candidate = deepcopy(baseline)
    target_policy = next(
        resource
        for resource in candidate["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    target_policy["Properties"]["PolicyDocument"]["Statement"][0][
        "Resource"
    ] = {"Fn::Sub": "arn:aws:iam::${AWS::AccountId}:policy/attacker"}
    mutations.append(candidate)

    candidate = deepcopy(baseline)
    duplicate = deepcopy(target_policy)
    candidate["Resources"]["DuplicateRuntimePolicy"] = duplicate
    mutations.append(candidate)

    candidate = deepcopy(baseline)
    candidate["Transform"] = "AWS::Serverless-2016-10-31"
    mutations.append(candidate)

    for template in mutations:
        with pytest.raises(RuntimeIamObserverV2Error, match="template"):
            _request(template=template)

    explicitly_tagged = deepcopy(baseline)
    explicitly_tagged["Resources"][role_id]["Properties"]["Tags"] = (
        exact_operation_tags(source_commit=COMMIT, source_tree=TREE)
    )
    assert _request(template=explicitly_tagged).expected_role_name == ROLE_NAME

    raw = _request_mapping()
    raw["reviewedTemplateBody"] = "{\n" + str(
        raw["reviewedTemplateBody"]
    )[1:]
    raw["reviewedTemplateSha256"] = hashlib.sha256(
        str(raw["reviewedTemplateBody"]).encode()
    ).hexdigest()
    with pytest.raises(RuntimeIamObserverV2Error, match="canonical"):
        RuntimeIamObservationRequestV1.from_mapping(raw)


def test_foundation_inputs_are_canonical_digest_and_identity_bound() -> None:
    for mutate in (
        lambda value: value.update(sourceCommit="e" * 40),
        lambda value: value.update(sourceTree="e" * 40),
        lambda value: value.update(account="999999999999"),
        lambda value: value.update(agentCoreStackId=STACK_ID.replace(
            "OpenClawAgentCore", "OpenClawVpc"
        )),
        lambda value: value.update(
            guardrailId="", guardrailVersion="", guardrailArn=""
        ),
    ):
        foundation = _foundation_mapping()
        mutate(foundation)
        raw = _request_mapping()
        raw["foundationRuntimeInputs"] = foundation
        raw["foundationInputsSha256"] = hashlib.sha256(
            canonical_json_bytes(foundation)
        ).hexdigest()
        with pytest.raises(RuntimeIamObserverV2Error, match="foundation"):
            RuntimeIamObservationRequestV1.from_mapping(raw)

    raw = _request_mapping()
    raw["foundationRuntimeInputs"]["guardrailArn"] = (
        "arn:aws:bedrock:eu-west-1:123456789012:guardrail/substituted"
    )
    with pytest.raises(RuntimeIamObserverV2Error, match="foundation"):
        RuntimeIamObservationRequestV1.from_mapping(raw)


def test_only_the_exact_single_guardrail_export_is_resolved() -> None:
    baseline, role_id, _, _ = _template_and_role()
    policy = next(
        resource
        for resource in baseline["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    guardrail = statements[-1]

    wrong_export = deepcopy(baseline)
    wrong_export_policy = next(
        resource
        for resource in wrong_export["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    wrong_export_policy["Properties"]["PolicyDocument"]["Statement"][-1][
        "Resource"
    ] = {"Fn::ImportValue": "AttackerExport"}

    duplicate_import = deepcopy(baseline)
    duplicate_policy = next(
        resource
        for resource in duplicate_import["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    duplicate_policy["Properties"]["PolicyDocument"]["Statement"].append(
        deepcopy(guardrail)
    )

    wrong_position = deepcopy(baseline)
    wrong_position_policy = next(
        resource
        for resource in wrong_position["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    wrong_position_policy["Properties"]["PolicyDocument"]["Statement"][-1][
        "Action"
    ] = "bedrock:InvokeModel"

    missing_import = deepcopy(baseline)
    missing_policy = next(
        resource
        for resource in missing_import["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Policy"
        and {"Ref": role_id}
        in resource.get("Properties", {}).get("Roles", [])
    )
    missing_policy["Properties"]["PolicyDocument"]["Statement"].pop()

    for template in (
        wrong_export,
        duplicate_import,
        wrong_position,
        missing_import,
    ):
        with pytest.raises(RuntimeIamObserverV2Error, match="template"):
            _request(template=template)


def test_raw_client_and_cross_subject_request_are_rejected_before_reads() -> None:
    fake = FakeIam()
    with pytest.raises(RuntimeIamObserverV2Error, match="attested"):
        RuntimeIamObserverV2(account=ACCOUNT, region=REGION, iam=fake)

    wrong = FakeIam()
    wrong.meta.region_name = REGION
    wrong.meta.endpoint_url = "https://s3.eu-west-1.amazonaws.com"
    wrong.meta.service_model.service_name = "s3"
    wrong.meta.config.region_name = REGION
    with attested_test_client(wrong, service="s3") as client:
        with pytest.raises(RuntimeIamObserverV2Error, match="crosses"):
            RuntimeIamObserverV2(
                account=ACCOUNT,
                region=REGION,
                iam=client,
            )

    with _observer(fake) as observer:
        crossed_template = json.loads(
            canonical_json_bytes(_reviewed_template())
            .decode("utf-8")
            .replace(ACCOUNT, "999999999999")
        )
        crossed_foundation = _foundation_mapping(
            account="999999999999",
            stack_id=STACK_ID.replace(ACCOUNT, "999999999999"),
        )
        crossed = _request(
            template=crossed_template,
            account="999999999999",
            stack_id=STACK_ID.replace(ACCOUNT, "999999999999"),
            foundation=crossed_foundation,
        )
        with pytest.raises(RuntimeIamObserverV2Error, match="crosses"):
            observer.observe(crossed)
    assert fake.calls == []


def test_url_encoded_trust_and_inline_documents_are_normalized_exactly() -> None:
    request = _request()
    exact = _exact_responses(request)
    exact["get_role"]["Role"]["AssumeRolePolicyDocument"] = quote(
        canonical_json_bytes(request.expected_trust).decode("utf-8"),
        safe="~",
    )
    fake = FakeIam()
    _queue_sweeps(fake, request, first=exact, second=exact)

    with _observer(fake) as observer:
        result = observer.observe(request)

    assert result.disposition is ObservationDisposition.PRESENT


def test_exact_role_requires_two_complete_sweeps_and_minimizes_evidence() -> None:
    request = _request()
    fake = FakeIam()
    _queue_sweeps(fake, request)

    with _observer(fake) as observer:
        result = observer.observe(request)

    assert isinstance(result, CanonicalRuntimeIamObservationV1)
    assert result.service == "iam"
    assert result.operation == "observe_runtime_role"
    assert result.subject == request.expected_role_arn
    assert result.request_sha256 == request.digest()
    assert result.disposition is ObservationDisposition.PRESENT
    assert result.provider_status == "EXACT_RUNTIME_ROLE"
    assert result.projection() == {
        "account": ACCOUNT,
        "expectedInlinePolicySha256": request.expected_inline_policy_sha256,
        "foundationInputsSha256": request.foundation_inputs_sha256,
        "logicalRoleId": request.logical_role_id,
        "operationTagsSha256": request.operation_tags_sha256,
        "region": REGION,
        "reviewedTemplateSha256": request.reviewed_template_sha256,
        "roleArn": request.expected_role_arn,
        "roleName": ROLE_NAME,
        "snapshotSha256": result.projection()["snapshotSha256"],
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "stackId": STACK_ID,
        "sweeps": 2,
    }
    assert len(result.projection()["snapshotSha256"]) == 64
    assert result.to_bytes() == canonical_json_bytes(result.to_mapping())
    serialized = result.to_bytes()
    assert b"bedrock:InvokeModel" not in serialized
    assert b"AssumeRolePolicyDocument" not in serialized
    assert b"PolicyDocument" not in serialized
    assert fake.calls == [
        ("get_role", {"RoleName": ROLE_NAME}),
        ("list_role_policies", {"RoleName": ROLE_NAME}),
        (
            "get_role_policy",
            {
                "RoleName": ROLE_NAME,
                "PolicyName": request.expected_inline_policy_name,
            },
        ),
        ("list_attached_role_policies", {"RoleName": ROLE_NAME}),
        ("list_role_tags", {"RoleName": ROLE_NAME}),
    ] * 2


@pytest.mark.parametrize(
    "mismatch",
    (
        "trust",
        "policy",
        "tag",
        "managed",
        "path",
        "session",
        "boundary",
        "description",
        "missing_inline",
    ),
)
def test_stable_authority_difference_is_failed_retained(mismatch: str) -> None:
    request = _request()
    responses = _exact_responses(request)
    if mismatch == "trust":
        responses["get_role"]["Role"]["AssumeRolePolicyDocument"]["Statement"][
            0
        ]["Principal"]["Service"] = "lambda.amazonaws.com"
    elif mismatch == "policy":
        responses["get_role_policy"]["PolicyDocument"] = quote(
            '{"Statement":[{"Action":"iam:*","Effect":"Allow","Resource":"*"}],'
            '"Version":"2012-10-17"}',
            safe="~",
        )
    elif mismatch == "tag":
        responses["list_role_tags"]["Tags"][0]["Value"] = "substituted"
    elif mismatch == "managed":
        responses["list_attached_role_policies"]["AttachedPolicies"] = [
            {
                "PolicyName": "AdministratorAccess",
                "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess",
            }
        ]
    elif mismatch == "path":
        responses["get_role"]["Role"]["Path"] = "/wider/"
    elif mismatch == "session":
        responses["get_role"]["Role"]["MaxSessionDuration"] = 43200
    elif mismatch == "boundary":
        responses["get_role"]["Role"]["PermissionsBoundary"] = {
            "PermissionsBoundaryType": "Policy",
            "PermissionsBoundaryArn": (
                "arn:aws:iam::aws:policy/AdministratorAccess"
            ),
        }
    elif mismatch == "description":
        responses["get_role"]["Role"]["Description"] = "unexpected"
    else:
        responses["list_role_policies"]["PolicyNames"] = []

    fake = FakeIam()
    _queue_sweeps(fake, request, first=responses, second=responses)
    with _observer(fake) as observer:
        result = observer.observe(request)

    assert result.disposition is ObservationDisposition.FAILED_RETAINED
    assert result.provider_status == "IAM_RUNTIME_ROLE_MISMATCH"
    assert len(result.projection()["snapshotSha256"]) == 64


def test_policy_and_tag_pagination_are_explicit_and_bounded() -> None:
    request = _request()
    exact = _exact_responses(request)
    fake = FakeIam()
    for _ in range(2):
        fake.queue("get_role", exact["get_role"])
        fake.queue(
            "list_role_policies",
            {"PolicyNames": [], "IsTruncated": True, "Marker": "inline-2"},
            exact["list_role_policies"],
        )
        fake.queue("get_role_policy", exact["get_role_policy"])
        fake.queue(
            "list_attached_role_policies",
            {
                "AttachedPolicies": [],
                "IsTruncated": True,
                "Marker": "managed-2",
            },
            exact["list_attached_role_policies"],
        )
        tags = exact["list_role_tags"]["Tags"]
        fake.queue(
            "list_role_tags",
            {
                "Tags": tags[:3],
                "IsTruncated": True,
                "Marker": "tags-2",
            },
            {"Tags": tags[3:], "IsTruncated": False},
        )

    with _observer(fake) as observer:
        result = observer.observe(request)

    assert result.disposition is ObservationDisposition.PRESENT
    assert (
        "list_role_tags",
        {"RoleName": ROLE_NAME, "Marker": "tags-2"},
    ) in fake.calls
    assert (
        "list_attached_role_policies",
        {"RoleName": ROLE_NAME, "Marker": "managed-2"},
    ) in fake.calls
    assert (
        "list_role_policies",
        {"RoleName": ROLE_NAME, "Marker": "inline-2"},
    ) in fake.calls


@pytest.mark.parametrize(
    "method",
    (
        "list_role_policies",
        "list_attached_role_policies",
        "list_role_tags",
    ),
)
def test_pagination_cycles_are_ambiguous(method: str) -> None:
    request = _request()
    exact = _exact_responses(request)
    fake = FakeIam()
    fake.queue("get_role", exact["get_role"])
    for current in (
        "list_role_policies",
        "list_attached_role_policies",
        "list_role_tags",
    ):
        if current == method:
            key = {
                "list_role_policies": "PolicyNames",
                "list_attached_role_policies": "AttachedPolicies",
                "list_role_tags": "Tags",
            }[current]
            fake.queue(
                current,
                {key: [], "IsTruncated": True, "Marker": "cycle"},
                {key: [], "IsTruncated": True, "Marker": "cycle"},
            )
            break
        fake.queue(current, exact[current])
        if current == "list_role_policies":
            fake.queue("get_role_policy", exact["get_role_policy"])

    with _observer(fake) as observer:
        with pytest.raises(RuntimeIamObserverV2Ambiguous, match="pagination"):
            observer.observe(request)


def test_duplicate_inline_inventory_and_malformed_provider_data_are_ambiguous() -> None:
    request = _request()
    exact = _exact_responses(request)
    duplicate = deepcopy(exact["list_role_policies"])
    duplicate["PolicyNames"] *= 2
    fake = FakeIam()
    fake.queue("get_role", exact["get_role"])
    fake.queue("list_role_policies", duplicate)

    with _observer(fake) as observer:
        with pytest.raises(RuntimeIamObserverV2Ambiguous, match="inline"):
            observer.observe(request)

    malformed = FakeIam()
    malformed.queue("get_role", {"Role": []})
    with _observer(malformed) as observer:
        with pytest.raises(RuntimeIamObserverV2Ambiguous, match="role"):
            observer.observe(request)

    malformed_policy = _exact_responses(request)
    malformed_policy["get_role"]["Role"]["AssumeRolePolicyDocument"] = {
        "Statement": object()
    }
    malformed = FakeIam()
    malformed.queue("get_role", malformed_policy["get_role"])
    with _observer(malformed) as observer:
        with pytest.raises(RuntimeIamObserverV2Ambiguous, match="policy"):
            observer.observe(request)


def test_missing_role_or_provider_error_is_ambiguous() -> None:
    request = _request()
    for response in (
        RuntimeError("NoSuchEntity"),
        {},
    ):
        fake = FakeIam()
        fake.queue("get_role", response)
        with _observer(fake) as observer:
            with pytest.raises(RuntimeIamObserverV2Ambiguous):
                observer.observe(request)


def test_byte_different_complete_sweeps_are_ambiguous() -> None:
    request = _request()
    changed = _exact_responses(request)
    changed["list_role_tags"]["Tags"][0]["Value"] = "changed"
    fake = FakeIam()
    _queue_sweeps(fake, request, second=changed)

    with _observer(fake) as observer:
        with pytest.raises(RuntimeIamObserverV2Ambiguous, match="changed"):
            observer.observe(request)
