"""Closed, retained-descriptor publication of exact CDK file assets.

The immutable request artifact is a small canonical header followed by the raw
asset bytes.  The provider dispatcher accepts only a
``VerifiedPrivateMutationV2`` capability, so neither the header nor the payload
is reopened by path after transaction validation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import re
import struct
from typing import Any, Iterator, Mapping, Protocol

from release_tools.aws_authority_v2 import (
    AttestedAwsClientV2,
    AwsAuthorityError,
)
from release_tools.contracts import (
    ContractError,
    VerifiedPrivateMutationV2,
    canonical_json_bytes,
    parse_canonical_object,
)


REQUIRED_REGION = "eu-west-1"
ASSET_ARTIFACT_MAGIC = b"PO-CDK-ASSET-V2\x00"
ASSET_ARTIFACT_HEADER_BYTES = 4
MAX_ASSET_HEADER_BYTES = 64 * 1024
MAX_ASSET_BYTES = 300 * 1024 * 1024
_FIELDS = {
    "schema",
    "account",
    "region",
    "sourceCommit",
    "sourceTree",
    "bucketName",
    "assetId",
    "objectKey",
    "contentType",
    "contentSha256",
    "contentSize",
}
_ACCOUNT = re.compile(r"[0-9]{12}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_OBJECT_KEY = re.compile(r"[0-9a-f]{64}\.(?:json|zip)")


class AssetPublicationError(RuntimeError):
    """An asset publication request is not exact and canonical."""


class AssetPublicationAmbiguous(AssetPublicationError):
    """An asset write may have persisted and needs independent observation."""


class S3MutationClient(Protocol):
    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AssetPublicationError(f"{label} is invalid")
    return value


def _validate_sdk_client(
    client: object,
    *,
    region: str,
    account: str,
) -> AttestedAwsClientV2:
    if not isinstance(client, AttestedAwsClientV2):
        raise AssetPublicationError(
            "S3 mutation requires an attested AWS client"
        )
    try:
        client.require_scope(
            service="s3",
            account=account,
            region=region,
            capability="mutation",
        )
    except AwsAuthorityError as error:
        raise AssetPublicationError(
            "S3 attested client crosses its exact subject"
        ) from error
    return client


@dataclass(frozen=True, slots=True)
class AssetPublicationV2:
    """Canonical metadata for one header-plus-raw-payload asset artifact."""

    SCHEMA = "personal-operator.asset-publication.v2"

    account: str
    region: str
    source_commit: str
    source_tree: str
    bucket_name: str
    asset_id: str
    object_key: str
    content_type: str
    content_sha256: str
    content_size: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AssetPublicationV2":
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise AssetPublicationError("asset publication fields are not exact")
        if raw["schema"] != cls.SCHEMA:
            raise AssetPublicationError("asset publication schema is invalid")
        account = _text(raw["account"], label="asset account")
        if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
            raise AssetPublicationError("asset account is invalid")
        region = _text(raw["region"], label="asset region")
        if region != REQUIRED_REGION:
            raise AssetPublicationError(
                f"asset region must be exactly {REQUIRED_REGION}"
            )
        commit = _text(raw["sourceCommit"], label="asset source commit")
        tree = _text(raw["sourceTree"], label="asset source tree")
        if _SHA40.fullmatch(commit) is None or _SHA40.fullmatch(tree) is None:
            raise AssetPublicationError("asset source identity is invalid")
        bucket = _text(raw["bucketName"], label="asset bucket")
        if bucket != f"cdk-hnb659fds-assets-{account}-{region}":
            raise AssetPublicationError("asset bucket crosses its exact subject")
        asset_id = _text(raw["assetId"], label="asset ID")
        if _SHA64.fullmatch(asset_id) is None:
            raise AssetPublicationError("asset ID is invalid")
        object_key = _text(raw["objectKey"], label="asset object key")
        if _OBJECT_KEY.fullmatch(object_key) is None:
            raise AssetPublicationError("asset object key is not canonical")
        if not object_key.startswith(asset_id + "."):
            raise AssetPublicationError("asset object key differs from its asset ID")
        content_type = _text(raw["contentType"], label="asset content type")
        expected_content_type = (
            "application/json"
            if object_key.endswith(".json")
            else "application/zip"
        )
        if content_type != expected_content_type:
            raise AssetPublicationError(
                "asset content type differs from the exact object"
            )
        digest = _text(raw["contentSha256"], label="asset content digest")
        if _SHA64.fullmatch(digest) is None:
            raise AssetPublicationError("asset content digest is invalid")
        content_size = raw["contentSize"]
        if (
            isinstance(content_size, bool)
            or not isinstance(content_size, int)
            or not 1 <= content_size <= MAX_ASSET_BYTES
        ):
            raise AssetPublicationError("asset content size is invalid")
        return cls(
            account,
            region,
            commit,
            tree,
            bucket,
            asset_id,
            object_key,
            content_type,
            digest,
            content_size,
        )

    @classmethod
    def from_header_bytes(cls, payload: bytes) -> "AssetPublicationV2":
        try:
            return cls.from_mapping(parse_canonical_object(payload))
        except AssetPublicationError:
            raise
        except Exception as error:
            raise AssetPublicationError(
                "asset publication header is not canonical"
            ) from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "account": self.account,
            "region": self.region,
            "sourceCommit": self.source_commit,
            "sourceTree": self.source_tree,
            "bucketName": self.bucket_name,
            "assetId": self.asset_id,
            "objectKey": self.object_key,
            "contentType": self.content_type,
            "contentSha256": self.content_sha256,
            "contentSize": self.content_size,
        }

    def to_header_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())

    @classmethod
    def build_artifact_bytes(
        cls,
        *,
        account: str,
        region: str,
        source_commit: str,
        source_tree: str,
        bucket_name: str,
        asset_id: str,
        object_key: str,
        content_type: str,
        payload: bytes,
    ) -> bytes:
        """Build fixture/small-asset bytes; release dispatch itself streams."""

        if not isinstance(payload, bytes):
            raise AssetPublicationError("asset payload type is invalid")
        metadata = cls.from_mapping(
            {
                "schema": cls.SCHEMA,
                "account": account,
                "region": region,
                "sourceCommit": source_commit,
                "sourceTree": source_tree,
                "bucketName": bucket_name,
                "assetId": asset_id,
                "objectKey": object_key,
                "contentType": content_type,
                "contentSha256": hashlib.sha256(payload).hexdigest(),
                "contentSize": len(payload),
            }
        )
        header = metadata.to_header_bytes()
        if len(header) > MAX_ASSET_HEADER_BYTES:
            raise AssetPublicationError("asset publication header exceeds the limit")
        return (
            ASSET_ARTIFACT_MAGIC
            + struct.pack(">I", len(header))
            + header
            + payload
        )


class _ChunkReader:
    """Small exact reader over the verified capability's chunk iterator."""

    def __init__(self, iterator: Iterator[bytes]) -> None:
        self._iterator = iterator
        self._buffer = b""
        self.consumed = 0

    def read_exact(self, size: int, *, label: str) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            if not self._buffer:
                try:
                    self._buffer = next(self._iterator)
                except StopIteration as error:
                    raise AssetPublicationError(
                        f"asset publication {label} is truncated"
                    ) from error
            take = min(remaining, len(self._buffer))
            chunks.append(self._buffer[:take])
            self._buffer = self._buffer[take:]
            remaining -= take
            self.consumed += take
        return b"".join(chunks)

    def iter_remainder(self) -> Iterator[bytes]:
        if self._buffer:
            buffered = self._buffer
            self._buffer = b""
            self.consumed += len(buffered)
            yield buffered
        for chunk in self._iterator:
            self.consumed += len(chunk)
            yield chunk


