"""In-package live authority for immutable staging release evidence."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
from typing import Any, Mapping, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from release_tools.agentcore import (
    AgentCoreClient,
    AgentCoreEndpointAbsent,
    AgentCoreEvidenceAbsent,
    AgentCoreEvidenceAdapter,
    AgentCoreEvidenceError,
    AgentCoreRuntimeAbsent,
)
from release_tools.contracts import (
    CONSUMER_RELEASE_STACKS,
    FOUNDATION_RELEASE_STACKS,
    ProductionObservationConfigV1,
    RuntimeContextV3,
    RuntimeImageEvidence,
    StagingTransactionV1,
    canonical_json_bytes,
)
from release_tools.ecr import (
    ArtifactBlobReader,
    EcrClient,
    EcrEvidenceAdapter,
    EcrEvidenceError,
    EcrImageAbsent,
    EcrRepositoryAbsent,
)


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_VERSION = re.compile(r"[1-9][0-9]{0,4}")

FOUNDATION_STACKS = FOUNDATION_RELEASE_STACKS
CONSUMER_STACKS = CONSUMER_RELEASE_STACKS
_COMPLETE_STACK_STATES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})


class ProductionObservationError(RuntimeError):
    """Live release evidence cannot prove the exact staged subject."""


class ProductionEvidenceAbsent(ProductionObservationError):
    """The exact phase subject is authoritatively absent."""


class HttpsArtifactBlobReader:
    """Read one bounded public pre-signed artifact without forwarding credentials."""

    def __init__(
        self,
        *,
        opener: Any | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        if opener is None:
            tls_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            explicit_opener = urllib_request.build_opener(
                urllib_request.ProxyHandler({}),
                urllib_request.HTTPSHandler(context=tls_context),
            )
            opener = explicit_opener.open
        if (
            not callable(opener)
            or not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 60
        ):
            raise ProductionObservationError("artifact reader configuration is invalid")
        self._opener = opener
        self._timeout_seconds = timeout_seconds

    def read(self, url: str, *, maximum_bytes: int) -> bytes:
        if (
            not isinstance(url, str)
            or urllib_parse.urlsplit(url).scheme != "https"
            or not isinstance(maximum_bytes, int)
            or isinstance(maximum_bytes, bool)
            or maximum_bytes <= 0
        ):
            raise ProductionObservationError(
                "artifact blob requires one bounded HTTPS URL"
            )
        request = urllib_request.Request(url, method="GET")
        try:
            with self._opener(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                if (
                    getattr(response, "status", None) != 200
                    or urllib_parse.urlsplit(response.geturl()).scheme != "https"
                ):
                    raise ProductionObservationError(
                        "artifact blob response is not exact HTTPS success"
                    )
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except (TypeError, ValueError) as error:
                        raise ProductionObservationError(
                            "artifact blob length is malformed"
                        ) from error
                    if not 0 <= content_length <= maximum_bytes:
                        raise ProductionObservationError(
                            "artifact blob exceeds its byte limit"
                        )
                payload = response.read(maximum_bytes + 1)
        except ProductionObservationError:
            raise
        except (OSError, TimeoutError, urllib_error.URLError) as error:
            raise ProductionObservationError(
                "artifact blob read ended without authoritative evidence"
            ) from error
        if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
            raise ProductionObservationError("artifact blob exceeds its byte limit")
        return payload


class DeploymentEvidenceAdapter(Protocol):
    """Live authority for CloudFormation-owned release phase subjects."""

    def observe_foundation(
        self, transaction: StagingTransactionV1
    ) -> bool | None: ...

    def observe_runtime_identity(
        self, transaction: StagingTransactionV1
    ) -> tuple[str, str] | None: ...

    def observe_consumer_changesets(
        self, transaction: StagingTransactionV1
    ) -> str | None: ...

    def observe_consumers(
        self, transaction: StagingTransactionV1
    ) -> str | None: ...

    def observe_verification(
        self,
        transaction: StagingTransactionV1,
        *,
        image: RuntimeImageEvidence,
        context: RuntimeContextV3,
    ) -> str | None: ...

    def observe_rollback(
        self, transaction: StagingTransactionV1
    ) -> bool | None: ...


def _client_error_code(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return ""
    body = response.get("Error")
    if not isinstance(body, Mapping):
        return ""
    code = body.get("Code")
    return code if isinstance(code, str) else ""


class CloudFormationEvidenceAdapter:
    """Read exact stack/change-set subjects with one injected regional client."""

    def __init__(
        self,
        client: Any,
        *,
        config: ProductionObservationConfigV1,
    ) -> None:
        self._client = client
        self._config = config

    @staticmethod
    def _template(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError) as error:
                raise ProductionObservationError(
                    "stack template is not canonical JSON"
                ) from error
            value = parsed
        if not isinstance(value, Mapping):
            raise ProductionObservationError("stack template is not an object")
        try:
            canonical_json_bytes(value)
        except (TypeError, ValueError) as error:
            raise ProductionObservationError(
                "stack template cannot be canonicalized"
            ) from error
        return dict(value)

    @staticmethod
    def _parameters(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise ProductionObservationError("stack parameters are malformed")
        result: list[dict[str, str]] = []
        observed: set[str] = set()
        allowed = {"ParameterKey", "ParameterValue", "ResolvedValue"}
        for raw in value:
            if not isinstance(raw, Mapping) or set(raw) - allowed:
                raise ProductionObservationError("stack parameter is malformed")
            key = raw.get("ParameterKey")
            parameter_value = raw.get("ParameterValue")
            if (
                not isinstance(key, str)
                or not key
                or key in observed
                or not isinstance(parameter_value, str)
                or (
                    "ResolvedValue" in raw
                    and not isinstance(raw.get("ResolvedValue"), str)
                )
            ):
                raise ProductionObservationError("stack parameter is not exact")
            observed.add(key)
            result.append({name: raw[name] for name in sorted(raw)})
        return sorted(result, key=lambda item: item["ParameterKey"])

    @staticmethod
    def _content_digest(value: Mapping[str, Any]) -> str:
        try:
            payload = canonical_json_bytes(value)
        except (TypeError, ValueError) as error:
            raise ProductionObservationError(
                "live CloudFormation content is not canonical"
            ) from error
        return hashlib.sha256(payload).hexdigest()

    def _call(self, method_name: str, **arguments: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name, None)
        if method is None or not callable(method):
            raise ProductionObservationError(
                f"injected CloudFormation adapter lacks {method_name}"
            )
        try:
            value = method(**arguments)
        except (TimeoutError, ConnectionError) as error:
            raise ProductionObservationError(
                f"{method_name} ended without authoritative evidence"
            ) from error
        except Exception as error:
            error_code = _client_error_code(error)
            if (
                method_name == "describe_stacks"
                and error_code == "ValidationError"
            ) or (
                method_name == "describe_change_set"
                and error_code == "ChangeSetNotFound"
            ):
                raise ProductionEvidenceAbsent(
                    f"{method_name} subject does not exist"
                ) from error
            raise ProductionObservationError(
                f"{method_name} failed without authoritative evidence"
            ) from error
        if not isinstance(value, dict):
            raise ProductionObservationError(f"{method_name} response is malformed")
        return value

    def _stack(
        self,
        stack_name: str,
        *,
        expected_template_parameter_digest: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            response = self._call("describe_stacks", StackName=stack_name)
        except ProductionEvidenceAbsent:
            return None
        if response.get("NextToken"):
            raise ProductionObservationError("stack observation was paginated")
        stacks = response.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise ProductionObservationError(
                "stack observation must return one exact stack"
            )
        stack = stacks[0]
        if not isinstance(stack, dict) or stack.get("StackName") != stack_name:
            raise ProductionObservationError("stack observation crossed its subject")
        stack_id = stack.get("StackId")
        expected_prefix = (
            f"arn:aws:cloudformation:{self._config.region}:"
            f"{self._config.account}:stack/{stack_name}/"
        )
        if not isinstance(stack_id, str) or not stack_id.startswith(expected_prefix):
            raise ProductionObservationError(
                "stack observation crossed account, region, or stack name"
            )
        status = stack.get("StackStatus")
        if status not in _COMPLETE_STACK_STATES:
            raise ProductionObservationError(
                f"stack {stack_name} is not in a proven complete state: {status!r}"
            )
        template = self._call("get_template", StackName=stack_name)
        if (
            set(template) - {"TemplateBody", "Stages", "ResponseMetadata"}
            or "TemplateBody" not in template
        ):
            raise ProductionObservationError("stack template response is not exact")
        canonical_template = self._template(template["TemplateBody"])
        canonical_parameters = self._parameters(stack.get("Parameters", []))
        template_parameter_digest = self._content_digest(
            {
                "parameters": canonical_parameters,
                "template": canonical_template,
            }
        )
        if (
            expected_template_parameter_digest is not None
            and template_parameter_digest != expected_template_parameter_digest
        ):
            raise ProductionObservationError(
                f"stack {stack_name} differs from reviewed template and parameters"
            )
        return {
            "stackId": stack_id,
            "stackName": stack_name,
            "status": status,
            "parameters": canonical_parameters,
            "outputs": sorted(
                stack.get("Outputs", []),
                key=lambda item: (
                    item.get("OutputKey", "") if isinstance(item, dict) else ""
                ),
            ),
            "capabilities": sorted(stack.get("Capabilities", [])),
            "roleArn": stack.get("RoleARN", ""),
            "template": canonical_template,
            "templateParameterDigest": template_parameter_digest,
        }

    def _snapshot(
        self,
        stack_names: tuple[str, ...],
        *,
        expected_digests: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...] | None:
        observed = tuple(
            self._stack(
                name,
                expected_template_parameter_digest=(
                    expected_digests[name] if expected_digests is not None else None
                ),
            )
            for name in stack_names
        )
        present = tuple(item for item in observed if item is not None)
        if not present:
            return None
        if len(present) != len(stack_names):
            raise ProductionObservationError(
                "release stack observation is partial and remains ambiguous"
            )
        return present

    @staticmethod
    def _digest(value: object) -> str:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    @staticmethod
    def _outputs(stack: Mapping[str, Any]) -> dict[str, str]:
        raw = stack.get("outputs")
        if not isinstance(raw, list):
            raise ProductionObservationError("stack outputs are malformed")
        result: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ProductionObservationError("stack output is malformed")
            key = item.get("OutputKey")
            value = item.get("OutputValue")
            if not isinstance(key, str) or not isinstance(value, str) or key in result:
                raise ProductionObservationError("stack output is not exact")
            result[key] = value
        return result

    def observe_foundation(self, transaction: StagingTransactionV1) -> bool | None:
        expected = dict(self._config.foundation_stack_template_parameter_digests)
        return (
            True
            if self._snapshot(
                FOUNDATION_STACKS,
                expected_digests=expected,
            )
            is not None
            else None
        )

    def observe_runtime_identity(
        self, transaction: StagingTransactionV1
    ) -> tuple[str, str] | None:
        foundation_expected = dict(
            self._config.foundation_stack_template_parameter_digests
        )
        observed: dict[str, dict[str, Any]] = {}
        for name in FOUNDATION_STACKS:
            stack = self._stack(
                name,
                expected_template_parameter_digest=(
                    None if name == "OpenClawAgentCore" else foundation_expected[name]
                ),
            )
            if stack is None:
                raise ProductionObservationError(
                    "runtime phase lastStable foundation prerequisite is missing"
                )
            observed[name] = stack
        stack = observed["OpenClawAgentCore"]
        outputs = self._outputs(stack)
        runtime_id = outputs.get("RuntimeId")
        runtime_version = outputs.get("RuntimeVersion")
        if runtime_id is None and runtime_version is None:
            if (
                stack["templateParameterDigest"]
                != foundation_expected["OpenClawAgentCore"]
            ):
                raise ProductionObservationError(
                    "runtime absence differs from reviewed foundation prerequisite"
                )
            return None
        if (
            stack["templateParameterDigest"]
            != self._config.runtime_stack_template_parameter_digest
        ):
            raise ProductionObservationError(
                "runtime stack differs from reviewed template and parameters"
            )
        expected_uri = (
            f"{transaction.account}.dkr.ecr.{transaction.region}.amazonaws.com/"
            f"personal-operator/bridge@{transaction.runtime_image_digest}"
        )
        if (
            not isinstance(runtime_id, str)
            or not isinstance(runtime_version, str)
            or outputs.get("RuntimeImageUri") != expected_uri
            or outputs.get("RuntimeSourceCommit") != transaction.source_commit
        ):
            raise ProductionObservationError(
                "runtime stack outputs differ from the exact release"
            )
        return runtime_id, runtime_version

    def _change_set(self, stack_name: str) -> dict[str, Any] | None:
        change_set_name = f"release-{self._config.source_commit}"
        try:
            response = self._call(
                "describe_change_set",
                StackName=stack_name,
                ChangeSetName=change_set_name,
                IncludePropertyValues=True,
            )
        except ProductionEvidenceAbsent:
            return None
        if "NextToken" in response:
            raise ProductionObservationError(
                "consumer change-set observation was paginated or truncated"
            )
        if response.get("StackName") != stack_name:
            raise ProductionObservationError("change set crossed its stack subject")
        if response.get("ChangeSetName") != change_set_name:
            raise ProductionObservationError("change set name is not commit bound")
        expected_stack_prefix = (
            f"arn:aws:cloudformation:{self._config.region}:"
            f"{self._config.account}:stack/{stack_name}/"
        )
        expected_change_set_prefix = (
            f"arn:aws:cloudformation:{self._config.region}:"
            f"{self._config.account}:changeSet/{change_set_name}/"
        )
        if (
            not isinstance(response.get("StackId"), str)
            or not response["StackId"].startswith(expected_stack_prefix)
            or not isinstance(response.get("ChangeSetId"), str)
            or not response["ChangeSetId"].startswith(expected_change_set_prefix)
        ):
            raise ProductionObservationError(
                "change set crossed account, region, stack, or commit subject"
            )
        if response.get("ChangeSetType") not in {"CREATE", "UPDATE"}:
            raise ProductionObservationError("change set type is not deployable")
        if response.get("Status") != "CREATE_COMPLETE":
            raise ProductionObservationError(
                f"change set is not complete: {response.get('Status')!r}"
            )
        sequence_fields = {
            "Parameters": "parameters",
            "NotificationARNs": "notificationArns",
            "Capabilities": "capabilities",
            "Tags": "tags",
            "Changes": "changes",
        }
        if not {"Parameters", "Capabilities", "Changes"}.issubset(response):
            raise ProductionObservationError(
                "change set response omits complete executable content"
            )
        sequences: dict[str, list[Any]] = {}
        for source, target in sequence_fields.items():
            raw = response.get(source, [])
            if not isinstance(raw, list):
                raise ProductionObservationError(
                    f"change set {source} content is malformed"
                )
            sequences[target] = list(raw)
        if any(
            not isinstance(item, Mapping)
            for field in ("parameters", "tags", "changes")
            for item in sequences[field]
        ) or any(
            not isinstance(item, str)
            for field in ("notificationArns", "capabilities")
            for item in sequences[field]
        ):
            raise ProductionObservationError("change set content is not canonical")
        sequences["parameters"] = sorted(
            sequences["parameters"],
            key=lambda item: canonical_json_bytes(item)
            if isinstance(item, Mapping)
            else b"",
        )
        sequences["notificationArns"] = sorted(sequences["notificationArns"])
        sequences["capabilities"] = sorted(sequences["capabilities"])
        sequences["tags"] = sorted(
            sequences["tags"],
            key=lambda item: canonical_json_bytes(item)
            if isinstance(item, Mapping)
            else b"",
        )
        sequences["changes"] = sorted(
            sequences["changes"],
            key=lambda item: canonical_json_bytes(item)
            if isinstance(item, Mapping)
            else b"",
        )
        mapping_fields = {
            "RollbackConfiguration": "rollbackConfiguration",
            "DeploymentMode": "deploymentMode",
            "DeploymentConfig": "deploymentConfig",
        }
        mappings: dict[str, dict[str, Any]] = {}
        for source, target in mapping_fields.items():
            raw = response.get(source, {})
            if not isinstance(raw, Mapping):
                raise ProductionObservationError(
                    f"change set {source} content is malformed"
                )
            mappings[target] = dict(raw)
        scalar_defaults = {
            "Description": "",
            "IncludeNestedStacks": False,
            "OnStackFailure": "",
            "ImportExistingResources": False,
        }
        scalars = {
            source: response.get(source, default)
            for source, default in scalar_defaults.items()
        }
        if (
            not isinstance(scalars["Description"], str)
            or not isinstance(scalars["IncludeNestedStacks"], bool)
            or not isinstance(scalars["OnStackFailure"], str)
            or not isinstance(scalars["ImportExistingResources"], bool)
        ):
            raise ProductionObservationError("change set scalar content is malformed")
        content = {
            "stackName": stack_name,
            "changeSetName": change_set_name,
            "changeSetType": response.get("ChangeSetType"),
            "description": scalars["Description"],
            **sequences,
            **mappings,
            "includeNestedStacks": scalars["IncludeNestedStacks"],
            "onStackFailure": scalars["OnStackFailure"],
            "importExistingResources": scalars["ImportExistingResources"],
        }
        content_digest = self._content_digest(content)
        expected_digest = dict(
            self._config.consumer_change_set_content_digests
        )[stack_name]
        if content_digest != expected_digest:
            raise ProductionObservationError(
                f"change set {stack_name} differs from reviewed complete content"
            )
        return {
            "content": content,
            "contentDigest": content_digest,
            "executionStatus": response.get("ExecutionStatus"),
        }

    def _change_sets(self) -> tuple[dict[str, Any], ...] | None:
        observed = tuple(self._change_set(name) for name in CONSUMER_STACKS)
        present = tuple(item for item in observed if item is not None)
        if not present:
            return None
        if len(present) != len(CONSUMER_STACKS):
            raise ProductionObservationError(
                "consumer change-set observation is partial and ambiguous"
            )
        return present

    def observe_consumer_changesets(
        self, transaction: StagingTransactionV1
    ) -> str | None:
        changes = self._change_sets()
        if changes is None:
            return None
        if any(item.get("executionStatus") != "AVAILABLE" for item in changes):
            raise ProductionObservationError(
                "consumer change sets are not all available for reviewed execution"
            )
        stable = tuple(
            item["content"]
            for item in changes
        )
        return self._digest({"consumerChangeSets": stable})

    def _consumer_application(self) -> tuple[dict[str, Any], ...] | None:
        changes = self._change_sets()
        if changes is None:
            raise ProductionObservationError(
                "consumer application change-set prerequisite is missing"
            )
        stacks = self._snapshot(
            CONSUMER_STACKS,
            expected_digests=dict(
                self._config.consumer_stack_template_parameter_digests
            ),
        )
        if stacks is None:
            if all(
                item.get("executionStatus") == "AVAILABLE" for item in changes
            ):
                return None
            raise ProductionObservationError(
                "consumer stack absence contradicts change-set execution state"
            )
        if any(
            item.get("executionStatus") != "EXECUTE_COMPLETE"
            for item in changes
        ):
            raise ProductionObservationError(
                "consumer changesets are not all proven executed"
            )
        return stacks

    def observe_consumers(self, transaction: StagingTransactionV1) -> str | None:
        stacks = self._consumer_application()
        if stacks is None:
            return None
        return self._digest({"consumerStacks": stacks})

    def observe_verification(
        self,
        transaction: StagingTransactionV1,
        *,
        image: RuntimeImageEvidence,
        context: RuntimeContextV3,
    ) -> str | None:
        expected_foundation = dict(
            self._config.foundation_stack_template_parameter_digests
        )
        expected_foundation["OpenClawAgentCore"] = (
            self._config.runtime_stack_template_parameter_digest
        )
        foundation = self._snapshot(
            FOUNDATION_STACKS,
            expected_digests=expected_foundation,
        )
        consumers = self._consumer_application()
        if foundation is None or consumers is None:
            return None
        application_digest = self._digest({"consumerStacks": consumers})
        if application_digest != transaction.consumer_application_sha256:
            raise ProductionObservationError(
                "live consumer application differs from the journal"
            )
        return self._digest(
            {
                "transactionId": transaction.transaction_id,
                "foundationStacks": foundation,
                "consumerStacks": consumers,
                "runtimeImageEvidence": image.to_mapping(),
                "runtimeContext": context.to_mapping(),
            }
        )

    def observe_rollback(self, transaction: StagingTransactionV1) -> bool | None:
        stacks = self._snapshot((*FOUNDATION_STACKS, *CONSUMER_STACKS))
        snapshot = {"stacks": stacks or ()}
        expected = transaction.rollback_reference.rsplit(":sha256:", 1)[1]
        if self._digest(snapshot) == expected:
            return True
        raise ProductionObservationError(
            "live stack state proves neither the exact rollback reference nor absence"
        )


class ProductionEvidenceComposer:
    """Compose every phase outcome from strict injected live adapters."""

    def __init__(
        self,
        *,
        ecr: EcrEvidenceAdapter,
        agentcore: AgentCoreEvidenceAdapter,
        deployment: DeploymentEvidenceAdapter,
        config: ProductionObservationConfigV1,
    ) -> None:
        if not isinstance(config, ProductionObservationConfigV1):
            raise ProductionObservationError(
                "production observation config is not canonical"
            )
        self._ecr = ecr
        self._agentcore = agentcore
        self._deployment = deployment
        self._config = config

    def _assert_transaction(self, transaction: StagingTransactionV1) -> None:
        if not isinstance(transaction, StagingTransactionV1):
            raise ProductionObservationError("staging transaction is not canonical")
        identity = (
            transaction.source_commit,
            transaction.source_tree,
            transaction.account,
            transaction.region,
        )
        expected = (
            self._config.source_commit,
            self._config.source_tree,
            self._config.account,
            self._config.region,
        )
        if identity != expected or transaction.state != "UNCERTAIN":
            raise ProductionObservationError(
                "production observation identity differs from the journal"
            )

    def _image(self) -> RuntimeImageEvidence:
        evidence = self._ecr.collect(
            source_commit=self._config.source_commit,
            source_tree=self._config.source_tree,
            account=self._config.account,
            region=self._config.region,
            build_context=self._config.build_context,
            builder_id=self._config.builder_id,
            builder_inputs=self._config.builder_inputs,
        )
        if (
            not isinstance(evidence, RuntimeImageEvidence)
            or evidence.source_commit != self._config.source_commit
            or evidence.source_tree != self._config.source_tree
            or evidence.account != self._config.account
            or evidence.region != self._config.region
        ):
            raise ProductionObservationError(
                "ECR evidence identity differs from the release"
            )
        return evidence

    def image_evidence(self) -> dict[str, object]:
        return {"runtime_image_evidence": self._image().to_mapping()}

    def _agentcore_arguments(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> dict[str, Any]:
        if (
            _RUNTIME_ID.fullmatch(runtime_id) is None
            or _VERSION.fullmatch(runtime_version) is None
            or _DIGEST.fullmatch(runtime_image_digest) is None
        ):
            raise ProductionObservationError(
                "AgentCore observation identity is invalid"
            )
        return {
            "source_commit": self._config.source_commit,
            "account": self._config.account,
            "region": self._config.region,
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "runtime_image_uri": (
                f"{self._config.account}.dkr.ecr.{self._config.region}.amazonaws.com/"
                f"personal-operator/bridge@{runtime_image_digest}"
            ),
            "expected_subnet_ids": self._config.runtime_subnet_ids,
            "expected_security_group_ids": (
                self._config.runtime_security_group_ids
            ),
            "expected_environment_variables": dict(
                self._config.runtime_environment_variables
            ),
            "expected_idle_runtime_session_timeout": (
                self._config.runtime_idle_session_timeout
            ),
            "expected_max_lifetime": self._config.runtime_max_lifetime,
        }

    def _runtime_context(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> RuntimeContextV3:
        context = self._agentcore.collect_context(
            **self._agentcore_arguments(
                runtime_id=runtime_id,
                runtime_version=runtime_version,
                runtime_image_digest=runtime_image_digest,
            )
        )
        expected_image_uri = (
            f"{self._config.account}.dkr.ecr.{self._config.region}.amazonaws.com/"
            f"personal-operator/bridge@{runtime_image_digest}"
        )
        if (
            not isinstance(context, RuntimeContextV3)
            or context.source_commit != self._config.source_commit
            or context.account != self._config.account
            or context.region != self._config.region
            or context.runtime_id != runtime_id
            or context.runtime_version != runtime_version
            or context.runtime_endpoint_name
            != f"release_{self._config.source_commit}"
            or context.runtime_image_uri != expected_image_uri
        ):
            raise ProductionObservationError(
                "AgentCore evidence identity differs from the release"
            )
        return context

    def _require_foundation(self, transaction: StagingTransactionV1) -> None:
        if self._deployment.observe_foundation(transaction) is not True:
            raise ProductionObservationError(
                "lastStable foundation prerequisite is missing"
            )

    def _require_image(self, transaction: StagingTransactionV1) -> RuntimeImageEvidence:
        try:
            image = self._image()
        except (EcrImageAbsent, EcrRepositoryAbsent) as error:
            raise ProductionObservationError(
                "lastStable image prerequisite is missing"
            ) from error
        if image.image_digest != transaction.runtime_image_digest:
            raise ProductionObservationError(
                "lastStable image prerequisite differs from the journal"
            )
        return image

    def _require_runtime(
        self,
        transaction: StagingTransactionV1,
    ) -> tuple[str, str]:
        identity = self._deployment.observe_runtime_identity(transaction)
        expected = (transaction.runtime_id, transaction.runtime_version)
        if identity is None or identity != expected:
            raise ProductionObservationError(
                "lastStable runtime prerequisite is missing or contradictory"
            )
        try:
            observed = self._agentcore.collect_runtime_identity(
                **self._agentcore_arguments(
                    runtime_id=identity[0],
                    runtime_version=identity[1],
                    runtime_image_digest=transaction.runtime_image_digest,
                )
            )
        except AgentCoreEvidenceAbsent as error:
            raise ProductionObservationError(
                "lastStable runtime prerequisite is missing"
            ) from error
        if observed != identity:
            raise ProductionObservationError(
                "AgentCore runtime prerequisite differs from CloudFormation"
            )
        return identity

    def _require_runtime_context(
        self,
        transaction: StagingTransactionV1,
    ) -> RuntimeContextV3:
        self._require_image(transaction)
        self._require_runtime(transaction)
        try:
            return self._runtime_context(
                runtime_id=transaction.runtime_id,
                runtime_version=transaction.runtime_version,
                runtime_image_digest=transaction.runtime_image_digest,
            )
        except AgentCoreEvidenceAbsent as error:
            raise ProductionObservationError(
                "lastStable endpoint prerequisite is missing"
            ) from error

    def endpoint_evidence(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> dict[str, object]:
        context = self._runtime_context(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_digest=runtime_image_digest,
        )
        return {"runtime_context": context.to_mapping()}

    def context_evidence(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> dict[str, object]:
        context = self._runtime_context(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_digest=runtime_image_digest,
        )
        return {
            "runtime_context": context.to_mapping(),
            "runtime_context_sha256": hashlib.sha256(context.to_bytes()).hexdigest(),
        }

    @staticmethod
    def _digest_evidence(field: str, digest: str | None) -> tuple[bool, dict[str, str]]:
        if digest is None:
            return False, {}
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ProductionObservationError(
                f"{field} live evidence digest is not canonical"
            )
        return True, {field: digest}

    def observe_phase(
        self,
        phase: str,
        transaction: StagingTransactionV1,
    ) -> tuple[bool, dict[str, str]]:
        self._assert_transaction(transaction)
        try:
            if phase == "foundation":
                return (
                    (True, {})
                    if self._deployment.observe_foundation(transaction) is True
                    else (False, {})
                )
            if phase == "image":
                self._require_foundation(transaction)
                try:
                    image = self._image()
                except EcrImageAbsent:
                    return False, {}
                except EcrRepositoryAbsent as error:
                    raise ProductionObservationError(
                        "lastStable repository prerequisite is missing"
                    ) from error
                return True, {"runtime_image_digest": image.image_digest}
            if phase == "runtime":
                self._require_image(transaction)
                identity = self._deployment.observe_runtime_identity(transaction)
                if identity is None:
                    return False, {}
                if (
                    not isinstance(identity, tuple)
                    or len(identity) != 2
                    or any(not isinstance(value, str) for value in identity)
                ):
                    raise ProductionObservationError(
                        "runtime identity observation is malformed"
                    )
                runtime_id, runtime_version = identity
                try:
                    observed = self._agentcore.collect_runtime_identity(
                        **self._agentcore_arguments(
                            runtime_id=runtime_id,
                            runtime_version=runtime_version,
                            runtime_image_digest=transaction.runtime_image_digest,
                        )
                    )
                except AgentCoreEvidenceAbsent as error:
                    raise ProductionObservationError(
                        "runtime phase prerequisite evidence is contradictory"
                    ) from error
                if observed != identity:
                    raise ProductionObservationError(
                        "AgentCore runtime identity differs from CloudFormation"
                    )
                return True, {
                    "runtime_id": runtime_id,
                    "runtime_version": runtime_version,
                }
            if phase in {"endpoint", "context"}:
                self._require_image(transaction)
                self._require_runtime(transaction)
                try:
                    context = self._runtime_context(
                        runtime_id=transaction.runtime_id,
                        runtime_version=transaction.runtime_version,
                        runtime_image_digest=transaction.runtime_image_digest,
                    )
                except AgentCoreEndpointAbsent:
                    if phase == "endpoint":
                        return False, {}
                    raise ProductionObservationError(
                        "lastStable endpoint prerequisite is missing"
                    )
                except AgentCoreRuntimeAbsent as error:
                    raise ProductionObservationError(
                        "lastStable runtime prerequisite is missing"
                    ) from error
                if phase == "endpoint":
                    return True, {}
                return True, {
                    "runtime_context_sha256": hashlib.sha256(
                        context.to_bytes()
                    ).hexdigest()
                }
            if phase == "consumer-changesets":
                self._require_runtime_context(transaction)
                return self._digest_evidence(
                    "consumer_changesets_sha256",
                    self._deployment.observe_consumer_changesets(transaction),
                )
            if phase == "consumers":
                self._require_runtime_context(transaction)
                return self._digest_evidence(
                    "consumer_application_sha256",
                    self._deployment.observe_consumers(transaction),
                )
            if phase == "verify":
                image = self._require_image(transaction)
                self._require_runtime(transaction)
                context = self._require_runtime_context(transaction)
                if (
                    hashlib.sha256(context.to_bytes()).hexdigest()
                    != transaction.runtime_context_sha256
                ):
                    raise ProductionObservationError(
                        "live runtime context differs from the journal"
                    )
                verification = self._deployment.observe_verification(
                    transaction,
                    image=image,
                    context=context,
                )
                if verification is None:
                    raise ProductionObservationError(
                        "verification prerequisites are missing or contradictory"
                    )
                return self._digest_evidence(
                    "verification_sha256",
                    verification,
                )
            if phase == "rollback":
                try:
                    disposition = self._agentcore.observe_retained_disposition(
                        **self._agentcore_arguments(
                            runtime_id=transaction.runtime_id,
                            runtime_version=transaction.runtime_version,
                            runtime_image_digest=transaction.runtime_image_digest,
                        )
                    )
                except AgentCoreEvidenceError as error:
                    raise ProductionObservationError(
                        "rollback retained AgentCore disposition is ambiguous"
                    ) from error
                if disposition not in {"PRESENT", "ABSENT"}:
                    raise ProductionObservationError(
                        "rollback retained AgentCore disposition is malformed"
                    )
                return (
                    (True, {})
                    if self._deployment.observe_rollback(transaction) is True
                    else (False, {})
                )
        except (AgentCoreEvidenceError, EcrEvidenceError) as error:
            raise ProductionObservationError(
                f"{phase} live evidence is unavailable or ambiguous"
            ) from error
        raise ProductionObservationError(f"unknown release phase {phase!r}")


def compose_production_evidence(
    *,
    ecr_client: EcrClient,
    artifact_blob_reader: ArtifactBlobReader,
    agentcore_client: AgentCoreClient,
    cloudformation_client: Any,
    config: ProductionObservationConfigV1,
) -> ProductionEvidenceComposer:
    """Wire exact live adapters from injected, already-authorized clients."""

    return ProductionEvidenceComposer(
        ecr=EcrEvidenceAdapter(ecr_client, blob_reader=artifact_blob_reader),
        agentcore=AgentCoreEvidenceAdapter(agentcore_client),
        deployment=CloudFormationEvidenceAdapter(
            cloudformation_client,
            config=config,
        ),
        config=config,
    )
