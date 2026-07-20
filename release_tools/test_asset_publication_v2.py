from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
from typing import Iterator

import pytest

from release_tools.asset_publication_v2 import (
    ASSET_ARTIFACT_MAGIC,
    AssetPublicationAmbiguous,
    AssetPublicationError,
    AssetPublicationV2,
    S3AssetPublisher,
)
from release_tools.contracts import (
    PrivateMutationEnvelopeV2,
    ReleasePlanV2,
    VerifiedPrivateMutationV2,
    write_new_private_mutation_envelope,
)
from release_tools.dispatch_attempt_v2 import (
    DispatchAttemptError,
    FreshDispatchAuthorityV1,
    ReleaseDispatchAttemptV1,
    _mint_fresh_dispatch_authority,
)
from release_tools.test_contracts import _release_plan_v2
from release_tools.test_aws_authority_v2 import attested_test_client
from release_tools.test_transaction import (
    _advance_v2_until_phase,
    _create_v2,
    _resolved_mutation_request,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
PAYLOAD = b'{"Resources":{}}'
ASSET_ID = "d" * 64


def _fresh_dispatch(
    verified: VerifiedPrivateMutationV2,
    *,
    provider: str = "S3",
    operation_sha256: str | None = None,
    resolved_request_sha256: str | None = None,
) -> tuple[FreshDispatchAuthorityV1, ReleaseDispatchAttemptV1]:
    resolved = verified.resolved_request
    request = resolved.mutation_request
    attempt = ReleaseDispatchAttemptV1(
        release_plan_sha256=request.plan_sha256,
        evidence_store_sha256="1" * 64,
        journal_path_sha256="2" * 64,
        journal_execution_id="3" * 64,
        journal_revision=1,
        completed_prefix_sha256=request.completed_prefix_sha256,
        step_id=request.step_id,
        subject=request.subject,
        operation_sha256=(operation_sha256 or request.operation_sha256),
        resolved_request_sha256=(
            resolved_request_sha256 or resolved.digest()
        ),
        provider=provider,
    )
    return _mint_fresh_dispatch_authority(attempt), attempt


class _ForgedFreshDispatchAuthority(FreshDispatchAuthorityV1):
    """Subclass that deliberately skips the token-gated base constructor."""

    __slots__ = ("_forged_attempt",)

    def __init__(self, attempt: ReleaseDispatchAttemptV1) -> None:
        self._forged_attempt = attempt

    def consume(self, **_kwargs: object) -> ReleaseDispatchAttemptV1:
        return self._forged_attempt


def _artifact(
    payload: bytes = PAYLOAD,
    *,
    account: str = ACCOUNT,
    region: str = REGION,
    source_commit: str = COMMIT,
    source_tree: str = TREE,
    suffix: str = "json",
    asset_id: str = ASSET_ID,
) -> bytes:
    return AssetPublicationV2.build_artifact_bytes(
        account=account,
        region=region,
        source_commit=source_commit,
        source_tree=source_tree,
        bucket_name=f"cdk-hnb659fds-assets-{account}-{region}",
        asset_id=asset_id,
        object_key=f"{asset_id}.{suffix}",
        content_type=(
            "application/json" if suffix == "json" else "application/zip"
        ),
        payload=payload,
    )


def _plan_for_asset(
    raw_artifact: bytes,
    *,
    asset_id: str,
    content_sha256: str,
) -> ReleasePlanV2:
    value = deepcopy(_release_plan_v2())
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    step = next(item for item in steps if item["kind"] == "ASSET_PUBLISH")
    artifact = next(
        item for item in artifacts if item["path"] == step["requestArtifact"]
    )
    request_sha256 = hashlib.sha256(raw_artifact).hexdigest()
    artifact.update(size=len(raw_artifact), sha256=request_sha256)
    step.update(
        subject=f"cdk:asset:{asset_id}",
        requestSha256=request_sha256,
        expectedRequestSha256=request_sha256,
        expectedContentSha256=content_sha256,
    )
    return ReleasePlanV2.from_mapping(value)


@contextmanager
def _verified_asset(
    tmp_path: Path,
    raw_artifact: bytes,
    *,
    asset_id: str = ASSET_ID,
    content_sha256: str,
) -> Iterator[VerifiedPrivateMutationV2]:
    plan = _plan_for_asset(
        raw_artifact,
        asset_id=asset_id,
        content_sha256=content_sha256,
    )
    journal = _create_v2(tmp_path, plan)
    journal.advance_preflight()
    _advance_v2_until_phase(journal, "foundation:ASSET_PUBLISH")
    journal.begin_step()
    request_path = tmp_path / "asset-request.bin"
    request_path.write_bytes(raw_artifact)
    envelope_path = tmp_path / "private-mutation.bin"
    write_new_private_mutation_envelope(
        envelope_path,
        resolved_request=_resolved_mutation_request(
            journal,
            request_artifact_size=len(raw_artifact),
        ),
        request_artifact_path=request_path,
        plan=plan,
        transaction=journal.current,
    )
    with PrivateMutationEnvelopeV2.open_verified(
        envelope_path,
        plan=plan,
        transaction=journal.current,
        scratch_dir=tmp_path / "scratch",
    ) as verified:
        yield verified


class FakeS3:
    def __init__(
        self,
        response: object | None = None,
        error: Exception | None = None,
        *,
        account: str = ACCOUNT,
        region: str = REGION,
        service: str = "s3",
        retries: dict[str, object] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.payloads: list[bytes] = []
        self.response = response
        self.error = error
        self._personal_operator_attested_account = account
        self.meta = SimpleNamespace(
            region_name=region,
            service_model=SimpleNamespace(service_name=service),
            config=SimpleNamespace(
                region_name=region,
                ignore_configured_endpoint_urls=True,
                proxies={},
                retries=(
                    {"mode": "standard", "total_max_attempts": 1}
                    if retries is None
                    else retries
                )
            ),
        )

    def put_object(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        body = kwargs["Body"]
        assert not isinstance(body, (bytes, bytearray))
        chunks: list[bytes] = []
        while True:
            chunk = body.read(64 * 1024)  # type: ignore[union-attr]
            if not chunk:
                break
            assert len(chunk) <= 64 * 1024
            chunks.append(chunk)
        payload = b"".join(chunks)
        assert body.seek(0) == 0  # type: ignore[union-attr]
        assert body.read(7) == payload[:7]  # type: ignore[union-attr]
        self.payloads.append(payload)
        if self.response is not None:
            return self.response
        return {
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "ServerSideEncryption": "aws:kms",
        }


def test_large_asset_artifact_uses_a_small_header_and_raw_payload() -> None:
    """A current-sized web archive must not cross the Base64/JSON boundary."""

    payload = b"web-asset\x00" * (2 * 1024 * 1024)
    artifact = _artifact(payload, suffix="zip")

    assert artifact.startswith(ASSET_ARTIFACT_MAGIC)
    header_size = struct.unpack(
        ">I",
        artifact[
            len(ASSET_ARTIFACT_MAGIC) : len(ASSET_ARTIFACT_MAGIC) + 4
        ],
    )[0]
    assert header_size < 4096
    assert artifact[len(ASSET_ARTIFACT_MAGIC) + 4 + header_size :] == payload
    assert len(artifact) < len(payload) + 4096
    assert base64.b64encode(payload) not in artifact


def test_synthesized_asset_id_is_independent_from_packaged_payload_digest() -> None:
    payload = b"deterministic-packaged-zip-bytes"
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    asset_id = "d" * 64
    assert asset_id != payload_sha256

    artifact = AssetPublicationV2.build_artifact_bytes(
        account=ACCOUNT,
        region=REGION,
        source_commit=COMMIT,
        source_tree=TREE,
        bucket_name=f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}",
        asset_id=asset_id,
        object_key=f"{asset_id}.zip",
        content_type="application/zip",
        payload=payload,
    )

    assert artifact.endswith(payload)
    assert payload_sha256.encode("ascii") in artifact
    assert asset_id.encode("ascii") in artifact


def test_asset_publisher_streams_exact_verified_payload_to_one_s3_call(
    tmp_path: Path,
) -> None:
    payload = b"z" * (5 * 1024 * 1024 + 17)
    content_sha256 = hashlib.sha256(payload).hexdigest()
    raw_artifact = _artifact(payload, suffix="zip")
    fake = FakeS3()

    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=content_sha256,
    ) as verified:
        fresh_authority, expected_attempt = _fresh_dispatch(verified)
        with attested_test_client(fake, service="s3") as client:
            acknowledgement = S3AssetPublisher(client).publish(
                verified,
                fresh_authority=fresh_authority,
            )

    assert acknowledgement == expected_attempt
    with pytest.raises(DispatchAttemptError, match="already consumed"):
        fresh_authority.consume(
            provider="S3",
            operation_sha256=expected_attempt.operation_sha256,
            resolved_request_sha256=expected_attempt.resolved_request_sha256,
        )
    assert fake.payloads == [payload]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
    assert call["Bucket"] == f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}"
    assert call["Key"] == f"{ASSET_ID}.zip"
    assert call["ContentLength"] == len(payload)
    assert call["ContentType"] == "application/zip"
    assert call["ChecksumAlgorithm"] == "SHA256"
    assert call["ChecksumSHA256"] == checksum
    assert call["ServerSideEncryption"] == "aws:kms"
    assert call["IfNoneMatch"] == "*"
    assert call["ExpectedBucketOwner"] == ACCOUNT
    assert call["Metadata"] == {
        "content-sha256": content_sha256,
        "asset-id": ASSET_ID,
        "source-commit": COMMIT,
        "source-tree": TREE,
    }


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.update(extra=True), "fields"),
        (lambda value: value.update(account="000000000000"), "account"),
        (lambda value: value.update(region="us-east-1"), "region"),
        (lambda value: value.update(bucketName="other"), "bucket"),
        (lambda value: value.update(assetId="e" * 64), "asset ID"),
        (lambda value: value.update(objectKey="../asset.json"), "object key"),
        (lambda value: value.update(contentType="text/html"), "content type"),
        (lambda value: value.update(contentSha256="not-a-digest"), "content digest"),
        (lambda value: value.update(contentSize=0), "content size"),
        (lambda value: value.update(sourceTree="B" * 40), "source"),
    ],
)
def test_asset_header_rejects_substitution_or_cross_subject(
    mutate: object,
    match: str,
) -> None:
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    value: dict[str, object] = {
        "schema": AssetPublicationV2.SCHEMA,
        "account": ACCOUNT,
        "region": REGION,
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "bucketName": f"cdk-hnb659fds-assets-{ACCOUNT}-{REGION}",
        "assetId": ASSET_ID,
        "objectKey": f"{ASSET_ID}.json",
        "contentType": "application/json",
        "contentSha256": digest,
        "contentSize": len(PAYLOAD),
    }
    assert callable(mutate)
    mutate(value)

    with pytest.raises(AssetPublicationError, match=match):
        AssetPublicationV2.from_mapping(value)


