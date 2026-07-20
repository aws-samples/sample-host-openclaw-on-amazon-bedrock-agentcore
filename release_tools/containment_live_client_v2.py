"""Live-AWS teardown client for the approved synthetic teardown adapter.

``release_tools.containment_adapter_v2`` turns the pure containment/purge
contract into mutating and read-only effects, but it dispatches every effect
through an injected :class:`AttestedTeardownClientV1`.  That abstract surface
is deliberately empty of live I/O; this module supplies the one implementation
that drives *real* boto3 delete/observe APIs while preserving every discipline
the accepted v2 boundary requires:

* Injected authority only.  A live client is constructed from an
  already-authenticated boto3-like client (mirroring
  ``AuthenticatedAwsAuthorityV2``); it never freezes credentials, builds a
  session, or reaches for an ambient provider chain.  Account and region are
  pinned to the exact preclosed release (``eu-west-1``).
* Closed method allowlist.  Each client is scoped to one service and one
  capability (mutation *or* observer).  ``invoke`` rejects any method outside
  the exact per-service teardown/observation catalog, and KMS is only ever
  scheduled for deletion (never immediately deleted).
* Fail closed on ambiguity.  A mutation is translated to
  ``{"acknowledged": True}`` only on a definitively successful response;
  throttling, a non-2xx status, a partial batch failure, a malformed body, or
  any raised transport/client error yields a shape *without*
  ``acknowledged=True`` so the adapter keeps the release ``UNCERTAIN`` and never
  replays a possibly-applied destructive effect.
* Exact account ownership.  Every S3 call carries
  ``ExpectedBucketOwner=<account>``; ``require_scope`` fails closed on any
  service/account/region/capability mismatch.

No credentials, session tokens, or raw provider responses are ever logged; the
translated observation exposes only a coarse live-state label and a monotonic
read sequence.  There is no dynamic import, ``eval``, or ``exec``.
"""

from __future__ import annotations

from typing import Any, Mapping

from release_tools.containment_adapter_v2 import (
    AttestedTeardownClientV1,
    ContainmentAdapterError,
    REQUIRED_REGION,
    require_exact_teardown_identity,
)


class LiveTeardownClientError(ContainmentAdapterError):
    """A live teardown client request crosses its exact closed scope."""


# Every live state the observer contract accepts.  UNKNOWN is deliberately a
# terminal-blocking value: the adapter maps it to an AMBIGUOUS disposition, so a
# throttled or malformed read can never advance the destructive cursor.
_LIVE_STATES = frozenset({"ABSENT", "PRESENT", "SCHEDULED", "CANCELED", "UNKNOWN"})

_CAPABILITIES = frozenset({"mutation", "observer"})

_S3_SERVICE = "s3"
_KMS_SERVICE = "kms"


# (service, capability) -> exact boto3 (snake_case) methods this client may call.
# These are precisely the methods the accepted adapter dispatches; anything
# else is rejected.  KMS mutation is ``schedule_key_deletion`` only: the
# immediate ``delete_key`` API is intentionally absent from every catalog.
_MUTATION_METHODS: Mapping[str, frozenset[str]] = {
    "cloudformation": frozenset({"delete_stack"}),
    "bedrock-agentcore-control": frozenset(
        {
            "delete_agent_runtime",
            "delete_agent_runtime_endpoint",
            "delete_resource_policy",
        }
    ),
    "s3": frozenset({"abort_multipart_upload", "delete_object", "delete_bucket"}),
    "ecr": frozenset(
        {"batch_delete_image", "delete_repository", "delete_signing_configuration"}
    ),
    "signer": frozenset({"cancel_signing_profile"}),
    "dynamodb": frozenset({"delete_table"}),
    "logs": frozenset({"delete_log_group"}),
    "kms": frozenset({"schedule_key_deletion"}),
}

_OBSERVER_METHODS: Mapping[str, frozenset[str]] = {
    "cloudformation": frozenset({"describe_stacks"}),
    "bedrock-agentcore-control": frozenset(
        {"get_agent_runtime", "get_agent_runtime_endpoint", "get_resource_policy"}
    ),
    "s3": frozenset({"head_bucket", "list_object_versions", "list_multipart_uploads"}),
    "ecr": frozenset(
        {"batch_get_image", "describe_repositories", "get_signing_configuration"}
    ),
    "signer": frozenset({"get_signing_profile"}),
    "dynamodb": frozenset({"describe_table"}),
    "logs": frozenset({"describe_log_groups"}),
    "kms": frozenset({"describe_key"}),
}

