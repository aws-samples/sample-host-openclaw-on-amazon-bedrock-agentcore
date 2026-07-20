"""Authenticated, exact-account AWS client authority for release v2.

Only this module freezes ambient credentials, proves the live STS account, and
constructs method-scoped client capabilities accepted by observers and mutation
dispatchers.
Credentials are read only from retained owner-only profile descriptors, passed
only to the retained SDK, and never enter a driver payload.
"""

from __future__ import annotations

import configparser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import os
from pathlib import Path
import pwd
import secrets
import stat
import sys
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol

from release_tools.contracts import ReleasePlanV2
from release_tools.evidence_runtime import (
    EvidenceRuntimeError,
    snapshot_evidence_runtime,
)


REQUIRED_REGION = "eu-west-1"
TARGET_PROFILE = "personal-operator-deploy"
SOURCE_PROFILE = "personal-operator-bootstrap"
DEPLOYMENT_ROLE_NAME = "PersonalOperatorDeploymentRole"
ASSUME_ROLE_DURATION_SECONDS = 3600
_PROFILE_FILE_LIMIT = 64 * 1024
AWS_AUTHORITY_SERVICES = (
    "sts",
    "s3",
    "cloudformation",
    "ecr",
    "bedrock-agentcore-control",
    "ssm",
    "signer",
    "cloudtrail",
    "iam",
)
_EXACT_SERVICE_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "sts": "https://sts.eu-west-1.amazonaws.com",
        "s3": "https://s3.eu-west-1.amazonaws.com",
        "cloudformation": "https://cloudformation.eu-west-1.amazonaws.com",
        "ecr": "https://api.ecr.eu-west-1.amazonaws.com",
        "bedrock-agentcore-control": (
            "https://bedrock-agentcore-control.eu-west-1.amazonaws.com"
        ),
        "ssm": "https://ssm.eu-west-1.amazonaws.com",
        "signer": "https://signer.eu-west-1.amazonaws.com",
        "cloudtrail": "https://cloudtrail.eu-west-1.amazonaws.com",
        "iam": "https://iam.amazonaws.com",
    }
)
_OBSERVER_METHOD_CATALOG: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "sts": frozenset({"get_caller_identity"}),
        "s3": frozenset({"head_bucket", "head_object"}),
        "cloudformation": frozenset(
            {
                "describe_stacks",
                "get_template",
                "get_stack_policy",
                "describe_change_set",
                "describe_stack_drift_detection_status",
                "describe_stack_resource_drifts",
            }
        ),
        "ecr": frozenset(
            {
                "batch_check_layer_availability",
                "batch_get_image",
                "describe_images",
                "describe_image_scan_findings",
                "describe_repositories",
                "get_signing_configuration",
                "describe_image_signing_status",
            }
        ),
        "bedrock-agentcore-control": frozenset(
            {
                "list_agent_runtime_versions",
                "list_agent_runtimes",
                "get_agent_runtime",
                "list_agent_runtime_endpoints",
                "get_agent_runtime_endpoint",
                "get_resource_policy",
            }
        ),
        "ssm": frozenset({"get_parameter"}),
        "signer": frozenset({"get_signing_profile"}),
        "cloudtrail": frozenset({"lookup_events"}),
        "iam": frozenset(
            {
                "get_role",
                "list_role_policies",
                "get_role_policy",
                "list_attached_role_policies",
                "list_role_tags",
            }
        ),
    }
)
# Method authority is necessary but never sufficient to dispatch an effect;
# provider adapters must additionally consume the exact plan/journal capability.
_MUTATION_METHOD_CATALOG: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "sts": frozenset(),
        "s3": frozenset({"put_object"}),
        "cloudformation": frozenset(
            {
                "create_stack",
                "update_stack",
                "create_change_set",
                "execute_change_set",
                "detect_stack_drift",
            }
        ),
        "ecr": frozenset(
            {
                "batch_check_layer_availability",
                "initiate_layer_upload",
                "upload_layer_part",
                "complete_layer_upload",
                "put_image",
            }
        ),
        "bedrock-agentcore-control": frozenset({"update_agent_runtime"}),
        "ssm": frozenset(),
        "signer": frozenset(),
        "cloudtrail": frozenset(),
        "iam": frozenset(),
    }
)
# Public catalogs are inspection-only snapshots. Dispatch closes over the
# independent private mapping proxies above, so rebinding either export cannot
# alter an existing or future capability.
AWS_OBSERVER_METHODS: Mapping[str, frozenset[str]] = MappingProxyType(
    dict(_OBSERVER_METHOD_CATALOG)
)
AWS_MUTATION_METHODS: Mapping[str, frozenset[str]] = MappingProxyType(
    dict(_MUTATION_METHOD_CATALOG)
)
_SDK_MODULE_ROOTS = frozenset(
    {
        "_awscrt",
        "awscrt",
        "boto3",
        "botocore",
        "certifi",
        "dateutil",
        "jmespath",
        "s3transfer",
        "six",
        "urllib3",
    }
)
_AUTHORITY_TOKEN = object()
_CLIENT_TOKEN = object()
_CLIENT_CAPABILITIES = frozenset({"authority", "observer", "mutation"})
_AMBIENT_AWS_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "BOTO_CONFIG",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "AWS_ACCESS_KEY_ID",
        "AWS_CA_BUNDLE",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_DATA_PATH",
        "AWS_DEFAULT_PROFILE",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    }
)
_ENVIRONMENT_LOCK = threading.RLock()
_ENVIRONMENT_STATE = threading.local()