def test_publisher_rejects_free_object_instead_of_verified_capability() -> None:
    with pytest.raises(TypeError, match="fresh_authority"):
        S3AssetPublisher(FakeS3()).publish(object())  # type: ignore[arg-type]


def test_asset_dispatch_fence_rejects_missing_duck_crossed_or_consumed_authority(
    tmp_path: Path,
) -> None:
    raw_artifact = _artifact()
    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        missing_fake = FakeS3()
        with attested_test_client(missing_fake, service="s3") as client:
            with pytest.raises(TypeError, match="fresh_authority"):
                S3AssetPublisher(client).publish(verified)
        assert missing_fake.calls == []

        crossed_provider, _ = _fresh_dispatch(
            verified, provider="CLOUDFORMATION"
        )
        crossed_operation, _ = _fresh_dispatch(
            verified,
            operation_sha256="sha256:" + "f" * 64,
        )
        crossed_request, _ = _fresh_dispatch(
            verified,
            resolved_request_sha256="e" * 64,
        )
        consumed, consumed_attempt = _fresh_dispatch(verified)
        _, forged_attempt = _fresh_dispatch(verified)
        forged = _ForgedFreshDispatchAuthority(forged_attempt)
        assert consumed.consume(
            provider="S3",
            operation_sha256=consumed_attempt.operation_sha256,
            resolved_request_sha256=consumed_attempt.resolved_request_sha256,
        ) == consumed_attempt

        for authority in (
            object(),
            crossed_provider,
            crossed_operation,
            crossed_request,
            consumed,
            forged,
        ):
            fake = FakeS3()
            with attested_test_client(fake, service="s3") as client:
                with pytest.raises(
                    AssetPublicationError,
                    match="dispatch authority",
                ):
                    S3AssetPublisher(client).publish(
                        verified,
                        fresh_authority=authority,  # type: ignore[arg-type]
                    )
            assert fake.calls == []

        with pytest.raises(DispatchAttemptError, match="already consumed"):
            consumed.consume(
                provider="S3",
                operation_sha256=consumed_attempt.operation_sha256,
                resolved_request_sha256=(
                    consumed_attempt.resolved_request_sha256
                ),
            )