_ALL_SERVICES = frozenset(_MUTATION_METHODS) | frozenset(_OBSERVER_METHODS)

# The adapter passes a generic ``ResourceIdentity`` kwarg for every non-S3,
# non-KMS target.  Map it to the exact boto3 request parameter per method so
# the raw SDK receives a well-formed request (and never the synthetic
# ``ExpectedAccount``/``ResourceIdentity`` keys, which real APIs reject).
_IDENTITY_PARAM: Mapping[tuple[str, str], str] = {
    ("cloudformation", "delete_stack"): "StackName",
    ("cloudformation", "describe_stacks"): "StackName",
    ("bedrock-agentcore-control", "delete_resource_policy"): "resourceArn",
    ("bedrock-agentcore-control", "get_resource_policy"): "resourceArn",
    ("bedrock-agentcore-control", "delete_agent_runtime"): "agentRuntimeArn",
    ("bedrock-agentcore-control", "get_agent_runtime"): "agentRuntimeArn",
    ("bedrock-agentcore-control", "delete_agent_runtime_endpoint"): (
        "agentRuntimeEndpointArn"
    ),
    ("bedrock-agentcore-control", "get_agent_runtime_endpoint"): (
        "agentRuntimeEndpointArn"
    ),
    ("ecr", "batch_delete_image"): "repositoryName",
    ("ecr", "batch_get_image"): "repositoryName",
    ("ecr", "delete_repository"): "repositoryName",
    ("ecr", "describe_repositories"): "repositoryName",
    ("ecr", "delete_signing_configuration"): "repositoryName",
    ("ecr", "get_signing_configuration"): "repositoryName",
    ("signer", "cancel_signing_profile"): "profileName",
    ("signer", "get_signing_profile"): "profileName",
    ("dynamodb", "delete_table"): "TableName",
    ("dynamodb", "describe_table"): "TableName",
    ("logs", "delete_log_group"): "logGroupName",
    ("logs", "describe_log_groups"): "logGroupNamePrefix",
}

# Provider error codes that authoritatively prove a subject is already absent.
_ABSENT_CODES: Mapping[str, frozenset[str]] = {
    "cloudformation": frozenset({"ValidationError"}),
    "bedrock-agentcore-control": frozenset(
        {"ResourceNotFoundException", "ResourceNotFound", "NotFoundException"}
    ),
    "s3": frozenset({"404", "NoSuchBucket", "NotFound"}),
    "ecr": frozenset(
        {
            "RepositoryNotFoundException",
            "ImageNotFoundException",
            "RegistryPolicyNotFoundException",
            "SigningConfigurationNotFoundException",
        }
    ),
    "signer": frozenset({"ResourceNotFoundException", "NotFoundException"}),
    "dynamodb": frozenset({"ResourceNotFoundException"}),
    "logs": frozenset({"ResourceNotFoundException"}),
    "kms": frozenset({"NotFoundException", "NotFound"}),
}