class AwsAuthorityError(RuntimeError):
    """The exact release AWS authority cannot be established or was consumed."""


class _FrozenSession(Protocol):
    def client(
        self,
        service: str,
        *,
        region_name: str,
        config: object,
        verify: str,
    ) -> object: ...


class _SessionFactory(Protocol):
    def __call__(self, **kwargs: str) -> _FrozenSession: ...


class _RawClientRegistryEntry:
    __slots__ = ("client", "service", "account", "region", "closed")

    def __init__(
        self,
        client: object,
        *,
        service: str,
        account: str,
        region: str,
    ) -> None:
        self.client: object | None = client
        self.service = service
        self.account = account
        self.region = region
        self.closed = False


class _ScopeRegistryEntry:
    __slots__ = ("raw_id", "capability")

    def __init__(self, raw_id: str, capability: str) -> None:
        self.raw_id = raw_id
        self.capability = capability


class _AuthorityRegistryEntry:
    __slots__ = ("raw_ids", "account", "region", "closed")

    def __init__(
        self,
        raw_ids: Mapping[str, str],
        *,
        account: str,
        region: str,
    ) -> None:
        self.raw_ids = MappingProxyType(dict(raw_ids))
        self.account = account
        self.region = region
        self.closed = False


_REGISTRY_LOCK = threading.RLock()
# This registry is the trusted in-process module boundary. Returned capability
# objects retain only opaque IDs; they never retain an entry, SDK object, bound
# registry method, or closure over one.
_RAW_CLIENT_REGISTRY: dict[str, _RawClientRegistryEntry] = {}
_SCOPE_REGISTRY: dict[str, _ScopeRegistryEntry] = {}
_AUTHORITY_REGISTRY: dict[str, _AuthorityRegistryEntry] = {}


def _opaque_registry_id(prefix: str, registry: Mapping[str, object]) -> str:
    while True:
        candidate = f"{prefix}_{secrets.token_hex(32)}"
        if candidate not in registry:
            return candidate


def _register_raw_scope(
    client: object,
    *,
    service: str,
    account: str,
    region: str,
    capability: str,
) -> str:
    if service not in AWS_AUTHORITY_SERVICES:
        raise AwsAuthorityError("attested AWS service is invalid")
    if capability not in _CLIENT_CAPABILITIES:
        raise AwsAuthorityError("attested AWS capability is invalid")
    with _REGISTRY_LOCK:
        raw_id = _opaque_registry_id("raw", _RAW_CLIENT_REGISTRY)
        scope_id = _opaque_registry_id("scope", _SCOPE_REGISTRY)
        _RAW_CLIENT_REGISTRY[raw_id] = _RawClientRegistryEntry(
            client,
            service=service,
            account=account,
            region=region,
        )
        _SCOPE_REGISTRY[scope_id] = _ScopeRegistryEntry(raw_id, capability)
        return scope_id


def _scope_snapshot(
    scope_id: object,
    *,
    require_open: bool = True,
) -> tuple[str, str, str, str, str]:
    if not isinstance(scope_id, str):
        raise AwsAuthorityError("attested AWS client identity is invalid")
    with _REGISTRY_LOCK:
        scope = _SCOPE_REGISTRY.get(scope_id)
        if scope is None:
            raise AwsAuthorityError("attested AWS client identity is invalid")
        raw = _RAW_CLIENT_REGISTRY.get(scope.raw_id)
        if raw is None:
            raise AwsAuthorityError("attested AWS client identity is invalid")
        if require_open and raw.closed:
            raise AwsAuthorityError("attested AWS client is closed")
        return (
            scope.raw_id,
            raw.service,
            raw.account,
            raw.region,
            scope.capability,
        )


def _register_child_scope(scope_id: str, capability: str) -> str:
    if capability not in {"observer", "mutation"}:
        raise AwsAuthorityError("attested AWS client cannot be rescoped")
    with _REGISTRY_LOCK:
        raw_id, service, _, _, parent_capability = _scope_snapshot(scope_id)
        if parent_capability != "authority":
            raise AwsAuthorityError("attested AWS client cannot be rescoped")
        catalog = (
            _OBSERVER_METHOD_CATALOG
            if capability == "observer"
            else _MUTATION_METHOD_CATALOG
        )
        if not catalog[service]:
            raise AwsAuthorityError(
                f"AWS service has no {capability} capability"
            )
        child_id = _opaque_registry_id("scope", _SCOPE_REGISTRY)
        _SCOPE_REGISTRY[child_id] = _ScopeRegistryEntry(raw_id, capability)
        return child_id


def _invoke_scope(
    scope_id: str,
    method_name: str,
    kwargs: Mapping[str, Any],
) -> object:
    if (
        not isinstance(method_name, str)
        or not method_name
        or method_name.startswith("_")
    ):
        raise AwsAuthorityError("attested AWS method is invalid")
    with _REGISTRY_LOCK:
        raw_id, service, _, _, capability = _scope_snapshot(scope_id)
        catalog = (
            _OBSERVER_METHOD_CATALOG
            if capability == "observer"
            else _MUTATION_METHOD_CATALOG
            if capability == "mutation"
            else None
        )
        if catalog is None:
            raise AwsAuthorityError("authority AWS client cannot invoke methods")
        if method_name not in catalog[service]:
            raise AwsAuthorityError(
                f"AWS method is outside the {capability} capability"
            )
        raw = _RAW_CLIENT_REGISTRY[raw_id]
        method = getattr(raw.client, method_name, None)
        if method is None or not callable(method):
            raise AwsAuthorityError(
                "attested AWS client lacks the requested method"
            )
        return method(**dict(kwargs))


