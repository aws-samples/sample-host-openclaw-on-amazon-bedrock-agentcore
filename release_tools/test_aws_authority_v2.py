from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Iterator

import pytest

import release_tools.aws_authority_v2 as aws_authority

from release_tools.aws_authority_v2 import (
    AWS_AUTHORITY_SERVICES,
    AWS_MUTATION_METHODS,
    AWS_OBSERVER_METHODS,
    AttestedAwsClientV2,
    AwsAuthorityError,
    AuthenticatedAwsAuthorityV2,
    _CLIENT_TOKEN,
    _authenticate_frozen_source,
    _validate_raw_client,
)
from release_tools.contracts import ReleasePlanV2
from release_tools.test_contracts import _release_plan_v2


ACCOUNT = "123456789012"
REGION = "eu-west-1"
CA_BUNDLE_PATH = "/dev/fd/777"
SERVICES = (
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


def _plan_v2(*, artifact_digest: str = "1" * 64) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    artifacts = value["artifacts"]
    steps = value["steps"]
    assert isinstance(artifacts, list)
    assert isinstance(steps, list)
    first_step = steps[0]
    first_request = next(
        item for item in artifacts if item["path"] == first_step["requestArtifact"]
    )
    first_request["sha256"] = artifact_digest
    first_step["requestSha256"] = artifact_digest
    first_step["expectedRequestSha256"] = artifact_digest
    return ReleasePlanV2.from_mapping(value)


class FrozenCredentials:
    access_key = "ASIAEXACT"
    secret_key = "secret"
    token = "session-token"


class FakeClient:
    def __init__(self, service: str, config: object, *, account: str) -> None:
        self.service = service
        self.account = account
        self.closed = False
        self.close_calls = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.events = object()
        self._endpoint = object()
        self._request_signer = object()
        self._serializer = object()
        client_region = "aws-global" if service == "iam" else REGION
        client_config = SimpleNamespace(**vars(config))
        client_config.region_name = client_region
        self.meta = SimpleNamespace(
            region_name=client_region,
            endpoint_url={
                "sts": "https://sts.eu-west-1.amazonaws.com",
                "s3": "https://s3.eu-west-1.amazonaws.com",
                "cloudformation": (
                    "https://cloudformation.eu-west-1.amazonaws.com"
                ),
                "ecr": "https://api.ecr.eu-west-1.amazonaws.com",
                "bedrock-agentcore-control": (
                    "https://bedrock-agentcore-control.eu-west-1.amazonaws.com"
                ),
                "ssm": "https://ssm.eu-west-1.amazonaws.com",
                "signer": "https://signer.eu-west-1.amazonaws.com",
                "cloudtrail": "https://cloudtrail.eu-west-1.amazonaws.com",
                "iam": "https://iam.amazonaws.com",
            }[service],
            service_model=SimpleNamespace(service_name=service),
            config=client_config,
        )

    def get_caller_identity(self) -> dict[str, str]:
        assert self.service == "sts"
        return {
            "Account": self.account,
            "Arn": f"arn:aws:sts::{self.account}:assumed-role/Test/session",
            "UserId": "test:session",
        }

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(**kwargs: object) -> dict[str, object]:
            self.calls.append((name, kwargs))
            return {"method": name, "request": kwargs}

        return invoke


class FrozenSession:
    def __init__(
        self,
        *,
        account: str = ACCOUNT,
        overrides: dict[str, object] | None = None,
    ) -> None:
        self.account = account
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str, object, str]] = []
        self.clients: dict[str, FakeClient] = {}

    def client(
        self,
        service: str,
        *,
        region_name: str,
        config: object,
        verify: str,
    ) -> FakeClient:
        self.calls.append((service, region_name, config, verify))
        client = self.overrides.get(service)
        if client is None:
            client = FakeClient(service, config, account=self.account)
        self.clients[service] = client
        return client  # type: ignore[return-value]


class SessionFactory:
    def __init__(self, frozen: FrozenSession) -> None:
        self.frozen = frozen
        self.calls: list[dict[str, str]] = []

    def __call__(self, **kwargs: str) -> FrozenSession:
        self.calls.append(kwargs)
        return self.frozen


def _config_factory(**kwargs: object) -> object:
    return SimpleNamespace(**kwargs)


