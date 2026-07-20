"""Read-only, fail-closed AWS observations for clean-account release v2.

Provider acknowledgements are deliberately outside this boundary.  Every
authority-bearing observation starts from the retained, already journal-bound
``VerifiedPrivateMutationV2`` capability (and, for ECR, the complete image
preflight capability), then reads the exact AWS subject twice before returning
``PRESENT``.  A raw SDK client or a caller-constructed request object can never
select a provider subject.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Mapping

from release_tools.asset_publication_v2 import (
    AssetPublicationError,
    AssetPublicationV2,
    _parse_verified_asset,
)
from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.cloudformation_v2 import (
    CloudFormationMutationDispatcher,
    CloudFormationMutationError,
    CloudFormationOperationV2,
    VerifiedCloudFormationPreflightV2,
    _planned_observed_parameters,
    _reviewed_template,
)
from release_tools.contracts import (
    ContractError,
    RuntimeConfigurationV1,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    expected_execution_role_arn,
    parse_canonical_object,
)
from release_tools.image_publication import (
    ArtifactSubstitutionError,
    ImagePublicationEffectV1,
    REPOSITORY_NAME,
    VerifiedImagePublicationPreflightV1,
    VerifiedImagePublicationObserveV1,
)
from release_tools.transaction import ObservationDisposition


REQUIRED_REGION = "eu-west-1"
_ACCOUNT = re.compile(r"[0-9]{12}")
_KMS_KEY_ARN = re.compile(
    r"arn:aws:kms:eu-west-1:([0-9]{12}):key/[A-Za-z0-9/_+=,.@-]{1,256}"
)
_STACK_IDENTIFIER = re.compile(
    r"arn:aws:cloudformation:eu-west-1:([0-9]{12}):stack/"
    r"([A-Za-z][A-Za-z0-9-]{0,127})/([A-Za-z0-9-]{1,128})"
)
_CHANGE_SET_IDENTIFIER = re.compile(
    r"arn:aws:cloudformation:eu-west-1:([0-9]{12}):changeSet/"
    r"(release-[0-9a-f]{40})/([A-Za-z0-9-]{1,128})"
)
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_RUNTIME_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_RUNTIME_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):agent/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:([1-9][0-9]{0,4})"
)
_ENDPOINT_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):agentEndpoint/"
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}"
)
_WORKLOAD_IDENTITY_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:([0-9]{12}):"
    r"workload-identity-directory/default/workload-identity/"
    r"[A-Za-z0-9_.-]{3,255}"
)
_READ_METHODS = frozenset(
    {
        "head_object",
        "describe_stacks",
        "describe_stack_drift_detection_status",
        "describe_stack_resource_drifts",
        "get_template",
        "get_stack_policy",
        "describe_change_set",
        "batch_check_layer_availability",
        "batch_get_image",
        "describe_repositories",
        "describe_images",
        "describe_image_scan_findings",
        "get_signing_configuration",
        "describe_image_signing_status",
        "get_signing_profile",
        "list_agent_runtime_versions",
        "get_agent_runtime",
        "list_agent_runtime_endpoints",
        "get_agent_runtime_endpoint",
        "get_resource_policy",
        "lookup_events",
    }
)


class ProductionObserverV2Error(RuntimeError):
    """Live state cannot satisfy the exact reviewed release boundary."""


class ProductionObserverV2Ambiguous(ProductionObserverV2Error):
    """A read did not produce authoritative evidence; never infer absence."""


_OBSERVATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class CanonicalReadObservationV2:
    """Canonical, data-minimized evidence from one exact provider subject."""

    SCHEMA = "personal-operator.canonical-read-observation.v2"

    service: str
    operation: str
    subject: str
    disposition: ObservationDisposition
    provider_status: str
    projection_bytes: bytes

    def __init__(
        self,
        *,
        service: str,
        operation: str,
        subject: str,
        disposition: ObservationDisposition,
        provider_status: str,
        projection_bytes: bytes,
        _token: object | None = None,
    ) -> None:
        if _token is not _OBSERVATION_TOKEN:
            raise ProductionObserverV2Error(
                "canonical provider observation is not directly constructible"
            )
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "provider_status", provider_status)
        object.__setattr__(self, "projection_bytes", projection_bytes)

    def projection(self) -> dict[str, Any]:
        return parse_canonical_object(self.projection_bytes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "service": self.service,
            "operation": self.operation,
            "subject": self.subject,
            "disposition": self.disposition.value,
            "providerStatus": self.provider_status,
            "projection": self.projection(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


def _new_observation(
    *,
    service: str,
    operation: str,
    subject: str,
    disposition: ObservationDisposition,
    provider_status: str,
    projection: Mapping[str, Any],
) -> CanonicalReadObservationV2:
    if (
        not isinstance(service, str)
        or service not in {
            "s3",
            "cloudformation",
            "ecr",
            "signer",
            "bedrock-agentcore-control",
            "cloudtrail",
        }
        or operation not in _READ_METHODS
        or not isinstance(subject, str)
        or not subject
        or "\x00" in subject
        or not isinstance(disposition, ObservationDisposition)
        or not isinstance(provider_status, str)
        or not provider_status
        or "\x00" in provider_status
        or not isinstance(projection, Mapping)
    ):
        raise ProductionObserverV2Error(
            "canonical provider observation identity is invalid"
        )
    try:
        projection_bytes = canonical_json_bytes(dict(projection))
        parse_canonical_object(projection_bytes)
    except (ContractError, TypeError, ValueError) as error:
        raise ProductionObserverV2Error(
            "canonical provider observation projection is invalid"
        ) from error
    return CanonicalReadObservationV2(
        service=service,
        operation=operation,
        subject=subject,
        disposition=disposition,
        provider_status=provider_status,
        projection_bytes=projection_bytes,
        _token=_OBSERVATION_TOKEN,
    )


def _validate_client(
    client: object,
    *,
    service: str,
    account: str,
    region: str,
) -> AttestedAwsClientV2:
    if not isinstance(client, AttestedAwsClientV2):
        raise ProductionObserverV2Error(
            "production observation requires attested AWS clients"
        )
    try:
        client.require_scope(
            service=service,
            account=account,
            region=region,
            capability="observer",
        )
    except AwsAuthorityError as error:
        raise ProductionObserverV2Error(
            "attested AWS observation client crosses its exact subject"
        ) from error
    return client


def _error_details(error: BaseException) -> tuple[str, str, int | None]:
    response = getattr(error, "response", None)
    body = response.get("Error") if isinstance(response, Mapping) else None
    metadata = (
        response.get("ResponseMetadata")
        if isinstance(response, Mapping)
        else None
    )
    code = body.get("Code") if isinstance(body, Mapping) else ""
    message = body.get("Message") if isinstance(body, Mapping) else ""
    status = (
        metadata.get("HTTPStatusCode")
        if isinstance(metadata, Mapping)
        else None
    )
    return (
        code if isinstance(code, str) else "",
        message if isinstance(message, str) else "",
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def _is_transport_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    return any(
        base.__module__ == "botocore.exceptions"
        and base.__name__ in {"ConnectionError", "HTTPClientError"}
        for base in type(error).__mro__
    )


class ProductionObserverV2:
    """Closed catalog of read-only v2 AWS evidence operations."""

    def __init__(
        self,
        *,
        account: str,
        region: str,
        s3: object,
        cloudformation: object,
        ecr: object,
        agentcore: object,
        signer: object,
        cloudtrail: object,
    ) -> None:
        if (
            not isinstance(account, str)
            or _ACCOUNT.fullmatch(account) is None
            or account == "000000000000"
        ):
            raise ProductionObserverV2Error(
                "production observation account is invalid"
            )
        if region != REQUIRED_REGION:
            raise ProductionObserverV2Error(
                f"production observation region must be exactly {REQUIRED_REGION}"
            )
        self._account = account
        self._region = region
        self._clients = {
            service: _validate_client(
                client,
                service=service,
                account=account,
                region=region,
            )
            for service, client in (
                ("s3", s3),
                ("cloudformation", cloudformation),
                ("ecr", ecr),
                ("bedrock-agentcore-control", agentcore),
                ("signer", signer),
                ("cloudtrail", cloudtrail),
            )
        }

    def _call(
        self,
        service: str,
        method: str,
        *,
        absent_codes: frozenset[str] = frozenset(),
        absent_subject: str = "",
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        if method not in _READ_METHODS:
            raise ProductionObserverV2Error(
                "production observer method is outside the read-only catalog"
            )
        try:
            response = self._clients[service].invoke(method, **kwargs)
        except Exception as error:
            code, message, status = _error_details(error)
            if code in absent_codes and status in {400, 404}:
                return None
            if (
                code == "ValidationError"
                and status == 400
                and absent_subject
                and message
                in {
                    f"Stack with id {absent_subject} does not exist",
                    f"Stack [{absent_subject}] does not exist",
                    f"ChangeSet [{absent_subject}] does not exist",
                }
            ):
                return None
            if _is_transport_error(error) or status == 429 or (
                isinstance(status, int) and 500 <= status <= 599
            ):
                raise ProductionObserverV2Ambiguous(
                    f"{service}.{method} ended without authoritative evidence"
                ) from error
            raise ProductionObserverV2Ambiguous(
                f"{service}.{method} failed without authoritative evidence"
            ) from error
        if not isinstance(response, Mapping):
            raise ProductionObserverV2Ambiguous(
                f"{service}.{method} returned malformed evidence"
            )
        return response

    @staticmethod
    def _canonical_verified(
        verified: object,
    ) -> VerifiedPrivateMutationV2:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise ProductionObserverV2Error(
                "production observation requires a retained verified mutation"
            )
        try:
            verified.resolved_request
            verified.metadata
        except ContractError as error:
            raise ProductionObserverV2Error(
                "retained verified mutation is closed or invalid"
            ) from error
        return verified

    def observe_asset(
        self,
        verified: VerifiedPrivateMutationV2,
    ) -> CanonicalReadObservationV2:
        """Observe one exact plan-bound S3 asset without reopening any path."""

        verified = self._canonical_verified(verified)
        try:
            asset, _ = _parse_verified_asset(verified)
        except AssetPublicationError as error:
            raise ProductionObserverV2Error(
                "plan-bound asset observation request is invalid"
            ) from error
        if (asset.account, asset.region) != (self._account, self._region):
            raise ProductionObserverV2Error(
                "plan-bound asset observation crosses AWS authority"
            )
        subject = f"cdk:asset:{asset.asset_id}"
        arguments = {
            "Bucket": asset.bucket_name,
            "Key": asset.object_key,
            "ExpectedBucketOwner": asset.account,
            "ChecksumMode": "ENABLED",
        }
        first = self._call(
            "s3",
            "head_object",
            absent_codes=frozenset({"NoSuchKey", "NotFound", "404"}),
            **arguments,
        )
        if first is None:
            return _new_observation(
                service="s3",
                operation="head_object",
                subject=subject,
                disposition=ObservationDisposition.ABSENT,
                provider_status="NOT_FOUND",
                projection={"assetId": asset.asset_id},
            )
        try:
            projection = self._asset_projection(first, asset)
        except ProductionObserverV2Error:
            if not self._asset_response_is_well_formed(first):
                raise ProductionObserverV2Ambiguous(
                    "S3 asset conflict response is malformed"
                )
            conflict_projection = self._asset_conflict_projection(first, asset)
            second = self._call(
                "s3",
                "head_object",
                absent_codes=frozenset({"NoSuchKey", "NotFound", "404"}),
                **arguments,
            )
            if second is None:
                raise ProductionObserverV2Ambiguous(
                    "S3 conflicting asset disappeared during observation"
                )
            try:
                self._asset_projection(second, asset)
            except ProductionObserverV2Error:
                if (
                    not self._asset_response_is_well_formed(second)
                    or self._asset_conflict_projection(second, asset)
                    != conflict_projection
                ):
                    raise ProductionObserverV2Ambiguous(
                        "S3 asset conflict changed during observation"
                    )
            else:
                raise ProductionObserverV2Ambiguous(
                    "S3 asset conflict was not stable"
                )
            return _new_observation(
                service="s3",
                operation="head_object",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status="RETAINED_OBJECT_CONFLICT",
                projection=conflict_projection,
            )
        second = self._call(
            "s3",
            "head_object",
            absent_codes=frozenset({"NoSuchKey", "NotFound", "404"}),
            **arguments,
        )
        if second is None:
            raise ProductionObserverV2Ambiguous(
                "S3 asset disappeared during exact observation"
            )
        if self._asset_projection(second, asset) != projection:
            raise ProductionObserverV2Ambiguous(
                "S3 asset changed during exact observation"
            )
        return _new_observation(
            service="s3",
            operation="head_object",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="PRESENT",
            projection=projection,
        )

    def _asset_projection(
        self,
        response: Mapping[str, Any],
        asset: AssetPublicationV2,
    ) -> dict[str, Any]:
        expected_checksum = base64.b64encode(
            bytes.fromhex(asset.content_sha256)
        ).decode("ascii")
        expected_metadata = {
            "content-sha256": asset.content_sha256,
            "asset-id": asset.asset_id,
            "source-commit": asset.source_commit,
            "source-tree": asset.source_tree,
        }
        kms_key = response.get("SSEKMSKeyId")
        kms_match = (
            _KMS_KEY_ARN.fullmatch(kms_key)
            if isinstance(kms_key, str)
            else None
        )
        version = response.get("VersionId")
        if (
            response.get("ContentLength") != asset.content_size
            or response.get("ContentType") != asset.content_type
            or response.get("ChecksumSHA256") != expected_checksum
            or response.get("ServerSideEncryption") != "aws:kms"
            or response.get("BucketKeyEnabled") is not True
            or kms_match is None
            or kms_match.group(1) != asset.account
            or not isinstance(version, str)
            or not version
            or version == "null"
            or "\x00" in version
            or response.get("Metadata") != expected_metadata
            or response.get("DeleteMarker") is True
        ):
            raise ProductionObserverV2Error(
                "live S3 asset differs from the exact plan-bound content"
            )
        checksum_type = response.get("ChecksumType")
        if checksum_type not in (None, "FULL_OBJECT"):
            raise ProductionObserverV2Error(
                "live S3 asset checksum type differs"
            )
        return {
            "assetId": asset.asset_id,
            "objectKey": asset.object_key,
            "contentSha256": asset.content_sha256,
            "contentSize": asset.content_size,
            "contentType": asset.content_type,
            "checksumSha256": expected_checksum,
            "serverSideEncryption": "aws:kms",
            "kmsKeyArn": kms_key,
            "bucketKeyEnabled": True,
            "versionId": version,
            "metadata": expected_metadata,
        }

    @staticmethod
    def _asset_response_is_well_formed(response: Mapping[str, Any]) -> bool:
        content_length = response.get("ContentLength")
        checksum = response.get("ChecksumSHA256")
        metadata = response.get("Metadata")
        kms_key = response.get("SSEKMSKeyId")
        version = response.get("VersionId")
        try:
            decoded_checksum = (
                base64.b64decode(checksum, validate=True)
                if isinstance(checksum, str)
                else b""
            )
        except (binascii.Error, TypeError, ValueError):
            return False
        return bool(
            isinstance(content_length, int)
            and not isinstance(content_length, bool)
            and content_length >= 0
            and isinstance(response.get("ContentType"), str)
            and "\x00" not in response["ContentType"]
            and len(decoded_checksum) == 32
            and isinstance(response.get("ServerSideEncryption"), str)
            and isinstance(response.get("BucketKeyEnabled"), bool)
            and isinstance(kms_key, str)
            and _KMS_KEY_ARN.fullmatch(kms_key) is not None
            and isinstance(version, str)
            and version
            and "\x00" not in version
            and isinstance(metadata, Mapping)
            and all(
                isinstance(key, str)
                and isinstance(value, str)
                and "\x00" not in key
                and "\x00" not in value
                for key, value in metadata.items()
            )
            and response.get("DeleteMarker") in (None, False, True)
            and response.get("ChecksumType")
            in (None, "FULL_OBJECT", "COMPOSITE")
        )

    @staticmethod
    def _asset_conflict_projection(
        response: Mapping[str, Any],
        asset: AssetPublicationV2,
    ) -> dict[str, Any]:
        raw_metadata = response.get("Metadata")
        if isinstance(raw_metadata, Mapping) and all(
            isinstance(key, str)
            and isinstance(value, str)
            and "\x00" not in key
            and "\x00" not in value
            for key, value in raw_metadata.items()
        ):
            metadata_sha256 = hashlib.sha256(
                canonical_json_bytes({"metadata": dict(raw_metadata)})
            ).hexdigest()
        else:
            metadata_sha256 = "MALFORMED"
        observed_size = response.get("ContentLength")
        if isinstance(observed_size, bool) or not isinstance(observed_size, int):
            observed_size = -1
        return {
            "assetId": asset.asset_id,
            "objectKey": asset.object_key,
            "expectedContentSha256": asset.content_sha256,
            "observedChecksumSha256": (
                response.get("ChecksumSHA256")
                if isinstance(response.get("ChecksumSHA256"), str)
                else "MALFORMED"
            ),
            "observedContentSize": observed_size,
            "observedMetadataSha256": metadata_sha256,
            "reason": "ASSET_SUBJECT_CONFLICT",
        }

    def observe_cloudformation(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedCloudFormationPreflightV2,
    ) -> CanonicalReadObservationV2:
        """Observe one exact preflight-closed CloudFormation operation."""

        verified = self._canonical_verified(verified)
        if not isinstance(preflight, VerifiedCloudFormationPreflightV2):
            raise ProductionObserverV2Error(
                "CloudFormation observation requires verified preflight authority"
            )
        try:
            preflight_operation = preflight._bind_verified_mutation(verified)
            (
                operation,
                parameters,
                _,
                target_stack_id,
                change_set_id,
            ) = CloudFormationMutationDispatcher._bind_verified_operation(verified)
        except CloudFormationMutationError as error:
            raise ProductionObserverV2Error(
                "plan-bound CloudFormation observation request is invalid"
            ) from error
        if preflight_operation != operation:
            raise ProductionObserverV2Error(
                "CloudFormation observation differs from verified preflight"
            )
        if (operation.account, operation.region) != (
            self._account,
            self._region,
        ):
            raise ProductionObserverV2Error(
                "plan-bound CloudFormation observation crosses AWS authority"
            )
        if (
            operation.kind == "STACK_UPDATE"
            and operation.stack_name == "OpenClawAgentCore"
        ):
            raise ProductionObserverV2Error(
                "AgentCore stack updates require a phase-specific composite "
                "observer"
            )
        if operation.kind in {
            "BOOTSTRAP_STACK",
            "STACK_CREATE",
            "STACK_UPDATE",
        }:
            return self._observe_stack(
                operation,
                parameters=parameters,
                target_stack_id=target_stack_id,
            )
        return self._observe_change_set(
            verified,
            operation,
            parameters=parameters,
            target_stack_id=target_stack_id,
            change_set_id=change_set_id,
        )

    def _describe_stack(
        self,
        selector: str,
    ) -> Mapping[str, Any] | None:
        response = self._call(
            "cloudformation",
            "describe_stacks",
            absent_subject=selector,
            StackName=selector,
        )
        if response is None:
            return None
        if response.get("NextToken") not in (None, ""):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack observation was paginated"
            )
        stacks = response.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack observation is not singular"
            )
        stack = stacks[0]
        if not isinstance(stack, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack observation is malformed"
            )
        return stack

    def _observe_stack(
        self,
        operation: CloudFormationOperationV2,
        *,
        parameters: tuple[tuple[str, str], ...],
        target_stack_id: str,
    ) -> CanonicalReadObservationV2:
        selector = target_stack_id or operation.stack_name
        first = self._describe_stack(selector)
        subject = (
            f"cfn:{operation.account}:{operation.region}:stack:"
            f"{operation.stack_name}:release:{operation.source_commit}"
        )
        if first is None:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.ABSENT,
                provider_status="NOT_FOUND",
                projection={"stackName": operation.stack_name},
            )
        stack_id = self._stack_identity(first, operation)
        if target_stack_id and stack_id != target_stack_id:
            raise ProductionObserverV2Error(
                "CloudFormation stack differs from its retained exact ID"
            )
        status = first.get("StackStatus")
        if not isinstance(status, str) or not status:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack status is malformed"
            )
        pending, present, failed = {
            "BOOTSTRAP_STACK": (
                {"REVIEW_IN_PROGRESS", "CREATE_IN_PROGRESS"},
                {"CREATE_COMPLETE"},
                {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"},
            ),
            "STACK_CREATE": (
                {"REVIEW_IN_PROGRESS", "CREATE_IN_PROGRESS"},
                {"CREATE_COMPLETE"},
                {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED"},
            ),
            "STACK_UPDATE": (
                {
                    "UPDATE_IN_PROGRESS",
                    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
                    "UPDATE_ROLLBACK_IN_PROGRESS",
                    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
                },
                {"UPDATE_COMPLETE"},
                {
                    "UPDATE_FAILED",
                    "UPDATE_ROLLBACK_COMPLETE",
                    "UPDATE_ROLLBACK_FAILED",
                },
            ),
        }[operation.kind]
        if status in pending:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=status,
                projection={"stackId": stack_id, "stackName": operation.stack_name},
            )
        if status in failed:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status=status,
                projection={"stackId": stack_id, "stackName": operation.stack_name},
            )
        if status not in present:
            raise ProductionObserverV2Error(
                "CloudFormation stack returned an unreviewed terminal status"
            )
        projection = self._complete_stack_projection(
            first,
            operation,
            stack_id=stack_id,
            parameters=parameters,
        )
        closing = self._describe_stack(stack_id)
        if closing is None:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack disappeared during exact observation"
            )
        closing_projection = self._stack_live_projection(
            closing,
            operation,
            expected_stack_id=stack_id,
        )
        if closing_projection != self._stack_live_projection(
            first,
            operation,
            expected_stack_id=stack_id,
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack changed during exact observation"
            )
        return _new_observation(
            service="cloudformation",
            operation="describe_stacks",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status=status,
            projection=projection,
        )

    def _stack_identity(
        self,
        stack: Mapping[str, Any],
        operation: CloudFormationOperationV2,
    ) -> str:
        stack_id = stack.get("StackId")
        match = (
            _STACK_IDENTIFIER.fullmatch(stack_id)
            if isinstance(stack_id, str)
            else None
        )
        if (
            match is None
            or match.group(1) != operation.account
            or match.group(2) != operation.stack_name
            or stack.get("StackName") != operation.stack_name
        ):
            raise ProductionObserverV2Error(
                "CloudFormation stack identity crosses its exact subject"
            )
        return stack_id

    def _stack_live_projection(
        self,
        stack: Mapping[str, Any],
        operation: CloudFormationOperationV2,
        *,
        expected_stack_id: str,
    ) -> dict[str, Any]:
        stack_id = self._stack_identity(stack, operation)
        if stack_id != expected_stack_id:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack identity changed during observation"
            )
        parameters = self._observed_parameters(stack.get("Parameters", []))
        request_projection = self._stack_request_projection(stack, operation)
        outputs = self._stack_outputs(stack)
        return {
            "stackId": stack_id,
            "stackStatus": stack.get("StackStatus"),
            "parameters": parameters,
            "request": request_projection,
            "outputs": outputs,
        }

    def _complete_stack_projection(
        self,
        stack: Mapping[str, Any],
        operation: CloudFormationOperationV2,
        *,
        stack_id: str,
        parameters: tuple[tuple[str, str], ...],
    ) -> dict[str, Any]:
        expected_template = _reviewed_template(
            operation.template_body
            if operation.kind == "BOOTSTRAP_STACK"
            else operation.reviewed_template_body
        )
        live_template = self._read_processed_template(stack_id=stack_id)
        if canonical_json_bytes(live_template) != canonical_json_bytes(
            expected_template
        ):
            raise ProductionObserverV2Error(
                "CloudFormation processed template differs from reviewed bytes"
            )
        self._read_empty_stack_policy(stack_id)
        closing_template = self._read_processed_template(stack_id=stack_id)
        if canonical_json_bytes(closing_template) != canonical_json_bytes(
            live_template
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation processed template changed during observation"
            )
        self._read_empty_stack_policy(stack_id)
        expected_parameters = _planned_observed_parameters(
            expected_template,
            parameters,
        )
        live_parameters = self._observed_parameters(stack.get("Parameters", []))
        if live_parameters != expected_parameters:
            raise ProductionObserverV2Error(
                "CloudFormation stack parameters differ from reviewed values"
            )
        raw_outputs = self._stack_outputs(stack)
        expected_outputs = expected_template.get("Outputs", {})
        if not isinstance(expected_outputs, Mapping) or not set(
            expected_outputs
        ).issubset(raw_outputs):
            raise ProductionObserverV2Error(
                "CloudFormation stack outputs differ from reviewed keys"
            )
        outputs = {
            key: raw_outputs[key]
            for key in sorted(expected_outputs)
        }
        request_projection = self._stack_request_projection(stack, operation)
        request_digest = hashlib.sha256(
            canonical_json_bytes(request_projection)
        ).hexdigest()
        if request_digest != operation.expected_observed_request_sha256:
            raise ProductionObserverV2Error(
                "CloudFormation persistent request differs from reviewed values"
            )
        return {
            "stackId": stack_id,
            "stackName": operation.stack_name,
            "stackStatus": stack.get("StackStatus"),
            "templateSha256": hashlib.sha256(
                canonical_json_bytes(expected_template)
            ).hexdigest(),
            "templateParameterSha256": hashlib.sha256(
                canonical_json_bytes(
                    {
                        "parameters": expected_parameters,
                        "template": expected_template,
                    }
                )
            ).hexdigest(),
            "observedRequestSha256": request_digest,
            "parameters": expected_parameters,
            "outputs": outputs,
        }

    def _read_processed_template(
        self,
        *,
        stack_id: str,
        change_set_id: str = "",
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "StackName": stack_id,
            "TemplateStage": "Processed",
        }
        if change_set_id:
            arguments["ChangeSetName"] = change_set_id
        response = self._call(
            "cloudformation",
            "get_template",
            **arguments,
        )
        assert response is not None
        stages = response.get("StagesAvailable")
        if stages is not None and (
            not isinstance(stages, list)
            or "Processed" not in stages
            or any(not isinstance(item, str) for item in stages)
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation processed template evidence is incomplete"
            )
        template = response.get("TemplateBody")
        if isinstance(template, str):
            try:
                template = json.loads(template)
            except (TypeError, ValueError) as error:
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation processed template is malformed"
                ) from error
        if not isinstance(template, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation processed template is malformed"
            )
        try:
            return parse_canonical_object(canonical_json_bytes(dict(template)))
        except (ContractError, TypeError, ValueError) as error:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation processed template is malformed"
            ) from error

    def _read_empty_stack_policy(self, stack_id: str) -> None:
        response = self._call(
            "cloudformation", "get_stack_policy", StackName=stack_id
        )
        assert response is not None
        if response.get("StackPolicyBody", "") not in (None, ""):
            raise ProductionObserverV2Error(
                "CloudFormation stack has an unreviewed stack policy"
            )

    @staticmethod
    def _observed_parameters(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation parameter evidence is malformed"
            )
        result: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, Mapping) or (
                set(item)
                - {
                    "ParameterKey",
                    "ParameterValue",
                    "ResolvedValue",
                    "UsePreviousValue",
                }
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation parameter evidence is malformed"
                )
            key = item.get("ParameterKey")
            value = item.get("ParameterValue")
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or item.get("UsePreviousValue") not in (None, False)
            ):
                raise ProductionObserverV2Error(
                    "CloudFormation parameter evidence is not exact"
                )
            normalized = {"ParameterKey": key, "ParameterValue": value}
            resolved = item.get("ResolvedValue")
            if resolved is not None:
                if not isinstance(resolved, str):
                    raise ProductionObserverV2Ambiguous(
                        "CloudFormation resolved parameter is malformed"
                    )
                normalized["ResolvedValue"] = resolved
            result.append(normalized)
        result.sort(key=lambda item: item["ParameterKey"])
        if len({item["ParameterKey"] for item in result}) != len(result):
            raise ProductionObserverV2Error(
                "CloudFormation parameter evidence contains duplicates"
            )
        return result

    @staticmethod
    def _stack_request_projection(
        stack: Mapping[str, Any],
        operation: CloudFormationOperationV2,
    ) -> dict[str, Any]:
        def string_list(name: str) -> list[str]:
            raw = stack.get(name, [])
            if (
                not isinstance(raw, list)
                or any(not isinstance(item, str) for item in raw)
                or len(set(raw)) != len(raw)
            ):
                raise ProductionObserverV2Ambiguous(
                    f"CloudFormation {name} evidence is malformed"
                )
            return sorted(raw)

        raw_tags = stack.get("Tags", [])
        if not isinstance(raw_tags, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation tag evidence is malformed"
            )
        tags: list[dict[str, str]] = []
        for item in raw_tags:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"Key", "Value"}
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("Value"), str)
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation tag evidence is malformed"
                )
            tags.append({"Key": item["Key"], "Value": item["Value"]})
        tags.sort(key=lambda item: (item["Key"], item["Value"]))
        if len({item["Key"] for item in tags}) != len(tags):
            raise ProductionObserverV2Error(
                "CloudFormation tag evidence contains duplicate keys"
            )
        rollback = stack.get("RollbackConfiguration", {})
        if rollback == {"RollbackTriggers": []}:
            rollback = {}
        deployment = stack.get("DeploymentConfig", {})
        if not isinstance(rollback, Mapping) or not isinstance(
            deployment, Mapping
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation deployment controls are malformed"
            )
        description = stack.get("Description", "")
        role = stack.get("RoleARN", "")
        timeout = stack.get("TimeoutInMinutes", 0)
        if (
            not isinstance(description, str)
            or not isinstance(role, str)
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation persistent request evidence is malformed"
            )
        return {
            "stackName": operation.stack_name,
            "description": description,
            "roleArn": role,
            "timeoutInMinutes": timeout,
            "capabilities": string_list("Capabilities"),
            "notificationArns": string_list("NotificationARNs"),
            "tags": tags,
            "rollbackConfiguration": dict(rollback),
            "deploymentConfig": dict(deployment),
            "disableRollback": stack.get("DisableRollback", False),
            "enableTerminationProtection": stack.get(
                "EnableTerminationProtection", False
            ),
            "retainExceptOnCreate": stack.get("RetainExceptOnCreate", False),
        }

    def _observe_change_set(
        self,
        verified: VerifiedPrivateMutationV2,
        operation: CloudFormationOperationV2,
        *,
        parameters: tuple[tuple[str, str], ...],
        target_stack_id: str,
        change_set_id: str,
    ) -> CanonicalReadObservationV2:
        if operation.kind == "CHANGESET_EXECUTE":
            return self._observe_executed_change_set(
                verified,
                operation,
                target_stack_id=target_stack_id,
                change_set_id=change_set_id,
            )
        stack_selector = operation.stack_name
        change_set_selector = operation.change_set_name
        first = self._describe_change_set_pages(
            stack_selector=stack_selector,
            change_set_selector=change_set_selector,
        )
        subject = (
            f"cfn:{operation.account}:{operation.region}:stack:"
            f"{operation.stack_name}:release:{operation.source_commit}"
        )
        if first is None:
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.ABSENT,
                provider_status="NOT_FOUND",
                projection={
                    "stackName": operation.stack_name,
                    "changeSetName": operation.change_set_name,
                },
            )
        stack_id, observed_change_set_id = self._change_set_identity(
            first,
            operation,
        )
        status = first.get("Status")
        execution_status = first.get("ExecutionStatus")
        if not isinstance(status, str) or not isinstance(execution_status, str):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change-set status is malformed"
            )
        if status in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}:
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=status,
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                },
            )
        if status == "FAILED":
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status=status,
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                },
            )
        if status != "CREATE_COMPLETE" or execution_status != "AVAILABLE":
            raise ProductionObserverV2Error(
                "CloudFormation change set is not safely executable"
            )
        request = verified.resolved_request.mutation_request
        cloudtrail = self._observe_create_change_set_event(
            operation,
            operation_sha256=request.operation_sha256,
            stack_id=stack_id,
            change_set_id=observed_change_set_id,
        )
        if cloudtrail is None:
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status="CREATE_COMPLETE_AWAITING_CLOUDTRAIL",
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                },
            )
        projection = self._complete_change_set_projection(
            first,
            operation,
            parameters=parameters,
            stack_id=stack_id,
            change_set_id=observed_change_set_id,
            cloudtrail=cloudtrail,
        )
        closing = self._describe_change_set_pages(
            stack_selector=stack_id,
            change_set_selector=observed_change_set_id,
        )
        if closing is None:
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change set disappeared during observation"
            )
        if self._change_set_live_projection(
            closing,
            operation,
            expected_stack_id=stack_id,
            expected_change_set_id=observed_change_set_id,
        ) != self._change_set_live_projection(
            first,
            operation,
            expected_stack_id=stack_id,
            expected_change_set_id=observed_change_set_id,
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change set changed during exact observation"
            )
        closing_template = self._read_processed_template(
            stack_id=stack_id,
            change_set_id=observed_change_set_id,
        )
        expected_template = _reviewed_template(operation.reviewed_template_body)
        if canonical_json_bytes(closing_template) != canonical_json_bytes(
            expected_template
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation proposed template changed during observation"
            )
        return _new_observation(
            service="cloudformation",
            operation="describe_change_set",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="CREATE_COMPLETE",
            projection=projection,
        )

    def _describe_change_set_pages(
        self,
        *,
        stack_selector: str,
        change_set_selector: str,
    ) -> dict[str, Any] | None:
        token = ""
        seen_tokens: set[str] = set()
        first: dict[str, Any] | None = None
        changes: list[Any] = []
        for _ in range(100):
            arguments: dict[str, Any] = {
                "StackName": stack_selector,
                "ChangeSetName": change_set_selector,
                "IncludePropertyValues": True,
            }
            if token:
                arguments["NextToken"] = token
            response = self._call(
                "cloudformation",
                "describe_change_set",
                absent_codes=frozenset({"ChangeSetNotFound"}),
                absent_subject=change_set_selector,
                **arguments,
            )
            if response is None:
                if first is not None:
                    raise ProductionObserverV2Ambiguous(
                        "CloudFormation change set vanished while paginating"
                    )
                return None
            page_changes = response.get("Changes")
            if not isinstance(page_changes, list) or any(
                not isinstance(item, Mapping) for item in page_changes
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation change-set page is malformed"
                )
            page = {
                key: value
                for key, value in response.items()
                if key
                not in {
                    "Changes",
                    "CreationTime",
                    "NextToken",
                    "ResponseMetadata",
                }
            }
            if first is None:
                first = page
            elif canonical_json_bytes(page) != canonical_json_bytes(first):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation change-set identity changed across pages"
                )
            changes.extend(page_changes)
            raw_token = response.get("NextToken")
            if raw_token in (None, ""):
                assert first is not None
                first["Changes"] = sorted(
                    changes,
                    key=lambda value: canonical_json_bytes(value),
                )
                return first
            if not isinstance(raw_token, str) or not raw_token:
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation change-set pagination token is malformed"
                )
            if raw_token in seen_tokens:
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation change-set pagination token cycle"
                )
            seen_tokens.add(raw_token)
            token = raw_token
        raise ProductionObserverV2Ambiguous(
            "CloudFormation change-set pagination exceeded its bound"
        )

    def _change_set_identity(
        self,
        change_set: Mapping[str, Any],
        operation: CloudFormationOperationV2,
    ) -> tuple[str, str]:
        stack_id = change_set.get("StackId")
        change_set_id = change_set.get("ChangeSetId")
        stack_match = (
            _STACK_IDENTIFIER.fullmatch(stack_id)
            if isinstance(stack_id, str)
            else None
        )
        change_match = (
            _CHANGE_SET_IDENTIFIER.fullmatch(change_set_id)
            if isinstance(change_set_id, str)
            else None
        )
        if (
            stack_match is None
            or stack_match.group(1) != operation.account
            or stack_match.group(2) != operation.stack_name
            or change_match is None
            or change_match.group(1) != operation.account
            or change_match.group(2) != operation.change_set_name
            or change_set.get("StackName") != operation.stack_name
            or change_set.get("ChangeSetName") != operation.change_set_name
        ):
            raise ProductionObserverV2Error(
                "CloudFormation change-set identity crosses its exact subject"
            )
        return stack_id, change_set_id

    def _observe_create_change_set_event(
        self,
        operation: CloudFormationOperationV2,
        *,
        operation_sha256: str,
        stack_id: str,
        change_set_id: str,
    ) -> dict[str, str] | None:
        token = ""
        seen_tokens: set[str] = set()
        matches: list[dict[str, str]] = []
        for _ in range(100):
            arguments: dict[str, Any] = {
                "LookupAttributes": [
                    {
                        "AttributeKey": "EventName",
                        "AttributeValue": "CreateChangeSet",
                    }
                ],
                "MaxResults": 50,
            }
            if token:
                arguments["NextToken"] = token
            response = self._call("cloudtrail", "lookup_events", **arguments)
            assert response is not None
            events = response.get("Events")
            if not isinstance(events, list):
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail change-set evidence is malformed"
                )
            for raw in events:
                event = self._match_change_set_event(
                    raw,
                    operation,
                    operation_sha256=operation_sha256,
                    stack_id=stack_id,
                    change_set_id=change_set_id,
                )
                if event is not None:
                    matches.append(event)
            raw_token = response.get("NextToken")
            if raw_token in (None, ""):
                break
            if not isinstance(raw_token, str) or not raw_token:
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail pagination token is malformed"
                )
            if raw_token in seen_tokens:
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail pagination token cycle"
                )
            seen_tokens.add(raw_token)
            token = raw_token
        else:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail pagination exceeded its bound"
            )
        if len(matches) > 1:
            raise ProductionObserverV2Ambiguous(
                "multiple CloudTrail events claim the exact change set"
            )
        return matches[0] if matches else None

    def _observe_execute_change_set_event(
        self,
        operation: CloudFormationOperationV2,
        *,
        operation_sha256: str,
        stack_id: str,
        change_set_id: str,
    ) -> dict[str, str] | None:
        token = ""
        seen_tokens: set[str] = set()
        matches: list[dict[str, str]] = []
        for _ in range(100):
            arguments: dict[str, Any] = {
                "LookupAttributes": [
                    {
                        "AttributeKey": "EventName",
                        "AttributeValue": "ExecuteChangeSet",
                    }
                ],
                "MaxResults": 50,
            }
            if token:
                arguments["NextToken"] = token
            response = self._call("cloudtrail", "lookup_events", **arguments)
            assert response is not None
            events = response.get("Events")
            if not isinstance(events, list):
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail change-set execution evidence is malformed"
                )
            for raw in events:
                event = self._match_execute_change_set_event(
                    raw,
                    operation,
                    operation_sha256=operation_sha256,
                    stack_id=stack_id,
                    change_set_id=change_set_id,
                )
                if event is not None:
                    matches.append(event)
            raw_token = response.get("NextToken")
            if raw_token in (None, ""):
                break
            if not isinstance(raw_token, str) or not raw_token:
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail pagination token is malformed"
                )
            if raw_token in seen_tokens:
                raise ProductionObserverV2Ambiguous(
                    "CloudTrail pagination token cycle"
                )
            seen_tokens.add(raw_token)
            token = raw_token
        else:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail pagination exceeded its bound"
            )
        if len(matches) > 1:
            raise ProductionObserverV2Ambiguous(
                "multiple CloudTrail events claim the exact change-set execution"
            )
        return matches[0] if matches else None

    @staticmethod
    def _match_execute_change_set_event(
        raw: object,
        operation: CloudFormationOperationV2,
        *,
        operation_sha256: str,
        stack_id: str,
        change_set_id: str,
    ) -> dict[str, str] | None:
        if not isinstance(raw, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event summary is malformed"
            )
        encoded = raw.get("CloudTrailEvent")
        if not isinstance(encoded, str):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is missing"
            )
        try:
            event = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is malformed"
            ) from error
        if not isinstance(event, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is malformed"
            )
        request = event.get("requestParameters")
        identity = event.get("userIdentity")
        if not isinstance(request, Mapping) or not isinstance(identity, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail exact execution event is incomplete"
            )
        if (
            request.get("stackName") != stack_id
            or request.get("changeSetName") != change_set_id
        ):
            return None
        expected_token = "po-" + operation_sha256.removeprefix("sha256:")
        checks = (
            raw.get("EventId") == event.get("eventID"),
            raw.get("EventName") == "ExecuteChangeSet",
            raw.get("ReadOnly") in ("false", False),
            event.get("eventName") == "ExecuteChangeSet",
            event.get("eventSource") == "cloudformation.amazonaws.com",
            event.get("awsRegion") == operation.region,
            event.get("recipientAccountId") == operation.account,
            event.get("readOnly") is False,
            "errorCode" not in event,
            identity.get("accountId") == operation.account,
            request.get("clientRequestToken") == expected_token,
            request.get("roleARN") in (None, ""),
            request.get("disableRollback") in (None, False),
            request.get("retainExceptOnCreate") in (None, False),
        )
        if not all(checks):
            raise ProductionObserverV2Error(
                "CloudTrail exact execution request differs from reviewed intent"
            )
        event_id = event.get("eventID")
        if not isinstance(event_id, str) or not event_id:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail exact event ID is malformed"
            )
        return {"eventId": event_id, "clientToken": expected_token}

    @staticmethod
    def _match_change_set_event(
        raw: object,
        operation: CloudFormationOperationV2,
        *,
        operation_sha256: str,
        stack_id: str,
        change_set_id: str,
    ) -> dict[str, str] | None:
        if not isinstance(raw, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event summary is malformed"
            )
        encoded = raw.get("CloudTrailEvent")
        if not isinstance(encoded, str):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is missing"
            )
        try:
            event = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is malformed"
            ) from error
        if not isinstance(event, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail event payload is malformed"
            )
        response = event.get("responseElements")
        if not isinstance(response, Mapping) or response.get("id") != change_set_id:
            return None
        request = event.get("requestParameters")
        identity = event.get("userIdentity")
        if not isinstance(request, Mapping) or not isinstance(identity, Mapping):
            raise ProductionObserverV2Ambiguous(
                "CloudTrail exact change-set event is incomplete"
            )
        expected_tags = [
            {"key": key, "value": value} for key, value in operation.tags
        ]
        expected_token = "po-" + operation_sha256.removeprefix("sha256:")
        checks = (
            raw.get("EventId") == event.get("eventID"),
            raw.get("EventName") == "CreateChangeSet",
            raw.get("ReadOnly") in ("false", False),
            event.get("eventName") == "CreateChangeSet",
            event.get("eventSource") == "cloudformation.amazonaws.com",
            event.get("awsRegion") == operation.region,
            event.get("recipientAccountId") == operation.account,
            event.get("readOnly") is False,
            "errorCode" not in event,
            identity.get("accountId") == operation.account,
            request.get("stackName") == operation.stack_name,
            request.get("changeSetName") == operation.change_set_name,
            request.get("changeSetType") == "CREATE",
            request.get("description")
            == f"Personal Operator release {operation.source_commit}",
            request.get("templateURL") == operation.template_url,
            request.get("parameters", [])
            == [
                {"parameterKey": key, "parameterValue": value}
                for key, value in operation.parameters
            ],
            request.get("capabilities", []) == list(operation.capabilities),
            request.get("notificationARNs", []) == [],
            request.get("tags", []) == expected_tags,
            request.get("roleARN") in (None, ""),
            request.get("clientToken") == expected_token,
            request.get("includeNestedStacks") is False,
            request.get("onStackFailure") == "DO_NOTHING",
            request.get("importExistingResources") is False,
            request.get("rollbackConfiguration", {}) in ({}, None),
            response.get("stackId") == stack_id,
        )
        if not all(checks):
            raise ProductionObserverV2Error(
                "CloudTrail exact change-set request differs from reviewed intent"
            )
        event_id = event.get("eventID")
        if not isinstance(event_id, str) or not event_id:
            raise ProductionObserverV2Ambiguous(
                "CloudTrail exact event ID is malformed"
            )
        return {"eventId": event_id, "clientToken": expected_token}

    def _complete_change_set_projection(
        self,
        change_set: Mapping[str, Any],
        operation: CloudFormationOperationV2,
        *,
        parameters: tuple[tuple[str, str], ...],
        stack_id: str,
        change_set_id: str,
        cloudtrail: Mapping[str, str],
    ) -> dict[str, Any]:
        expected_template = _reviewed_template(operation.reviewed_template_body)
        live_template = self._read_processed_template(
            stack_id=stack_id,
            change_set_id=change_set_id,
        )
        if canonical_json_bytes(live_template) != canonical_json_bytes(
            expected_template
        ):
            raise ProductionObserverV2Error(
                "CloudFormation proposed template differs from reviewed bytes"
            )
        expected_parameters = _planned_observed_parameters(
            expected_template,
            parameters,
        )
        if self._observed_parameters(
            change_set.get("Parameters", [])
        ) != expected_parameters:
            raise ProductionObserverV2Error(
                "CloudFormation change-set parameters differ"
            )
        request_projection = self._change_set_request_projection(
            change_set,
            operation,
        )
        request_digest = hashlib.sha256(
            canonical_json_bytes(request_projection)
        ).hexdigest()
        if request_digest != operation.expected_observed_request_sha256:
            raise ProductionObserverV2Error(
                "CloudFormation change-set request differs from reviewed values"
            )
        changes = change_set.get("Changes")
        if not isinstance(changes, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change-set changes are malformed"
            )
        changes_sha256 = hashlib.sha256(
            canonical_json_bytes({"changes": changes})
        ).hexdigest()
        return {
            "stackId": stack_id,
            "changeSetId": change_set_id,
            "stackName": operation.stack_name,
            "changeSetName": operation.change_set_name,
            "status": change_set.get("Status"),
            "executionStatus": change_set.get("ExecutionStatus"),
            "templateSha256": hashlib.sha256(
                canonical_json_bytes(expected_template)
            ).hexdigest(),
            "templateParameterSha256": hashlib.sha256(
                canonical_json_bytes(
                    {
                        "parameters": expected_parameters,
                        "template": expected_template,
                    }
                )
            ).hexdigest(),
            "observedRequestSha256": request_digest,
            "changesSha256": changes_sha256,
            "cloudTrailEventId": cloudtrail["eventId"],
        }

    def _change_set_live_projection(
        self,
        change_set: Mapping[str, Any],
        operation: CloudFormationOperationV2,
        *,
        expected_stack_id: str,
        expected_change_set_id: str,
    ) -> dict[str, Any]:
        stack_id, change_set_id = self._change_set_identity(
            change_set, operation
        )
        if (
            stack_id != expected_stack_id
            or change_set_id != expected_change_set_id
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change-set identity changed"
            )
        changes = change_set.get("Changes")
        if not isinstance(changes, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change-set changes are malformed"
            )
        return {
            "stackId": stack_id,
            "changeSetId": change_set_id,
            "status": change_set.get("Status"),
            "executionStatus": change_set.get("ExecutionStatus"),
            "parameters": self._observed_parameters(
                change_set.get("Parameters", [])
            ),
            "request": self._change_set_request_projection(
                change_set, operation
            ),
            "changesSha256": hashlib.sha256(
                canonical_json_bytes({"changes": changes})
            ).hexdigest(),
        }

    @staticmethod
    def _change_set_request_projection(
        change_set: Mapping[str, Any],
        operation: CloudFormationOperationV2,
    ) -> dict[str, Any]:
        capabilities = change_set.get("Capabilities", [])
        notifications = change_set.get("NotificationARNs", [])
        raw_tags = change_set.get("Tags", [])
        rollback = change_set.get("RollbackConfiguration", {})
        deployment = change_set.get("DeploymentConfig", {})
        if (
            not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
            or not isinstance(notifications, list)
            or any(not isinstance(item, str) for item in notifications)
            or not isinstance(raw_tags, list)
            or not isinstance(rollback, Mapping)
            or not isinstance(deployment, Mapping)
        ):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation change-set request evidence is malformed"
            )
        tags: list[dict[str, str]] = []
        for item in raw_tags:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"Key", "Value"}
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("Value"), str)
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation change-set tags are malformed"
                )
            tags.append({"Key": item["Key"], "Value": item["Value"]})
        tags.sort(key=lambda item: (item["Key"], item["Value"]))
        mode = change_set.get("DeploymentMode", "")
        if mode is None:
            mode = ""
        if not isinstance(mode, str):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation deployment mode is malformed"
            )
        return {
            "stackName": operation.stack_name,
            "changeSetName": operation.change_set_name,
            "changeSetType": "CREATE",
            "description": change_set.get("Description", ""),
            "roleArn": "",
            "capabilities": sorted(capabilities),
            "notificationArns": sorted(notifications),
            "tags": tags,
            "rollbackConfiguration": dict(rollback),
            "deploymentConfig": dict(deployment),
            "deploymentMode": mode,
            "includeNestedStacks": change_set.get("IncludeNestedStacks", False),
            "onStackFailure": change_set.get("OnStackFailure", ""),
            "importExistingResources": change_set.get(
                "ImportExistingResources", False
            ),
        }

    def _observe_executed_change_set(
        self,
        verified: VerifiedPrivateMutationV2,
        operation: CloudFormationOperationV2,
        *,
        target_stack_id: str,
        change_set_id: str,
    ) -> CanonicalReadObservationV2:
        if not target_stack_id or not change_set_id:
            raise ProductionObserverV2Error(
                "change-set execution lacks retained exact IDs"
            )
        observed = self._describe_change_set_pages(
            stack_selector=target_stack_id,
            change_set_selector=change_set_id,
        )
        subject = (
            f"cfn:{operation.account}:{operation.region}:stack:"
            f"{operation.stack_name}:release:{operation.source_commit}"
        )
        if observed is None:
            raise ProductionObserverV2Ambiguous(
                "retained change set is missing during execution observation"
            )
        stack_id, observed_change_set_id = self._change_set_identity(
            observed, operation
        )
        if (stack_id, observed_change_set_id) != (
            target_stack_id,
            change_set_id,
        ):
            raise ProductionObserverV2Error(
                "executed change set crossed its retained exact IDs"
            )
        status = observed.get("Status")
        execution = observed.get("ExecutionStatus")
        predecessor_sha256 = self._execution_predecessor_sha256(verified)
        if status == "CREATE_COMPLETE" and execution == "AVAILABLE":
            closing = self._describe_change_set_pages(
                stack_selector=target_stack_id,
                change_set_selector=change_set_id,
            )
            if closing is None or self._change_set_live_projection(
                closing,
                operation,
                expected_stack_id=target_stack_id,
                expected_change_set_id=change_set_id,
            ) != self._change_set_live_projection(
                observed,
                operation,
                expected_stack_id=target_stack_id,
                expected_change_set_id=change_set_id,
            ):
                raise ProductionObserverV2Ambiguous(
                    "retained change set changed before execution"
                )
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.ABSENT,
                provider_status="AVAILABLE_NOT_EXECUTED",
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                    "predecessorObservationSha256": predecessor_sha256,
                },
            )
        if execution == "EXECUTE_IN_PROGRESS":
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(execution),
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                    "predecessorObservationSha256": predecessor_sha256,
                },
            )
        if execution != "EXECUTE_COMPLETE" or status != "CREATE_COMPLETE":
            raise ProductionObserverV2Error(
                "change-set execution returned an unreviewed status"
            )
        operation_sha256 = (
            verified.resolved_request.mutation_request.operation_sha256
        )
        expected_request = {
            "stackName": operation.stack_name,
            "changeSetName": operation.change_set_name,
            "changeSetType": "CREATE",
            "executionOnly": True,
            "roleArn": "",
        }
        request_sha256 = hashlib.sha256(
            canonical_json_bytes(expected_request)
        ).hexdigest()
        if request_sha256 != operation.expected_observed_request_sha256:
            raise ProductionObserverV2Error(
                "change-set execution request differs from reviewed values"
            )
        cloudtrail = self._observe_execute_change_set_event(
            operation,
            operation_sha256=operation_sha256,
            stack_id=target_stack_id,
            change_set_id=change_set_id,
        )
        if cloudtrail is None:
            return _new_observation(
                service="cloudformation",
                operation="describe_change_set",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status="EXECUTE_COMPLETE_AWAITING_CLOUDTRAIL",
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                    "predecessorObservationSha256": predecessor_sha256,
                },
            )
        stack = self._describe_stack(target_stack_id)
        if stack is None:
            raise ProductionObserverV2Ambiguous(
                "executed change set has no exact resulting stack"
            )
        resulting_stack_id = self._stack_identity(stack, operation)
        if resulting_stack_id != target_stack_id:
            raise ProductionObserverV2Error(
                "executed change set crossed its resulting stack ID"
            )
        stack_status = stack.get("StackStatus")
        if stack_status in {
            "REVIEW_IN_PROGRESS",
            "CREATE_IN_PROGRESS",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(stack_status),
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                    "cloudTrailEventId": cloudtrail["eventId"],
                    "predecessorObservationSha256": predecessor_sha256,
                },
            )
        if stack_status in {
            "CREATE_FAILED",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status=str(stack_status),
                projection={
                    "stackId": stack_id,
                    "changeSetId": observed_change_set_id,
                    "cloudTrailEventId": cloudtrail["eventId"],
                    "predecessorObservationSha256": predecessor_sha256,
                },
            )
        if stack_status != "CREATE_COMPLETE":
            raise ProductionObserverV2Error(
                "executed change set has an unreviewed stack status"
            )
        change_projection = self._change_set_live_projection(
            observed,
            operation,
            expected_stack_id=target_stack_id,
            expected_change_set_id=change_set_id,
        )
        stack_projection = self._applied_stack_projection(
            stack,
            operation,
            change_set=observed,
            expected_stack_id=target_stack_id,
        )
        proposed_template = self._read_processed_template(
            stack_id=target_stack_id,
            change_set_id=change_set_id,
        )
        applied_template = self._read_processed_template(
            stack_id=target_stack_id,
        )
        if canonical_json_bytes(proposed_template) != canonical_json_bytes(
            applied_template
        ):
            raise ProductionObserverV2Error(
                "executed change-set template was not applied to the stack"
            )
        self._read_empty_stack_policy(target_stack_id)
        closing_change_set = self._describe_change_set_pages(
            stack_selector=target_stack_id,
            change_set_selector=change_set_id,
        )
        closing_stack = self._describe_stack(target_stack_id)
        if closing_change_set is None or closing_stack is None:
            raise ProductionObserverV2Ambiguous(
                "executed change set disappeared during exact observation"
            )
        if self._change_set_live_projection(
            closing_change_set,
            operation,
            expected_stack_id=target_stack_id,
            expected_change_set_id=change_set_id,
        ) != change_projection or self._applied_stack_projection(
            closing_stack,
            operation,
            change_set=closing_change_set,
            expected_stack_id=target_stack_id,
        ) != stack_projection:
            raise ProductionObserverV2Ambiguous(
                "executed change set or resulting stack changed"
            )
        closing_proposed_template = self._read_processed_template(
            stack_id=target_stack_id,
            change_set_id=change_set_id,
        )
        closing_applied_template = self._read_processed_template(
            stack_id=target_stack_id,
        )
        self._read_empty_stack_policy(target_stack_id)
        if (
            canonical_json_bytes(closing_proposed_template)
            != canonical_json_bytes(proposed_template)
            or canonical_json_bytes(closing_applied_template)
            != canonical_json_bytes(applied_template)
        ):
            raise ProductionObserverV2Ambiguous(
                "executed change-set template changed during observation"
            )
        return _new_observation(
            service="cloudformation",
            operation="describe_stacks",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="CREATE_COMPLETE",
            projection={
                "stackId": stack_id,
                "changeSetId": observed_change_set_id,
                "changesSha256": change_projection["changesSha256"],
                "cloudTrailEventId": cloudtrail["eventId"],
                "executionRequestSha256": request_sha256,
                "predecessorObservationSha256": predecessor_sha256,
                "resultingTemplateSha256": hashlib.sha256(
                    canonical_json_bytes(applied_template)
                ).hexdigest(),
            },
        )

    @staticmethod
    def _execution_predecessor_sha256(
        verified: VerifiedPrivateMutationV2,
    ) -> str:
        resolved = verified.resolved_request
        predecessor = {
            "router-cron": resolved.router_cron_changesets_sha256,
            "scheduler": resolved.scheduler_changeset_sha256,
            "web": resolved.web_changeset_sha256,
        }.get(resolved.step_phase, "")
        if (
            not isinstance(predecessor, str)
            or re.fullmatch(r"[0-9a-f]{64}", predecessor) is None
        ):
            raise ProductionObserverV2Error(
                "change-set execution lacks its retained predecessor evidence"
            )
        return predecessor

    def _applied_stack_projection(
        self,
        stack: Mapping[str, Any],
        operation: CloudFormationOperationV2,
        *,
        change_set: Mapping[str, Any],
        expected_stack_id: str,
    ) -> dict[str, Any]:
        stack_id = self._stack_identity(stack, operation)
        if stack_id != expected_stack_id:
            raise ProductionObserverV2Ambiguous(
                "resulting stack identity changed during observation"
            )
        parameters = self._observed_parameters(stack.get("Parameters", []))
        expected_parameters = self._observed_parameters(
            change_set.get("Parameters", [])
        )
        capabilities = stack.get("Capabilities", [])
        expected_capabilities = change_set.get("Capabilities", [])
        tags = self._normalized_tags(stack.get("Tags", []))
        expected_tags = self._normalized_tags(change_set.get("Tags", []))
        if (
            parameters != expected_parameters
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
            or not isinstance(expected_capabilities, list)
            or any(not isinstance(item, str) for item in expected_capabilities)
            or sorted(capabilities) != sorted(expected_capabilities)
            or tags != expected_tags
        ):
            raise ProductionObserverV2Error(
                "executed change-set parameters or controls were not applied"
            )
        return {
            "stackId": stack_id,
            "stackStatus": stack.get("StackStatus"),
            "parameters": parameters,
            "capabilities": sorted(capabilities),
            "tags": tags,
        }

    @staticmethod
    def _normalized_tags(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation tag evidence is malformed"
            )
        tags: list[dict[str, str]] = []
        for item in raw:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"Key", "Value"}
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("Value"), str)
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation tag evidence is malformed"
                )
            tags.append({"Key": item["Key"], "Value": item["Value"]})
        tags.sort(key=lambda item: (item["Key"], item["Value"]))
        if len({item["Key"] for item in tags}) != len(tags):
            raise ProductionObserverV2Error(
                "CloudFormation tag evidence contains duplicate keys"
            )
        return tags

    def observe_agentcore_runtime_stack(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedCloudFormationPreflightV2,
    ) -> CanonicalReadObservationV2:
        """Derive one exact transitional runtime from its reviewed stack."""

        verified = self._canonical_verified(verified)
        if not isinstance(preflight, VerifiedCloudFormationPreflightV2):
            raise ProductionObserverV2Error(
                "AgentCore runtime observation requires CF preflight authority"
            )
        try:
            preflight_operation = preflight._bind_verified_mutation(verified)
            operation, parameters, _, target_stack_id, _ = (
                CloudFormationMutationDispatcher._bind_verified_operation(verified)
            )
            resolved = verified.resolved_request
        except (CloudFormationMutationError, ContractError) as error:
            raise ProductionObserverV2Error(
                "plan-bound AgentCore runtime observation is invalid"
            ) from error
        if (
            preflight_operation != operation
            or (operation.account, operation.region)
            != (self._account, self._region)
            or (resolved.account, resolved.region)
            != (self._account, self._region)
            or operation.kind != "STACK_UPDATE"
            or operation.stack_name != "OpenClawAgentCore"
            or resolved.step_phase != "runtime"
            or not target_stack_id
            or resolved.foundation_runtime_inputs is None
            or resolved.agent_core_stack_id != target_stack_id
            or not resolved.runtime_image_digest
            or any(
                (
                    resolved.runtime_id,
                    resolved.runtime_version,
                    resolved.runtime_arn,
                )
            )
        ):
            raise ProductionObserverV2Error(
                "AgentCore runtime observation is not the exact runtime stack step"
            )
        subject = resolved.mutation_request.subject
        stack = self._describe_stack(target_stack_id)
        if stack is None:
            raise ProductionObserverV2Ambiguous(
                "retained AgentCore runtime stack is missing"
            )
        stack_id = self._stack_identity(stack, operation)
        if stack_id != target_stack_id:
            raise ProductionObserverV2Error(
                "AgentCore runtime stack changed its retained exact ID"
            )
        stack_status = stack.get("StackStatus")
        if stack_status in {
            "UPDATE_IN_PROGRESS",
            "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(stack_status),
                projection={"agentCoreStackId": stack_id},
            )
        if stack_status in {
            "UPDATE_FAILED",
            "UPDATE_ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_FAILED",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status=str(stack_status),
                projection={"agentCoreStackId": stack_id},
            )
        if stack_status != "UPDATE_COMPLETE":
            raise ProductionObserverV2Error(
                "AgentCore runtime stack status is not reviewed"
            )
        outputs = self._stack_outputs(stack)
        runtime_id = outputs.get("RuntimeId")
        runtime_version = outputs.get("RuntimeVersion")
        runtime_arn = outputs.get("RuntimeArn")
        arn_match = (
            _RUNTIME_ARN.fullmatch(runtime_arn)
            if isinstance(runtime_arn, str)
            else None
        )
        if (
            not isinstance(runtime_id, str)
            or _RUNTIME_ID.fullmatch(runtime_id) is None
            or not isinstance(runtime_version, str)
            or _RUNTIME_VERSION.fullmatch(runtime_version) is None
            or arn_match is None
            or arn_match.group(1) != resolved.account
            or arn_match.group(2) != runtime_version
        ):
            raise ProductionObserverV2Error(
                "AgentCore runtime stack outputs are invalid"
            )
        runtime_resolved = replace(
            resolved,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_arn=runtime_arn,
        )
        runtime_first = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=runtime_version,
        )
        if runtime_first is None:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime output is missing from the exact API subject"
            )
        if runtime_first.get("status") in {"CREATING", "UPDATING"}:
            return _new_observation(
                service="bedrock-agentcore-control",
                operation="get_agent_runtime",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(runtime_first.get("status")),
                projection={
                    "agentCoreStackId": stack_id,
                    "runtimeId": runtime_id,
                    "runtimeVersion": runtime_version,
                },
            )
        runtime_projection = self._runtime_projection(
            runtime_first,
            runtime_resolved,
            allow_transitional_hardening=True,
        )
        runtime_resource_arn = (
            f"arn:aws:bedrock-agentcore:{resolved.region}:{resolved.account}:"
            f"runtime/{runtime_id}"
        )
        self._assert_command_deny_policy(runtime_resource_arn)
        runtime_second = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=runtime_version,
        )
        if runtime_second is None:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime disappeared during observation"
            )
        if self._runtime_projection(
            runtime_second,
            runtime_resolved,
            allow_transitional_hardening=True,
        ) != runtime_projection:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime changed during exact observation"
            )
        self._assert_command_deny_policy(runtime_resource_arn)
        cloudformation_projection = self._complete_stack_projection(
            stack,
            operation,
            stack_id=stack_id,
            parameters=parameters,
        )
        closing_stack = self._describe_stack(target_stack_id)
        if closing_stack is None or self._stack_live_projection(
            closing_stack,
            operation,
            expected_stack_id=stack_id,
        ) != self._stack_live_projection(
            stack,
            operation,
            expected_stack_id=stack_id,
        ):
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime stack changed during observation"
            )
        return _new_observation(
            service="bedrock-agentcore-control",
            operation="get_agent_runtime",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="READY",
            projection={
                "agentCoreStackId": stack_id,
                "cloudFormationTemplateSha256": (
                    cloudformation_projection["templateSha256"]
                ),
                "cloudFormationRequestSha256": (
                    cloudformation_projection["observedRequestSha256"]
                ),
                **runtime_projection,
            },
        )

    def observe_agentcore_endpoint(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedCloudFormationPreflightV2,
    ) -> CanonicalReadObservationV2:
        """Observe the exact Endpoint produced by the endpoint stack update.

        The CloudFormation output supplies the endpoint ID.  AgentCore is then
        read independently and must return that same ID, its exact API ARN, the
        retained runtime tuple, hardened runtime configuration, and both
        command-deny policies.
        """

        verified = self._canonical_verified(verified)
        if not isinstance(preflight, VerifiedCloudFormationPreflightV2):
            raise ProductionObserverV2Error(
                "AgentCore endpoint observation requires CF preflight authority"
            )
        try:
            preflight_operation = preflight._bind_verified_mutation(verified)
            operation, parameters, _, target_stack_id, _ = (
                CloudFormationMutationDispatcher._bind_verified_operation(verified)
            )
            resolved = verified.resolved_request
        except (CloudFormationMutationError, ContractError) as error:
            raise ProductionObserverV2Error(
                "plan-bound AgentCore endpoint observation is invalid"
            ) from error
        if (
            preflight_operation != operation
            or (operation.account, operation.region)
            != (self._account, self._region)
            or (resolved.account, resolved.region)
            != (self._account, self._region)
            or operation.kind != "STACK_UPDATE"
            or operation.stack_name != "OpenClawAgentCore"
            or resolved.step_phase != "endpoint"
            or not target_stack_id
            or resolved.foundation_runtime_inputs is None
            or not resolved.runtime_id
            or not resolved.runtime_version
            or not resolved.runtime_arn
        ):
            raise ProductionObserverV2Error(
                "AgentCore endpoint observation is not the exact endpoint step"
            )
        stack = self._describe_stack(target_stack_id)
        if stack is None:
            raise ProductionObserverV2Ambiguous(
                "retained AgentCore endpoint stack is missing"
            )
        stack_id = self._stack_identity(stack, operation)
        if stack_id != target_stack_id:
            raise ProductionObserverV2Error(
                "AgentCore endpoint stack changed its retained exact ID"
            )
        stack_status = stack.get("StackStatus")
        subject = resolved.mutation_request.subject
        if stack_status in {
            "UPDATE_IN_PROGRESS",
            "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(stack_status),
                projection={"agentCoreStackId": stack_id},
            )
        if stack_status in {
            "UPDATE_FAILED",
            "UPDATE_ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_FAILED",
        }:
            return _new_observation(
                service="cloudformation",
                operation="describe_stacks",
                subject=subject,
                disposition=ObservationDisposition.FAILED_RETAINED,
                provider_status=str(stack_status),
                projection={"agentCoreStackId": stack_id},
            )
        if stack_status != "UPDATE_COMPLETE":
            raise ProductionObserverV2Error(
                "AgentCore endpoint stack status is not reviewed"
            )
        outputs = self._stack_outputs(stack)
        expected_runtime_outputs = {
            "RuntimeId": resolved.runtime_id,
            "RuntimeVersion": resolved.runtime_version,
            "RuntimeArn": resolved.runtime_arn,
        }
        if any(
            outputs.get(key) != value
            for key, value in expected_runtime_outputs.items()
        ):
            raise ProductionObserverV2Error(
                "AgentCore endpoint stack outputs differ from retained runtime"
            )
        endpoint_id = outputs.get("RuntimeEndpointId")
        endpoint_output_name = outputs.get("RuntimeEndpointName")
        if (
            not isinstance(endpoint_id, str)
            or _RUNTIME_ID.fullmatch(endpoint_id) is None
            or endpoint_output_name != f"release_{resolved.source_commit}"
        ):
            raise ProductionObserverV2Error(
                "AgentCore endpoint stack output ID is invalid"
            )
        endpoint_name = f"release_{resolved.source_commit}"
        endpoint_resource_arn = (
            f"arn:aws:bedrock-agentcore:{resolved.region}:{resolved.account}:"
            f"runtime/{resolved.runtime_id}/runtime-endpoint/{endpoint_id}"
        )
        runtime_resource_arn = (
            f"arn:aws:bedrock-agentcore:{resolved.region}:{resolved.account}:"
            f"runtime/{resolved.runtime_id}"
        )
        listed = self._list_runtime_endpoints(resolved.runtime_id)
        matching = [item for item in listed if item.get("name") == endpoint_name]
        if len(matching) != 1:
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint inventory is not singular"
            )
        listed_projection = self._validate_endpoint_mapping(
            matching[0],
            endpoint_id=endpoint_id,
            endpoint_name=endpoint_name,
            endpoint_arn=None,
            runtime_arn=resolved.runtime_arn,
            runtime_version=resolved.runtime_version,
            require_ready=True,
        )
        endpoint_arn = listed_projection["endpointArn"]
        runtime_first = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=resolved.runtime_id,
            agentRuntimeVersion=resolved.runtime_version,
        )
        if runtime_first is None:
            raise ProductionObserverV2Ambiguous(
                "retained AgentCore runtime is missing behind endpoint outputs"
            )
        runtime_projection = self._runtime_projection(runtime_first, resolved)
        endpoint_first = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=resolved.runtime_id,
            endpointName=endpoint_name,
        )
        if endpoint_first is None:
            raise ProductionObserverV2Ambiguous(
                "retained AgentCore endpoint output is missing from the API"
            )
        status = endpoint_first.get("status")
        if status in {"CREATING", "UPDATING"}:
            return _new_observation(
                service="bedrock-agentcore-control",
                operation="get_agent_runtime_endpoint",
                subject=subject,
                disposition=ObservationDisposition.PENDING,
                provider_status=str(status),
                projection={
                    "runtimeId": resolved.runtime_id,
                    "endpointId": endpoint_id,
                },
            )
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}:
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint API failure cannot be terminal evidence "
                "for a CloudFormation stack update"
            )
        endpoint_projection = self._validate_endpoint_mapping(
            endpoint_first,
            endpoint_id=endpoint_id,
            endpoint_name=endpoint_name,
            endpoint_arn=endpoint_arn,
            runtime_arn=resolved.runtime_arn,
            runtime_version=resolved.runtime_version,
            require_ready=True,
        )
        self._assert_command_deny_policy(runtime_resource_arn)
        self._assert_command_deny_policy(endpoint_resource_arn)
        runtime_second = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=resolved.runtime_id,
            agentRuntimeVersion=resolved.runtime_version,
        )
        endpoint_second = self._call(
            "bedrock-agentcore-control",
            "get_agent_runtime_endpoint",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            agentRuntimeId=resolved.runtime_id,
            endpointName=endpoint_name,
        )
        if runtime_second is None or endpoint_second is None:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime or endpoint disappeared during observation"
            )
        if self._runtime_projection(runtime_second, resolved) != runtime_projection:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime changed during endpoint observation"
            )
        if self._validate_endpoint_mapping(
            endpoint_second,
            endpoint_id=endpoint_id,
            endpoint_name=endpoint_name,
            endpoint_arn=endpoint_arn,
            runtime_arn=resolved.runtime_arn,
            runtime_version=resolved.runtime_version,
            require_ready=True,
        ) != endpoint_projection:
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint changed during exact observation"
            )
        closing_stack = self._describe_stack(target_stack_id)
        if closing_stack is None:
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint stack disappeared during observation"
            )
        if (
            self._stack_identity(closing_stack, operation) != stack_id
            or closing_stack.get("StackStatus") != stack_status
            or self._stack_outputs(closing_stack) != outputs
            or self._stack_live_projection(
                closing_stack,
                operation,
                expected_stack_id=stack_id,
            )
            != self._stack_live_projection(
                stack,
                operation,
                expected_stack_id=stack_id,
            )
        ):
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint stack changed during observation"
            )
        closing_listed = self._list_runtime_endpoints(resolved.runtime_id)
        closing_matching = [
            item for item in closing_listed if item.get("name") == endpoint_name
        ]
        if len(closing_matching) != 1 or self._validate_endpoint_mapping(
            closing_matching[0],
            endpoint_id=endpoint_id,
            endpoint_name=endpoint_name,
            endpoint_arn=endpoint_arn,
            runtime_arn=resolved.runtime_arn,
            runtime_version=resolved.runtime_version,
            require_ready=True,
        ) != listed_projection:
            raise ProductionObserverV2Ambiguous(
                "AgentCore endpoint inventory changed during observation"
            )
        self._assert_command_deny_policy(runtime_resource_arn)
        self._assert_command_deny_policy(endpoint_resource_arn)
        cloudformation_projection = self._complete_stack_projection(
            stack,
            operation,
            stack_id=stack_id,
            parameters=parameters,
        )
        return _new_observation(
            service="bedrock-agentcore-control",
            operation="get_agent_runtime_endpoint",
            subject=subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="READY",
            projection={
                "agentCoreStackId": stack_id,
                "cloudFormationTemplateSha256": (
                    cloudformation_projection["templateSha256"]
                ),
                "cloudFormationRequestSha256": (
                    cloudformation_projection["observedRequestSha256"]
                ),
                **runtime_projection,
                **endpoint_projection,
            },
        )

    @staticmethod
    def _stack_outputs(stack: Mapping[str, Any]) -> dict[str, str]:
        raw = stack.get("Outputs", [])
        if not isinstance(raw, list):
            raise ProductionObserverV2Ambiguous(
                "CloudFormation stack outputs are malformed"
            )
        outputs: dict[str, str] = {}
        for item in raw:
            if (
                not isinstance(item, Mapping)
                or set(item)
                - {"OutputKey", "OutputValue", "Description", "ExportName"}
            ):
                raise ProductionObserverV2Ambiguous(
                    "CloudFormation stack output is malformed"
                )
            key = item.get("OutputKey")
            value = item.get("OutputValue")
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or key in outputs
            ):
                raise ProductionObserverV2Error(
                    "CloudFormation stack outputs are not exact"
                )
            outputs[key] = value
        return outputs

    def _list_runtime_endpoints(self, runtime_id: str) -> list[dict[str, Any]]:
        token = ""
        seen_tokens: set[str] = set()
        endpoints: list[dict[str, Any]] = []
        for _ in range(100):
            arguments: dict[str, Any] = {
                "agentRuntimeId": runtime_id,
                "maxResults": 100,
            }
            if token:
                arguments["nextToken"] = token
            response = self._call(
                "bedrock-agentcore-control",
                "list_agent_runtime_endpoints",
                **arguments,
            )
            assert response is not None
            raw_endpoints = response.get("runtimeEndpoints")
            if not isinstance(raw_endpoints, list):
                raise ProductionObserverV2Ambiguous(
                    "AgentCore endpoint inventory is malformed"
                )
            for item in raw_endpoints:
                if not isinstance(item, Mapping):
                    raise ProductionObserverV2Ambiguous(
                        "AgentCore endpoint summary is malformed"
                    )
                endpoints.append(dict(item))
            raw_token = response.get("nextToken")
            if raw_token in (None, ""):
                return endpoints
            if not isinstance(raw_token, str) or not raw_token:
                raise ProductionObserverV2Ambiguous(
                    "AgentCore endpoint pagination token is malformed"
                )
            if raw_token in seen_tokens:
                raise ProductionObserverV2Ambiguous(
                    "AgentCore endpoint pagination token cycle"
                )
            seen_tokens.add(raw_token)
            token = raw_token
        raise ProductionObserverV2Ambiguous(
            "AgentCore endpoint pagination exceeded its bound"
        )

    def _runtime_projection(
        self,
        runtime: Mapping[str, Any],
        resolved: Any,
        *,
        allow_transitional_hardening: bool = False,
    ) -> dict[str, Any]:
        status = runtime.get("status")
        if status in {"CREATING", "UPDATING"}:
            raise ProductionObserverV2Ambiguous(
                "AgentCore runtime is not yet stable"
            )
        if status != "READY":
            raise ProductionObserverV2Error(
                "AgentCore runtime is not READY"
            )
        if runtime.get("failureReason") not in (None, ""):
            raise ProductionObserverV2Error(
                "READY AgentCore runtime carries a failure reason"
            )
        arn = runtime.get("agentRuntimeArn")
        arn_match = _RUNTIME_ARN.fullmatch(arn) if isinstance(arn, str) else None
        if (
            runtime.get("agentRuntimeId") != resolved.runtime_id
            or runtime.get("agentRuntimeName") != "personal_operator_bridge"
            or runtime.get("agentRuntimeVersion") != resolved.runtime_version
            or arn != resolved.runtime_arn
            or arn_match is None
            or arn_match.group(1) != resolved.account
            or arn_match.group(2) != resolved.runtime_version
            or runtime.get("roleArn")
            != expected_execution_role_arn(resolved.account, resolved.region)
        ):
            raise ProductionObserverV2Error(
                "AgentCore runtime identity or role differs"
            )
        workload = runtime.get("workloadIdentityDetails")
        workload_arn = (
            workload.get("workloadIdentityArn")
            if isinstance(workload, Mapping)
            else None
        )
        workload_match = (
            _WORKLOAD_IDENTITY_ARN.fullmatch(workload_arn)
            if isinstance(workload_arn, str)
            else None
        )
        if (
            not isinstance(workload, Mapping)
            or set(workload) != {"workloadIdentityArn"}
            or workload_match is None
            or workload_match.group(1) != resolved.account
        ):
            raise ProductionObserverV2Error(
                "AgentCore workload identity differs from the exact runtime"
            )
        foundation = resolved.foundation_runtime_inputs
        if foundation is None:
            raise ProductionObserverV2Error(
                "AgentCore runtime lacks foundation inputs"
            )
        expected_image_uri = (
            f"{resolved.account}.dkr.ecr.{resolved.region}.amazonaws.com/"
            f"personal-operator/bridge@{resolved.runtime_image_digest}"
        )
        metadata = runtime.get("metadataConfiguration")
        requires_mmdsv2 = metadata == {"requireMMDSV2": True}
        network = runtime.get("networkConfiguration")
        vpc = (
            network.get("networkModeConfig")
            if isinstance(network, Mapping)
            else None
        )
        service_s3_endpoint = (
            vpc.get("requireServiceS3Endpoint")
            if isinstance(vpc, Mapping)
            else None
        )
        if allow_transitional_hardening:
            metadata_disposition = (
                metadata.get("requireMMDSV2")
                if isinstance(metadata, Mapping)
                else None
            )
            valid_metadata = requires_mmdsv2 or metadata in (None, {}) or (
                isinstance(metadata, Mapping)
                and set(metadata) == {"requireMMDSV2"}
                and (
                    metadata_disposition is False
                    or metadata_disposition is None
                )
            )
            valid_service_s3_endpoint = (
                service_s3_endpoint is None
                or service_s3_endpoint is False
                or service_s3_endpoint is True
            )
            if not valid_metadata or not valid_service_s3_endpoint:
                raise ProductionObserverV2Error(
                    "AgentCore transitional hardening state is invalid"
                )
        configuration_mapping = {
            "agentRuntimeArtifact": runtime.get("agentRuntimeArtifact"),
            "authorizerConfiguration": runtime.get(
                "authorizerConfiguration", {}
            ),
            "environmentVariables": runtime.get("environmentVariables"),
            "filesystemConfigurations": runtime.get(
                "filesystemConfigurations"
            ),
            "lifecycleConfiguration": runtime.get("lifecycleConfiguration"),
            "metadataConfiguration": runtime.get("metadataConfiguration"),
            "networkConfiguration": runtime.get("networkConfiguration"),
            "protocolConfiguration": runtime.get("protocolConfiguration"),
            "requestHeaderConfiguration": runtime.get(
                "requestHeaderConfiguration", {}
            ),
        }
        if allow_transitional_hardening:
            configuration_mapping["metadataConfiguration"] = {
                "requireMMDSV2": True
            }
            if isinstance(network, Mapping) and isinstance(vpc, Mapping):
                normalized_network = dict(network)
                normalized_vpc = dict(vpc)
                normalized_vpc.pop("requireServiceS3Endpoint", None)
                normalized_network["networkModeConfig"] = normalized_vpc
                configuration_mapping["networkConfiguration"] = (
                    normalized_network
                )
        try:
            configuration = RuntimeConfigurationV1.from_mapping(
                configuration_mapping,
                runtime_image_uri=expected_image_uri,
                account=resolved.account,
                region=resolved.region,
            )
        except ContractError as error:
            raise ProductionObserverV2Error(
                "AgentCore runtime configuration differs"
            ) from error
        if (
            configuration.subnet_ids != foundation.private_subnet_ids
            or configuration.security_group_ids
            != foundation.runtime_security_group_ids
        ):
            raise ProductionObserverV2Error(
                "AgentCore runtime network differs from foundation"
            )
        environment = dict(configuration.environment_variables)
        if (
            environment.get("S3_USER_FILES_BUCKET")
            != foundation.user_files_bucket_name
            or environment.get("WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME")
            != foundation.workspace_broker_function_name
            or environment.get("BEDROCK_GUARDRAIL_ID", "")
            != foundation.guardrail_id
            or environment.get("BEDROCK_GUARDRAIL_VERSION", "")
            != foundation.guardrail_version
        ):
            raise ProductionObserverV2Error(
                "AgentCore runtime guardrail or foundation environment differs"
            )
        role = expected_execution_role_arn(resolved.account, resolved.region)
        return {
            "runtimeId": resolved.runtime_id,
            "runtimeVersion": resolved.runtime_version,
            "runtimeArn": resolved.runtime_arn,
            "workloadIdentityArn": workload_arn,
            "runtimeConfiguration": configuration.to_mapping(),
            "runtimeConfigurationSha256": configuration.digest_for_role(role),
            "guardrailId": foundation.guardrail_id,
            "guardrailVersion": foundation.guardrail_version,
            "requiresMMDSV2": requires_mmdsv2,
            "requiresServiceS3Endpoint": service_s3_endpoint is True,
        }

    @staticmethod
    def _validate_endpoint_mapping(
        endpoint: Mapping[str, Any],
        *,
        endpoint_id: str,
        endpoint_name: str,
        endpoint_arn: str | None,
        runtime_arn: str,
        runtime_version: str,
        require_ready: bool,
    ) -> dict[str, str]:
        status = endpoint.get("status")
        if require_ready and status != "READY":
            raise ProductionObserverV2Error(
                "AgentCore endpoint is not READY"
            )
        observed_endpoint_arn = endpoint.get("agentRuntimeEndpointArn")
        endpoint_arn_match = (
            _ENDPOINT_ARN.fullmatch(observed_endpoint_arn)
            if isinstance(observed_endpoint_arn, str)
            else None
        )
        runtime_arn_match = _RUNTIME_ARN.fullmatch(runtime_arn)
        if (
            endpoint.get("id") != endpoint_id
            or endpoint.get("name") != endpoint_name
            or endpoint_arn_match is None
            or runtime_arn_match is None
            or endpoint_arn_match.group(1) != runtime_arn_match.group(1)
            or (
                endpoint_arn is not None
                and observed_endpoint_arn != endpoint_arn
            )
            or endpoint.get("agentRuntimeArn") != runtime_arn
            or endpoint.get("liveVersion") != runtime_version
            or endpoint.get("targetVersion") != runtime_version
        ):
            specific = (
                "AgentCore endpoint ARN differs"
                if (
                    endpoint_arn_match is None
                    or runtime_arn_match is None
                    or endpoint_arn_match.group(1)
                    != runtime_arn_match.group(1)
                    or (
                        endpoint_arn is not None
                        and observed_endpoint_arn != endpoint_arn
                    )
                )
                else "AgentCore endpoint identity or version differs"
            )
            raise ProductionObserverV2Error(specific)
        return {
            "endpointId": endpoint_id,
            "endpointName": endpoint_name,
            "endpointArn": observed_endpoint_arn,
        }

    def _assert_command_deny_policy(self, resource_arn: str) -> None:
        response = self._call(
            "bedrock-agentcore-control",
            "get_resource_policy",
            absent_codes=frozenset({"ResourceNotFoundException"}),
            resourceArn=resource_arn,
        )
        if response is None:
            raise ProductionObserverV2Error(
                "AgentCore command-deny policy is absent"
            )
        encoded = response.get("policy")
        if not isinstance(encoded, str):
            raise ProductionObserverV2Ambiguous(
                "AgentCore command-deny policy is malformed"
            )
        try:
            policy = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise ProductionObserverV2Ambiguous(
                "AgentCore command-deny policy is malformed"
            ) from error
        expected = {
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
        }
        if policy != expected:
            raise ProductionObserverV2Error(
                "AgentCore command-deny policy differs"
            )

    def observe_image_release(
        self,
        capability: VerifiedImagePublicationObserveV1,
    ) -> CanonicalReadObservationV2:
        """Observe the exact aggregate image closure, scan, and signature."""

        if not isinstance(capability, VerifiedImagePublicationObserveV1):
            raise ProductionObserverV2Error(
                "aggregate image observation requires its private capability"
            )
        plan = capability.publication_plan
        if (plan.account, plan.region) != (self._account, self._region):
            raise ProductionObserverV2Error(
                "aggregate image observation crosses AWS authority"
            )
        expected_subject = (
            f"ecr:{plan.account}:{plan.region}:repository:{REPOSITORY_NAME}:"
            f"release:{plan.source_commit}"
        )
        if capability.subject != expected_subject:
            raise ProductionObserverV2Error(
                "aggregate image observation capability subject differs"
            )

        repository_first = self._image_repository_projection(plan.account)
        if repository_first is None:
            return _new_observation(
                service="ecr",
                operation="describe_repositories",
                subject=expected_subject,
                disposition=ObservationDisposition.ABSENT,
                provider_status="REPOSITORY_NOT_FOUND",
                projection={"repositoryName": REPOSITORY_NAME},
            )

        first_effect_observations: list[bytes] = []
        for effect in capability.ordered_effects:
            observed = (
                self._observe_image_blob(effect)
                if effect.effect_kind == "ECR_BLOB_PUT"
                else self._observe_image_manifest(effect)
            )
            first_effect_observations.append(observed.to_bytes())
            if observed.disposition is not ObservationDisposition.PRESENT:
                if observed.disposition is ObservationDisposition.FAILED_RETAINED:
                    raise ProductionObserverV2Error(
                        "aggregate image closure contains a conflicting subject"
                    )
                return _new_observation(
                    service="ecr",
                    operation=observed.operation,
                    subject=expected_subject,
                    disposition=ObservationDisposition.PENDING,
                    provider_status="PARTIAL_CLOSURE",
                    projection={
                        "missingSubject": observed.subject,
                        "runtimeImageDigest": plan.subject.digest,
                    },
                )

        image_first = self._image_detail_projection(
            account=plan.account,
            digest=plan.subject.digest,
            tag=plan.commit_tag,
        )
        if image_first is None:
            return _new_observation(
                service="ecr",
                operation="describe_images",
                subject=expected_subject,
                disposition=ObservationDisposition.PENDING,
                provider_status="IMAGE_NOT_FOUND",
                projection={"runtimeImageDigest": plan.subject.digest},
            )

        scan_disposition, scan_status, scan_projection = self._image_scan_projection(
            account=plan.account,
            digest=plan.subject.digest,
        )
        closing_scan = self._image_scan_projection(
            account=plan.account,
            digest=plan.subject.digest,
        )
        if closing_scan != (scan_disposition, scan_status, scan_projection):
            raise ProductionObserverV2Ambiguous(
                "ECR scan changed during aggregate observation"
            )
        if scan_disposition is not ObservationDisposition.PRESENT:
            return _new_observation(
                service="ecr",
                operation="describe_image_scan_findings",
                subject=expected_subject,
                disposition=scan_disposition,
                provider_status=scan_status,
                projection=scan_projection,
            )

        profile = (
            f"arn:aws:signer:{plan.region}:{plan.account}:/signing-profiles/"
            "personal_operator_bridge"
        )
        signing_profile_first = self._image_signing_profile(
            account=plan.account,
            profile=profile,
        )
        signing_configuration_first = self._image_signing_configuration(
            account=plan.account,
            profile=profile,
        )
        signing_disposition, signing_status, signing_projection = (
            self._image_signing_projection(
                account=plan.account,
                digest=plan.subject.digest,
                profile=profile,
            )
        )
        signing_configuration_second = self._image_signing_configuration(
            account=plan.account,
            profile=profile,
        )
        closing_signing = self._image_signing_projection(
            account=plan.account,
            digest=plan.subject.digest,
            profile=profile,
        )
        signing_profile_second = self._image_signing_profile(
            account=plan.account,
            profile=profile,
        )
        if (
            signing_profile_second != signing_profile_first
            or signing_configuration_second != signing_configuration_first
            or closing_signing
            != (signing_disposition, signing_status, signing_projection)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR signing changed during aggregate observation"
            )
        if signing_disposition is not ObservationDisposition.PRESENT:
            return _new_observation(
                service="ecr",
                operation="describe_image_signing_status",
                subject=expected_subject,
                disposition=signing_disposition,
                provider_status=signing_status,
                projection=signing_projection,
            )

        for effect, first_observation in zip(
            capability.ordered_effects,
            first_effect_observations,
            strict=True,
        ):
            closing_observation = (
                self._observe_image_blob(effect)
                if effect.effect_kind == "ECR_BLOB_PUT"
                else self._observe_image_manifest(effect)
            )
            if closing_observation.to_bytes() != first_observation:
                raise ProductionObserverV2Ambiguous(
                    "ECR release closure changed during aggregate observation"
                )

        final_scan = self._image_scan_projection(
            account=plan.account,
            digest=plan.subject.digest,
        )
        if final_scan != (scan_disposition, scan_status, scan_projection):
            raise ProductionObserverV2Ambiguous(
                "ECR scan changed during aggregate observation"
            )

        image_second = self._image_detail_projection(
            account=plan.account,
            digest=plan.subject.digest,
            tag=plan.commit_tag,
        )
        repository_second = self._image_repository_projection(plan.account)
        if image_second != image_first or repository_second != repository_first:
            raise ProductionObserverV2Ambiguous(
                "ECR release identity changed during aggregate observation"
            )
        return _new_observation(
            service="ecr",
            operation="describe_image_scan_findings",
            subject=expected_subject,
            disposition=ObservationDisposition.PRESENT,
            provider_status="COMPLETE",
            projection={
                "repositoryName": REPOSITORY_NAME,
                "commitTag": plan.commit_tag,
                "runtimeImageDigest": plan.subject.digest,
                "imageUri": (
                    f"{plan.account}.dkr.ecr.{plan.region}.amazonaws.com/"
                    f"{REPOSITORY_NAME}@{plan.subject.digest}"
                ),
                "scanStatus": "COMPLETE",
                "criticalFindings": scan_projection["criticalFindings"],
                "highFindings": scan_projection["highFindings"],
                "sbomManifestDigest": plan.sbom_manifest.digest,
                "provenanceManifestDigest": plan.provenance_manifest.digest,
                "signingProfileArn": profile,
                "signatureStatus": "SIGNED",
            },
        )

    def _image_repository_projection(
        self, account: str
    ) -> dict[str, str] | None:
        response = self._call(
            "ecr",
            "describe_repositories",
            absent_codes=frozenset({"RepositoryNotFoundException"}),
            registryId=account,
            repositoryNames=[REPOSITORY_NAME],
        )
        if response is None:
            return None
        if response.get("nextToken") not in (None, ""):
            raise ProductionObserverV2Ambiguous(
                "ECR repository observation was paginated"
            )
        repositories = response.get("repositories")
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise ProductionObserverV2Ambiguous(
                "ECR repository observation is not singular"
            )
        repository = repositories[0]
        if not isinstance(repository, Mapping):
            raise ProductionObserverV2Ambiguous(
                "ECR repository observation is malformed"
            )
        expected_arn = (
            f"arn:aws:ecr:{self._region}:{account}:repository/{REPOSITORY_NAME}"
        )
        expected_uri = (
            f"{account}.dkr.ecr.{self._region}.amazonaws.com/{REPOSITORY_NAME}"
        )
        scanning = repository.get("imageScanningConfiguration")
        encryption = repository.get("encryptionConfiguration")
        key = encryption.get("kmsKey") if isinstance(encryption, Mapping) else None
        if (
            repository.get("registryId") != account
            or repository.get("repositoryName") != REPOSITORY_NAME
            or repository.get("repositoryArn") != expected_arn
            or repository.get("repositoryUri") != expected_uri
            or repository.get("imageTagMutability") != "IMMUTABLE"
            or not isinstance(scanning, Mapping)
            or scanning.get("scanOnPush") is not True
            or not isinstance(encryption, Mapping)
            or encryption.get("encryptionType") != "KMS"
            or not isinstance(key, str)
            or (_KMS_KEY_ARN.fullmatch(key) is None)
            or _KMS_KEY_ARN.fullmatch(key).group(1) != account
        ):
            raise ProductionObserverV2Error(
                "ECR repository security configuration differs"
            )
        return {
            "repositoryArn": expected_arn,
            "repositoryUri": expected_uri,
            "kmsKeyArn": key,
        }

    def _image_detail_projection(
        self,
        *,
        account: str,
        digest: str,
        tag: str,
    ) -> dict[str, Any] | None:
        response = self._call(
            "ecr",
            "describe_images",
            absent_codes=frozenset({"ImageNotFoundException"}),
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageIds=[{"imageDigest": digest}],
        )
        if response is None:
            return None
        if response.get("nextToken") not in (None, ""):
            raise ProductionObserverV2Ambiguous(
                "ECR image observation was paginated"
            )
        details = response.get("imageDetails")
        if not isinstance(details, list) or len(details) != 1:
            raise ProductionObserverV2Ambiguous(
                "ECR image observation is not singular"
            )
        detail = details[0]
        if not isinstance(detail, Mapping):
            raise ProductionObserverV2Ambiguous(
                "ECR image observation is malformed"
            )
        tags = detail.get("imageTags")
        size = detail.get("imageSizeInBytes")
        if (
            detail.get("registryId") != account
            or detail.get("repositoryName") != REPOSITORY_NAME
            or detail.get("imageDigest") != digest
            or not isinstance(tags, list)
            or any(not isinstance(candidate, str) for candidate in tags)
            or sorted(tags) != [tag]
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise ProductionObserverV2Error(
                "ECR image identity differs from the aggregate plan"
            )
        return {"digest": digest, "tag": tag, "size": size}

    def _image_scan_projection(
        self,
        *,
        account: str,
        digest: str,
    ) -> tuple[ObservationDisposition, str, dict[str, Any]]:
        response = self._call(
            "ecr",
            "describe_image_scan_findings",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageId={"imageDigest": digest},
        )
        assert response is not None
        status_body = response.get("imageScanStatus")
        if (
            response.get("registryId") != account
            or response.get("repositoryName") != REPOSITORY_NAME
            or response.get("imageId") != {"imageDigest": digest}
            or not isinstance(status_body, Mapping)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR scan observation is malformed"
            )
        status = status_body.get("status")
        if not isinstance(status, str):
            raise ProductionObserverV2Ambiguous(
                "ECR scan status is malformed"
            )
        findings = response.get("imageScanFindings")
        if findings is None and status != "COMPLETE":
            counts: Mapping[str, Any] = {}
        elif isinstance(findings, Mapping) and isinstance(
            findings.get("findingSeverityCounts"), Mapping
        ):
            counts = findings["findingSeverityCounts"]
        else:
            raise ProductionObserverV2Ambiguous(
                "ECR scan findings are malformed"
            )
        critical = counts.get("CRITICAL", 0)
        high = counts.get("HIGH", 0)
        if (
            not isinstance(critical, int)
            or isinstance(critical, bool)
            or critical < 0
            or not isinstance(high, int)
            or isinstance(high, bool)
            or high < 0
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR scan status or findings are malformed"
            )
        projection = {
            "runtimeImageDigest": digest,
            "rawScanStatus": status,
            "criticalFindings": critical,
            "highFindings": high,
        }
        if status in {"IN_PROGRESS", "ACTIVE", "PENDING"}:
            return ObservationDisposition.PENDING, status, projection
        terminal = {
            "FAILED",
            "FAILURE",
            "ERROR",
            "UNSUPPORTED_IMAGE",
            "SCAN_ELIGIBILITY_EXPIRED",
            "FINDINGS_UNAVAILABLE",
            "LIMIT_EXCEEDED",
            "IMAGE_ARCHIVED",
        }
        if status in terminal or (status == "COMPLETE" and (critical or high)):
            return (
                ObservationDisposition.FAILED_RETAINED,
                "SCAN_POLICY_FAILED",
                projection,
            )
        if status != "COMPLETE":
            raise ProductionObserverV2Error(
                "ECR scan returned an unreviewed status"
            )
        return ObservationDisposition.PRESENT, "COMPLETE", projection

    def _image_signing_configuration(
        self,
        *,
        account: str,
        profile: str,
    ) -> dict[str, str]:
        response = self._call("ecr", "get_signing_configuration")
        assert response is not None
        body = response.get("signingConfiguration")
        rules = body.get("rules") if isinstance(body, Mapping) else None
        expected_filter = [
            {"filter": REPOSITORY_NAME, "filterType": "WILDCARD_MATCH"}
        ]
        if response.get("registryId") != account or not isinstance(rules, list):
            raise ProductionObserverV2Ambiguous(
                "ECR signing configuration is malformed"
            )
        expected_rule = {
            "signingProfileArn": profile,
            "repositoryFilters": expected_filter,
        }
        if any(not isinstance(rule, Mapping) for rule in rules):
            raise ProductionObserverV2Ambiguous(
                "ECR signing configuration is malformed"
            )
        if rules != [expected_rule]:
            raise ProductionObserverV2Error(
                "ECR signing configuration differs from the release subject"
            )
        return {"signingProfileArn": profile}

    def _image_signing_profile(
        self,
        *,
        account: str,
        profile: str,
    ) -> dict[str, str]:
        response = self._call(
            "signer",
            "get_signing_profile",
            profileName="personal_operator_bridge",
            profileOwner=account,
        )
        assert response is not None
        version = response.get("profileVersion")
        version_arn = response.get("profileVersionArn")
        validity = response.get("signatureValidityPeriod")
        if (
            response.get("profileName") != "personal_operator_bridge"
            or not isinstance(version, str)
            or re.fullmatch(r"[A-Za-z0-9]{10}", version) is None
            or version_arn != f"{profile}/{version}"
            or response.get("platformId") != "Notation-OCI-SHA384-ECDSA"
            or validity != {"value": 3650, "type": "DAYS"}
            or response.get("status") != "Active"
            or response.get("arn") != profile
            or response.get("revocationRecord") not in (None, {})
            or response.get("statusReason") not in (None, "")
            or response.get("overrides") not in (None, {})
            or response.get("signingParameters") not in (None, {})
        ):
            raise ProductionObserverV2Error(
                "Signer profile differs from the exact managed Notation profile"
            )
        return {
            "signingProfileArn": profile,
            "signingProfileVersion": version,
            "signingProfileVersionArn": version_arn,
        }

    def _image_signing_projection(
        self,
        *,
        account: str,
        digest: str,
        profile: str,
    ) -> tuple[ObservationDisposition, str, dict[str, str]]:
        response = self._call(
            "ecr",
            "describe_image_signing_status",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageId={"imageDigest": digest},
        )
        assert response is not None
        statuses = response.get("signingStatuses")
        if (
            response.get("registryId") != account
            or response.get("repositoryName") != REPOSITORY_NAME
            or response.get("imageId") != {"imageDigest": digest}
            or not isinstance(statuses, list)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR signing observation is malformed"
            )
        matching = [
            item
            for item in statuses
            if isinstance(item, Mapping)
            and item.get("signingProfileArn") == profile
        ]
        if len(matching) != 1 or not isinstance(matching[0].get("status"), str):
            raise ProductionObserverV2Ambiguous(
                "ECR signing observation is not singular"
            )
        signing = matching[0]
        status = str(signing["status"])
        failure_code = signing.get("failureCode")
        failure_reason = signing.get("failureReason")
        if any(
            detail is not None and not isinstance(detail, str)
            for detail in (failure_code, failure_reason)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR signing failure details are malformed"
            )
        if status in {"COMPLETE", "IN_PROGRESS", "PENDING"} and any(
            detail not in (None, "")
            for detail in (failure_code, failure_reason)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR signing nonfailure status contains failure details"
            )
        projection = {
            "runtimeImageDigest": digest,
            "signingProfileArn": profile,
            "rawSignatureStatus": status,
        }
        if status in {"IN_PROGRESS", "PENDING"}:
            return ObservationDisposition.PENDING, status, projection
        if status in {"FAILED", "FAILURE", "ERROR"}:
            return (
                ObservationDisposition.FAILED_RETAINED,
                "SIGNATURE_VERIFICATION_FAILED",
                projection,
            )
        if status != "COMPLETE":
            raise ProductionObserverV2Error(
                "ECR signing returned an unreviewed status"
            )
        return ObservationDisposition.PRESENT, "SIGNED", projection

    def observe_image_effect(
        self,
        verified: VerifiedPrivateMutationV2,
        preflight: VerifiedImagePublicationPreflightV1,
    ) -> CanonicalReadObservationV2:
        """Observe one exact preflight-closed OCI registry effect."""

        verified = self._canonical_verified(verified)
        if not isinstance(preflight, VerifiedImagePublicationPreflightV1):
            raise ProductionObserverV2Error(
                "image observation requires complete preflight authority"
            )
        try:
            effect = preflight._bind_verified_mutation(verified)
        except ArtifactSubstitutionError as error:
            raise ProductionObserverV2Error(
                "plan-bound image observation request is invalid"
            ) from error
        if (effect.account, effect.region) != (self._account, self._region):
            raise ProductionObserverV2Error(
                "plan-bound image observation crosses AWS authority"
            )
        if effect.effect_kind == "ECR_BLOB_PUT":
            return self._observe_image_blob(effect)
        return self._observe_image_manifest(effect)

    def _observe_image_blob(
        self,
        effect: ImagePublicationEffectV1,
    ) -> CanonicalReadObservationV2:
        arguments = {
            "registryId": effect.account,
            "repositoryName": REPOSITORY_NAME,
            "layerDigests": [effect.digest],
        }
        first = self._call(
            "ecr", "batch_check_layer_availability", **arguments
        )
        assert first is not None
        disposition, projection = self._blob_projection(first, effect)
        second = self._call(
            "ecr", "batch_check_layer_availability", **arguments
        )
        assert second is not None
        second_disposition, second_projection = self._blob_projection(
            second, effect
        )
        if second_disposition is not disposition or second_projection != projection:
            raise ProductionObserverV2Ambiguous(
                "ECR blob changed during exact observation"
            )
        return _new_observation(
            service="ecr",
            operation="batch_check_layer_availability",
            subject=effect.provider_subject,
            disposition=disposition,
            provider_status=(
                "AVAILABLE"
                if disposition is ObservationDisposition.PRESENT
                else (
                    "MISSING"
                    if disposition is ObservationDisposition.ABSENT
                    else "IMAGE_SUBJECT_CONFLICT"
                )
            ),
            projection=projection,
        )

    @staticmethod
    def _blob_projection(
        response: Mapping[str, Any],
        effect: ImagePublicationEffectV1,
    ) -> tuple[ObservationDisposition, dict[str, Any]]:
        layers = response.get("layers")
        failures = response.get("failures")
        if not isinstance(layers, list) or not isinstance(failures, list):
            raise ProductionObserverV2Ambiguous(
                "ECR layer observation is incomplete"
            )
        if not layers and len(failures) == 1:
            failure = failures[0]
            if (
                isinstance(failure, Mapping)
                and set(failure) <= {
                    "layerDigest",
                    "failureCode",
                    "failureReason",
                }
                and failure.get("layerDigest") == effect.digest
                and failure.get("failureCode") == "MissingLayerDigest"
            ):
                return ObservationDisposition.ABSENT, {
                    "digest": effect.digest,
                    "reason": "MissingLayerDigest",
                }
        if failures or len(layers) != 1:
            raise ProductionObserverV2Error(
                "ECR layer observation is contradictory"
            )
        layer = layers[0]
        if not isinstance(layer, Mapping):
            raise ProductionObserverV2Ambiguous(
                "ECR layer observation is malformed"
            )
        if (
            set(layer)
            - {"layerDigest", "layerAvailability", "layerSize", "mediaType"}
            or layer.get("layerDigest") != effect.digest
            or layer.get("layerAvailability") != "AVAILABLE"
            or layer.get("layerSize") not in (None, effect.size)
            or layer.get("mediaType") not in (None, effect.media_type)
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR layer response contradicts its content-addressed subject"
            )
        return ObservationDisposition.PRESENT, {
            "digest": effect.digest,
            "size": effect.size,
            "mediaType": effect.media_type,
            "availability": "AVAILABLE",
        }

    def _observe_image_manifest(
        self,
        effect: ImagePublicationEffectV1,
    ) -> CanonicalReadObservationV2:
        query_ids: list[dict[str, str]] = [
            {"imageDigest": effect.digest}
        ]
        if effect.tag is not None:
            query_ids.append({"imageTag": effect.tag})
        results: list[
            tuple[ObservationDisposition, dict[str, Any]]
        ] = []
        for query_id in query_ids:
            arguments = {
                "registryId": effect.account,
                "repositoryName": REPOSITORY_NAME,
                "imageIds": [query_id],
                "acceptedMediaTypes": [effect.media_type],
            }
            first = self._call("ecr", "batch_get_image", **arguments)
            assert first is not None
            disposition, projection = self._manifest_projection(
                first,
                effect,
                query_id=query_id,
            )
            second = self._call("ecr", "batch_get_image", **arguments)
            assert second is not None
            second_disposition, second_projection = self._manifest_projection(
                second,
                effect,
                query_id=query_id,
            )
            if second_disposition is not disposition or second_projection != projection:
                raise ProductionObserverV2Ambiguous(
                    "ECR manifest changed during exact observation"
                )
            results.append((disposition, projection))
            if disposition is ObservationDisposition.FAILED_RETAINED:
                break
        for query_id, (disposition, projection) in zip(query_ids, results):
            closing = self._call(
                "ecr",
                "batch_get_image",
                registryId=effect.account,
                repositoryName=REPOSITORY_NAME,
                imageIds=[query_id],
                acceptedMediaTypes=[effect.media_type],
            )
            assert closing is not None
            closing_disposition, closing_projection = self._manifest_projection(
                closing,
                effect,
                query_id=query_id,
            )
            if (
                closing_disposition is not disposition
                or closing_projection != projection
            ):
                raise ProductionObserverV2Ambiguous(
                    "ECR digest/tag pair changed during exact observation"
                )
        dispositions = [item[0] for item in results]
        if ObservationDisposition.FAILED_RETAINED in dispositions:
            disposition = ObservationDisposition.FAILED_RETAINED
            projection = next(
                projection
                for candidate, projection in results
                if candidate is ObservationDisposition.FAILED_RETAINED
            )
        elif all(
            candidate is ObservationDisposition.ABSENT
            for candidate in dispositions
        ):
            disposition = ObservationDisposition.ABSENT
            projection = results[0][1]
        elif all(
            candidate is ObservationDisposition.PRESENT
            for candidate in dispositions
        ):
            disposition = ObservationDisposition.PRESENT
            projection = {
                **results[0][1],
                "tagBound": effect.tag is not None,
            }
        elif (
            effect.tag is not None
            and dispositions
            == [ObservationDisposition.PRESENT, ObservationDisposition.ABSENT]
        ):
            disposition = ObservationDisposition.ABSENT
            projection = {
                "digest": effect.digest,
                "tag": effect.tag,
                "reason": "TAG_NOT_BOUND",
            }
        else:
            raise ProductionObserverV2Ambiguous(
                "ECR digest/tag pair is contradictory"
            )
        provider_status = {
            ObservationDisposition.PRESENT: "PRESENT",
            ObservationDisposition.FAILED_RETAINED: (
                "IMMUTABLE_SUBJECT_CONFLICT"
            ),
        }.get(disposition, "MISSING")
        if projection.get("reason") == "TAG_NOT_BOUND":
            provider_status = "TAG_NOT_BOUND"
        return _new_observation(
            service="ecr",
            operation="batch_get_image",
            subject=effect.provider_subject,
            disposition=disposition,
            provider_status=provider_status,
            projection=projection,
        )

    @staticmethod
    def _manifest_projection(
        response: Mapping[str, Any],
        effect: ImagePublicationEffectV1,
        *,
        query_id: Mapping[str, str],
    ) -> tuple[ObservationDisposition, dict[str, Any]]:
        images = response.get("images")
        failures = response.get("failures")
        if not isinstance(images, list) or not isinstance(failures, list):
            raise ProductionObserverV2Ambiguous(
                "ECR manifest observation is incomplete"
            )
        if not images and len(failures) == 1:
            failure = failures[0]
            if (
                isinstance(failure, Mapping)
                and set(failure)
                <= {"imageId", "failureCode", "failureReason"}
                and failure.get("imageId") == dict(query_id)
                and failure.get("failureCode") == "ImageNotFound"
            ):
                return ObservationDisposition.ABSENT, {
                    "digest": effect.digest,
                    "reason": "ImageNotFound",
                }
        if failures or len(images) != 1:
            raise ProductionObserverV2Error(
                "ECR manifest observation is contradictory"
            )
        image = images[0]
        if not isinstance(image, Mapping):
            raise ProductionObserverV2Ambiguous(
                "ECR manifest observation is malformed"
            )
        payload = image.get("imageManifest")
        response_id = image.get("imageId")
        if (
            set(image)
            - {
                "registryId",
                "repositoryName",
                "imageId",
                "imageManifest",
                "imageManifestMediaType",
            }
            or image.get("registryId") != effect.account
            or image.get("repositoryName") != REPOSITORY_NAME
            or image.get("imageManifestMediaType") != effect.media_type
            or not isinstance(payload, str)
            or not isinstance(response_id, Mapping)
            or set(response_id) - {"imageDigest", "imageTag"}
        ):
            raise ProductionObserverV2Ambiguous(
                "ECR manifest identity differs from the plan-bound subject"
            )
        response_digest = response_id.get("imageDigest")
        response_tag = response_id.get("imageTag")
        queried_by_tag = "imageTag" in query_id
        if response_digest != effect.digest:
            if queried_by_tag:
                if (
                    not isinstance(response_digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", response_digest)
                    is None
                ):
                    raise ProductionObserverV2Ambiguous(
                        "ECR tag response digest is malformed"
                    )
                return ObservationDisposition.FAILED_RETAINED, {
                    "digest": effect.digest,
                    "tag": effect.tag or "",
                    "observedDigest": response_digest,
                    "reason": "IMAGE_SUBJECT_CONFLICT",
                }
            raise ProductionObserverV2Ambiguous(
                "ECR manifest digest differs from the queried subject"
            )
        if effect.tag is None:
            if response_tag not in (None, ""):
                raise ProductionObserverV2Ambiguous(
                    "ECR untagged manifest returned an unexpected tag"
                )
        elif queried_by_tag:
            if response_tag != effect.tag:
                raise ProductionObserverV2Ambiguous(
                    "ECR manifest tag differs from the queried subject"
                )
        elif response_tag not in (None, effect.tag):
            raise ProductionObserverV2Ambiguous(
                "ECR digest query returned an unrelated tag"
            )
        try:
            payload_bytes = payload.encode("utf-8")
        except UnicodeError as error:
            raise ProductionObserverV2Ambiguous(
                "ECR manifest payload is not UTF-8"
            ) from error
        if payload_bytes != effect.payload:
            raise ProductionObserverV2Ambiguous(
                "ECR manifest payload contradicts its content digest"
            )
        if "sha256:" + hashlib.sha256(payload_bytes).hexdigest() != effect.digest:
            raise ProductionObserverV2Ambiguous(
                "ECR manifest payload digest differs"
            )
        return ObservationDisposition.PRESENT, {
            "digest": effect.digest,
            "size": effect.size,
            "mediaType": effect.media_type,
            "tag": effect.tag or "",
            "subjectDigest": effect.subject_digest or "",
            "artifactType": effect.artifact_type or "",
        }


__all__ = [
    "CanonicalReadObservationV2",
    "ProductionObserverV2",
    "ProductionObserverV2Ambiguous",
    "ProductionObserverV2Error",
]