def _close_raw_id(raw_id: str) -> None:
    with _REGISTRY_LOCK:
        raw = _RAW_CLIENT_REGISTRY.get(raw_id)
        if raw is None or raw.closed:
            return
        raw.closed = True
        client = raw.client
        raw.client = None
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _close_scope(scope_id: str) -> None:
    raw_id, _, _, _, _ = _scope_snapshot(scope_id, require_open=False)
    _close_raw_id(raw_id)


def _register_authority(
    clients: Mapping[str, "AttestedAwsClientV2"],
    *,
    account: str,
    region: str,
) -> str:
    if tuple(clients) != AWS_AUTHORITY_SERVICES:
        raise AwsAuthorityError("authenticated AWS client set is not exact")
    raw_ids: dict[str, str] = {}
    with _REGISTRY_LOCK:
        for service, client in clients.items():
            raw_id, actual_service, actual_account, actual_region, capability = (
                _scope_snapshot(client._scope_id)
            )
            if (
                actual_service,
                actual_account,
                actual_region,
                capability,
            ) != (service, account, region, "authority"):
                raise AwsAuthorityError(
                    "authenticated AWS client set is not exact"
                )
            raw_ids[service] = raw_id
        authority_id = _opaque_registry_id("authority", _AUTHORITY_REGISTRY)
        _AUTHORITY_REGISTRY[authority_id] = _AuthorityRegistryEntry(
            raw_ids,
            account=account,
            region=region,
        )
        return authority_id


def _authority_snapshot(
    authority_id: object,
    *,
    require_open: bool = True,
) -> tuple[str, str]:
    if not isinstance(authority_id, str):
        raise AwsAuthorityError("authenticated AWS authority identity is invalid")
    with _REGISTRY_LOCK:
        authority = _AUTHORITY_REGISTRY.get(authority_id)
        if authority is None:
            raise AwsAuthorityError(
                "authenticated AWS authority identity is invalid"
            )
        if require_open and authority.closed:
            raise AwsAuthorityError("authenticated AWS authority is closed")
        return authority.account, authority.region


def _authority_scope(
    authority_id: str,
    *,
    service: str,
    capability: str,
) -> str:
    with _REGISTRY_LOCK:
        _authority_snapshot(authority_id)
        authority = _AUTHORITY_REGISTRY[authority_id]
        raw_id = authority.raw_ids.get(service)
        if raw_id is None:
            raise AwsAuthorityError(
                "AWS service is outside the release authority"
            )
        if capability not in {"observer", "mutation"}:
            raise AwsAuthorityError(
                "AWS client access must be scoped to observer or mutation"
            )
        catalog = (
            _OBSERVER_METHOD_CATALOG
            if capability == "observer"
            else _MUTATION_METHOD_CATALOG
        )
        if not catalog[service]:
            raise AwsAuthorityError(
                f"AWS service has no {capability} capability"
            )
        scope_id = _opaque_registry_id("scope", _SCOPE_REGISTRY)
        _SCOPE_REGISTRY[scope_id] = _ScopeRegistryEntry(raw_id, capability)
        return scope_id


def _close_authority(authority_id: str) -> None:
    with _REGISTRY_LOCK:
        authority = _AUTHORITY_REGISTRY.get(authority_id)
        if authority is None or authority.closed:
            return
        authority.closed = True
        raw_ids = tuple(authority.raw_ids.values())
        for raw_id in raw_ids:
            _close_raw_id(raw_id)


class AttestedAwsClientV2:
    """Unforgeable-in-normal-use client wrapper created only after STS proof."""

    __slots__ = ("_scope_id",)

    def __init__(
        self,
        client: object | None,
        *,
        service: str,
        account: str,
        region: str,
        capability: str = "mutation",
        _token: object | None = None,
        _scope_id: str | None = None,
    ) -> None:
        if _token is not _CLIENT_TOKEN:
            raise AwsAuthorityError("attested AWS client is not constructible")
        if _scope_id is None:
            if client is None:
                raise AwsAuthorityError("attested AWS client is unavailable")
            scope_id = _register_raw_scope(
                client,
                service=service,
                account=account,
                region=region,
                capability=capability,
            )
        else:
            if client is not None:
                raise AwsAuthorityError("attested AWS client identity is invalid")
            _, actual_service, actual_account, actual_region, actual_capability = (
                _scope_snapshot(_scope_id)
            )
            if (
                actual_service,
                actual_account,
                actual_region,
                actual_capability,
            ) != (service, account, region, capability):
                raise AwsAuthorityError("attested AWS client identity is invalid")
            scope_id = _scope_id
        object.__setattr__(self, "_scope_id", scope_id)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("attested AWS client is immutable")

    @property
    def service_name(self) -> str:
        self._require_open()
        return _scope_snapshot(self._scope_id)[1]

    @property
    def account(self) -> str:
        self._require_open()
        return _scope_snapshot(self._scope_id)[2]

    @property
    def region(self) -> str:
        self._require_open()
        return _scope_snapshot(self._scope_id)[3]

    @property
    def capability(self) -> str:
        self._require_open()
        return _scope_snapshot(self._scope_id)[4]

    def _require_open(self) -> None:
        _scope_snapshot(self._scope_id)

    def require_scope(
        self,
        *,
        service: str,
        account: str,
        region: str,
        capability: str,
    ) -> None:
        self._require_open()
        _, actual_service, actual_account, actual_region, actual_capability = (
            _scope_snapshot(self._scope_id)
        )
        if (
            actual_service,
            actual_account,
            actual_region,
            actual_capability,
        ) != (
            service,
            account,
            region,
            capability,
        ):
            raise AwsAuthorityError("attested AWS client scope differs")

    def invoke(self, method_name: str, **kwargs: Any) -> object:
        return _invoke_scope(self._scope_id, method_name, kwargs)

    def _scoped(self, capability: str) -> "AttestedAwsClientV2":
        child_id = _register_child_scope(self._scope_id, capability)
        _, service, account, region, _ = _scope_snapshot(child_id)
        return AttestedAwsClientV2(
            None,
            service=service,
            account=account,
            region=region,
            capability=capability,
            _token=_CLIENT_TOKEN,
            _scope_id=child_id,
        )

    def close(self) -> None:
        _close_scope(self._scope_id)