def test_raw_client_with_forgeable_account_marker_is_rejected(
    tmp_path: Path,
) -> None:
    fake = FakeS3()
    raw_artifact = _artifact()
    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    ) as verified:
        fresh_authority, _ = _fresh_dispatch(verified)
        with pytest.raises(AssetPublicationError, match="attested"):
            S3AssetPublisher(fake).publish(
                verified,
                fresh_authority=fresh_authority,
            )
    assert fake.calls == []


def test_verified_artifact_identity_drift_fails_before_provider_call(
    tmp_path: Path,
) -> None:
    raw_artifact = _artifact(source_tree="c" * 40)
    content_sha256 = hashlib.sha256(PAYLOAD).hexdigest()
    fake = FakeS3()

    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=content_sha256,
    ) as verified:
        fresh_authority, _ = _fresh_dispatch(verified)
        with attested_test_client(fake, service="s3") as client:
            with pytest.raises(AssetPublicationError, match="release identity"):
                S3AssetPublisher(client).publish(
                    verified,
                    fresh_authority=fresh_authority,
                )

    assert fake.calls == []


def test_plan_expected_content_must_equal_verified_payload_before_put(
    tmp_path: Path,
) -> None:
    raw_artifact = _artifact()
    fake = FakeS3()

    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256="e" * 64,
    ) as verified:
        fresh_authority, _ = _fresh_dispatch(verified)
        with attested_test_client(fake, service="s3") as client:
            with pytest.raises(AssetPublicationError, match="planned content"):
                S3AssetPublisher(client).publish(
                    verified,
                    fresh_authority=fresh_authority,
                )

    assert fake.calls == []