class _CompositeTestClient(AttestedAwsClientV2):
    """Test-only union used by existing provider unit-test fixtures."""

    __slots__ = ("_test_client",)

    def __init__(self, client: object, *, service: str) -> None:
        super().__init__(
            client,
            service=service,
            account=ACCOUNT,
            region=REGION,
            capability="observer",
            _token=_CLIENT_TOKEN,
        )
        object.__setattr__(self, "_test_client", client)

    def require_scope(
        self,
        *,
        service: str,
        account: str,
        region: str,
        capability: str,
    ) -> None:
        self._require_open()
        catalog = (
            AWS_OBSERVER_METHODS
            if capability == "observer"
            else AWS_MUTATION_METHODS
            if capability == "mutation"
            else None
        )
        if (
            self.service_name != service
            or account != ACCOUNT
            or region != REGION
            or catalog is None
            or not catalog[service]
        ):
            raise AwsAuthorityError("attested AWS client scope differs")

    def invoke(self, method_name: str, **kwargs: object) -> object:
        self._require_open()
        allowed = (
            AWS_OBSERVER_METHODS[self.service_name]
            | AWS_MUTATION_METHODS[self.service_name]
        )
        if method_name not in allowed:
            raise AwsAuthorityError("AWS method is outside the test capability")
        method = getattr(self._test_client, method_name, None)
        if method is None or not callable(method):
            raise AwsAuthorityError(
                "attested AWS client lacks the requested method"
            )
        return method(**kwargs)


@contextmanager
def attested_test_client(
    client: object,
    *,
    service: str,
) -> Iterator[object]:
    _validate_raw_client(client, service=service, region=REGION)
    authority = _CompositeTestClient(client, service=service)
    try:
        yield authority
    finally:
        authority.close()


def test_authority_freezes_credentials_attests_account_and_builds_closed_clients() -> None:
    frozen = FrozenSession()
    factory = SessionFactory(frozen)

    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=factory,
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )

    assert isinstance(authority, AuthenticatedAwsAuthorityV2)
    assert factory.calls == [
        {
            "region_name": REGION,
            "aws_access_key_id": "ASIAEXACT",
            "aws_secret_access_key": "secret",
            "aws_session_token": "session-token",
        }
    ]
    assert [service for service, _, _, _ in frozen.calls] == list(SERVICES)
    for service, region, config, verify in frozen.calls:
        assert region == REGION
        assert verify == CA_BUNDLE_PATH
        assert config.region_name == REGION
        assert config.ignore_configured_endpoint_urls is True
        assert config.proxies == {}
        assert config.retries == {
            "mode": "standard",
            "total_max_attempts": 1,
        }
        client = authority.observer_client(service)
        assert client.service_name == service
        assert client.account == ACCOUNT
        assert client.region == REGION
        client.require_scope(
            service=service,
            account=ACCOUNT,
            region=REGION,
            capability="observer",
        )

    authority.close()
    assert all(client.closed for client in frozen.clients.values())
    assert all(client.close_calls == 1 for client in frozen.clients.values())
    with pytest.raises(AwsAuthorityError, match="closed"):
        authority.observer_client("s3")


@pytest.mark.parametrize(
    ("credentials", "frozen", "match"),
    [
        (object(), FrozenSession(), "credentials"),
        (FrozenCredentials(), FrozenSession(account="999999999999"), "account"),
    ],
)
def test_authority_rejects_missing_credentials_or_cross_account_identity(
    credentials: object,
    frozen: FrozenSession,
    match: str,
) -> None:
    with pytest.raises(AwsAuthorityError, match=match):
        _authenticate_frozen_source(
            _plan_v2(),
            frozen_credentials=credentials,
            frozen_session_factory=SessionFactory(frozen),
            config_factory=_config_factory,
            ca_bundle_path=CA_BUNDLE_PATH,
        )

    assert all(client.closed for client in frozen.clients.values())


def test_authority_rejects_unknown_service_and_is_not_directly_constructible() -> None:
    with pytest.raises(AwsAuthorityError, match="constructible"):
        AuthenticatedAwsAuthorityV2({}, account=ACCOUNT, region=REGION)

    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(FrozenSession()),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )
    with pytest.raises(AwsAuthorityError, match="service"):
        authority.observer_client("lambda")
    with pytest.raises(AwsAuthorityError, match="scoped"):
        authority.client("s3")
    authority.close()