def _error_details(error: BaseException) -> tuple[str, str, int | None]:
    """Extract (code, message, http-status) from a botocore-shaped error.

    Mirrors ``production_observer_v2`` so no botocore import is required and a
    synthetic fake can present the same ``error.response`` structure.
    """

    response = getattr(error, "response", None)
    body = response.get("Error") if isinstance(response, Mapping) else None
    metadata = (
        response.get("ResponseMetadata") if isinstance(response, Mapping) else None
    )
    code = body.get("Code") if isinstance(body, Mapping) else ""
    message = body.get("Message") if isinstance(body, Mapping) else ""
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return (
        code if isinstance(code, str) else "",
        message if isinstance(message, str) else "",
        status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


def _http_status(response: Mapping[str, Any]) -> int | None:
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    status = metadata.get("HTTPStatusCode")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


class LiveTeardownClientV1(AttestedTeardownClientV1):
    """One service/capability-scoped bridge from the adapter to a boto3 client.

    Instances subclass :class:`AttestedTeardownClientV1` so the adapter's
    ``isinstance`` gate accepts them.  A caller builds the full
    ``service -> client`` mapping the provider/observer expect via
    :meth:`mutation_clients` / :meth:`observer_clients`, passing the already
    authenticated boto3-like clients it holds.
    """

    __slots__ = ("_client", "_service", "_account", "_region", "_capability", "_reads")

    def __init__(
        self,
        client: object,
        *,
        service: str,
        account: str,
        region: str,
        capability: str,
    ) -> None:
        if client is None:
            raise LiveTeardownClientError("live teardown client is unavailable")
        if service not in _ALL_SERVICES:
            raise LiveTeardownClientError("live teardown service is outside the catalog")
        if capability not in _CAPABILITIES:
            raise LiveTeardownClientError("live teardown capability is invalid")
        if (
            not isinstance(account, str)
            or len(account) != 12
            or not account.isdigit()
            or account == "000000000000"
        ):
            raise LiveTeardownClientError("live teardown account is invalid")
        if region != REQUIRED_REGION:
            raise LiveTeardownClientError(
                f"live teardown region must be exactly {REQUIRED_REGION}"
            )
        catalog = _MUTATION_METHODS if capability == "mutation" else _OBSERVER_METHODS
        if service not in catalog or not catalog[service]:
            raise LiveTeardownClientError(
                "live teardown service has no capability of this kind"
            )
        self._client = client
        self._service = service
        self._account = account
        self._region = region
        self._capability = capability
        self._reads = 0

    # -- construction helpers -------------------------------------------------

    @classmethod
    def _scoped_map(
        cls,
        boto3_clients: Mapping[str, object],
        *,
        account: str,
        region: str,
        capability: str,
    ) -> dict[str, "LiveTeardownClientV1"]:
        if not isinstance(boto3_clients, Mapping) or not boto3_clients:
            raise LiveTeardownClientError("live teardown client map is invalid")
        catalog = _MUTATION_METHODS if capability == "mutation" else _OBSERVER_METHODS
        scoped: dict[str, LiveTeardownClientV1] = {}
        for service, client in boto3_clients.items():
            if service not in catalog or not catalog[service]:
                # Ignore services with no capability of this kind (e.g. a client
                # only needed for the other phase); never fabricate one.
                continue
            scoped[service] = cls(
                client,
                service=service,
                account=account,
                region=region,
                capability=capability,
            )
        if not scoped:
            raise LiveTeardownClientError("live teardown client map is empty")
        return scoped

    @classmethod
    def mutation_clients(
        cls,
        boto3_clients: Mapping[str, object],
        *,
        account: str,
        region: str,
    ) -> dict[str, "LiveTeardownClientV1"]:
        """Build the ``service -> client`` mapping for the destructive provider."""

        return cls._scoped_map(
            boto3_clients, account=account, region=region, capability="mutation"
        )

    @classmethod
    def observer_clients(
        cls,
        boto3_clients: Mapping[str, object],
        *,
        account: str,
        region: str,
    ) -> dict[str, "LiveTeardownClientV1"]:
        """Build the ``service -> client`` mapping for the teardown observer."""

        return cls._scoped_map(
            boto3_clients, account=account, region=region, capability="observer"
        )

    # -- attested surface -----------------------------------------------------

    def require_scope(
        self, *, service: str, account: str, region: str, capability: str
    ) -> None:
        if (service, account, region, capability) != (
            self._service,
            self._account,
            self._region,
            self._capability,
        ):
            raise LiveTeardownClientError("live teardown client scope differs")

    def invoke(self, method_name: str, **kwargs: Any) -> object:
        if not isinstance(method_name, str) or not method_name or method_name.startswith("_"):
            raise LiveTeardownClientError("live teardown method is invalid")
        catalog = (
            _MUTATION_METHODS if self._capability == "mutation" else _OBSERVER_METHODS
        )
        if method_name not in catalog[self._service]:
            raise LiveTeardownClientError(
                "live teardown method is outside the closed capability"
            )
        request, context = self._prepare_request(method_name, dict(kwargs))
        if self._capability == "mutation":
            return self._mutate(method_name, request)
        return self._observe(method_name, request, context)

    # -- request shaping ------------------------------------------------------

    def _prepare_request(
        self, method_name: str, kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Translate adapter kwargs into an exact boto3 request.

        Strips the synthetic ``ExpectedAccount``/``ResourceIdentity`` keys, maps
        the identity to the exact boto3 parameter, and always injects
        ``ExpectedBucketOwner`` for S3.  ``context`` carries matching hints the
        observer needs (e.g. a target S3 version/upload id).
        """

        context: dict[str, Any] = {}
        # Account ownership is proven by our pinned scope; the adapter's generic
        # ExpectedAccount marker is not a real boto3 parameter.
        kwargs.pop("ExpectedAccount", None)

        if self._service == _S3_SERVICE:
            if "Bucket" not in kwargs:
                raise LiveTeardownClientError("S3 teardown request lacks its bucket")
            # ALWAYS assert the exact account owns the (global) S3 subject.
            kwargs["ExpectedBucketOwner"] = self._account
            if method_name == "list_object_versions":
                context["target_version_id"] = kwargs.get("VersionId")
                context["target_key"] = kwargs.get("Key")
                prefix = kwargs.pop("Key", None)
                kwargs.pop("VersionId", None)
                if prefix is not None:
                    kwargs["Prefix"] = prefix
            elif method_name == "list_multipart_uploads":
                context["target_upload_id"] = kwargs.get("UploadId")
                context["target_key"] = kwargs.get("Key")
                prefix = kwargs.pop("Key", None)
                kwargs.pop("UploadId", None)
                if prefix is not None:
                    kwargs["Prefix"] = prefix
            return kwargs, context

        if self._service == _KMS_SERVICE:
            if "KeyId" not in kwargs:
                raise LiveTeardownClientError("KMS teardown request lacks its key id")
            return kwargs, context

        identity = kwargs.pop("ResourceIdentity", None)
        if identity is None:
            raise LiveTeardownClientError(
                "live teardown request lacks its exact resource identity"
            )
        require_exact_teardown_identity(identity, label="live teardown target")
        param = _IDENTITY_PARAM.get((self._service, method_name))
        if param is None:
            raise LiveTeardownClientError(
                "live teardown method has no exact identity binding"
            )
        kwargs[param] = identity
        context["identity"] = identity
        return kwargs, context

    def _call_raw(self, method_name: str, request: Mapping[str, Any]) -> object:
        method = getattr(self._client, method_name, None)
        if method is None or not callable(method):
            raise LiveTeardownClientError(
                "attested boto3 client lacks the requested method"
            )
        return method(**dict(request))

    # -- mutation -------------------------------------------------------------

    def _mutate(self, method_name: str, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._call_raw(method_name, request)
        except Exception:
            # A raised transport/client/throttling error is an unknown outcome.
            # Fail closed: never acknowledge, so the adapter stays UNCERTAIN and
            # never replays a possibly-applied destructive effect.
            return {"acknowledged": False, "outcome": "UNCERTAIN"}
        if not isinstance(response, Mapping):
            return {"acknowledged": False, "outcome": "UNCERTAIN"}
        status = _http_status(response)
        if status is None or not (200 <= status <= 299):
            return {"acknowledged": False, "outcome": "UNCERTAIN"}
        # A partial batch delete leaves some images live: not a definitive
        # success, so it must not acknowledge.
        failures = response.get("failures")
        if isinstance(failures, list) and failures:
            return {"acknowledged": False, "outcome": "UNCERTAIN"}
        return {"acknowledged": True}

    # -- observation ----------------------------------------------------------

    def _next_read_sequence(self) -> int:
        self._reads += 1
        return self._reads

    def _observe(
        self, method_name: str, request: Mapping[str, Any], context: Mapping[str, Any]
    ) -> dict[str, Any]:
        sequence = self._next_read_sequence()
        try:
            response = self._call_raw(method_name, request)
        except Exception as error:  # botocore-shaped or transport error
            code, _message, status = _error_details(error)
            if code in _ABSENT_CODES.get(self._service, frozenset()) or status == 404:
                # An authoritative not-found proves the subject is absent.
                state = "ABSENT"
            else:
                # Throttling, transport, or an unreviewed error: never infer
                # absence.  UNKNOWN keeps the disposition AMBIGUOUS.
                state = "UNKNOWN"
            return {"liveState": state, "readSequence": sequence}
        if not isinstance(response, Mapping):
            return {"liveState": "UNKNOWN", "readSequence": sequence}
        state = self._map_live_state(method_name, response, context)
        if state not in _LIVE_STATES:
            state = "UNKNOWN"
        return {"liveState": state, "readSequence": sequence}

    def _map_live_state(
        self,
        method_name: str,
        response: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> str:
        service = self._service
        if service == "cloudformation":  # describe_stacks
            stacks = response.get("Stacks")
            if not isinstance(stacks, list) or not stacks:
                return "ABSENT"
            stack = stacks[0]
            status = stack.get("StackStatus") if isinstance(stack, Mapping) else None
            if status in {"DELETE_COMPLETE"}:
                return "ABSENT"
            return "PRESENT"
        if service == "kms":  # describe_key
            metadata = response.get("KeyMetadata")
            key_state = metadata.get("KeyState") if isinstance(metadata, Mapping) else None
            if key_state == "PendingDeletion":
                return "SCHEDULED"
            if key_state in {"Enabled", "Disabled", "Creating", "Unavailable"}:
                return "PRESENT"
            return "UNKNOWN"
        if service == "signer":  # get_signing_profile
            status = response.get("status")
            if status in {"Canceled", "Cancelled"}:
                return "CANCELED"
            if status in {"Active", "PendingDeletion"}:
                return "PRESENT"
            return "UNKNOWN"
        if service == "s3":
            return self._map_s3_state(method_name, response, context)
        if service == "ecr":
            return self._map_ecr_state(method_name, response)
        if service == "dynamodb":  # describe_table
            table = response.get("Table")
            if isinstance(table, Mapping) and table.get("TableStatus"):
                return "PRESENT"
            return "UNKNOWN"
        if service == "bedrock-agentcore-control":
            # A well-formed get_* response means the subject is still present;
            # absence surfaces as a not-found error handled in ``_observe``.
            return "PRESENT"
        if service == "logs":  # describe_log_groups
            groups = response.get("logGroups")
            target = context.get("identity")
            if isinstance(groups, list) and any(
                isinstance(group, Mapping)
                and group.get("logGroupName") == target
                for group in groups
            ):
                return "PRESENT"
            if isinstance(groups, list):
                return "ABSENT"
            return "UNKNOWN"
        return "UNKNOWN"

    @staticmethod
    def _map_s3_state(
        method_name: str,
        response: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> str:
        if method_name == "head_bucket":
            status = _http_status(response)
            if status is not None and 200 <= status <= 299:
                return "PRESENT"
            return "UNKNOWN"
        if method_name == "list_object_versions":
            versions = response.get("Versions")
            target = context.get("target_version_id")
            if not isinstance(versions, list):
                return "UNKNOWN"
            if any(
                isinstance(item, Mapping) and item.get("VersionId") == target
                for item in versions
            ):
                return "PRESENT"
            return "ABSENT"
        if method_name == "list_multipart_uploads":
            uploads = response.get("Uploads")
            target = context.get("target_upload_id")
            if not isinstance(uploads, list):
                return "UNKNOWN"
            if any(
                isinstance(item, Mapping) and item.get("UploadId") == target
                for item in uploads
            ):
                return "PRESENT"
            return "ABSENT"
        return "UNKNOWN"

    @staticmethod
    def _map_ecr_state(method_name: str, response: Mapping[str, Any]) -> str:
        if method_name == "batch_get_image":
            images = response.get("images")
            failures = response.get("failures")
            if isinstance(images, list) and images:
                return "PRESENT"
            if isinstance(images, list) and not images:
                return "ABSENT"
            if isinstance(failures, list) and failures:
                return "ABSENT"
            return "UNKNOWN"
        if method_name == "describe_repositories":
            repositories = response.get("repositories")
            if isinstance(repositories, list) and repositories:
                return "PRESENT"
            if isinstance(repositories, list):
                return "ABSENT"
            return "UNKNOWN"
        if method_name == "get_signing_configuration":
            status = _http_status(response)
            if status is not None and 200 <= status <= 299:
                return "PRESENT"
            return "UNKNOWN"
        return "UNKNOWN"


__all__ = [
    "LiveTeardownClientV1",
    "LiveTeardownClientError",
]
