"""Exact, two-sweep IAM observation for the AgentCore runtime role.

The observer accepts retained canonical CloudFormation template bytes, never a
caller-selected IAM policy.  It derives the sole runtime-role trust and inline
policy from those bytes and compares two complete IAM inventory sweeps before
emitting a minimized canonical observation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import unquote

from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.contracts import (
    ContractError,
    FoundationRuntimeInputsV1,
    MAX_CONTRACT_BYTES,
    canonical_json_bytes,
    parse_canonical_object,
)
from release_tools.transaction import ObservationDisposition


REQUIRED_REGION = "eu-west-1"
STACK_NAME = "OpenClawAgentCore"
ROLE_NAME = "openclaw-agentcore-execution-role-eu-west-1"
GUARDRAIL_ARN_EXPORT = (
    "OpenClawGuardrails:"
    "ExportsOutputFnGetAttContentGuardrailGuardrailArnB39948C5"
)
MAX_IAM_PAGES = 100
MAX_IAM_ITEMS = 1_000

_ACCOUNT = re.compile(r"[0-9]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOGICAL_ID = re.compile(r"[A-Za-z][A-Za-z0-9]{0,254}")
_STACK_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_POLICY_NAME = re.compile(r"[\w+=,.@-]{1,128}")
_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "account",
        "region",
        "sourceCommit",
        "sourceTree",
        "stackId",
        "logicalRoleId",
        "reviewedTemplateBody",
        "reviewedTemplateSha256",
        "foundationRuntimeInputs",
        "foundationInputsSha256",
        "operationTagsSha256",
    }
)
_REQUEST_TOKEN = object()
_OBSERVATION_TOKEN = object()


class RuntimeIamObserverV2Error(RuntimeError):
    """The retained IAM subject or observer authority is invalid."""


class RuntimeIamObserverV2Ambiguous(RuntimeIamObserverV2Error):
    """IAM did not yield stable, complete authoritative evidence."""


def exact_operation_tags(
    *, source_commit: str, source_tree: str
) -> list[dict[str, str]]:
    """Return the only stack-operation tags accepted by release v2."""

    return [
        {"Key": "SourceCommit", "Value": source_commit},
        {"Key": "SourceTree", "Value": source_tree},
        {"Key": "TransactionId", "Value": f"release_{source_commit}"},
    ]


def _account(value: object) -> str:
    if (
        not isinstance(value, str)
        or _ACCOUNT.fullmatch(value) is None
        or value == "000000000000"
    ):
        raise RuntimeIamObserverV2Error("runtime IAM account is invalid")
    return value


def _region(value: object) -> str:
    if value != REQUIRED_REGION:
        raise RuntimeIamObserverV2Error(
            f"runtime IAM region must be exactly {REQUIRED_REGION}"
        )
    return REQUIRED_REGION


def _commit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise RuntimeIamObserverV2Error(f"runtime IAM {label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RuntimeIamObserverV2Error(f"runtime IAM {label} is invalid")
    return value


def _stack_id(value: object, *, account: str, region: str) -> str:
    prefix = f"arn:aws:cloudformation:{region}:{account}:stack/{STACK_NAME}/"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or _STACK_UUID.fullmatch(value.removeprefix(prefix)) is None
    ):
        raise RuntimeIamObserverV2Error(
            "runtime IAM CloudFormation stack identity is invalid"
        )
    return value


def _logical_id(value: object) -> str:
    if not isinstance(value, str) or _LOGICAL_ID.fullmatch(value) is None:
        raise RuntimeIamObserverV2Error(
            "runtime IAM logical role identity is invalid"
        )
    return value


def _contains_ref(value: object, logical_id: str) -> bool:
    if isinstance(value, Mapping):
        if set(value) == {"Ref"} and value.get("Ref") == logical_id:
            return True
        return any(_contains_ref(item, logical_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ref(item, logical_id) for item in value)
    return False


def _contains_intrinsic(value: object) -> bool:
    if isinstance(value, Mapping):
        if any(key == "Ref" or key.startswith("Fn::") for key in value):
            return True
        return any(_contains_intrinsic(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_intrinsic(item) for item in value)
    return False


def _canonical_mapping_bytes(value: object, *, label: str) -> bytes:
    if not isinstance(value, Mapping):
        raise RuntimeIamObserverV2Error(f"runtime IAM {label} is malformed")
    try:
        payload = canonical_json_bytes(dict(value))
        parse_canonical_object(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise RuntimeIamObserverV2Error(
            f"runtime IAM {label} is not canonicalizable"
        ) from error
    return payload


def _expected_trust(*, account: str, region: str) -> dict[str, object]:
    return {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Condition": {
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account}:*"
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": account},
                },
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
            }
        ],
        "Version": "2012-10-17",
    }


def _derive_template_authority(
    *,
    body: str,
    body_sha256: str,
    account: str,
    region: str,
    source_commit: str,
    source_tree: str,
    logical_role_id: str,
    foundation: FoundationRuntimeInputsV1,
) -> tuple[bytes, str, bytes]:
    encoded = body.encode("utf-8") if isinstance(body, str) else b""
    if not encoded or len(encoded) > MAX_CONTRACT_BYTES:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template body is invalid"
        )
    try:
        template = parse_canonical_object(encoded)
    except ContractError as error:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template is not canonical"
        ) from error
    if hashlib.sha256(encoded).hexdigest() != body_sha256:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template digest differs"
        )
    if "Transform" in template:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template has a dynamic transform"
        )
    resources = template.get("Resources")
    if not isinstance(resources, Mapping):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template resources are malformed"
        )

    matching_roles: list[tuple[str, Mapping[str, Any]]] = []
    for candidate_id, resource in resources.items():
        if not isinstance(candidate_id, str) or not isinstance(resource, Mapping):
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template resource is malformed"
            )
        properties = resource.get("Properties")
        if (
            resource.get("Type") == "AWS::IAM::Role"
            and isinstance(properties, Mapping)
            and properties.get("RoleName") == ROLE_NAME
        ):
            matching_roles.append((candidate_id, resource))
    if len(matching_roles) != 1 or matching_roles[0][0] != logical_role_id:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template has no unique exact role"
        )

    _, role = matching_roles[0]
    if not set(role).issubset(
        {"Type", "Properties", "Metadata", "DeletionPolicy", "UpdateReplacePolicy"}
    ):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template role has dynamic attributes"
        )
    properties = role["Properties"]
    assert isinstance(properties, Mapping)
    required_role_fields = {
        "AssumeRolePolicyDocument",
        "MaxSessionDuration",
        "Path",
        "RoleName",
    }
    if set(properties) not in (
        required_role_fields,
        required_role_fields | {"Tags"},
    ):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template role properties are not exact"
        )
    if (
        properties.get("RoleName") != ROLE_NAME
        or properties.get("Path") != "/"
        or properties.get("MaxSessionDuration") != 3600
        or isinstance(properties.get("MaxSessionDuration"), bool)
    ):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template role defaults are not frozen"
        )
    trust_bytes = _canonical_mapping_bytes(
        properties.get("AssumeRolePolicyDocument"),
        label="reviewed trust policy",
    )
    if trust_bytes != canonical_json_bytes(
        _expected_trust(account=account, region=region)
    ):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template trust is not exact"
        )
    if "Tags" in properties:
        if properties["Tags"] != exact_operation_tags(
            source_commit=source_commit,
            source_tree=source_tree,
        ):
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template tags are not exact"
            )

    policies: list[Mapping[str, Any]] = []
    for resource in resources.values():
        assert isinstance(resource, Mapping)
        if not _contains_ref(resource, logical_role_id):
            continue
        if resource is role:
            continue
        if resource.get("Type") != "AWS::IAM::Policy":
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template dynamically widens role authority"
            )
        policy_properties = resource.get("Properties")
        if (
            not isinstance(policy_properties, Mapping)
            or policy_properties.get("Roles") != [{"Ref": logical_role_id}]
        ):
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template policy target is dynamic"
            )
        policies.append(resource)
    if len(policies) != 1:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template must have one inline policy"
        )
    policy_properties = policies[0].get("Properties")
    assert isinstance(policy_properties, Mapping)
    if set(policy_properties) != {"PolicyDocument", "PolicyName", "Roles"}:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template policy properties are not exact"
        )
    policy_name = policy_properties.get("PolicyName")
    if not isinstance(policy_name, str) or _POLICY_NAME.fullmatch(policy_name) is None:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template policy name is dynamic"
        )
    policy_document = policy_properties.get("PolicyDocument")
    if not isinstance(policy_document, Mapping):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template inline policy is malformed"
        )
    try:
        resolved_policy = parse_canonical_object(
            canonical_json_bytes(dict(policy_document))
        )
    except (ContractError, TypeError, ValueError) as error:
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template inline policy is malformed"
        ) from error
    statements = resolved_policy.get("Statement")
    if not isinstance(statements, list):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template inline policy is malformed"
        )
    import_count = 0
    for statement in statements:
        if not _contains_intrinsic(statement):
            continue
        if (
            not isinstance(statement, dict)
            or set(statement) != {"Action", "Effect", "Resource"}
            or statement.get("Action") != "bedrock:ApplyGuardrail"
            or statement.get("Effect") != "Allow"
            or statement.get("Resource")
            != {"Fn::ImportValue": GUARDRAIL_ARN_EXPORT}
        ):
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template policy contains dynamic values"
            )
        import_count += 1
        statement["Resource"] = foundation.guardrail_arn
    if import_count != 1 or _contains_intrinsic(resolved_policy):
        raise RuntimeIamObserverV2Error(
            "runtime IAM reviewed template must contain one exact guardrail export"
        )
    policy_bytes = _canonical_mapping_bytes(
        resolved_policy,
        label="reviewed inline policy",
    )
    return trust_bytes, policy_name, policy_bytes


@dataclass(frozen=True, slots=True, init=False)
class RuntimeIamObservationRequestV1:
    """Retained template- and CloudFormation-bound runtime IAM subject."""

    SCHEMA = "personal-operator.runtime-iam-observation-request.v1"

    account: str
    region: str
    source_commit: str
    source_tree: str
    stack_id: str
    logical_role_id: str
    reviewed_template_body: str
    reviewed_template_sha256: str
    foundation_runtime_inputs: FoundationRuntimeInputsV1
    foundation_inputs_sha256: str
    operation_tags_sha256: str
    _expected_trust_bytes: bytes
    expected_inline_policy_name: str
    _expected_inline_policy_bytes: bytes

    def __init__(
        self,
        *,
        account: str,
        region: str,
        source_commit: str,
        source_tree: str,
        stack_id: str,
        logical_role_id: str,
        reviewed_template_body: str,
        reviewed_template_sha256: str,
        foundation_runtime_inputs: FoundationRuntimeInputsV1,
        foundation_inputs_sha256: str,
        operation_tags_sha256: str,
        expected_trust_bytes: bytes,
        expected_inline_policy_name: str,
        expected_inline_policy_bytes: bytes,
        _token: object | None = None,
    ) -> None:
        if _token is not _REQUEST_TOKEN:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request is not directly constructible"
            )
        for name, value in (
            ("account", account),
            ("region", region),
            ("source_commit", source_commit),
            ("source_tree", source_tree),
            ("stack_id", stack_id),
            ("logical_role_id", logical_role_id),
            ("reviewed_template_body", reviewed_template_body),
            ("reviewed_template_sha256", reviewed_template_sha256),
            ("foundation_runtime_inputs", foundation_runtime_inputs),
            ("foundation_inputs_sha256", foundation_inputs_sha256),
            ("operation_tags_sha256", operation_tags_sha256),
            ("_expected_trust_bytes", expected_trust_bytes),
            ("expected_inline_policy_name", expected_inline_policy_name),
            ("_expected_inline_policy_bytes", expected_inline_policy_bytes),
        ):
            object.__setattr__(self, name, value)

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "RuntimeIamObservationRequestV1":
        if not isinstance(raw, Mapping) or set(raw) != _REQUEST_FIELDS:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request has the wrong fields"
            )
        if raw.get("schema") != cls.SCHEMA:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request schema is invalid"
            )
        account = _account(raw.get("account"))
        region = _region(raw.get("region"))
        source_commit = _commit(raw.get("sourceCommit"), label="source commit")
        source_tree = _commit(raw.get("sourceTree"), label="source tree")
        stack_id = _stack_id(raw.get("stackId"), account=account, region=region)
        logical_role_id = _logical_id(raw.get("logicalRoleId"))
        template_sha256 = _digest(
            raw.get("reviewedTemplateSha256"),
            label="reviewed template digest",
        )
        operation_tags_sha256 = _digest(
            raw.get("operationTagsSha256"),
            label="operation tags digest",
        )
        expected_tags_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "tags": exact_operation_tags(
                        source_commit=source_commit,
                        source_tree=source_tree,
                    )
                }
            )
        ).hexdigest()
        if operation_tags_sha256 != expected_tags_digest:
            raise RuntimeIamObserverV2Error(
                "runtime IAM operation tags digest differs"
            )
        foundation_sha256 = _digest(
            raw.get("foundationInputsSha256"),
            label="foundation inputs digest",
        )
        try:
            foundation = FoundationRuntimeInputsV1.from_mapping(
                raw.get("foundationRuntimeInputs")
            )
        except (ContractError, TypeError, ValueError) as error:
            raise RuntimeIamObserverV2Error(
                "runtime IAM foundation inputs are malformed"
            ) from error
        if foundation.digest() != foundation_sha256:
            raise RuntimeIamObserverV2Error(
                "runtime IAM foundation inputs digest differs"
            )
        if (
            foundation.source_commit,
            foundation.source_tree,
            foundation.account,
            foundation.region,
            foundation.agent_core_stack_id,
        ) != (
            source_commit,
            source_tree,
            account,
            region,
            stack_id,
        ):
            raise RuntimeIamObserverV2Error(
                "runtime IAM foundation inputs cross the request identity"
            )
        if not foundation.guardrail_arn:
            raise RuntimeIamObserverV2Error(
                "runtime IAM foundation inputs lack the exact guardrail"
            )
        body = raw.get("reviewedTemplateBody")
        if not isinstance(body, str) or "\x00" in body:
            raise RuntimeIamObserverV2Error(
                "runtime IAM reviewed template body is invalid"
            )
        trust_bytes, policy_name, policy_bytes = _derive_template_authority(
            body=body,
            body_sha256=template_sha256,
            account=account,
            region=region,
            source_commit=source_commit,
            source_tree=source_tree,
            logical_role_id=logical_role_id,
            foundation=foundation,
        )
        return cls(
            account=account,
            region=region,
            source_commit=source_commit,
            source_tree=source_tree,
            stack_id=stack_id,
            logical_role_id=logical_role_id,
            reviewed_template_body=body,
            reviewed_template_sha256=template_sha256,
            foundation_runtime_inputs=foundation,
            foundation_inputs_sha256=foundation_sha256,
            operation_tags_sha256=operation_tags_sha256,
            expected_trust_bytes=trust_bytes,
            expected_inline_policy_name=policy_name,
            expected_inline_policy_bytes=policy_bytes,
            _token=_REQUEST_TOKEN,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RuntimeIamObservationRequestV1":
        try:
            raw = parse_canonical_object(payload)
        except ContractError as error:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request bytes are not canonical"
            ) from error
        return cls.from_mapping(raw)

    @property
    def expected_role_name(self) -> str:
        return ROLE_NAME

    @property
    def expected_role_arn(self) -> str:
        return f"arn:aws:iam::{self.account}:role/{ROLE_NAME}"

    @property
    def expected_path(self) -> str:
        return "/"

    @property
    def expected_max_session_duration(self) -> int:
        return 3600

    @property
    def expected_trust(self) -> dict[str, Any]:
        return parse_canonical_object(self._expected_trust_bytes)

    @property
    def expected_inline_policy(self) -> dict[str, Any]:
        return parse_canonical_object(self._expected_inline_policy_bytes)

    @property
    def expected_inline_policy_sha256(self) -> str:
        return hashlib.sha256(self._expected_inline_policy_bytes).hexdigest()

    @property
    def expected_live_tags(self) -> list[dict[str, str]]:
        return sorted(
            [
                *exact_operation_tags(
                    source_commit=self.source_commit,
                    source_tree=self.source_tree,
                ),
                {
                    "Key": "aws:cloudformation:logical-id",
                    "Value": self.logical_role_id,
                },
                {
                    "Key": "aws:cloudformation:stack-id",
                    "Value": self.stack_id,
                },
                {
                    "Key": "aws:cloudformation:stack-name",
                    "Value": STACK_NAME,
                },
            ],
            key=lambda item: item["Key"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "account": self.account,
            "region": self.region,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "stackId": self.stack_id,
            "logicalRoleId": self.logical_role_id,
            "reviewedTemplateBody": self.reviewed_template_body,
            "reviewedTemplateSha256": self.reviewed_template_sha256,
            "foundationRuntimeInputs": self.foundation_runtime_inputs.to_mapping(),
            "foundationInputsSha256": self.foundation_inputs_sha256,
            "operationTagsSha256": self.operation_tags_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class CanonicalRuntimeIamObservationV1:
    """Data-minimized evidence for the exact runtime execution role."""

    SCHEMA = "personal-operator.runtime-iam-observation.v1"

    service: str
    operation: str
    subject: str
    request_sha256: str
    disposition: ObservationDisposition
    provider_status: str
    projection_bytes: bytes

    def __init__(
        self,
        *,
        service: str,
        operation: str,
        subject: str,
        request_sha256: str,
        disposition: ObservationDisposition,
        provider_status: str,
        projection_bytes: bytes,
        _token: object | None = None,
    ) -> None:
        if _token is not _OBSERVATION_TOKEN:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation is not directly constructible"
            )
        for name, value in (
            ("service", service),
            ("operation", operation),
            ("subject", subject),
            ("request_sha256", request_sha256),
            ("disposition", disposition),
            ("provider_status", provider_status),
            ("projection_bytes", projection_bytes),
        ):
            object.__setattr__(self, name, value)

    def projection(self) -> dict[str, Any]:
        return parse_canonical_object(self.projection_bytes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "service": self.service,
            "operation": self.operation,
            "subject": self.subject,
            "requestSha256": self.request_sha256,
            "disposition": self.disposition.value,
            "providerStatus": self.provider_status,
            "projection": self.projection(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _decode_policy_document(value: object, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        try:
            payload = _canonical_mapping_bytes(value, label=label)
        except RuntimeIamObserverV2Error as error:
            raise RuntimeIamObserverV2Ambiguous(
                f"IAM {label} is malformed"
            ) from error
    elif isinstance(value, str) and value and len(value) <= MAX_CONTRACT_BYTES * 3:
        try:
            decoded = unquote(value).encode("utf-8")
            if not decoded or len(decoded) > MAX_CONTRACT_BYTES:
                raise ValueError("decoded policy document exceeds its bound")

            def reject_duplicates(
                pairs: list[tuple[str, Any]],
            ) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("duplicate policy document key")
                    result[key] = item
                return result

            parsed = json.loads(
                decoded.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("non-finite policy document number")
                ),
            )
            if not isinstance(parsed, Mapping):
                raise ValueError("policy document is not an object")
            payload = canonical_json_bytes(parsed)
        except (
            ContractError,
            json.JSONDecodeError,
            UnicodeError,
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeIamObserverV2Ambiguous(
                f"IAM {label} is malformed"
            ) from error
    else:
        raise RuntimeIamObserverV2Ambiguous(f"IAM {label} is malformed")
    try:
        return parse_canonical_object(payload)
    except ContractError as error:
        raise RuntimeIamObserverV2Ambiguous(
            f"IAM {label} is malformed"
        ) from error


def _response_mapping(response: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise RuntimeIamObserverV2Ambiguous(f"IAM {label} response is malformed")
    return response


class RuntimeIamObserverV2:
    """Observe the exact runtime IAM role twice through attested read authority."""

    def __init__(self, *, account: str, region: str, iam: object) -> None:
        self._account = _account(account)
        self._region = _region(region)
        if not isinstance(iam, AttestedAwsClientV2):
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation requires an attested AWS client"
            )
        try:
            iam.require_scope(
                service="iam",
                account=self._account,
                region=self._region,
                capability="observer",
            )
        except AwsAuthorityError as error:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation client crosses its exact subject"
            ) from error
        self._iam = iam

    def observe(
        self, request: RuntimeIamObservationRequestV1
    ) -> CanonicalRuntimeIamObservationV1:
        if not isinstance(request, RuntimeIamObservationRequestV1):
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request is not canonical"
            )
        try:
            request = RuntimeIamObservationRequestV1.from_bytes(
                request.to_bytes()
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            RuntimeIamObserverV2Error,
        ) as error:
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request is not canonical"
            ) from error
        if (request.account, request.region) != (self._account, self._region):
            raise RuntimeIamObserverV2Error(
                "runtime IAM observation request crosses its exact AWS subject"
            )
        first = self._sweep(request)
        second = self._sweep(request)
        first_bytes = canonical_json_bytes(first)
        second_bytes = canonical_json_bytes(second)
        if first_bytes != second_bytes:
            raise RuntimeIamObserverV2Ambiguous(
                "IAM runtime role changed between complete sweeps"
            )
        exact = self._matches_expected(request, first)
        projection = {
            "account": request.account,
            "expectedInlinePolicySha256": (
                request.expected_inline_policy_sha256
            ),
            "foundationInputsSha256": request.foundation_inputs_sha256,
            "logicalRoleId": request.logical_role_id,
            "operationTagsSha256": request.operation_tags_sha256,
            "region": request.region,
            "reviewedTemplateSha256": request.reviewed_template_sha256,
            "roleArn": request.expected_role_arn,
            "roleName": request.expected_role_name,
            "snapshotSha256": hashlib.sha256(first_bytes).hexdigest(),
            "sourceCommit": request.source_commit,
            "sourceTree": request.source_tree,
            "stackId": request.stack_id,
            "sweeps": 2,
        }
        projection_bytes = canonical_json_bytes(projection)
        return CanonicalRuntimeIamObservationV1(
            service="iam",
            operation="observe_runtime_role",
            subject=request.expected_role_arn,
            request_sha256=request.digest(),
            disposition=(
                ObservationDisposition.PRESENT
                if exact
                else ObservationDisposition.FAILED_RETAINED
            ),
            provider_status=(
                "EXACT_RUNTIME_ROLE"
                if exact
                else "IAM_RUNTIME_ROLE_MISMATCH"
            ),
            projection_bytes=projection_bytes,
            _token=_OBSERVATION_TOKEN,
        )

    def _invoke(self, method: str, **kwargs: object) -> object:
        try:
            return self._iam.invoke(method, **kwargs)
        except Exception as error:
            raise RuntimeIamObserverV2Ambiguous(
                f"IAM {method} failed"
            ) from error

    def _sweep(self, request: RuntimeIamObservationRequestV1) -> dict[str, Any]:
        role = self._get_role(request)
        inline_names = self._list_inline_policy_names(request)
        inline = [
            self._get_inline_policy(request, policy_name)
            for policy_name in inline_names
        ]
        managed = self._list_attached_policies(request)
        tags = self._list_role_tags(request)
        return {
            "inlinePolicies": inline,
            "managedPolicies": managed,
            "role": role,
            "tags": tags,
        }

    def _get_role(self, request: RuntimeIamObservationRequestV1) -> dict[str, Any]:
        response = _response_mapping(
            self._invoke("get_role", RoleName=request.expected_role_name),
            label="get_role",
        )
        if not set(response).issubset({"Role", "ResponseMetadata"}):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM get_role response is malformed"
            )
        role = response.get("Role")
        if not isinstance(role, Mapping):
            raise RuntimeIamObserverV2Ambiguous("IAM role response is malformed")
        allowed = {
            "Path",
            "RoleName",
            "RoleId",
            "Arn",
            "CreateDate",
            "AssumeRolePolicyDocument",
            "Description",
            "MaxSessionDuration",
            "PermissionsBoundary",
            "Tags",
            "RoleLastUsed",
        }
        required = {
            "Path",
            "RoleName",
            "Arn",
            "AssumeRolePolicyDocument",
            "MaxSessionDuration",
        }
        if not required.issubset(role) or not set(role).issubset(allowed):
            raise RuntimeIamObserverV2Ambiguous("IAM role response is malformed")
        path = role.get("Path")
        name = role.get("RoleName")
        arn = role.get("Arn")
        duration = role.get("MaxSessionDuration")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(name, str)
            or not name
            or not isinstance(arn, str)
            or not arn
            or not isinstance(duration, int)
            or isinstance(duration, bool)
        ):
            raise RuntimeIamObserverV2Ambiguous("IAM role response is malformed")
        embedded_tags: list[dict[str, str]] | None = None
        if "Tags" in role:
            embedded_tags = self._normalize_tags(role.get("Tags"))
        if "Description" in role and not isinstance(role.get("Description"), str):
            raise RuntimeIamObserverV2Ambiguous("IAM role response is malformed")
        if "PermissionsBoundary" in role and not isinstance(
            role.get("PermissionsBoundary"), Mapping
        ):
            raise RuntimeIamObserverV2Ambiguous("IAM role response is malformed")
        return {
            "arn": arn,
            "descriptionPresent": "Description" in role,
            "embeddedTags": embedded_tags,
            "maxSessionDuration": duration,
            "path": path,
            "permissionsBoundaryPresent": "PermissionsBoundary" in role,
            "roleName": name,
            "trust": _decode_policy_document(
                role.get("AssumeRolePolicyDocument"),
                label="assume-role policy",
            ),
        }

    def _page_inventory(
        self,
        *,
        method: str,
        role_name: str,
        inventory_key: str,
    ) -> list[object]:
        marker: str | None = None
        seen_markers: set[str] = set()
        items: list[object] = []
        for _ in range(MAX_IAM_PAGES):
            kwargs: dict[str, object] = {"RoleName": role_name}
            if marker is not None:
                kwargs["Marker"] = marker
            response = _response_mapping(
                self._invoke(method, **kwargs),
                label=method,
            )
            allowed = {
                inventory_key,
                "IsTruncated",
                "Marker",
                "ResponseMetadata",
            }
            if not set(response).issubset(allowed):
                raise RuntimeIamObserverV2Ambiguous(
                    f"IAM {method} response is malformed"
                )
            page = response.get(inventory_key)
            truncated = response.get("IsTruncated")
            if not isinstance(page, list) or not isinstance(truncated, bool):
                raise RuntimeIamObserverV2Ambiguous(
                    f"IAM {method} response is malformed"
                )
            items.extend(page)
            if len(items) > MAX_IAM_ITEMS:
                raise RuntimeIamObserverV2Ambiguous(
                    f"IAM {method} inventory exceeds its bound"
                )
            next_marker = response.get("Marker")
            if not truncated:
                if next_marker is not None:
                    raise RuntimeIamObserverV2Ambiguous(
                        f"IAM {method} pagination is malformed"
                    )
                return items
            if (
                not isinstance(next_marker, str)
                or not next_marker
                or "\x00" in next_marker
                or len(next_marker) > 1_024
                or next_marker in seen_markers
            ):
                raise RuntimeIamObserverV2Ambiguous(
                    f"IAM {method} pagination cycle or marker is invalid"
                )
            seen_markers.add(next_marker)
            marker = next_marker
        raise RuntimeIamObserverV2Ambiguous(
            f"IAM {method} pagination exceeds its page bound"
        )

    def _list_inline_policy_names(
        self, request: RuntimeIamObservationRequestV1
    ) -> list[str]:
        raw = self._page_inventory(
            method="list_role_policies",
            role_name=request.expected_role_name,
            inventory_key="PolicyNames",
        )
        if any(
            not isinstance(item, str) or _POLICY_NAME.fullmatch(item) is None
            for item in raw
        ):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM inline policy inventory is malformed"
            )
        names = [str(item) for item in raw]
        if len(set(names)) != len(names):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM inline policy inventory has duplicates"
            )
        return sorted(names)

    def _get_inline_policy(
        self,
        request: RuntimeIamObservationRequestV1,
        policy_name: str,
    ) -> dict[str, Any]:
        response = _response_mapping(
            self._invoke(
                "get_role_policy",
                RoleName=request.expected_role_name,
                PolicyName=policy_name,
            ),
            label="get_role_policy",
        )
        if (
            not set(response).issubset(
                {"RoleName", "PolicyName", "PolicyDocument", "ResponseMetadata"}
            )
            or response.get("RoleName") != request.expected_role_name
            or response.get("PolicyName") != policy_name
            or "PolicyDocument" not in response
        ):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM inline policy response is malformed"
            )
        return {
            "policyDocument": _decode_policy_document(
                response.get("PolicyDocument"),
                label="inline policy document",
            ),
            "policyName": policy_name,
        }

    def _list_attached_policies(
        self, request: RuntimeIamObservationRequestV1
    ) -> list[dict[str, str]]:
        raw = self._page_inventory(
            method="list_attached_role_policies",
            role_name=request.expected_role_name,
            inventory_key="AttachedPolicies",
        )
        result: list[dict[str, str]] = []
        for item in raw:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"PolicyName", "PolicyArn"}
                or not isinstance(item.get("PolicyName"), str)
                or _POLICY_NAME.fullmatch(item["PolicyName"]) is None
                or not isinstance(item.get("PolicyArn"), str)
                or not item["PolicyArn"].startswith("arn:aws:iam::")
            ):
                raise RuntimeIamObserverV2Ambiguous(
                    "IAM managed policy inventory is malformed"
                )
            result.append(
                {
                    "policyArn": item["PolicyArn"],
                    "policyName": item["PolicyName"],
                }
            )
        if len({item["policyArn"] for item in result}) != len(result):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM managed policy inventory has duplicates"
            )
        return sorted(result, key=lambda item: (item["policyArn"], item["policyName"]))

    def _normalize_tags(self, value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise RuntimeIamObserverV2Ambiguous("IAM tag inventory is malformed")
        result: list[dict[str, str]] = []
        for item in value:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"Key", "Value"}
                or not isinstance(item.get("Key"), str)
                or not item["Key"]
                or "\x00" in item["Key"]
                or not isinstance(item.get("Value"), str)
                or "\x00" in item["Value"]
            ):
                raise RuntimeIamObserverV2Ambiguous("IAM tag inventory is malformed")
            result.append({"Key": item["Key"], "Value": item["Value"]})
        if len({item["Key"] for item in result}) != len(result):
            raise RuntimeIamObserverV2Ambiguous(
                "IAM tag inventory has duplicate keys"
            )
        return sorted(result, key=lambda item: item["Key"])

    def _list_role_tags(
        self, request: RuntimeIamObservationRequestV1
    ) -> list[dict[str, str]]:
        return self._normalize_tags(
            self._page_inventory(
                method="list_role_tags",
                role_name=request.expected_role_name,
                inventory_key="Tags",
            )
        )

    @staticmethod
    def _matches_expected(
        request: RuntimeIamObservationRequestV1,
        snapshot: Mapping[str, Any],
    ) -> bool:
        role = snapshot.get("role")
        inline = snapshot.get("inlinePolicies")
        if not isinstance(role, Mapping) or not isinstance(inline, list):
            return False
        embedded_tags = role.get("embeddedTags")
        return (
            role.get("arn") == request.expected_role_arn
            and role.get("roleName") == request.expected_role_name
            and role.get("path") == request.expected_path
            and role.get("maxSessionDuration")
            == request.expected_max_session_duration
            and role.get("descriptionPresent") is False
            and role.get("permissionsBoundaryPresent") is False
            and role.get("trust") == request.expected_trust
            and embedded_tags in (None, request.expected_live_tags)
            and inline
            == [
                {
                    "policyDocument": request.expected_inline_policy,
                    "policyName": request.expected_inline_policy_name,
                }
            ]
            and snapshot.get("managedPolicies") == []
            and snapshot.get("tags") == request.expected_live_tags
        )


__all__ = [
    "CanonicalRuntimeIamObservationV1",
    "RuntimeIamObservationRequestV1",
    "RuntimeIamObserverV2",
    "RuntimeIamObserverV2Ambiguous",
    "RuntimeIamObserverV2Error",
    "exact_operation_tags",
]