def test_attested_clients_are_method_scoped_and_never_proxy_raw_attributes() -> None:
    frozen = FrozenSession()
    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )

    observer = authority.observer_client("s3")
    assert observer.capability == "observer"
    observer.require_scope(
        service="s3",
        account=ACCOUNT,
        region=REGION,
        capability="observer",
    )
    assert observer.invoke("head_object", Bucket="bucket", Key="key") == {
        "method": "head_object",
        "request": {"Bucket": "bucket", "Key": "key"},
    }
    with pytest.raises(AwsAuthorityError, match="outside.*observer"):
        observer.invoke("put_object", Bucket="bucket", Key="key", Body=b"x")
    with pytest.raises(AttributeError):
        observer.delete_bucket(Bucket="bucket")  # type: ignore[attr-defined]
    for forbidden_attribute in (
        "meta",
        "events",
        "_endpoint",
        "_request_signer",
        "_serializer",
        "_client",
        "require_subject",
    ):
        with pytest.raises(AttributeError):
            getattr(observer, forbidden_attribute)
    for attribute in (
        "_handle",
        "_allowed_methods",
        "_invoke_bound",
        "_closed_bound",
        "_close_bound",
    ):
        with pytest.raises(AttributeError):
            getattr(observer, attribute)
    with pytest.raises(AttributeError):
        observer._allowed_methods = (  # type: ignore[attr-defined]
            AWS_OBSERVER_METHODS["s3"] | AWS_MUTATION_METHODS["s3"]
        )
    with pytest.raises(AwsAuthorityError, match="outside.*observer"):
        observer.invoke("put_object", Bucket="bucket", Key="key", Body=b"x")
    with pytest.raises(AwsAuthorityError, match="scope differs"):
        observer.require_scope(
            service="s3",
            account=ACCOUNT,
            region=REGION,
            capability="mutation",
        )

    mutation = authority.mutation_client("s3")
    assert mutation.capability == "mutation"
    assert mutation.invoke("put_object", Bucket="bucket", Key="key", Body=b"x") == {
        "method": "put_object",
        "request": {"Bucket": "bucket", "Key": "key", "Body": b"x"},
    }
    with pytest.raises(AwsAuthorityError, match="outside.*mutation"):
        mutation.invoke("head_object", Bucket="bucket", Key="key")
    with pytest.raises(AttributeError):
        mutation._allowed_methods = (  # type: ignore[attr-defined]
            AWS_OBSERVER_METHODS["s3"] | AWS_MUTATION_METHODS["s3"]
        )
    with pytest.raises(AwsAuthorityError, match="outside.*mutation"):
        mutation.invoke("head_object", Bucket="bucket", Key="key")
    with pytest.raises(AwsAuthorityError, match="method is invalid"):
        mutation.invoke("_make_api_call")

    assert frozen.clients["s3"].calls == [
        ("head_object", {"Bucket": "bucket", "Key": "key"}),
        ("put_object", {"Bucket": "bucket", "Key": "key", "Body": b"x"}),
    ]
    authority.close()
    with pytest.raises(AwsAuthorityError, match="closed"):
        observer.invoke("head_object", Bucket="bucket", Key="key")


def _reachable_without_module_globals(*roots: object) -> list[object]:
    """Traverse ordinary returned-object state, bound methods, and closures."""

    pending = list(roots)
    observed: list[object] = []
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        observed.append(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
            continue
        bound_self = getattr(value, "__self__", None)
        if bound_self is not None:
            pending.append(bound_self)
        function = getattr(value, "__func__", value)
        closure = getattr(function, "__closure__", None)
        if closure:
            pending.extend(cell.cell_contents for cell in closure)
        instance_dict = getattr(value, "__dict__", None)
        if isinstance(instance_dict, dict):
            pending.append(instance_dict)
        for owner in type(value).__mro__:
            slots = owner.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"}:
                    continue
                attribute = slot
                if slot.startswith("__") and not slot.endswith("__"):
                    attribute = f"_{owner.__name__.lstrip('_')}{slot}"
                try:
                    pending.append(object.__getattribute__(value, attribute))
                except (AttributeError, TypeError):
                    pass
    return observed


def test_returned_capabilities_have_no_raw_sdk_path_through_object_state() -> None:
    frozen = FrozenSession()
    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )
    observer = authority.observer_client("s3")
    raw = frozen.clients["s3"]

    reachable = _reachable_without_module_globals(
        authority,
        authority.client,
        authority.close,
        observer,
        observer.invoke,
        observer.close,
        observer.require_scope,
    )

    for bound_method, expected_self in (
        (authority.client, authority),
        (authority.close, authority),
        (observer.invoke, observer),
        (observer.close, observer),
        (observer.require_scope, observer),
    ):
        assert bound_method.__self__ is expected_self
        assert bound_method.__func__.__closure__ is None
    assert all(value is not raw for value in reachable)
    for attribute in (
        "_clients",
        "_handle",
        "_invoke_bound",
        "_closed_bound",
        "_close_bound",
        "_allowed_methods",
    ):
        with pytest.raises(AttributeError):
            getattr(observer, attribute)
        with pytest.raises(AttributeError):
            getattr(authority, attribute)
    with pytest.raises(AttributeError):
        observer._service = "cloudformation"  # type: ignore[attr-defined]
    assert observer.invoke("head_bucket", Bucket="exact") == {
        "method": "head_bucket",
        "request": {"Bucket": "exact"},
    }
    authority.close()


def test_exported_catalog_rebinding_cannot_widen_private_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = FrozenSession()
    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )
    observer = authority.observer_client("s3")
    mutation = authority.mutation_client("s3")
    widened_observer = dict(AWS_OBSERVER_METHODS)
    widened_observer["s3"] = frozenset({"head_object", "put_object"})
    widened_mutation = dict(AWS_MUTATION_METHODS)
    widened_mutation["s3"] = frozenset({"put_object", "head_object"})
    monkeypatch.setattr(
        aws_authority,
        "AWS_OBSERVER_METHODS",
        widened_observer,
    )
    monkeypatch.setattr(
        aws_authority,
        "AWS_MUTATION_METHODS",
        widened_mutation,
    )

    with pytest.raises(AwsAuthorityError, match="outside.*observer"):
        observer.invoke("put_object", Bucket="bucket", Key="key", Body=b"x")
    with pytest.raises(AwsAuthorityError, match="outside.*mutation"):
        mutation.invoke("head_object", Bucket="bucket", Key="key")
    new_observer = authority.observer_client("s3")
    new_mutation = authority.mutation_client("s3")
    with pytest.raises(AwsAuthorityError, match="outside.*observer"):
        new_observer.invoke(
            "put_object",
            Bucket="bucket",
            Key="key",
            Body=b"x",
        )
    with pytest.raises(AwsAuthorityError, match="outside.*mutation"):
        new_mutation.invoke("head_object", Bucket="bucket", Key="key")
    assert frozen.clients["s3"].calls == []
    authority.close()


