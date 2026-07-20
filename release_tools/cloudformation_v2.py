"""Closed, SDK-injected CloudFormation mutation operations for release v2.

The module deliberately has no filesystem, credential, process, or observation
authority.  A caller supplies one canonically validated operation and an
already account-checked regional client.  Provider output is acknowledgement
only; authoritative live evidence is collected elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.contracts import (
    ContractError,
    MAX_CONTRACT_BYTES,
    ReleasePlanV2,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    parse_canonical_object,
)


REQUIRED_REGION = "eu-west-1"
BOOTSTRAP_STACK = "CDKToolkit"
FOUNDATION_STACKS = (
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
)
CONSUMER_STACKS = (
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
)
_ACCOUNT = re.compile(r"[0-9]{12}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPERATION = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_RUNTIME_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_PARAMETER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]{0,254}")
_KINDS = frozenset(
    {
        "BOOTSTRAP_STACK",
        "STACK_CREATE",
        "STACK_UPDATE",
        "CHANGESET_CREATE",
        "CHANGESET_EXECUTE",
    }
)
_FIELDS = {
    "schema",
    "kind",
    "account",
    "region",
    "sourceCommit",
    "sourceTree",
    "stackName",
    "changeSetName",
    "templateBody",
    "templateUrl",
    "reviewedTemplateBody",
    "templateAssetId",
    "templateContentSha256",
    "expectedTemplateParameterSha256",
    "expectedObservedRequestSha256",
    "parameters",
    "capabilities",
    "tags",
}


class CloudFormationMutationError(RuntimeError):
    """The requested CloudFormation operation is not closed and canonical."""


class CloudFormationMutationAmbiguous(CloudFormationMutationError):
    """A provider call may have taken effect and requires live reconciliation."""


class CloudFormationClient(Protocol):
    def create_stack(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def update_stack(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def create_change_set(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def execute_change_set(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _exact_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CloudFormationMutationError(f"{label} is invalid")
    return value


def _operation_token(operation_sha256: str) -> str:
    if _OPERATION.fullmatch(operation_sha256) is None:
        raise CloudFormationMutationError("operation digest is invalid")
    return "po-" + operation_sha256.removeprefix("sha256:")


def _reviewed_template(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise CloudFormationMutationError(
            "reviewed CloudFormation template is not JSON"
        ) from error
    if not isinstance(parsed, Mapping):
        raise CloudFormationMutationError(
            "reviewed CloudFormation template is not an object"
        )
    try:
        canonical_json_bytes(parsed)
    except (TypeError, ValueError) as error:
        raise CloudFormationMutationError(
            "reviewed CloudFormation template cannot be canonicalized"
        ) from error
    return dict(parsed)


def _template_parameter_digest(
    template: Mapping[str, Any],
    parameters: tuple[tuple[str, str], ...],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "parameters": _planned_observed_parameters(
                    template,
                    parameters,
                ),
                "template": dict(template),
            }
        )
    ).hexdigest()


def _planned_observed_parameters(
    template: Mapping[str, Any],
    parameters: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    raw_definitions = template.get("Parameters", {})
    if not isinstance(raw_definitions, Mapping):
        raise CloudFormationMutationError(
            "reviewed template parameter definitions are malformed"
        )
    explicit = dict(parameters)
    if len(explicit) != len(parameters):
        raise CloudFormationMutationError(
            "reviewed template parameters are not unique"
        )
    unknown = set(explicit) - set(raw_definitions)
    if unknown:
        raise CloudFormationMutationError(
            "operation supplies an unknown reviewed template parameter"
        )
    result: list[dict[str, str]] = []
    for key in sorted(raw_definitions):
        definition = raw_definitions[key]
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(definition, Mapping)
        ):
            raise CloudFormationMutationError(
                "reviewed template parameter definition is invalid"
            )
        value = explicit.get(key, definition.get("Default"))
        if not isinstance(value, str):
            raise CloudFormationMutationError(
                "reviewed template parameter lacks an exact value"
            )
        item = {"ParameterKey": key, "ParameterValue": value}
        parameter_type = definition.get("Type")
        if (
            isinstance(parameter_type, str)
            and parameter_type.startswith("AWS::SSM::Parameter::Value<")
        ):
            if (
                key != "BootstrapVersion"
                or parameter_type != "AWS::SSM::Parameter::Value<String>"
                or value != "/cdk-bootstrap/hnb659fds/version"
            ):
                raise CloudFormationMutationError(
                    "reviewed template has an unbound SSM parameter"
                )
            item["ResolvedValue"] = "6"
        result.append(item)
    return result


def _observed_request_projection_digest(
    *,
    kind: str,
    stack_name: str,
    change_set_name: str,
    template: Mapping[str, Any] | None,
    capabilities: tuple[str, ...],
    tags: tuple[tuple[str, str], ...],
) -> str:
    if kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "STACK_UPDATE"}:
        if template is None:
            raise CloudFormationMutationError(
                "stack request projection lacks its reviewed template"
            )
        description = template.get("Description", "")
        if not isinstance(description, str):
            raise CloudFormationMutationError(
                "reviewed template description is invalid"
            )
        projection: Mapping[str, Any] = {
            "stackName": stack_name,
            "description": description,
            "roleArn": "",
            "timeoutInMinutes": 0,
            "capabilities": sorted(capabilities),
            "notificationArns": [],
            "tags": [
                {"Key": key, "Value": value}
                for key, value in sorted(tags)
            ],
            "rollbackConfiguration": {},
            "deploymentConfig": {},
            "disableRollback": True,
            "enableTerminationProtection": True,
            "retainExceptOnCreate": False,
        }
    elif kind == "CHANGESET_CREATE":
        projection = {
            "stackName": stack_name,
            "changeSetName": change_set_name,
            "changeSetType": "CREATE",
            "description": "Personal Operator release "
            + tags[-1][1].removeprefix("release_") if tags else "",
            "roleArn": "",
            "capabilities": sorted(capabilities),
            "notificationArns": [],
            "tags": [
                {"Key": key, "Value": value}
                for key, value in sorted(tags)
            ],
            "rollbackConfiguration": {},
            "deploymentConfig": {},
            "deploymentMode": "",
            "includeNestedStacks": False,
            "onStackFailure": "DO_NOTHING",
            "importExistingResources": False,
        }
    else:
        projection = {
            "stackName": stack_name,
            "changeSetName": change_set_name,
            "changeSetType": "CREATE",
            "executionOnly": True,
            "roleArn": "",
        }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def minimal_bootstrap_template_body(
    *, account: str, region: str, source_commit: str, source_tree: str
) -> str:
    """Build the only bootstrap accepted by the direct v2 release.

    The direct release never assumes CDK publishing, deployment, or lookup
    roles. Its bootstrap therefore owns only the retained encrypted file-asset
    bucket and the version parameter referenced by synthesized CDK templates.
    """

    bucket_name = f"cdk-hnb659fds-assets-{account}-{region}"
    bucket_arn = {"Fn::GetAtt": ["StagingBucket", "Arn"]}
    object_arn = {"Fn::Join": ["", [bucket_arn, "/*"]]}
    tags = [
        {"Key": "SourceCommit", "Value": source_commit},
        {"Key": "SourceTree", "Value": source_tree},
        {"Key": "TransactionId", "Value": f"release_{source_commit}"},
    ]
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Personal Operator minimal direct-release asset bootstrap",
        "Resources": {
            "BootstrapVersion": {
                "Type": "AWS::SSM::Parameter",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "Name": "/cdk-bootstrap/hnb659fds/version",
                    "Type": "String",
                    "Value": "6",
                    "Description": (
                        "Minimum CDK bootstrap contract used by the direct "
                        "Personal Operator release"
                    ),
                    "Tags": {item["Key"]: item["Value"] for item in tags},
                },
            },
            "StagingBucket": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "BucketName": bucket_name,
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {
                                "ServerSideEncryptionByDefault": {
                                    "SSEAlgorithm": "aws:kms"
                                },
                                "BucketKeyEnabled": True,
                            }
                        ]
                    },
                    "OwnershipControls": {
                        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
                    },
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "Id": "AbortIncompleteMultipartUploads",
                                "Status": "Enabled",
                                "AbortIncompleteMultipartUpload": {
                                    "DaysAfterInitiation": 1
                                },
                            }
                        ]
                    },
                    "Tags": tags,
                },
            },
            "StagingBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": {"Ref": "StagingBucket"},
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "DenyInsecureTransport",
                                "Effect": "Deny",
                                "Principal": "*",
                                "Action": "s3:*",
                                "Resource": [bucket_arn, object_arn],
                                "Condition": {
                                    "Bool": {"aws:SecureTransport": "false"}
                                },
                            },
                            {
                                "Sid": "DenyWrongAssetEncryption",
                                "Effect": "Deny",
                                "Principal": "*",
                                "Action": "s3:PutObject",
                                "Resource": object_arn,
                                "Condition": {
                                    "StringNotEquals": {
                                        "s3:x-amz-server-side-encryption": "aws:kms"
                                    }
                                },
                            },
                        ],
                    },
                },
            },
        },
        "Outputs": {
            "BucketName": {"Value": {"Ref": "StagingBucket"}},
            "BootstrapVersion": {"Value": {"Ref": "BootstrapVersion"}},
        },
    }
    return canonical_json_bytes(template).decode("utf-8")


def _validate_sdk_client(
    client: object,
    *,
    service: str,
    region: str,
    account: str,
) -> AttestedAwsClientV2:
    if not isinstance(client, AttestedAwsClientV2):
        raise CloudFormationMutationError(
            "CloudFormation mutation requires an attested AWS client"
        )
    try:
        client.require_scope(
            service=service,
            account=account,
            region=region,
            capability="mutation",
        )
    except AwsAuthorityError as error:
        raise CloudFormationMutationError(
            "CloudFormation attested client crosses its exact subject"
        ) from error
    return client


@dataclass(frozen=True, slots=True)
class CloudFormationOperationV2:
    SCHEMA = "personal-operator.cloudformation-operation.v2"

    kind: str
    account: str
    region: str
    source_commit: str
    source_tree: str
    stack_name: str
    change_set_name: str
    template_body: str
    template_url: str
    reviewed_template_body: str
    template_asset_id: str
    template_content_sha256: str
    expected_template_parameter_sha256: str
    expected_observed_request_sha256: str
    parameters: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...]
    tags: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CloudFormationOperationV2":
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise CloudFormationMutationError(
                "CloudFormation operation fields are not exact"
            )
        if raw["schema"] != cls.SCHEMA:
            raise CloudFormationMutationError(
                "CloudFormation operation schema is invalid"
            )
        kind = _exact_text(raw["kind"], label="operation kind")
        if kind not in _KINDS:
            raise CloudFormationMutationError("operation kind is not closed")
        account = _exact_text(raw["account"], label="account")
        if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
            raise CloudFormationMutationError("account subject is invalid")
        region = _exact_text(raw["region"], label="region")
        if region != REQUIRED_REGION:
            raise CloudFormationMutationError(
                f"region must be exactly {REQUIRED_REGION}"
            )
        commit = _exact_text(raw["sourceCommit"], label="source commit")
        tree = _exact_text(raw["sourceTree"], label="source tree")
        if _COMMIT.fullmatch(commit) is None or _COMMIT.fullmatch(tree) is None:
            raise CloudFormationMutationError("source identity is invalid")
        stack = _exact_text(raw["stackName"], label="stack name")
        allowed_stacks = {
            "BOOTSTRAP_STACK": {BOOTSTRAP_STACK},
            "STACK_CREATE": set(FOUNDATION_STACKS),
            "STACK_UPDATE": {"OpenClawAgentCore"},
            "CHANGESET_CREATE": set(CONSUMER_STACKS),
            "CHANGESET_EXECUTE": set(CONSUMER_STACKS),
        }[kind]
        if stack not in allowed_stacks:
            raise CloudFormationMutationError("stack subject is not closed")

        change_set = raw["changeSetName"]
        if not isinstance(change_set, str) or "\x00" in change_set:
            raise CloudFormationMutationError("change set name is invalid")
        expects_change_set = kind in {"CHANGESET_CREATE", "CHANGESET_EXECUTE"}
        expected_change_set = f"release-{commit}" if expects_change_set else ""
        if change_set != expected_change_set:
            raise CloudFormationMutationError(
                "change set name is not exact and commit-bound"
            )

        template_body = raw["templateBody"]
        template_url = raw["templateUrl"]
        reviewed_template_body = raw["reviewedTemplateBody"]
        template_asset_id = raw["templateAssetId"]
        template_content_sha256 = raw["templateContentSha256"]
        expected_template_parameter_sha256 = raw[
            "expectedTemplateParameterSha256"
        ]
        expected_observed_request_sha256 = raw[
            "expectedObservedRequestSha256"
        ]
        if not all(
            isinstance(value, str) and "\x00" not in value
            for value in (
                template_body,
                template_url,
                reviewed_template_body,
                template_asset_id,
                template_content_sha256,
                expected_template_parameter_sha256,
                expected_observed_request_sha256,
            )
        ):
            raise CloudFormationMutationError("template binding is invalid")
        expects_template = kind != "CHANGESET_EXECUTE"
        if not expects_template:
            if (
                template_body
                or template_url
                or reviewed_template_body
                or template_asset_id
                or template_content_sha256
                or expected_template_parameter_sha256
            ):
                raise CloudFormationMutationError(
                    "change set execution must not carry a template"
                )
        elif kind == "BOOTSTRAP_STACK":
            expected_body = minimal_bootstrap_template_body(
                account=account,
                region=region,
                source_commit=commit,
                source_tree=tree,
            )
            if (
                template_body != expected_body
                or template_url
                or reviewed_template_body != expected_body
                or template_asset_id
                or hashlib.sha256(template_body.encode("utf-8")).hexdigest()
                != template_content_sha256
            ):
                raise CloudFormationMutationError(
                    "bootstrap template is not the pinned minimal release template"
                )
        else:
            if (
                _SHA256.fullmatch(template_asset_id) is None
                or _SHA256.fullmatch(template_content_sha256) is None
            ):
                raise CloudFormationMutationError(
                    "template asset or content digest is invalid"
                )
            expected_url = (
                f"https://cdk-hnb659fds-assets-{account}-{region}.s3."
                f"{region}.amazonaws.com/"
                f"{template_asset_id}.json"
            )
            if template_body or template_url != expected_url:
                raise CloudFormationMutationError(
                    "template URL crosses its exact account, region, or content subject"
                )
            if (
                not reviewed_template_body
                or hashlib.sha256(
                    reviewed_template_body.encode("utf-8")
                ).hexdigest()
                != template_content_sha256
            ):
                raise CloudFormationMutationError(
                    "reviewed template content differs from its exact digest"
                )
        expects_template_parameter = kind in {
            "BOOTSTRAP_STACK",
            "STACK_CREATE",
            "CHANGESET_CREATE",
        }
        if bool(expected_template_parameter_sha256) != expects_template_parameter:
            raise CloudFormationMutationError(
                "expected template and parameter binding is incomplete"
            )
        if (
            expected_template_parameter_sha256
            and _SHA256.fullmatch(expected_template_parameter_sha256) is None
        ):
            raise CloudFormationMutationError(
                "expected template and parameter digest is invalid"
            )
        if _SHA256.fullmatch(expected_observed_request_sha256) is None:
            raise CloudFormationMutationError(
                "expected observed request digest is invalid"
            )

        parameters = cls._pairs(
            raw["parameters"],
            left="ParameterKey",
            right="ParameterValue",
            label="parameter",
        )
        cls._validate_parameters(
            kind=kind,
            account=account,
            region=region,
            parameters=parameters,
        )
        raw_capabilities = raw["capabilities"]
        if (
            not isinstance(raw_capabilities, list)
            or any(not isinstance(item, str) for item in raw_capabilities)
        ):
            raise CloudFormationMutationError("capabilities are invalid")
        capabilities = tuple(raw_capabilities)
        expected_capabilities = (
            ()
            if kind in {"BOOTSTRAP_STACK", "CHANGESET_EXECUTE"}
            else ("CAPABILITY_NAMED_IAM",)
        )
        if capabilities != expected_capabilities:
            raise CloudFormationMutationError("capabilities are not exact")

        tags = cls._pairs(
            raw["tags"], left="Key", right="Value", label="tag"
        )
        expected_tags = (
            ()
            if kind == "CHANGESET_EXECUTE"
            else (
                ("SourceCommit", commit),
                ("SourceTree", tree),
                ("TransactionId", f"release_{commit}"),
            )
        )
        if tags != expected_tags:
            raise CloudFormationMutationError("release tags are not exact")
        reviewed_template = (
            _reviewed_template(
                template_body
                if kind == "BOOTSTRAP_STACK"
                else reviewed_template_body
            )
            if expects_template
            else None
        )
        if expected_template_parameter_sha256:
            actual_template_parameter_sha256 = _template_parameter_digest(
                reviewed_template or {},
                parameters,
            )
            if (
                actual_template_parameter_sha256
                != expected_template_parameter_sha256
            ):
                raise CloudFormationMutationError(
                    "reviewed template and parameter digest differs"
                )
        actual_observed_request_sha256 = _observed_request_projection_digest(
            kind=kind,
            stack_name=stack,
            change_set_name=change_set,
            template=reviewed_template,
            capabilities=capabilities,
            tags=tags,
        )
        if actual_observed_request_sha256 != expected_observed_request_sha256:
            raise CloudFormationMutationError(
                "reviewed observed request projection differs"
            )
        return cls(
            kind,
            account,
            region,
            commit,
            tree,
            stack,
            change_set,
            template_body,
            template_url,
            reviewed_template_body,
            template_asset_id,
            template_content_sha256,
            expected_template_parameter_sha256,
            expected_observed_request_sha256,
            parameters,
            capabilities,
            tags,
        )

    @staticmethod
    def _pairs(
        raw: object,
        *,
        left: str,
        right: str,
        label: str,
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(raw, list):
            raise CloudFormationMutationError(f"{label} inventory is invalid")
        result: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, Mapping) or set(item) != {left, right}:
                raise CloudFormationMutationError(f"{label} fields are not exact")
            left_value = _exact_text(item[left], label=f"{label} name")
            right_value = item[right]
            if not isinstance(right_value, str) or "\x00" in right_value:
                raise CloudFormationMutationError(f"{label} value is invalid")
            result.append((left_value, right_value))
        if result != sorted(result) or len(set(result)) != len(result):
            raise CloudFormationMutationError(
                f"{label} inventory is not sorted and unique"
            )
        return tuple(result)

    @staticmethod
    def _validate_parameters(
        *,
        kind: str,
        account: str,
        region: str,
        parameters: tuple[tuple[str, str], ...],
    ) -> None:
        if any(_PARAMETER_KEY.fullmatch(key) is None for key, _ in parameters):
            raise CloudFormationMutationError("parameter name is invalid")
        del account, region
        if parameters:
            raise CloudFormationMutationError(
                "pre-cloud operation parameter set must be empty"
            )

    @staticmethod
    def _hardened_runtime_parameters(
        *,
        account: str,
        region: str,
        runtime_id: str,
        runtime_version: str,
        runtime_arn: str,
    ) -> tuple[tuple[str, str], ...]:
        arn_pattern = re.compile(
            rf"arn:aws:bedrock-agentcore:{region}:{account}:agent/"
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
            r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:"
            rf"{re.escape(runtime_version)}"
        )
        if (
            _RUNTIME_ID.fullmatch(runtime_id) is None
            or _RUNTIME_VERSION.fullmatch(runtime_version) is None
            or arn_pattern.fullmatch(runtime_arn) is None
        ):
            raise CloudFormationMutationError(
                "runtime update parameter values cross their exact subject"
            )
        return (
            ("HardenedRuntimeArn", runtime_arn),
            ("HardenedRuntimeId", runtime_id),
            ("HardenedRuntimeVersion", runtime_version),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "CloudFormationOperationV2":
        try:
            return cls.from_mapping(parse_canonical_object(payload))
        except CloudFormationMutationError:
            raise
        except Exception as error:
            raise CloudFormationMutationError(
                "CloudFormation operation is not canonical"
            ) from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "account": self.account,
            "region": self.region,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "stackName": self.stack_name,
            "changeSetName": self.change_set_name,
            "templateBody": self.template_body,
            "templateUrl": self.template_url,
            "reviewedTemplateBody": self.reviewed_template_body,
            "templateAssetId": self.template_asset_id,
            "templateContentSha256": self.template_content_sha256,
            "expectedTemplateParameterSha256": (
                self.expected_template_parameter_sha256
            ),
            "expectedObservedRequestSha256": (
                self.expected_observed_request_sha256
            ),
            "parameters": [
                {"ParameterKey": key, "ParameterValue": value}
                for key, value in self.parameters
            ],
            "capabilities": list(self.capabilities),
            "tags": [{"Key": key, "Value": value} for key, value in self.tags],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


_CLOUDFORMATION_PREFLIGHT_TOKEN = object()


class VerifiedCloudFormationPreflightV2:
    """Unforgeable-in-normal-use closure over one exact planned operation."""

    __slots__ = ("_release_plan_sha256", "_request_sha256", "_operation")

    def __init__(
        self,
        *,
        release_plan_sha256: str,
        request_sha256: str,
        operation: CloudFormationOperationV2,
        _token: object | None = None,
    ) -> None:
        if _token is not _CLOUDFORMATION_PREFLIGHT_TOKEN:
            raise CloudFormationMutationError(
                "verified CloudFormation preflight is not constructible"
            )
        self._release_plan_sha256 = release_plan_sha256
        self._request_sha256 = request_sha256
        self._operation = operation

    def _bind_verified_mutation(
        self,
        verified: VerifiedPrivateMutationV2,
    ) -> CloudFormationOperationV2:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise CloudFormationMutationError(
                "CloudFormation dispatch requires a verified private mutation"
            )
        try:
            resolved = verified.resolved_request
            metadata = verified.metadata
            payload = verified.read_artifact_bytes(limit=MAX_CONTRACT_BYTES)
        except ContractError as error:
            raise CloudFormationMutationError(
                "CloudFormation verified mutation is closed or invalid"
            ) from error
        request = resolved.mutation_request
        if (
            request.plan_sha256 != self._release_plan_sha256
            or request.request_sha256 != self._request_sha256
            or metadata.request_artifact_sha256 != self._request_sha256
        ):
            raise CloudFormationMutationError(
                "CloudFormation verified mutation differs from preflight"
            )
        parsed = CloudFormationOperationV2.from_bytes(payload)
        if parsed != self._operation:
            raise CloudFormationMutationError(
                "CloudFormation retained operation differs from preflight"
            )
        return parsed


def validate_cloudformation_preflight(
    operation: CloudFormationOperationV2,
    *,
    release_plan: ReleasePlanV2,
) -> VerifiedCloudFormationPreflightV2:
    """Bind one canonical CF operation to its plan step and retained asset."""

    if not isinstance(operation, CloudFormationOperationV2) or not isinstance(
        release_plan, ReleasePlanV2
    ):
        raise CloudFormationMutationError(
            "CloudFormation preflight inputs are invalid"
        )
    try:
        canonical_plan = ReleasePlanV2.from_bytes(release_plan.to_bytes())
        canonical_operation = CloudFormationOperationV2.from_bytes(
            operation.to_bytes()
        )
    except (ContractError, CloudFormationMutationError) as error:
        raise CloudFormationMutationError(
            "CloudFormation preflight inputs are invalid"
        ) from error
    payload = canonical_operation.to_bytes()
    request_sha256 = hashlib.sha256(payload).hexdigest()
    if (
        canonical_operation.account,
        canonical_operation.region,
        canonical_operation.source_commit,
        canonical_operation.source_tree,
    ) != (
        canonical_plan.account,
        canonical_plan.region,
        canonical_plan.source_commit,
        canonical_plan.source_tree,
    ):
        raise CloudFormationMutationError(
            "CloudFormation operation crosses its release-plan identity"
        )
    matching_steps = tuple(
        step
        for step in canonical_plan.steps
        if step.request_sha256 == request_sha256
    )
    expected_subject = (
        f"cfn:{canonical_plan.account}:{canonical_plan.region}:stack:"
        f"{canonical_operation.stack_name}:release:"
        f"{canonical_plan.source_commit}"
    )
    if len(matching_steps) != 1:
        raise CloudFormationMutationError(
            "CloudFormation operation is not uniquely planned"
        )
    step = matching_steps[0]
    artifact = next(
        (
            item
            for item in canonical_plan.artifacts
            if item.path == step.request_artifact
        ),
        None,
    )
    if (
        step.kind != canonical_operation.kind
        or step.subject != expected_subject
        or artifact is None
        or artifact.sha256 != request_sha256
        or artifact.size != len(payload)
        or step.expected_template_parameter_sha256
        != canonical_operation.expected_template_parameter_sha256
        or step.expected_observed_request_sha256
        != canonical_operation.expected_observed_request_sha256
    ):
        raise CloudFormationMutationError(
            "CloudFormation operation differs from its exact plan step"
        )
    expected_template_sha256 = getattr(
        step, "expected_template_sha256", ""
    )
    if canonical_operation.kind == "STACK_UPDATE":
        if (
            expected_template_sha256
            != canonical_operation.template_content_sha256
        ):
            raise CloudFormationMutationError(
                "CloudFormation update template differs from the plan"
            )
    elif expected_template_sha256:
        raise CloudFormationMutationError(
            "CloudFormation plan has an unexpected update template binding"
        )

    if canonical_operation.kind not in {
        "BOOTSTRAP_STACK",
        "CHANGESET_EXECUTE",
    }:
        asset_subject = f"cdk:asset:{canonical_operation.template_asset_id}"
        assets = tuple(
            candidate
            for candidate in canonical_plan.steps
            if candidate.kind == "ASSET_PUBLISH"
            and candidate.subject == asset_subject
        )
        if (
            len(assets) != 1
            or assets[0].ordinal >= step.ordinal
            or assets[0].expected_content_sha256
            != canonical_operation.template_content_sha256
        ):
            raise CloudFormationMutationError(
                "CloudFormation template asset differs from its planned content"
            )
    return VerifiedCloudFormationPreflightV2(
        release_plan_sha256=canonical_plan.digest(),
        request_sha256=request_sha256,
        operation=canonical_operation,
        _token=_CLOUDFORMATION_PREFLIGHT_TOKEN,
    )


class CloudFormationMutationDispatcher:
    """Dispatch one exact provider mutation and return acknowledgement only."""

    def __init__(self, client: CloudFormationClient) -> None:
        self._client = client

    @staticmethod
    def _parameters(
        parameters: tuple[tuple[str, str], ...],
    ) -> list[dict[str, str]]:
        return [
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in parameters
        ]

    @staticmethod
    def _tags(operation: CloudFormationOperationV2) -> list[dict[str, str]]:
        return [{"Key": key, "Value": value} for key, value in operation.tags]

    @staticmethod
    def _bind_verified_operation(
        verified: VerifiedPrivateMutationV2,
    ) -> tuple[
        CloudFormationOperationV2,
        tuple[tuple[str, str], ...],
        str,
        str,
        str,
    ]:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise CloudFormationMutationError(
                "CloudFormation dispatch requires a verified private mutation"
            )
        try:
            resolved = verified.resolved_request
            payload = verified.read_artifact_bytes(limit=MAX_CONTRACT_BYTES)
        except ContractError as error:
            raise CloudFormationMutationError(
                "CloudFormation verified mutation is closed or invalid"
            ) from error
        operation = CloudFormationOperationV2.from_bytes(payload)
        request = resolved.mutation_request
        expected_subject = (
            f"cfn:{resolved.account}:{resolved.region}:stack:"
            f"{operation.stack_name}:release:{resolved.source_commit}"
        )
        if (
            operation.account,
            operation.region,
            operation.source_commit,
            operation.source_tree,
        ) != (
            resolved.account,
            resolved.region,
            resolved.source_commit,
            resolved.source_tree,
        ):
            raise CloudFormationMutationError(
                "CloudFormation operation crosses its resolved release identity"
            )
        if request.kind != operation.kind or request.subject != expected_subject:
            raise CloudFormationMutationError(
                "CloudFormation operation differs from the planned step"
            )
        if (
            operation.expected_template_parameter_sha256
            != resolved.expected_template_parameter_sha256
        ):
            raise CloudFormationMutationError(
                "CloudFormation template expectation differs from the plan"
            )
        if (
            operation.expected_observed_request_sha256
            != resolved.expected_observed_request_sha256
        ):
            raise CloudFormationMutationError(
                "CloudFormation request expectation differs from the plan"
            )
        if operation.kind == "STACK_UPDATE" and (
            operation.template_content_sha256
            != getattr(resolved, "expected_template_sha256", "")
        ):
            raise CloudFormationMutationError(
                "CloudFormation update template expectation differs from the plan"
            )
        expected_phases = {
            "BOOTSTRAP_STACK": {"foundation"},
            "STACK_CREATE": {"foundation"},
            "STACK_UPDATE": {"runtime", "endpoint"},
            "CHANGESET_CREATE": {
                "router-cron-cs",
                "scheduler-cs",
                "web-cs",
            },
            "CHANGESET_EXECUTE": {"router-cron", "scheduler", "web"},
        }[operation.kind]
        if resolved.step_phase not in expected_phases:
            raise CloudFormationMutationError(
                "CloudFormation operation phase binding is invalid"
            )
        expected_phase_stacks = {
            "router-cron-cs": {"OpenClawRouter", "OpenClawCron"},
            "router-cron": {"OpenClawRouter", "OpenClawCron"},
            "scheduler-cs": {"PersonalOperatorScheduler"},
            "scheduler": {"PersonalOperatorScheduler"},
            "web-cs": {"PersonalOperatorWeb"},
            "web": {"PersonalOperatorWeb"},
        }
        allowed_phase_stacks = expected_phase_stacks.get(resolved.step_phase)
        if (
            allowed_phase_stacks is not None
            and operation.stack_name not in allowed_phase_stacks
        ):
            raise CloudFormationMutationError(
                "CloudFormation stack differs from its release phase"
            )
        parameters: tuple[tuple[str, str], ...] = ()
        target_stack_id = ""
        change_set_id = ""
        if resolved.step_phase == "runtime":
            if (
                resolved.foundation_runtime_inputs is None
                or not resolved.agent_core_stack_id
                or not resolved.runtime_image_digest
                or resolved.runtime_id
                or resolved.runtime_endpoint_id
            ):
                raise CloudFormationMutationError(
                    "runtime stack update generated inputs are not exact"
                )
            target_stack_id = resolved.agent_core_stack_id
        elif resolved.step_phase == "endpoint":
            if (
                resolved.foundation_runtime_inputs is None
                or not resolved.agent_core_stack_id
                or not resolved.runtime_image_digest
                or not resolved.runtime_id
                or resolved.runtime_endpoint_id
            ):
                raise CloudFormationMutationError(
                    "endpoint stack update generated inputs are not exact"
                )
            target_stack_id = resolved.agent_core_stack_id
            parameters = CloudFormationOperationV2._hardened_runtime_parameters(
                account=resolved.account,
                region=resolved.region,
                runtime_id=resolved.runtime_id,
                runtime_version=resolved.runtime_version,
                runtime_arn=resolved.runtime_arn,
            )
            _template_parameter_digest(
                _reviewed_template(operation.reviewed_template_body),
                parameters,
            )
        elif resolved.step_phase in {
            "router-cron-cs",
            "router-cron",
            "scheduler-cs",
            "scheduler",
            "web-cs",
            "web",
        } and (
            not resolved.runtime_endpoint_id
            or not resolved.runtime_context_sha256
        ):
            raise CloudFormationMutationError(
                "consumer stack operation generated inputs are incomplete"
            )
        required_predecessor = {
            "router-cron": resolved.router_cron_changesets_sha256,
            "scheduler": resolved.scheduler_changeset_sha256,
            "web": resolved.web_changeset_sha256,
        }.get(resolved.step_phase, "not-required")
        if required_predecessor == "":
            raise CloudFormationMutationError(
                "change set execution lacks its observed predecessor"
            )
        if operation.kind == "CHANGESET_EXECUTE":
            identity = {
                "OpenClawRouter": (
                    resolved.router_target_stack_id,
                    resolved.router_change_set_id,
                ),
                "OpenClawCron": (
                    resolved.cron_target_stack_id,
                    resolved.cron_change_set_id,
                ),
                "PersonalOperatorScheduler": (
                    resolved.scheduler_target_stack_id,
                    resolved.scheduler_change_set_id,
                ),
                "PersonalOperatorWeb": (
                    resolved.web_target_stack_id,
                    resolved.web_change_set_id,
                ),
            }[operation.stack_name]
            target_stack_id, change_set_id = identity
            if not target_stack_id or not change_set_id:
                raise CloudFormationMutationError(
                    "change set execution lacks exact observed CloudFormation IDs"
                )
        return (
            operation,
            parameters,
            request.operation_sha256,
            target_stack_id,
            change_set_id,
        )

    def dispatch(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedCloudFormationPreflightV2 | None = None,
    ) -> dict[str, bool]:
        if not isinstance(preflight, VerifiedCloudFormationPreflightV2):
            raise CloudFormationMutationError(
                "CloudFormation dispatch requires verified preflight authority"
            )
        preflight_operation = preflight._bind_verified_mutation(verified)
        (
            operation,
            parameters,
            operation_sha256,
            target_stack_id,
            change_set_id,
        ) = self._bind_verified_operation(verified)
        if operation != preflight_operation:
            raise CloudFormationMutationError(
                "CloudFormation operation differs from verified preflight"
            )
        client = _validate_sdk_client(
            self._client,
            service="cloudformation",
            region=operation.region,
            account=operation.account,
        )
        token = _operation_token(operation_sha256)
        template = (
            {"TemplateBody": operation.template_body}
            if operation.kind == "BOOTSTRAP_STACK"
            else {"TemplateURL": operation.template_url}
        )
        common = {
            "StackName": operation.stack_name,
            **template,
            "Parameters": self._parameters(parameters),
            "Capabilities": list(operation.capabilities),
            "Tags": self._tags(operation),
        }
        try:
            if operation.kind in {"BOOTSTRAP_STACK", "STACK_CREATE"}:
                response = client.invoke(
                    "create_stack",
                    **common,
                    ClientRequestToken=token,
                    EnableTerminationProtection=True,
                    OnFailure="DO_NOTHING",
                )
            elif operation.kind == "STACK_UPDATE":
                update_request = {**common, "StackName": target_stack_id}
                response = client.invoke(
                    "update_stack",
                    **update_request,
                    ClientRequestToken=token,
                )
            elif operation.kind == "CHANGESET_CREATE":
                response = client.invoke(
                    "create_change_set",
                    **common,
                    ChangeSetName=operation.change_set_name,
                    ChangeSetType="CREATE",
                    Description=(
                        f"Personal Operator release {operation.source_commit}"
                    ),
                    ClientToken=token,
                    IncludeNestedStacks=False,
                    ImportExistingResources=False,
                    OnStackFailure="DO_NOTHING",
                )
            else:
                response = client.invoke(
                    "execute_change_set",
                    StackName=target_stack_id,
                    ChangeSetName=change_set_id,
                    ClientRequestToken=token,
                )
        except Exception as error:
            raise CloudFormationMutationAmbiguous(
                "CloudFormation dispatch has unknown effect; authoritative "
                "reconciliation is required"
            ) from error
        if not isinstance(response, Mapping):
            raise CloudFormationMutationAmbiguous(
                "CloudFormation acknowledgement is malformed; authoritative "
                "reconciliation is required"
            )
        return {"dispatched": True}


__all__ = [
    "CloudFormationMutationAmbiguous",
    "CloudFormationMutationDispatcher",
    "CloudFormationMutationError",
    "CloudFormationOperationV2",
    "VerifiedCloudFormationPreflightV2",
    "validate_cloudformation_preflight",
    "minimal_bootstrap_template_body",
]
