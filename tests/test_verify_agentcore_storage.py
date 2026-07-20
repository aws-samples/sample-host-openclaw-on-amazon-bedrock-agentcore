"""Contract tests for fatal AgentCore runtime/session-storage verification."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-agentcore-storage.py"
SPEC = importlib.util.spec_from_file_location("verify_agentcore_storage", SCRIPT)
assert SPEC and SPEC.loader
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)

RUNTIME_ID = "openclaw_agent-abcdefghij"
VERSION = "7"
SOURCE_COMMIT = "a" * 40
ENDPOINT_NAME = f"release_{SOURCE_COMMIT}"
ENDPOINT_ID = "openclaw_endpoint-abcdefghij"
ACCOUNT = "123456789012"
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agent/01234567-89ab-4cde-8fab-0123456789ab:7"
)
ENDPOINT_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agentEndpoint/11234567-89ab-4cde-8fab-0123456789ab"
)
ROLE_ARN = "arn:aws:iam::123456789012:role/openclaw-agentcore-execution-role-eu-west-1"
KMS_ARN = "arn:aws:kms:eu-west-1:123456789012:key/01234567-89ab-4cde-8fab-0123456789ab"


def runtime_response(*, status: str = "READY", version: str = VERSION, filesystems=None):
    return {
        "agentRuntimeArn": RUNTIME_ARN.removesuffix(f":{VERSION}") + f":{version}",
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeVersion": version,
        "roleArn": ROLE_ARN,
        "status": status,
        "filesystemConfigurations": (
            [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
            if filesystems is None
            else filesystems
        ),
    }


def endpoint_response(*, status: str = "READY", live: str = VERSION, target: str = VERSION):
    return {
        "agentRuntimeArn": RUNTIME_ARN.removesuffix(f":{VERSION}") + f":{live}",
        "agentRuntimeEndpointArn": ENDPOINT_ARN,
        "name": ENDPOINT_NAME,
        "id": ENDPOINT_ID,
        "status": status,
        "liveVersion": live,
        "targetVersion": target,
    }


class FakeControl:
    def __init__(self, latest, endpoints, exact=None, region="eu-west-1"):
        self.meta = SimpleNamespace(region_name=region)
        self.latest = list(latest)
        self.endpoints = list(endpoints)
        if exact is None:
            self.exact = list(latest)
        elif isinstance(exact, list):
            self.exact = list(exact)
        else:
            self.exact = [exact]
        self.calls = []

    def get_agent_runtime(self, **kwargs):
        self.calls.append(("runtime", kwargs))
        if "agentRuntimeVersion" in kwargs:
            if len(self.exact) > 1:
                return self.exact.pop(0)
            return self.exact[0]
        if len(self.latest) > 1:
            return self.latest.pop(0)
        return self.latest[0]

    def get_agent_runtime_endpoint(self, **kwargs):
        self.calls.append(("endpoint", kwargs))
        if len(self.endpoints) > 1:
            return self.endpoints.pop(0)
        return self.endpoints[0]


class FakeS3:
    def __init__(
        self,
        *,
        region="eu-west-1",
        location="eu-west-1",
        versioning=None,
        encryption=None,
        public=None,
    ):
        self.meta = SimpleNamespace(region_name=region)
        self.location = location
        self.versioning = versioning or {"Status": "Enabled"}
        self.encryption = encryption or {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": KMS_ARN,
                        },
                        "BucketKeyEnabled": True,
                    }
                ]
            }
        }
        self.public = public or {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }

    def get_bucket_location(self, **_kwargs):
        return {"LocationConstraint": self.location}

    def get_bucket_versioning(self, **_kwargs):
        return self.versioning

    def get_bucket_encryption(self, **_kwargs):
        return self.encryption

    def get_public_access_block(self, **_kwargs):
        return self.public


def verify(control=None, s3=None, **overrides):
    return storage.verify_agentcore_storage(
        control_client=control
        or FakeControl([runtime_response()], [endpoint_response()]),
        s3_client=s3 or FakeS3(),
        runtime_id=RUNTIME_ID,
        endpoint_name=ENDPOINT_NAME,
        expected_endpoint_id=ENDPOINT_ID,
        expected_runtime_arn=RUNTIME_ARN,
        expected_role_arn=ROLE_ARN,
        bucket="personal-operator-workspaces",
        expected_kms_key_arn=KMS_ARN,
        timeout_seconds=10,
        poll_seconds=0,
        sleep=lambda _seconds: None,
        monotonic=iter([0, 1, 2, 3, 4, 5]).__next__,
        **overrides,
    )


def test_verifies_ready_endpoint_exact_runtime_version_and_bucket_controls() -> None:
    control = FakeControl([runtime_response()], [endpoint_response()])

    evidence = verify(control=control)

    assert evidence == {
        "bucket": "personal-operator-workspaces",
        "endpointId": ENDPOINT_ID,
        "endpointName": ENDPOINT_NAME,
        "mountPath": "/mnt/workspace",
        "region": "eu-west-1",
        "runtimeArn": RUNTIME_ARN,
        "runtimeId": RUNTIME_ID,
        "runtimeVersion": VERSION,
    }
    assert control.calls == [
        (
            "runtime",
            {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": VERSION},
        ),
        (
            "endpoint",
            {"agentRuntimeId": RUNTIME_ID, "endpointName": ENDPOINT_NAME},
        ),
    ]


@pytest.mark.parametrize(
    "filesystems",
    [
        [],
        [
            {"sessionStorage": {"mountPath": "/mnt/workspace"}},
            {"sessionStorage": {"mountPath": "/mnt/other"}},
        ],
        [{"sessionStorage": {"mountPath": "/mnt/other"}}],
        [{"s3FilesAccessPoint": {"mountPath": "/mnt/workspace"}}],
    ],
)
def test_rejects_any_runtime_without_one_exact_session_mount(filesystems) -> None:
    with pytest.raises(storage.StorageVerificationError, match="filesystem|mount"):
        verify(
            control=FakeControl(
                [runtime_response(filesystems=filesystems)], [endpoint_response()]
            )
        )


def test_polls_until_the_exact_runtime_version_and_endpoint_are_ready() -> None:
    control = FakeControl(
        [runtime_response()],
        [endpoint_response(status="UPDATING"), endpoint_response()],
        exact=[runtime_response(status="UPDATING"), runtime_response()],
    )

    assert verify(control=control)["runtimeVersion"] == VERSION
    assert control.calls[-2] == (
        "runtime",
        {"agentRuntimeId": RUNTIME_ID, "agentRuntimeVersion": VERSION},
    )


def test_newer_runtime_version_cannot_move_the_release_endpoint_binding() -> None:
    newer = runtime_response(version="8")
    control = FakeControl(
        [newer],
        [endpoint_response()],
        exact=runtime_response(),
    )

    evidence = verify(control=control)

    assert evidence["runtimeVersion"] == VERSION
    assert all(
        kwargs.get("agentRuntimeVersion") == VERSION
        for kind, kwargs in control.calls
        if kind == "runtime"
    )


@pytest.mark.parametrize(
    ("latest", "endpoint", "exact", "message"),
    [
        (runtime_response(status="CREATE_FAILED"), endpoint_response(), None, "failed"),
        (runtime_response(), endpoint_response(status="UPDATE_FAILED"), None, "failed"),
        (runtime_response(), endpoint_response(live="6", target="7"), None, "version"),
        (runtime_response(), endpoint_response(live="6", target="6"), None, "version"),
        (
            runtime_response(),
            endpoint_response(),
            {**runtime_response(), "agentRuntimeArn": RUNTIME_ARN.replace("agent/", "agent/f")},
            "ARN",
        ),
    ],
)
def test_rejects_terminal_status_and_every_endpoint_or_version_drift(
    latest, endpoint, exact, message
) -> None:
    with pytest.raises(storage.StorageVerificationError, match=message):
        verify(control=FakeControl([latest], [endpoint], exact=exact))


def test_rejects_endpoint_id_drift_even_when_name_and_version_match() -> None:
    endpoint = {**endpoint_response(), "id": "other_endpoint-abcdefghij"}

    with pytest.raises(storage.StorageVerificationError, match="endpoint ID"):
        verify(control=FakeControl([runtime_response()], [endpoint]))


@pytest.mark.parametrize(
    ("s3", "message"),
    [
        (FakeS3(region="us-west-2"), "region"),
        (FakeS3(location="us-west-2"), "region"),
        (FakeS3(versioning={"Status": "Suspended"}), "versioning"),
        (
            FakeS3(
                encryption={
                    "ServerSideEncryptionConfiguration": {
                        "Rules": [
                            {
                                "ApplyServerSideEncryptionByDefault": {
                                    "SSEAlgorithm": "AES256"
                                }
                            }
                        ]
                    }
                }
            ),
            "KMS",
        ),
        (
            FakeS3(
                public={
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": False,
                        "RestrictPublicBuckets": True,
                    }
                }
            ),
            "public",
        ),
    ],
)
def test_rejects_bucket_region_versioning_kms_or_public_access_drift(s3, message) -> None:
    with pytest.raises(storage.StorageVerificationError, match=message):
        verify(s3=s3)


def test_rejects_wrong_control_region_before_any_api_call() -> None:
    control = FakeControl(
        [runtime_response()], [endpoint_response()], region="us-east-1"
    )
    with pytest.raises(storage.StorageVerificationError, match="region"):
        verify(control=control)
    assert control.calls == []


def test_times_out_without_accepting_non_ready_state() -> None:
    with pytest.raises(storage.StorageVerificationError, match="timed out"):
        storage.verify_agentcore_storage(
            control_client=FakeControl(
                [runtime_response(status="UPDATING")],
                [endpoint_response(status="UPDATING")],
            ),
            s3_client=FakeS3(),
            runtime_id=RUNTIME_ID,
            expected_endpoint_id=ENDPOINT_ID,
            endpoint_name=ENDPOINT_NAME,
            expected_runtime_arn=RUNTIME_ARN,
            expected_role_arn=ROLE_ARN,
            bucket="personal-operator-workspaces",
            expected_kms_key_arn=KMS_ARN,
            timeout_seconds=1,
            poll_seconds=0,
            sleep=lambda _seconds: None,
            monotonic=iter([0, 2]).__next__,
        )