class AuthenticatedAwsAuthorityV2:
    """One exact-account set of retained SDK client capabilities."""

    __slots__ = ("_authority_id",)

    def __init__(
        self,
        clients: Mapping[str, AttestedAwsClientV2],
        *,
        account: str,
        region: str,
        _token: object | None = None,
    ) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise AwsAuthorityError("authenticated AWS authority is not constructible")
        authority_id = _register_authority(
            clients,
            account=account,
            region=region,
        )
        object.__setattr__(self, "_authority_id", authority_id)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("authenticated AWS authority is immutable")

    @property
    def account(self) -> str:
        return _authority_snapshot(self._authority_id, require_open=False)[0]

    @property
    def region(self) -> str:
        return _authority_snapshot(self._authority_id, require_open=False)[1]

    def client(
        self,
        service: str,
        *,
        capability: str | None = None,
    ) -> AttestedAwsClientV2:
        if capability not in {"observer", "mutation"}:
            raise AwsAuthorityError(
                "AWS client access must be scoped to observer or mutation"
            )
        scope_id = _authority_scope(
            self._authority_id,
            service=service,
            capability=capability,
        )
        _, actual_service, account, region, actual_capability = _scope_snapshot(
            scope_id
        )
        return AttestedAwsClientV2(
            None,
            service=actual_service,
            account=account,
            region=region,
            capability=actual_capability,
            _token=_CLIENT_TOKEN,
            _scope_id=scope_id,
        )

    def observer_client(self, service: str) -> AttestedAwsClientV2:
        return self.client(service, capability="observer")

    def mutation_client(self, service: str) -> AttestedAwsClientV2:
        return self.client(service, capability="mutation")

    def close(self) -> None:
        _close_authority(self._authority_id)

    @classmethod
    @contextmanager
    def open(
        cls,
        plan: ReleasePlanV2,
        *,
        site_packages: Path,
    ) -> Iterator["AuthenticatedAwsAuthorityV2"]:
        """Authenticate the reviewed SDK snapshot and yield live client authority."""

        canonical_plan = ReleasePlanV2.from_bytes(plan.to_bytes())
        if canonical_plan.region != REQUIRED_REGION:
            raise AwsAuthorityError(
                f"AWS authority region must be exactly {REQUIRED_REGION}"
            )
        preloaded = sorted(
            name
            for name in sys.modules
            if name.partition(".")[0] in _SDK_MODULE_ROOTS
        )
        if preloaded:
            raise AwsAuthorityError(
                "AWS SDK modules were loaded before runtime authentication"
            )
        try:
            with tempfile.TemporaryDirectory(
                prefix="personal-operator-aws-authority-v2-"
            ) as temporary_root:
                retained = Path(temporary_root) / "sdk"
                observed = snapshot_evidence_runtime(
                    Path(site_packages),
                    destination=retained,
                )
                if observed != canonical_plan.evidence_runtime_sha256:
                    raise AwsAuthorityError(
                        "AWS evidence runtime differs from the release plan"
                    )
                ca_file = retained / "botocore" / "cacert.pem"
                ca_descriptor = os.open(
                    ca_file,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                authority: AuthenticatedAwsAuthorityV2 | None = None
                retained_text = str(retained)
                previous_dont_write = sys.dont_write_bytecode
                with _closed_aws_environment():
                    try:
                        locked_preloaded = sorted(
                            name
                            for name in sys.modules
                            if name.partition(".")[0] in _SDK_MODULE_ROOTS
                        )
                        if locked_preloaded:
                            raise AwsAuthorityError(
                                "AWS SDK modules were loaded before runtime "
                                "authentication"
                            )
                        os.set_inheritable(ca_descriptor, False)
                        ca_stat = os.fstat(ca_descriptor)
                        if (
                            os.get_inheritable(ca_descriptor)
                            or not stat.S_ISREG(ca_stat.st_mode)
                            or ca_stat.st_size <= 0
                        ):
                            raise AwsAuthorityError("AWS CA bundle is invalid")
                        ca_path = _descriptor_path(ca_descriptor)
                        sys.path.insert(0, retained_text)
                        sys.dont_write_bytecode = True
                        importlib.invalidate_caches()
                        boto3 = importlib.import_module("boto3")
                        config_module = importlib.import_module(
                            "botocore.config"
                        )
                        authority = _authenticate_closed_profile(
                            canonical_plan,
                            aws_directory=_default_aws_directory(),
                            source_session_factory=boto3.Session,
                            frozen_session_factory=boto3.Session,
                            config_factory=config_module.Config,
                            ca_bundle_path=ca_path,
                        )
                        yield authority
                    finally:
                        if authority is not None:
                            authority.close()
                        sys.dont_write_bytecode = previous_dont_write
                        try:
                            sys.path.remove(retained_text)
                        except ValueError:
                            pass
                        importlib.invalidate_caches()
                        os.close(ca_descriptor)
        except AwsAuthorityError:
            raise
        except (EvidenceRuntimeError, OSError) as error:
            raise AwsAuthorityError(
                "authenticated AWS authority is unavailable"
            ) from error


def _descriptor_path(descriptor: int) -> str:
    if not isinstance(descriptor, int) or descriptor < 0:
        raise AwsAuthorityError("AWS CA descriptor is invalid")
    for root in (Path("/dev/fd"), Path("/proc/self/fd")):
        candidate = root / str(descriptor)
        if candidate.exists():
            return str(candidate)
    raise AwsAuthorityError("AWS CA descriptor path is unavailable")


def _retained_ca_bundle_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AwsAuthorityError("retained AWS CA bundle path is invalid")
    candidate = Path(value)
    if (
        candidate.parent not in {Path("/dev/fd"), Path("/proc/self/fd")}
        or not candidate.name.isascii()
        or not candidate.name.isdecimal()
        or (len(candidate.name) > 1 and candidate.name.startswith("0"))
    ):
        raise AwsAuthorityError("retained AWS CA bundle path is invalid")
    return value


@contextmanager
def _closed_aws_environment() -> Iterator[None]:
    """Serialize lifetimes and scrub ambient AWS/HTTP provider input."""

    _ENVIRONMENT_LOCK.acquire()
    entered = False
    try:
        depth = getattr(_ENVIRONMENT_STATE, "depth", 0)
        if depth == 0:
            baseline_keys = _ambient_environment_keys()
            _ENVIRONMENT_STATE.baseline = {
                key: os.environ[key] for key in baseline_keys
            }
        _ENVIRONMENT_STATE.depth = depth + 1
        entered = True
        _scrub_ambient_environment()
        yield
    finally:
        try:
            if entered:
                _scrub_ambient_environment()
                remaining_depth = _ENVIRONMENT_STATE.depth - 1
                _ENVIRONMENT_STATE.depth = remaining_depth
                if remaining_depth == 0:
                    baseline = _ENVIRONMENT_STATE.baseline
                    for key in _ambient_environment_keys():
                        os.environ.pop(key, None)
                    os.environ.update(baseline)
                    del _ENVIRONMENT_STATE.baseline
                    del _ENVIRONMENT_STATE.depth
        finally:
            _ENVIRONMENT_LOCK.release()


def _ambient_environment_keys() -> set[str]:
    return {
        key
        for key in os.environ
        if key in _AMBIENT_AWS_ENVIRONMENT
        or key == "HOME"
        or key.startswith("AWS_")
        or key.startswith("BOTO_")
        or key.casefold()
        in {
            "all_proxy",
            "curl_ca_bundle",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "requests_ca_bundle",
            "ssl_cert_dir",
            "ssl_cert_file",
        }
    }


def _scrub_ambient_environment() -> None:
    for key in _ambient_environment_keys():
        os.environ.pop(key, None)
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    os.environ["BOTO_CONFIG"] = os.devnull
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["AWS_DATA_PATH"] = os.devnull
    os.environ["HOME"] = os.devnull


def _default_aws_directory() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, TypeError, ValueError) as error:
        raise AwsAuthorityError(
            "closed AWS profile directory is unavailable"
        ) from error
    if not home.is_absolute():
        raise AwsAuthorityError("closed AWS profile directory is unavailable")
    return home / ".aws"