@pytest.mark.parametrize(
    "method",
    [
        "get_role",
        "list_role_policies",
        "get_role_policy",
        "list_attached_role_policies",
    ],
)
def test_iam_authority_is_read_only_and_supports_exact_role_policy_evidence(
    method: str,
) -> None:
    frozen = FrozenSession()
    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )
    iam = authority.observer_client("iam")

    assert iam.invoke(
        method,
        RoleName="openclaw-agentcore-execution-role-eu-west-1",
    )["method"] == method
    with pytest.raises(AwsAuthorityError, match="outside.*observer"):
        iam.invoke(
            "attach_role_policy",
            RoleName="openclaw-agentcore-execution-role-eu-west-1",
            PolicyArn=f"arn:aws:iam::{ACCOUNT}:policy/admin",
        )
    with pytest.raises(AwsAuthorityError, match="mutation capability"):
        authority.mutation_client("iam")
    authority.close()


def test_service_method_catalogs_are_exact_and_immutable() -> None:
    assert tuple(AWS_OBSERVER_METHODS) == AWS_AUTHORITY_SERVICES
    assert tuple(AWS_MUTATION_METHODS) == AWS_AUTHORITY_SERVICES
    assert AWS_OBSERVER_METHODS["iam"] == frozenset(
        {
            "get_role",
            "list_role_policies",
            "get_role_policy",
            "list_attached_role_policies",
        }
    )
    assert AWS_MUTATION_METHODS["iam"] == frozenset()
    assert AWS_OBSERVER_METHODS["s3"] == frozenset(
        {"head_bucket", "head_object"}
    )
    assert AWS_OBSERVER_METHODS["cloudformation"] == frozenset(
        {
            "describe_stacks",
            "get_template",
            "get_stack_policy",
            "describe_change_set",
            "describe_stack_drift_detection_status",
            "describe_stack_resource_drifts",
        }
    )
    assert AWS_OBSERVER_METHODS["ecr"] == frozenset(
        {
            "batch_check_layer_availability",
            "batch_get_image",
            "describe_images",
            "describe_image_scan_findings",
            "describe_repositories",
            "get_signing_configuration",
            "describe_image_signing_status",
        }
    )
    assert "get_download_url_for_layer" not in AWS_OBSERVER_METHODS["ecr"]
    assert AWS_OBSERVER_METHODS["bedrock-agentcore-control"] == frozenset(
        {
            "list_agent_runtime_versions",
            "list_agent_runtimes",
            "get_agent_runtime",
            "list_agent_runtime_endpoints",
            "get_agent_runtime_endpoint",
            "get_resource_policy",
        }
    )
    assert AWS_MUTATION_METHODS["cloudformation"] == frozenset(
        {
            "create_stack",
            "update_stack",
            "create_change_set",
            "execute_change_set",
            "detect_stack_drift",
        }
    )
    with pytest.raises(TypeError):
        AWS_OBSERVER_METHODS["s3"] = frozenset({"delete_bucket"})  # type: ignore[index]
    with pytest.raises(TypeError):
        AWS_MUTATION_METHODS["s3"] = frozenset({"delete_bucket"})  # type: ignore[index]


def test_iam_accepts_only_the_sdk_global_endpoint_derived_from_exact_region() -> None:
    config = _config_factory(
        region_name=REGION,
        ignore_configured_endpoint_urls=True,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    iam = FakeClient("iam", config, account=ACCOUNT)
    _validate_raw_client(iam, service="iam", region=REGION)

    iam.meta.region_name = REGION
    iam.meta.config.region_name = REGION
    with pytest.raises(AwsAuthorityError, match="configuration"):
        _validate_raw_client(iam, service="iam", region=REGION)

    iam.meta.region_name = "aws-global"
    iam.meta.config.region_name = "aws-global"
    iam.meta.endpoint_url = "https://attacker.invalid"
    with pytest.raises(AwsAuthorityError, match="configuration"):
        _validate_raw_client(iam, service="iam", region=REGION)


def test_client_rejected_during_attestation_is_closed_with_prior_clients() -> None:
    config = _config_factory(
        region_name=REGION,
        ignore_configured_endpoint_urls=True,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 2},
    )
    rejected = FakeClient("s3", config, account=ACCOUNT)
    frozen = FrozenSession(overrides={"s3": rejected})

    with pytest.raises(AwsAuthorityError, match="configuration"):
        _authenticate_frozen_source(
            _plan_v2(),
            frozen_credentials=FrozenCredentials(),
            frozen_session_factory=SessionFactory(frozen),
            config_factory=_config_factory,
            ca_bundle_path=CA_BUNDLE_PATH,
        )

    assert rejected.closed is True
    assert rejected.close_calls == 1
    assert frozen.clients["sts"].closed is True