@pytest.mark.parametrize(
    "client",
    [
        FakeS3(account="999999999999"),
        FakeS3(region="us-east-1"),
        FakeS3(service="cloudformation"),
        FakeS3(retries={"mode": "standard", "total_max_attempts": 2}),
    ],
)
def test_publisher_rejects_wrong_service_region_or_retrying_client_before_call(
    tmp_path: Path,
    client: FakeS3,
) -> None:
    raw_artifact = _artifact()
    content_sha256 = hashlib.sha256(PAYLOAD).hexdigest()

    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=content_sha256,
    ) as verified:
        fresh_authority, _ = _fresh_dispatch(verified)
        with pytest.raises(AssetPublicationError, match="client"):
            S3AssetPublisher(client).publish(
                verified,
                fresh_authority=fresh_authority,
            )

    assert client.calls == []


@pytest.mark.parametrize(
    "fake",
    [
        FakeS3(error=RuntimeError("unknown effect")),
        FakeS3(response=[]),
        FakeS3(response={"ChecksumSHA256": "wrong"}),
    ],
)
def test_provider_failure_or_incomplete_ack_is_ambiguous(
    tmp_path: Path,
    fake: FakeS3,
) -> None:
    raw_artifact = _artifact()
    content_sha256 = hashlib.sha256(PAYLOAD).hexdigest()

    with _verified_asset(
        tmp_path,
        raw_artifact,
        content_sha256=content_sha256,
    ) as verified:
        fresh_authority, _ = _fresh_dispatch(verified)
        with attested_test_client(fake, service="s3") as client:
            with pytest.raises(AssetPublicationAmbiguous, match="reconciliation"):
                S3AssetPublisher(client).publish(
                    verified,
                    fresh_authority=fresh_authority,
                )

    assert len(fake.calls) == 1


def test_module_has_no_sdk_credentials_observer_or_path_reopen_authority() -> None:
    source = (Path(__file__).parent / "asset_publication_v2.py").read_text(
        encoding="utf-8"
    )
    assert "boto3" not in source
    assert "botocore" not in source
    assert "subprocess" not in source
    assert "journal" not in source.casefold()
    assert "open(" not in source
    assert "head_object" not in source
    assert "get_object" not in source