def _parse_verified_asset(
    verified: VerifiedPrivateMutationV2,
) -> tuple[AssetPublicationV2, int]:
    try:
        resolved = verified.resolved_request
    except ContractError as error:
        raise AssetPublicationError(
            "asset publication capability is closed or invalid"
        ) from error
    request = resolved.mutation_request
    if resolved.step_phase != "foundation" or request.kind != "ASSET_PUBLISH":
        raise AssetPublicationError("asset publication step binding is invalid")
    iterator = verified.iter_artifact_chunks()
    reader = _ChunkReader(iterator)
    try:
        magic = reader.read_exact(len(ASSET_ARTIFACT_MAGIC), label="magic")
        if magic != ASSET_ARTIFACT_MAGIC:
            raise AssetPublicationError("asset publication magic is invalid")
        header_size = struct.unpack(
            ">I",
            reader.read_exact(
                ASSET_ARTIFACT_HEADER_BYTES,
                label="header length",
            ),
        )[0]
        if not 1 <= header_size <= MAX_ASSET_HEADER_BYTES:
            raise AssetPublicationError(
                "asset publication header size is invalid"
            )
        header = reader.read_exact(header_size, label="header")
        metadata = AssetPublicationV2.from_header_bytes(header)
        payload_offset = (
            len(ASSET_ARTIFACT_MAGIC)
            + ASSET_ARTIFACT_HEADER_BYTES
            + header_size
        )
        if (
            metadata.account,
            metadata.region,
            metadata.source_commit,
            metadata.source_tree,
        ) != (
            resolved.account,
            resolved.region,
            resolved.source_commit,
            resolved.source_tree,
        ):
            raise AssetPublicationError(
                "asset publication crosses its resolved release identity"
            )
        expected_subject = f"cdk:asset:{metadata.asset_id}"
        if request.subject != expected_subject:
            raise AssetPublicationError(
                "asset publication content differs from the planned subject"
            )
        if metadata.content_sha256 != resolved.expected_content_sha256:
            raise AssetPublicationError(
                "asset publication payload differs from the planned content"
            )
        expected_artifact_size = payload_offset + metadata.content_size
        if expected_artifact_size != verified.metadata.request_artifact_size:
            raise AssetPublicationError("asset publication content size differs")
        digest = hashlib.sha256()
        size = 0
        for chunk in reader.iter_remainder():
            size += len(chunk)
            if size > metadata.content_size:
                raise AssetPublicationError(
                    "asset publication has trailing payload bytes"
                )
            digest.update(chunk)
        if size != metadata.content_size:
            raise AssetPublicationError("asset publication payload is truncated")
        if digest.hexdigest() != metadata.content_sha256:
            raise AssetPublicationError("asset publication content digest differs")
        return metadata, payload_offset
    except ContractError as error:
        raise AssetPublicationError(
            "asset publication verified stream is invalid"
        ) from error
    finally:
        iterator.close()