def _read_retained_profile_file(directory_descriptor: int, name: str) -> bytes:
    if name not in {"config", "credentials"}:
        raise AwsAuthorityError("closed AWS profile file is invalid")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        os.set_inheritable(descriptor, False)
        observed = os.fstat(descriptor)
        if (
            os.get_inheritable(descriptor)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_size <= 0
            or observed.st_size > _PROFILE_FILE_LIMIT
        ):
            raise AwsAuthorityError("closed AWS profile file is unsafe")
        chunks: list[bytes] = []
        remaining = observed.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                raise AwsAuthorityError("closed AWS profile file is incomplete")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AwsAuthorityError("closed AWS profile file changed during read")
        retained = os.fstat(descriptor)
        if (
            retained.st_dev,
            retained.st_ino,
            retained.st_size,
            retained.st_nlink,
            retained.st_uid,
            stat.S_IMODE(retained.st_mode),
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            1,
            os.geteuid(),
            0o600,
        ):
            raise AwsAuthorityError("closed AWS profile file changed during read")
        return b"".join(chunks)
    except AwsAuthorityError:
        raise
    except OSError as error:
        raise AwsAuthorityError("closed AWS profile file is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_closed_profile_files(aws_directory: Path) -> tuple[bytes, bytes]:
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(aws_directory, directory_flags)
        os.set_inheritable(descriptor, False)
        observed = os.fstat(descriptor)
        directory_mode = stat.S_IMODE(observed.st_mode)
        if (
            os.get_inheritable(descriptor)
            or not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or directory_mode not in {0o700, 0o755}
        ):
            raise AwsAuthorityError("closed AWS profile directory is unsafe")
        config_bytes = _read_retained_profile_file(descriptor, "config")
        credentials_bytes = _read_retained_profile_file(
            descriptor,
            "credentials",
        )
        retained = os.fstat(descriptor)
        if (
            retained.st_dev,
            retained.st_ino,
            retained.st_uid,
            stat.S_IMODE(retained.st_mode),
        ) != (
            observed.st_dev,
            observed.st_ino,
            os.geteuid(),
            directory_mode,
        ):
            raise AwsAuthorityError("closed AWS profile directory changed")
        return config_bytes, credentials_bytes
    except AwsAuthorityError:
        raise
    except OSError as error:
        raise AwsAuthorityError(
            "closed AWS profile directory is unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_closed_ini(raw: bytes, *, label: str) -> configparser.RawConfigParser:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AwsAuthorityError(f"closed AWS {label} profile is invalid") from None
    if "\x00" in text:
        raise AwsAuthorityError(f"closed AWS {label} profile is invalid")
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str.lower
    try:
        parser.read_string(text, source=f"retained AWS {label}")
    except configparser.Error:
        raise AwsAuthorityError(f"closed AWS {label} profile is invalid") from None
    if parser.defaults():
        raise AwsAuthorityError(f"closed AWS {label} profile is invalid")
    return parser


def _closed_profile_static_credentials(
    plan: ReleasePlanV2,
    *,
    aws_directory: Path,
) -> tuple[str, str]:
    config_bytes, credentials_bytes = _read_closed_profile_files(aws_directory)
    config = _parse_closed_ini(config_bytes, label="configuration")
    credentials = _parse_closed_ini(credentials_bytes, label="credentials")
    target_section = f"profile {TARGET_PROFILE}"
    relevant_config = {
        target_section.casefold(),
        TARGET_PROFILE.casefold(),
        SOURCE_PROFILE.casefold(),
        f"profile {SOURCE_PROFILE}".casefold(),
    }
    for section in config.sections():
        if section.casefold() in relevant_config and section not in {
            target_section,
            f"profile {SOURCE_PROFILE}",
        }:
            raise AwsAuthorityError("closed AWS target profile is not exact")
    relevant_credentials = {
        TARGET_PROFILE.casefold(),
        SOURCE_PROFILE.casefold(),
        f"profile {TARGET_PROFILE}".casefold(),
        f"profile {SOURCE_PROFILE}".casefold(),
    }
    for section in credentials.sections():
        if (
            section.casefold() in relevant_credentials
            and section != SOURCE_PROFILE
        ):
            raise AwsAuthorityError("closed AWS source profile is not exact")
    if not config.has_section(target_section):
        raise AwsAuthorityError("closed AWS target profile is missing")
    if not credentials.has_section(SOURCE_PROFILE):
        raise AwsAuthorityError("closed AWS source profile is missing")
    target = dict(config.items(target_section, raw=True))
    source_configuration_section = f"profile {SOURCE_PROFILE}"
    if not config.has_section(source_configuration_section):
        raise AwsAuthorityError("closed AWS source profile is missing")
    expected_role = (
        f"arn:aws:iam::{plan.account}:role/{DEPLOYMENT_ROLE_NAME}"
    )
    if target != {
        "role_arn": expected_role,
        "source_profile": SOURCE_PROFILE,
        "duration_seconds": str(ASSUME_ROLE_DURATION_SECONDS),
        "region": REQUIRED_REGION,
        "output": "json",
    }:
        raise AwsAuthorityError("closed AWS target profile is not exact")
    source_configuration = dict(
        config.items(source_configuration_section, raw=True)
    )
    if source_configuration != {
        "region": REQUIRED_REGION,
        "output": "json",
    }:
        raise AwsAuthorityError("closed AWS source profile is not exact")
    source = dict(credentials.items(SOURCE_PROFILE, raw=True))
    if set(source) != {"aws_access_key_id", "aws_secret_access_key"}:
        raise AwsAuthorityError("closed AWS source profile is not static")
    access_key = source.get("aws_access_key_id")
    secret_key = source.get("aws_secret_access_key")
    if (
        not isinstance(access_key, str)
        or len(access_key) != 20
        or not access_key.startswith("AKIA")
        or not access_key.isascii()
        or not access_key.isalnum()
        or not isinstance(secret_key, str)
        or len(secret_key) != 40
        or not secret_key.isascii()
        or any(character.isspace() or ord(character) < 33 for character in secret_key)
    ):
        raise AwsAuthorityError("closed AWS source profile is invalid")
    return access_key, secret_key


def _hardened_sdk_config(
    config_factory: Callable[..., object],
    *,
    region: str,
) -> object:
    return config_factory(
        region_name=region,
        ignore_configured_endpoint_urls=True,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 1},
    )


def _role_session_name(plan: ReleasePlanV2) -> str:
    digest = hashlib.sha256(plan.to_bytes()).hexdigest()
    return f"po-v2-{digest[:24]}"


def _validated_assumed_credentials(
    response: object,
    *,
    plan: ReleasePlanV2,
    session_name: str,
    now: datetime,
) -> tuple[str, str, str]:
    if not isinstance(response, Mapping):
        raise AwsAuthorityError("AWS assumed role response is invalid")
    credentials = response.get("Credentials")
    assumed_user = response.get("AssumedRoleUser")
    if (
        not isinstance(credentials, Mapping)
        or set(credentials) != {
            "AccessKeyId",
            "SecretAccessKey",
            "SessionToken",
            "Expiration",
        }
        or not isinstance(assumed_user, Mapping)
    ):
        raise AwsAuthorityError("AWS assumed role credentials are invalid")
    access_key = credentials.get("AccessKeyId")
    secret_key = credentials.get("SecretAccessKey")
    token = credentials.get("SessionToken")
    expiration = credentials.get("Expiration")
    expected_arn = (
        f"arn:aws:sts::{plan.account}:assumed-role/"
        f"{DEPLOYMENT_ROLE_NAME}/{session_name}"
    )
    assumed_id = assumed_user.get("AssumedRoleId")
    if (
        not isinstance(access_key, str)
        or len(access_key) != 20
        or not access_key.startswith("ASIA")
        or not access_key.isascii()
        or not access_key.isalnum()
        or not isinstance(secret_key, str)
        or len(secret_key) != 40
        or not secret_key.isascii()
        or any(character.isspace() or ord(character) < 33 for character in secret_key)
        or not isinstance(token, str)
        or not token
        or len(token) > 4096
        or not token.isascii()
        or any(ord(character) < 33 or character.isspace() for character in token)
        or not isinstance(expiration, datetime)
        or expiration.tzinfo is None
        or expiration.utcoffset() is None
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or expiration.astimezone(timezone.utc)
        <= now.astimezone(timezone.utc) + timedelta(minutes=5)
        or expiration.astimezone(timezone.utc)
        > now.astimezone(timezone.utc)
        + timedelta(seconds=ASSUME_ROLE_DURATION_SECONDS + 300)
        or assumed_user.get("Arn") != expected_arn
        or not isinstance(assumed_id, str)
        or not assumed_id.endswith(f":{session_name}")
    ):
        raise AwsAuthorityError("AWS assumed role credentials are invalid")
    return access_key, secret_key, token


def _authenticate_closed_profile(
    plan: ReleasePlanV2,
    *,
    aws_directory: Path,
    source_session_factory: _SessionFactory,
    frozen_session_factory: _SessionFactory,
    config_factory: Callable[..., object],
    ca_bundle_path: str,
    now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> AuthenticatedAwsAuthorityV2:
    """Assume the one reviewed role without invoking an ambient provider chain."""

    canonical_plan = ReleasePlanV2.from_bytes(plan.to_bytes())
    retained_ca = _retained_ca_bundle_path(ca_bundle_path)
    if canonical_plan.region != REQUIRED_REGION:
        raise AwsAuthorityError(
            f"AWS authority region must be exactly {REQUIRED_REGION}"
        )
    with _closed_aws_environment():
        access_key, secret_key = _closed_profile_static_credentials(
            canonical_plan,
            aws_directory=Path(aws_directory),
        )
        config = _hardened_sdk_config(
            config_factory,
            region=canonical_plan.region,
        )
        try:
            source_session = source_session_factory(
                region_name=canonical_plan.region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        except Exception:
            raise AwsAuthorityError("AWS bootstrap session is unavailable") from None
        source_sts: object | None = None
        try:
            source_sts = source_session.client(
                "sts",
                region_name=canonical_plan.region,
                config=config,
                verify=retained_ca,
            )
            _validate_raw_client(
                source_sts,
                service="sts",
                region=canonical_plan.region,
            )
            meta = getattr(source_sts, "meta", None)
            if getattr(meta, "endpoint_url", None) != (
                f"https://sts.{canonical_plan.region}.amazonaws.com"
            ):
                raise AwsAuthorityError(
                    "AWS bootstrap STS endpoint is not exact"
                )
            assume_role = getattr(source_sts, "assume_role", None)
            if assume_role is None or not callable(assume_role):
                raise AwsAuthorityError("AWS bootstrap STS is unavailable")
            session_name = _role_session_name(canonical_plan)
            response = assume_role(
                RoleArn=(
                    f"arn:aws:iam::{canonical_plan.account}:role/"
                    f"{DEPLOYMENT_ROLE_NAME}"
                ),
                RoleSessionName=session_name,
                DurationSeconds=ASSUME_ROLE_DURATION_SECONDS,
            )
            temporary = _validated_assumed_credentials(
                response,
                plan=canonical_plan,
                session_name=session_name,
                now=now_factory(),
            )
        except AwsAuthorityError:
            raise
        except Exception:
            raise AwsAuthorityError("AWS assumed role cannot be established") from None
        finally:
            if source_sts is not None:
                close = getattr(source_sts, "close", None)
                if callable(close):
                    close()
        return _authenticate_frozen_source(
            canonical_plan,
            frozen_credentials=_ExplicitFrozenCredentials(*temporary),
            frozen_session_factory=frozen_session_factory,
            config_factory=config_factory,
            ca_bundle_path=retained_ca,
            expected_role_session=session_name,
        )


class _ExplicitFrozenCredentials:
    __slots__ = ("access_key", "secret_key", "token")

    def __init__(self, access_key: str, secret_key: str, token: str | None) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

    def __repr__(self) -> str:
        return "<explicit AWS credentials>"


def _authenticate_frozen_source(
    plan: ReleasePlanV2,
    *,
    frozen_credentials: object,
    frozen_session_factory: Callable[..., _FrozenSession],
    config_factory: Callable[..., object],
    ca_bundle_path: str,
    expected_role_session: str | None = None,
) -> AuthenticatedAwsAuthorityV2:
    """Build exact clients from caller-retained, non-refreshable credentials."""

    canonical_plan = ReleasePlanV2.from_bytes(plan.to_bytes())
    retained_ca = _retained_ca_bundle_path(ca_bundle_path)
    if canonical_plan.region != REQUIRED_REGION:
        raise AwsAuthorityError(
            f"AWS authority region must be exactly {REQUIRED_REGION}"
        )
    access_key = getattr(frozen_credentials, "access_key", None)
    secret_key = getattr(frozen_credentials, "secret_key", None)
    session_token = getattr(frozen_credentials, "token", None)
    if (
        not isinstance(access_key, str)
        or not access_key
        or not isinstance(secret_key, str)
        or not secret_key
        or (
            session_token is not None
            and (not isinstance(session_token, str) or not session_token)
        )
    ):
        raise AwsAuthorityError("AWS credentials cannot be frozen")
    session_arguments = {
        "region_name": canonical_plan.region,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if session_token is not None:
        session_arguments["aws_session_token"] = session_token
    try:
        session = frozen_session_factory(**session_arguments)
    except Exception:
        raise AwsAuthorityError("AWS client session cannot be created") from None
    config = _hardened_sdk_config(
        config_factory,
        region=canonical_plan.region,
    )
    raw_clients: dict[str, object] = {}
    wrapped_clients: dict[str, AttestedAwsClientV2] = {}
    try:
        for service in AWS_AUTHORITY_SERVICES:
            raw = session.client(
                service,
                region_name=canonical_plan.region,
                config=config,
                verify=retained_ca,
            )
            raw_clients[service] = raw
            _validate_raw_client(
                raw,
                service=service,
                region=canonical_plan.region,
            )
            if (
                expected_role_session is not None
                and getattr(getattr(raw, "meta", None), "endpoint_url", None)
                != _EXACT_SERVICE_ENDPOINTS[service]
            ):
                raise AwsAuthorityError("AWS client endpoint is not exact")
        identity_method = getattr(raw_clients["sts"], "get_caller_identity", None)
        if identity_method is None or not callable(identity_method):
            raise AwsAuthorityError("STS client lacks account attestation")
        identity = identity_method()
        expected_identity_arn = (
            f"arn:aws:sts::{canonical_plan.account}:assumed-role/"
            f"{DEPLOYMENT_ROLE_NAME}/{expected_role_session}"
            if expected_role_session is not None
            else None
        )
        if (
            not isinstance(identity, Mapping)
            or identity.get("Account") != canonical_plan.account
            or (
                expected_identity_arn is not None
                and identity.get("Arn") != expected_identity_arn
            )
            or (
                expected_role_session is not None
                and (
                    not isinstance(identity.get("UserId"), str)
                    or not str(identity.get("UserId")).endswith(
                        f":{expected_role_session}"
                    )
                )
            )
        ):
            raise AwsAuthorityError(
                "AWS credentials differ from the release account"
            )
        for service, raw in raw_clients.items():
            wrapped_clients[service] = AttestedAwsClientV2(
                raw,
                service=service,
                account=canonical_plan.account,
                region=canonical_plan.region,
                capability="authority",
                _token=_CLIENT_TOKEN,
            )
        return AuthenticatedAwsAuthorityV2(
            wrapped_clients,
            account=canonical_plan.account,
            region=canonical_plan.region,
            _token=_AUTHORITY_TOKEN,
        )
    except AwsAuthorityError:
        _close_unaccepted_clients(raw_clients, wrapped_clients)
        raise
    except Exception:
        _close_unaccepted_clients(raw_clients, wrapped_clients)
        raise AwsAuthorityError("AWS client authority cannot be created") from None


def _close_unaccepted_clients(
    raw_clients: Mapping[str, object],
    wrapped_clients: Mapping[str, AttestedAwsClientV2],
) -> None:
    for service, raw in raw_clients.items():
        wrapper = wrapped_clients.get(service)
        try:
            if wrapper is not None:
                wrapper.close()
            else:
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
        except Exception:
            continue


def _validate_raw_client(client: object, *, service: str, region: str) -> None:
    meta = getattr(client, "meta", None)
    service_model = getattr(meta, "service_model", None)
    config = getattr(meta, "config", None)
    client_region = "aws-global" if service == "iam" else region
    endpoint_is_exact = (
        getattr(meta, "endpoint_url", None) == "https://iam.amazonaws.com"
        if service == "iam"
        else True
    )
    if (
        region != REQUIRED_REGION
        or getattr(meta, "region_name", None) != client_region
        or getattr(service_model, "service_name", None) != service
        or getattr(config, "region_name", None) != client_region
        or getattr(config, "ignore_configured_endpoint_urls", None) is not True
        or getattr(config, "proxies", None) != {}
        or getattr(config, "retries", None)
        != {"mode": "standard", "total_max_attempts": 1}
        or not endpoint_is_exact
    ):
        raise AwsAuthorityError("AWS client configuration is not exact")


__all__ = [
    "AWS_AUTHORITY_SERVICES",
    "AWS_MUTATION_METHODS",
    "AWS_OBSERVER_METHODS",
    "AttestedAwsClientV2",
    "AuthenticatedAwsAuthorityV2",
    "AwsAuthorityError",
]