def test_explicit_retained_ca_defeats_hostile_ambient_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_CA_BUNDLE", "/tmp/attacker-aws.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/attacker-requests.pem")
    frozen = FrozenSession()

    authority = _authenticate_frozen_source(
        _plan_v2(),
        frozen_credentials=FrozenCredentials(),
        frozen_session_factory=SessionFactory(frozen),
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
    )

    assert {verify for _, _, _, verify in frozen.calls} == {CA_BUNDLE_PATH}
    authority.close()


@pytest.mark.parametrize(
    "poison",
    [
        "",
        "relative.pem",
        "/tmp/attacker.pem",
        "/dev/fd/not-a-number",
        "/dev/fd/0777",
    ],
)
def test_retained_ca_must_be_an_exact_descriptor_path(poison: str) -> None:
    with pytest.raises(AwsAuthorityError, match="CA bundle"):
        _authenticate_frozen_source(
            _plan_v2(),
            frozen_credentials=FrozenCredentials(),
            frozen_session_factory=SessionFactory(FrozenSession()),
            config_factory=_config_factory,
            ca_bundle_path=poison,
        )


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
SOURCE_ACCESS_KEY = "AKIA" + "A" * 16
SOURCE_SECRET_KEY = "s" * 40


class AssumeRoleClient:
    def __init__(
        self,
        config: object,
        *,
        response_override: dict[str, object] | None = None,
    ) -> None:
        client_config = SimpleNamespace(**vars(config))
        self.meta = SimpleNamespace(
            region_name=REGION,
            endpoint_url="https://sts.eu-west-1.amazonaws.com",
            service_model=SimpleNamespace(service_name="sts"),
            config=client_config,
        )
        self.response_override = response_override
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def assume_role(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        session_name = str(kwargs["RoleSessionName"])
        if self.response_override is not None:
            return self.response_override
        return {
            "Credentials": {
                "AccessKeyId": "ASIA" + "B" * 16,
                "SecretAccessKey": "t" * 40,
                "SessionToken": "temporary-session-token",
                "Expiration": NOW + timedelta(seconds=3600),
            },
            "AssumedRoleUser": {
                "AssumedRoleId": f"AROATEST:{session_name}",
                "Arn": (
                    f"arn:aws:sts::{ACCOUNT}:assumed-role/"
                    f"PersonalOperatorDeploymentRole/{session_name}"
                ),
            },
        }

    def close(self) -> None:
        self.closed = True


class SourceStaticSession:
    def __init__(self, client: AssumeRoleClient) -> None:
        self.assume_role_client = client
        self.calls: list[tuple[str, str, object, str]] = []

    def client(
        self,
        service: str,
        *,
        region_name: str,
        config: object,
        verify: str,
    ) -> AssumeRoleClient:
        self.calls.append((service, region_name, config, verify))
        return self.assume_role_client


class ExplicitSessionFactory:
    def __init__(self, session: object) -> None:
        self.session = session
        self.calls: list[dict[str, str]] = []

    def __call__(self, **kwargs: str) -> object:
        forbidden = {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_ROLE_ARN",
            "AWS_ENDPOINT_URL_STS",
            "AWS_ACCOUNT_ID_ENDPOINT_MODE",
            "AWS_RETRY_MODE",
            "AWS_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "HTTPS_PROXY",
            "https_proxy",
        }
        assert forbidden.isdisjoint(os.environ)
        assert os.environ["AWS_CONFIG_FILE"] == os.devnull
        assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
        assert os.environ["BOTO_CONFIG"] == os.devnull
        assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
        assert os.environ["AWS_DATA_PATH"] == os.devnull
        assert os.environ["HOME"] == os.devnull
        self.calls.append(kwargs)
        return self.session


class ExactRoleFrozenSession(FrozenSession):
    def __init__(self) -> None:
        super().__init__()
        self.expected_session_name = ""

    def client(
        self,
        service: str,
        *,
        region_name: str,
        config: object,
        verify: str,
    ) -> FakeClient:
        client = super().client(
            service,
            region_name=region_name,
            config=config,
            verify=verify,
        )
        if service == "sts":
            session = self

            def exact_identity() -> dict[str, str]:
                return {
                    "Account": ACCOUNT,
                    "Arn": (
                        f"arn:aws:sts::{ACCOUNT}:assumed-role/"
                        "PersonalOperatorDeploymentRole/"
                        f"{session.expected_session_name}"
                    ),
                    "UserId": f"AROATEST:{session.expected_session_name}",
                }

            client.get_caller_identity = exact_identity  # type: ignore[method-assign]
        return client


def _write_closed_profiles(
    root: Path,
    *,
    target_lines: list[str] | None = None,
    source_config_lines: list[str] | None = None,
    source_lines: list[str] | None = None,
) -> Path:
    aws_directory = root / ".aws"
    aws_directory.mkdir(mode=0o700)
    config = aws_directory / "config"
    credentials = aws_directory / "credentials"
    config.write_text(
        "\n".join(
            [
                "[profile personal-operator-deploy]",
                *(target_lines or [
                    (
                        "role_arn = arn:aws:iam::123456789012:role/"
                        "PersonalOperatorDeploymentRole"
                    ),
                    "source_profile = personal-operator-bootstrap",
                    "duration_seconds = 3600",
                ]),
                "region = eu-west-1",
                "output = json",
                "",
                "[profile personal-operator-bootstrap]",
                *(source_config_lines or [
                    "region = eu-west-1",
                    "output = json",
                ]),
                "",
            ]
        ),
        encoding="utf-8",
    )
    credentials.write_text(
        "\n".join(
            [
                "[personal-operator-bootstrap]",
                *(source_lines or [
                    f"aws_access_key_id = {SOURCE_ACCESS_KEY}",
                    f"aws_secret_access_key = {SOURCE_SECRET_KEY}",
                ]),
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    credentials.chmod(0o600)
    return aws_directory


def _closed_profile_authority(
    aws_directory: Path,
    *,
    response_override: dict[str, object] | None = None,
    final_overrides: dict[str, object] | None = None,
) -> tuple[
    AuthenticatedAwsAuthorityV2,
    ExplicitSessionFactory,
    AssumeRoleClient,
    ExplicitSessionFactory,
    ExactRoleFrozenSession,
]:
    assume_client = AssumeRoleClient(
        _config_factory(
            region_name=REGION,
            ignore_configured_endpoint_urls=True,
            proxies={},
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
        response_override=response_override,
    )
    source_session = SourceStaticSession(assume_client)
    source_factory = ExplicitSessionFactory(source_session)
    final_session = ExactRoleFrozenSession()
    final_session.overrides.update(final_overrides or {})
    final_session.expected_session_name = aws_authority._role_session_name(
        _plan_v2()
    )
    final_factory = ExplicitSessionFactory(final_session)
    authority = aws_authority._authenticate_closed_profile(
        _plan_v2(),
        aws_directory=aws_directory,
        source_session_factory=source_factory,
        frozen_session_factory=final_factory,
        config_factory=_config_factory,
        ca_bundle_path=CA_BUNDLE_PATH,
        now_factory=lambda: NOW,
    )
    return (
        authority,
        source_factory,
        assume_client,
        final_factory,
        final_session,
    )


def test_closed_profile_bootstrap_ignores_ambient_providers_and_assumes_exact_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    hostile = {
        "AWS_ACCESS_KEY_ID": "attacker",
        "AWS_SECRET_ACCESS_KEY": "attacker",
        "AWS_SESSION_TOKEN": "attacker",
        "AWS_PROFILE": "attacker",
        "AWS_DEFAULT_PROFILE": "attacker",
        "AWS_CONFIG_FILE": "/tmp/attacker-config",
        "AWS_SHARED_CREDENTIALS_FILE": "/tmp/attacker-credentials",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/attacker-token",
        "AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/Attacker",
        "AWS_ENDPOINT_URL_STS": "https://attacker.invalid",
        "AWS_DATA_PATH": "/tmp/attacker-models",
        "AWS_ACCOUNT_ID_ENDPOINT_MODE": "required",
        "AWS_RETRY_MODE": "adaptive",
        "AWS_CA_BUNDLE": "/tmp/attacker-ca",
        "REQUESTS_CA_BUNDLE": "/tmp/attacker-ca",
        "HTTPS_PROXY": "https://attacker.invalid",
        "https_proxy": "https://attacker.invalid",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    authority, source_factory, assume_client, final_factory, final_session = (
        _closed_profile_authority(aws_directory)
    )

    assert source_factory.calls == [
        {
            "region_name": REGION,
            "aws_access_key_id": SOURCE_ACCESS_KEY,
            "aws_secret_access_key": SOURCE_SECRET_KEY,
        }
    ]
    assert len(assume_client.calls) == 1
    assume_request = assume_client.calls[0]
    assert assume_request == {
        "RoleArn": (
            f"arn:aws:iam::{ACCOUNT}:role/PersonalOperatorDeploymentRole"
        ),
        "RoleSessionName": assume_request["RoleSessionName"],
        "DurationSeconds": 3600,
    }
    assert isinstance(assume_request["RoleSessionName"], str)
    assert str(assume_request["RoleSessionName"]).startswith("po-v2-")
    assert final_factory.calls == [
        {
            "region_name": REGION,
            "aws_access_key_id": "ASIA" + "B" * 16,
            "aws_secret_access_key": "t" * 40,
            "aws_session_token": "temporary-session-token",
        }
    ]
    assert {verify for _, _, _, verify in final_session.calls} == {
        CA_BUNDLE_PATH
    }
    assert all(
        config.ignore_configured_endpoint_urls is True
        and config.proxies == {}
        and config.retries == {"mode": "standard", "total_max_attempts": 1}
        for _, _, config, _ in final_session.calls
    )
    assert assume_client.closed is True
    assert all(os.environ[key] == value for key, value in hostile.items())
    authority.close()


def test_closed_environment_nested_scope_never_restores_inner_hostile_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DATA_PATH", "/original-reviewed-baseline")

    with aws_authority._closed_aws_environment():
        assert os.environ["AWS_DATA_PATH"] == os.devnull
        os.environ["AWS_DATA_PATH"] = "/inner-attacker"
        with aws_authority._closed_aws_environment():
            assert os.environ["AWS_DATA_PATH"] == os.devnull
            assert os.environ["AWS_CONFIG_FILE"] == os.devnull
        assert os.environ["AWS_DATA_PATH"] == os.devnull
        assert os.environ["AWS_CONFIG_FILE"] == os.devnull

    assert os.environ["AWS_DATA_PATH"] == "/original-reviewed-baseline"


def test_closed_environment_pins_home_and_loader_paths_away_from_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_home = tmp_path / "hostile-home"
    caller_models = caller_home / ".aws" / "models"
    caller_models.mkdir(parents=True)
    caller_data = tmp_path / "hostile-aws-data"
    caller_data.mkdir()
    monkeypatch.setenv("HOME", str(caller_home))
    monkeypatch.setenv("AWS_DATA_PATH", str(caller_data))

    with aws_authority._closed_aws_environment():
        assert os.environ["HOME"] == os.devnull
        assert os.environ["AWS_DATA_PATH"] == os.devnull
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; from botocore.session import Session; "
                    "session = Session(); "
                    "loader = session.get_component('data_loader'); "
                    "loader.load_service_model('sts', 'service-2'); "
                    "print(json.dumps(loader.search_paths))"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        search_paths = json.loads(probe.stdout)
        assert all(str(caller_home) not in path for path in search_paths)
        assert all(str(caller_data) not in path for path in search_paths)
        assert os.devnull in search_paths
        assert f"{os.devnull}/.aws/models" in search_paths

    assert os.environ["HOME"] == str(caller_home)
    assert os.environ["AWS_DATA_PATH"] == str(caller_data)


def test_closed_environment_serializes_competing_thread_lifetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DATA_PATH", "/original-reviewed-baseline")
    contender_started = threading.Event()
    contender_entered = threading.Event()
    contender_done = threading.Event()
    failures: list[BaseException] = []

    def contend() -> None:
        contender_started.set()
        try:
            with aws_authority._closed_aws_environment():
                contender_entered.set()
                assert os.environ["AWS_DATA_PATH"] == os.devnull
        except BaseException as error:
            failures.append(error)
        finally:
            contender_done.set()

    worker = threading.Thread(target=contend, daemon=True)
    with aws_authority._closed_aws_environment():
        worker.start()
        assert contender_started.wait(timeout=1)
        assert not contender_entered.wait(timeout=0.05)
        assert os.environ["AWS_DATA_PATH"] == os.devnull

    assert contender_entered.wait(timeout=1)
    assert contender_done.wait(timeout=1)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert failures == []
    assert os.environ["AWS_DATA_PATH"] == "/original-reviewed-baseline"


@pytest.mark.parametrize(
    "target_lines",
    [
        [
            "role_arn = arn:aws:iam::999999999999:role/PersonalOperatorDeploymentRole",
            "source_profile = personal-operator-bootstrap",
            "duration_seconds = 3600",
        ],
        [
            "role_arn = arn:aws:iam::123456789012:role/PersonalOperatorDeploymentRole",
            "source_profile = attacker",
            "duration_seconds = 3600",
        ],
        [
            "role_arn = arn:aws:iam::123456789012:role/PersonalOperatorDeploymentRole",
            "source_profile = personal-operator-bootstrap",
            "duration_seconds = 900",
        ],
        [
            "role_arn = arn:aws:iam::123456789012:role/PersonalOperatorDeploymentRole",
            "source_profile = personal-operator-bootstrap",
            "duration_seconds = 3600",
            "credential_process = attacker",
        ],
        [
            "role_arn = arn:aws:iam::123456789012:role/PersonalOperatorDeploymentRole",
            "source_profile = personal-operator-bootstrap",
            "duration_seconds = 3600",
            "sso_session = attacker",
        ],
        [
            "role_arn = arn:aws:iam::123456789012:role/PersonalOperatorDeploymentRole",
            "source_profile = personal-operator-bootstrap",
            "duration_seconds = 3600",
            "web_identity_token_file = /tmp/attacker",
        ],
    ],
)
def test_closed_profile_rejects_substitution_or_refreshable_target_provider(
    tmp_path: Path,
    target_lines: list[str],
) -> None:
    aws_directory = _write_closed_profiles(tmp_path, target_lines=target_lines)

    with pytest.raises(AwsAuthorityError, match="profile"):
        _closed_profile_authority(aws_directory)


@pytest.mark.parametrize(
    "source_lines",
    [
        [
            f"aws_access_key_id = {SOURCE_ACCESS_KEY}",
            f"aws_secret_access_key = {SOURCE_SECRET_KEY}",
            "aws_session_token = attacker",
        ],
        [
            f"aws_access_key_id = {SOURCE_ACCESS_KEY}",
            f"aws_secret_access_key = {SOURCE_SECRET_KEY}",
            "credential_process = attacker",
        ],
        [f"aws_access_key_id = {SOURCE_ACCESS_KEY}"],
    ],
)
def test_closed_profile_rejects_nonstatic_or_incomplete_source_credentials(
    tmp_path: Path,
    source_lines: list[str],
) -> None:
    aws_directory = _write_closed_profiles(tmp_path, source_lines=source_lines)

    with pytest.raises(AwsAuthorityError, match="profile"):
        _closed_profile_authority(aws_directory)


@pytest.mark.parametrize(
    "source_config_lines",
    [
        ["region = us-east-1", "output = json"],
        ["region = eu-west-1", "output = text"],
        [
            "region = eu-west-1",
            "output = json",
            "credential_process = attacker",
        ],
        [
            "region = eu-west-1",
            "output = json",
            "sso_session = attacker",
        ],
    ],
)
def test_closed_profile_rejects_source_configuration_substitution(
    tmp_path: Path,
    source_config_lines: list[str],
) -> None:
    aws_directory = _write_closed_profiles(
        tmp_path,
        source_config_lines=source_config_lines,
    )

    with pytest.raises(AwsAuthorityError, match="profile"):
        _closed_profile_authority(aws_directory)


@pytest.mark.parametrize("filename", ["config", "credentials"])
@pytest.mark.parametrize("attack", ["symlink", "mode", "hardlink"])
def test_closed_profile_files_must_be_retained_owner_only_single_link_regulars(
    tmp_path: Path,
    filename: str,
    attack: str,
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    target = aws_directory / filename
    if attack == "symlink":
        replacement = aws_directory / f"{filename}.real"
        target.rename(replacement)
        target.symlink_to(replacement.name)
    elif attack == "mode":
        target.chmod(0o640)
    else:
        os.link(target, aws_directory / f"{filename}.hardlink")

    with pytest.raises(AwsAuthorityError, match="profile"):
        _closed_profile_authority(aws_directory)


@pytest.mark.parametrize("attack", ["symlink", "mode"])
def test_closed_profile_directory_must_be_retained_owner_controlled(
    tmp_path: Path,
    attack: str,
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    if attack == "symlink":
        retained = tmp_path / ".aws-retained"
        aws_directory.rename(retained)
        aws_directory.symlink_to(retained.name)
    else:
        aws_directory.chmod(0o775)

    with pytest.raises(AwsAuthorityError, match="profile directory"):
        _closed_profile_authority(aws_directory)


def test_closed_profile_accepts_current_owner_0755_aws_directory(
    tmp_path: Path,
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    aws_directory.chmod(0o755)

    authority, *_ = _closed_profile_authority(aws_directory)

    authority.close()


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {
            "AccessKeyId": "ASIA" + "B" * 16,
            "SecretAccessKey": "t" * 40,
            "SessionToken": "temporary-session-token",
            "Expiration": NOW - timedelta(seconds=1),
        },
        {
            "AccessKeyId": "ASIA" + "B" * 16,
            "SecretAccessKey": "t" * 40,
            "SessionToken": "temporary-session-token",
            "Expiration": NOW + timedelta(seconds=7200),
        },
    ],
)
def test_closed_profile_rejects_malformed_or_out_of_window_assumed_credentials(
    tmp_path: Path,
    credentials: dict[str, object],
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    response = {
        "Credentials": credentials,
        "AssumedRoleUser": {
            "AssumedRoleId": "AROATEST:po-v2-attacker",
            "Arn": (
                f"arn:aws:sts::{ACCOUNT}:assumed-role/"
                "PersonalOperatorDeploymentRole/po-v2-attacker"
            ),
        },
    }

    with pytest.raises(AwsAuthorityError, match="assumed role"):
        _closed_profile_authority(
            aws_directory,
            response_override=response,
        )


def test_closed_profile_rejects_substituted_final_service_endpoint(
    tmp_path: Path,
) -> None:
    aws_directory = _write_closed_profiles(tmp_path)
    config = _config_factory(
        region_name=REGION,
        ignore_configured_endpoint_urls=True,
        proxies={},
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    attacker = FakeClient("s3", config, account=ACCOUNT)
    attacker.meta.endpoint_url = "https://attacker.invalid"

    with pytest.raises(AwsAuthorityError, match="endpoint"):
        _closed_profile_authority(
            aws_directory,
            final_overrides={"s3": attacker},
        )
    assert attacker.closed is True