class _VerifiedAssetBody(io.RawIOBase):
    """Seekable payload-only view backed by the retained verified descriptor."""

    def __init__(
        self,
        verified: VerifiedPrivateMutationV2,
        *,
        payload_offset: int,
        payload_size: int,
    ) -> None:
        super().__init__()
        self._verified = verified
        self._payload_offset = payload_offset
        self._payload_size = payload_size
        self._position = 0
        self._iterator: Iterator[bytes] | None = None
        self._buffer = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def _close_iterator(self) -> None:
        if self._iterator is not None:
            self._iterator.close()
            self._iterator = None
        self._buffer = b""

    def _ensure_iterator(self) -> None:
        if self._iterator is not None or self._position >= self._payload_size:
            return
        self._iterator = self._verified.iter_artifact_chunks()
        skip = self._payload_offset + self._position
        while skip:
            try:
                chunk = next(self._iterator)
            except StopIteration as error:
                self._close_iterator()
                raise AssetPublicationError(
                    "verified asset body is truncated"
                ) from error
            if len(chunk) <= skip:
                skip -= len(chunk)
            else:
                self._buffer = chunk[skip:]
                skip = 0

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        remaining = self._payload_size - self._position
        if size is None or size < 0:
            size = remaining
        else:
            size = min(size, remaining)
        if size == 0:
            return b""
        self._ensure_iterator()
        chunks: list[bytes] = []
        needed = size
        while needed:
            if not self._buffer:
                if self._iterator is None:
                    raise AssetPublicationError("verified asset body is truncated")
                try:
                    self._buffer = next(self._iterator)
                except StopIteration as error:
                    self._close_iterator()
                    raise AssetPublicationError(
                        "verified asset body is truncated"
                    ) from error
            take = min(needed, len(self._buffer))
            chunks.append(self._buffer[:take])
            self._buffer = self._buffer[take:]
            self._position += take
            needed -= take
        if self._position == self._payload_size:
            self._close_iterator()
        return b"".join(chunks)

    def readinto(self, buffer: Any) -> int:
        payload = self.read(len(buffer))
        buffer[: len(payload)] = payload
        return len(payload)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._payload_size + offset
        else:
            raise ValueError("invalid whence")
        if not 0 <= position <= self._payload_size:
            raise ValueError("asset body seek crosses payload boundary")
        self._close_iterator()
        self._position = position
        return position

    def close(self) -> None:
        if not self.closed:
            self._close_iterator()
        super().close()


class S3AssetPublisher:
    """Write one absent exact object; never use its response as evidence."""

    def __init__(self, client: S3MutationClient) -> None:
        self._client = client

    def publish(self, verified: VerifiedPrivateMutationV2) -> dict[str, bool]:
        if not isinstance(verified, VerifiedPrivateMutationV2):
            raise AssetPublicationError(
                "asset publication requires a verified private mutation"
            )
        asset, payload_offset = _parse_verified_asset(verified)
        client = _validate_sdk_client(
            self._client,
            region=asset.region,
            account=asset.account,
        )
        checksum = base64.b64encode(
            bytes.fromhex(asset.content_sha256)
        ).decode("ascii")
        body = _VerifiedAssetBody(
            verified,
            payload_offset=payload_offset,
            payload_size=asset.content_size,
        )
        try:
            response = client.invoke(
                "put_object",
                Bucket=asset.bucket_name,
                Key=asset.object_key,
                Body=body,
                ContentLength=asset.content_size,
                ContentType=asset.content_type,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
                ServerSideEncryption="aws:kms",
                IfNoneMatch="*",
                ExpectedBucketOwner=asset.account,
                Metadata={
                    "content-sha256": asset.content_sha256,
                    "asset-id": asset.asset_id,
                    "source-commit": asset.source_commit,
                    "source-tree": asset.source_tree,
                },
            )
        except Exception as error:
            raise AssetPublicationAmbiguous(
                "asset publication has unknown effect; authoritative "
                "reconciliation is required"
            ) from error
        finally:
            body.close()
        if (
            not isinstance(response, Mapping)
            or response.get("ChecksumSHA256") != checksum
            or response.get("ServerSideEncryption") != "aws:kms"
        ):
            raise AssetPublicationAmbiguous(
                "asset acknowledgement is incomplete; authoritative "
                "reconciliation is required"
            )
        return {"dispatched": True}


__all__ = [
    "ASSET_ARTIFACT_HEADER_BYTES",
    "ASSET_ARTIFACT_MAGIC",
    "AssetPublicationAmbiguous",
    "AssetPublicationError",
    "AssetPublicationV2",
    "S3AssetPublisher",
]
