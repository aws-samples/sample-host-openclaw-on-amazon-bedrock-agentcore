from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from release_tools.ecr import (
    EcrEvidenceAdapter,
    EcrEvidenceAmbiguous,
    EcrEvidenceError,
    EcrEvidenceIncomplete,
    EcrImageAbsent,
    EcrImageScanFailed,
    EcrImageSigningFailed,
    EcrRepositoryAbsent,
    PROVENANCE_ARTIFACT_TYPE,
    SBOM_ARTIFACT_TYPE,
)
from release_tools.image_publication import (
    BRIDGE_BUILD_TYPE as BRIDGE_BUILD_TYPE_V2,
    CAPABILITY_TOOL_NAMES,
    BuilderDependency,
    ImagePublicationPlanV1,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPES,
    OCI_MANIFEST_MEDIA_TYPE,
    OciDescriptor,
    ProbeEvidenceDescriptor,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
BUILD_CONTEXT = "bridge"
BUILDER_ID = "https://personal-operator.invalid/builders/bridge-v1"
BUILDER_INPUT = "sha256:" + "f" * 64
RUNTIME_BUILD_CLOSURE = "sha256:" + "8" * 64
PROFILE = (
    f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
    "personal_operator_bridge"
)


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


SBOM_BLOB = _json(
    {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": "2026-07-18T00:00:00Z",
            "creators": ["Tool: personal-operator-release-builder-1.0"],
        },
        "dataLicense": "CC0-1.0",
        "documentNamespace": (
            "https://personal-operator.invalid/spdx/"
            + DIGEST.removeprefix("sha256:")
        ),
        "files": [
            {
                "SPDXID": "SPDXRef-File-agentcore-contract",
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": "d" * 64}
                ],
                "copyrightText": "NOASSERTION",
                "fileName": "/opt/app/bridge/agentcore-contract.js",
                "licenseConcluded": "NOASSERTION",
            }
        ],
        "name": "personal-operator-bridge",
        "packages": [
            {
                "SPDXID": "SPDXRef-Package-runtime-image",
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": DIGEST.removeprefix("sha256:"),
                    }
                ],
                "copyrightText": "NOASSERTION",
                "downloadLocation": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceLocator": (
                            "pkg:oci/personal-operator/bridge@" + DIGEST
                        ),
                        "referenceType": "purl",
                    }
                ],
                "filesAnalyzed": True,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "name": "personal-operator/bridge",
                "packageVerificationCode": {
                    "packageVerificationCodeValue": "e" * 40
                },
            }
        ],
        "relationships": [
            {
                "relatedSpdxElement": "SPDXRef-Package-runtime-image",
                "relationshipType": "DESCRIBES",
                "spdxElementId": "SPDXRef-DOCUMENT",
            },
            {
                "relatedSpdxElement": "SPDXRef-File-agentcore-contract",
                "relationshipType": "CONTAINS",
                "spdxElementId": "SPDXRef-Package-runtime-image",
            },
        ],
        "spdxVersion": "SPDX-2.3",
    }
)
PROVENANCE_BLOB = _json(
    {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "digest": {"sha256": DIGEST.removeprefix("sha256:")},
                "name": "personal-operator/bridge",
            }
        ],
        "predicate": {
            "buildDefinition": {
                "buildType": "https://personal-operator.invalid/build/bridge-v1",
                "externalParameters": {
                    "buildContext": BUILD_CONTEXT,
                    "sourceCommit": COMMIT,
                    "sourceTree": TREE,
                },
                "resolvedDependencies": [
                    {
                        "digest": {
                            "sha256": BUILDER_INPUT.removeprefix("sha256:")
                        },
                        "uri": "pkg:docker/node@24.15.0-slim",
                    }
                ],
            },
            "runDetails": {"builder": {"id": BUILDER_ID}},
        },
    }
)


def _artifact_manifest(artifact_type: str, blob: bytes) -> bytes:
    return _json(
        {
            "artifactType": artifact_type,
            "layers": [
                {
                    "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
                    "mediaType": artifact_type,
                    "size": len(blob),
                }
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
            "subject": {
                "digest": DIGEST,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 123456,
            },
        }
    )


SBOM_MANIFEST = _artifact_manifest(SBOM_ARTIFACT_TYPE, SBOM_BLOB)
PROVENANCE_MANIFEST = _artifact_manifest(
    PROVENANCE_ARTIFACT_TYPE,
    PROVENANCE_BLOB,
)
SBOM_DIGEST = "sha256:" + hashlib.sha256(SBOM_MANIFEST).hexdigest()
PROVENANCE_DIGEST = "sha256:" + hashlib.sha256(PROVENANCE_MANIFEST).hexdigest()


def _bound_provenance_blob() -> bytes:
    value = json.loads(PROVENANCE_BLOB)
    definition = value["predicate"]["buildDefinition"]
    definition["buildType"] = BRIDGE_BUILD_TYPE_V2
    definition["externalParameters"].update(
        {
            "gitArchiveSha256": "3" * 64,
            "buildArchiveSha256": "4" * 64,
            "catalogSourceSha256": "5" * 64,
            "capabilityCatalogDigest": "9" * 64,
            "runtimeBuildClosureSha256": (
                RUNTIME_BUILD_CLOSURE.removeprefix("sha256:")
            ),
            "platform": "linux/arm64",
        }
    )
    definition["resolvedDependencies"].append(
        {
            "digest": {
                "sha256": RUNTIME_BUILD_CLOSURE.removeprefix("sha256:")
            },
            "uri": "urn:personal-operator:runtime-build-closure",
        }
    )
    definition["internalParameters"] = {}
    value["predicate"]["runDetails"]["metadata"] = {
        "invocationId": "urn:sha256:" + DIGEST.removeprefix("sha256:")
    }
    return _json(value)


def _publication_plan(
    *,
    provenance_blob: bytes,
    provenance_manifest: bytes,
) -> ImagePublicationPlanV1:
    layer_media_type = next(iter(sorted(OCI_LAYER_MEDIA_TYPES)))
    return ImagePublicationPlanV1(
        COMMIT,
        TREE,
        ACCOUNT,
        REGION,
        "3" * 64,
        "4" * 64,
        10240,
        "5" * 64,
        "9" * 64,
        CAPABILITY_TOOL_NAMES,
        "2026-07-20T00:00:00Z",
        BUILDER_ID,
        (
            BuilderDependency("pkg:docker/node@24.15.0-slim", BUILDER_INPUT),
            BuilderDependency(
                "urn:personal-operator:runtime-build-closure",
                RUNTIME_BUILD_CLOSURE,
            ),
        ),
        OciDescriptor(OCI_MANIFEST_MEDIA_TYPE, DIGEST, 123456),
        OciDescriptor(OCI_CONFIG_MEDIA_TYPE, "sha256:" + "6" * 64, 123),
        (OciDescriptor(layer_media_type, "sha256:" + "7" * 64, 456),),
        OciDescriptor(
            SBOM_ARTIFACT_TYPE,
            "sha256:" + hashlib.sha256(SBOM_BLOB).hexdigest(),
            len(SBOM_BLOB),
        ),
        OciDescriptor(
            OCI_MANIFEST_MEDIA_TYPE,
            SBOM_DIGEST,
            len(SBOM_MANIFEST),
        ),
        OciDescriptor(
            PROVENANCE_ARTIFACT_TYPE,
            "sha256:" + hashlib.sha256(provenance_blob).hexdigest(),
            len(provenance_blob),
        ),
        OciDescriptor(
            OCI_MANIFEST_MEDIA_TYPE,
            "sha256:" + hashlib.sha256(provenance_manifest).hexdigest(),
            len(provenance_manifest),
        ),
        (
            ProbeEvidenceDescriptor("fresh-1", "a" * 64, 1),
            ProbeEvidenceDescriptor("fresh-2", "a" * 64, 1),
        ),
    )


def _responses() -> dict[str, object]:
    detail = {
        "registryId": ACCOUNT,
        "repositoryName": "personal-operator/bridge",
        "imageDigest": DIGEST,
        "imageTags": [f"commit-{COMMIT}"],
        "imageSizeInBytes": 123456,
    }
    return {
        "describe_repositories": {
            "repositories": [
                {
                    "registryId": ACCOUNT,
                    "repositoryName": "personal-operator/bridge",
                    "imageTagMutability": "IMMUTABLE",
                    "imageScanningConfiguration": {"scanOnPush": True},
                }
            ]
        },
        "describe_images_by_tag": {"imageDetails": [deepcopy(detail)]},
        "describe_images_by_digest": {"imageDetails": [deepcopy(detail)]},
        "describe_image_scan_findings": {
            "registryId": ACCOUNT,
            "repositoryName": "personal-operator/bridge",
            "imageId": {"imageDigest": DIGEST},
            "imageScanStatus": {"status": "COMPLETE"},
            "imageScanFindings": {
                "findingSeverityCounts": {"CRITICAL": 0, "HIGH": 0}
            },
        },
        "list_image_referrers": {
            "referrers": [
                {
                    "digest": SBOM_DIGEST,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "size": len(SBOM_MANIFEST),
                    "artifactType": SBOM_ARTIFACT_TYPE,
                    "artifactStatus": "ACTIVE",
                },
                {
                    "digest": PROVENANCE_DIGEST,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "size": len(PROVENANCE_MANIFEST),
                    "artifactType": PROVENANCE_ARTIFACT_TYPE,
                    "artifactStatus": "ACTIVE",
                },
            ]
        },
        "batch_get_image": {
            SBOM_DIGEST: {
                "failures": [],
                "images": [
                    {
                        "imageId": {"imageDigest": SBOM_DIGEST},
                        "imageManifest": SBOM_MANIFEST.decode("utf-8"),
                        "imageManifestMediaType": (
                            "application/vnd.oci.image.manifest.v1+json"
                        ),
                        "registryId": ACCOUNT,
                        "repositoryName": "personal-operator/bridge",
                    }
                ],
            },
            PROVENANCE_DIGEST: {
                "failures": [],
                "images": [
                    {
                        "imageId": {"imageDigest": PROVENANCE_DIGEST},
                        "imageManifest": PROVENANCE_MANIFEST.decode("utf-8"),
                        "imageManifestMediaType": (
                            "application/vnd.oci.image.manifest.v1+json"
                        ),
                        "registryId": ACCOUNT,
                        "repositoryName": "personal-operator/bridge",
                    }
                ],
            },
        },
        "get_download_url_for_layer": {
            "sha256:" + hashlib.sha256(SBOM_BLOB).hexdigest(): {
                "downloadUrl": "memory://sbom",
                "layerDigest": "sha256:" + hashlib.sha256(SBOM_BLOB).hexdigest(),
            },
            "sha256:" + hashlib.sha256(PROVENANCE_BLOB).hexdigest(): {
                "downloadUrl": "memory://provenance",
                "layerDigest": (
                    "sha256:" + hashlib.sha256(PROVENANCE_BLOB).hexdigest()
                ),
            },
        },
        "describe_image_signing_status": {
            "registryId": ACCOUNT,
            "repositoryName": "personal-operator/bridge",
            "imageId": {"imageDigest": DIGEST},
            "signingStatuses": [
                {"signingProfileArn": PROFILE, "status": "COMPLETE"}
            ],
        },
        "get_signing_configuration": {
            "registryId": ACCOUNT,
            "signingConfiguration": {
                "rules": [
                    {
                        "signingProfileArn": PROFILE,
                        "repositoryFilters": [
                            {
                                "filter": "personal-operator/bridge",
                                "filterType": "WILDCARD_MATCH",
                            }
                        ],
                    }
                ]
            },
        },
    }


class FakeEcr:
    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or _responses()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _return(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        return deepcopy(self.responses[name])

    def describe_repositories(self, **kwargs):
        return self._return("describe_repositories", kwargs)

    def describe_images(self, **kwargs):
        image_id = kwargs["imageIds"][0]
        suffix = "by_tag" if "imageTag" in image_id else "by_digest"
        return self._return(f"describe_images_{suffix}", kwargs)

    def describe_image_scan_findings(self, **kwargs):
        return self._return("describe_image_scan_findings", kwargs)

    def list_image_referrers(self, **kwargs):
        return self._return("list_image_referrers", kwargs)

    def batch_get_image(self, **kwargs):
        digest = kwargs["imageIds"][0]["imageDigest"]
        self.calls.append(("batch_get_image", kwargs))
        return deepcopy(self.responses["batch_get_image"][digest])

    def get_download_url_for_layer(self, **kwargs):
        digest = kwargs["layerDigest"]
        self.calls.append(("get_download_url_for_layer", kwargs))
        return deepcopy(self.responses["get_download_url_for_layer"][digest])

    def describe_image_signing_status(self, **kwargs):
        return self._return("describe_image_signing_status", kwargs)

    def get_signing_configuration(self, **kwargs):
        return self._return("get_signing_configuration", kwargs)


class FakeBlobReader:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.blobs = blobs or {
            "memory://sbom": SBOM_BLOB,
            "memory://provenance": PROVENANCE_BLOB,
        }

    def read(self, url: str, *, maximum_bytes: int) -> bytes:
        self.calls.append((url, maximum_bytes))
        return self.blobs[url]


def _collect(fake: FakeEcr, blob_reader: FakeBlobReader | None = None):
    return EcrEvidenceAdapter(
        fake,
        blob_reader=blob_reader or FakeBlobReader(),
    ).collect(
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        build_context=BUILD_CONTEXT,
        builder_id=BUILDER_ID,
        builder_inputs=(BUILDER_INPUT,),
    )


def test_missing_exact_image_is_authoritative_absence() -> None:
    class ImageNotFound(Exception):
        response = {"Error": {"Code": "ImageNotFoundException"}}

    class MissingImageEcr(FakeEcr):
        def describe_images(self, **kwargs):
            raise ImageNotFound("missing")

    with pytest.raises(EcrImageAbsent, match="absent"):
        _collect(MissingImageEcr())


def test_missing_retained_repository_is_not_image_absence() -> None:
    class RepositoryNotFound(Exception):
        response = {"Error": {"Code": "RepositoryNotFoundException"}}

    class MissingRepositoryEcr(FakeEcr):
        def describe_repositories(self, **kwargs):
            raise RepositoryNotFound("missing")

    with pytest.raises(EcrRepositoryAbsent, match="absent"):
        _collect(MissingRepositoryEcr())


def _replace_provenance(
    responses: dict[str, object],
    blob: bytes,
) -> FakeBlobReader:
    manifest = _artifact_manifest(PROVENANCE_ARTIFACT_TYPE, blob)
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    layer_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    referrers = responses["list_image_referrers"]["referrers"]  # type: ignore[index]
    item = next(
        value
        for value in referrers
        if value["artifactType"] == PROVENANCE_ARTIFACT_TYPE
    )
    item["digest"] = manifest_digest
    item["size"] = len(manifest)
    responses["batch_get_image"] = {  # type: ignore[index]
        **responses["batch_get_image"],  # type: ignore[index]
        manifest_digest: {
            "failures": [],
            "images": [
                {
                    "imageId": {"imageDigest": manifest_digest},
                    "imageManifest": manifest.decode("utf-8"),
                    "imageManifestMediaType": (
                        "application/vnd.oci.image.manifest.v1+json"
                    ),
                    "registryId": ACCOUNT,
                    "repositoryName": "personal-operator/bridge",
                }
            ],
        },
    }
    responses["get_download_url_for_layer"] = {  # type: ignore[index]
        **responses["get_download_url_for_layer"],  # type: ignore[index]
        layer_digest: {
            "downloadUrl": "memory://provenance",
            "layerDigest": layer_digest,
        },
    }
    return FakeBlobReader(
        {"memory://sbom": SBOM_BLOB, "memory://provenance": blob}
    )


def _replace_sbom(
    responses: dict[str, object],
    blob: bytes,
) -> FakeBlobReader:
    manifest = _artifact_manifest(SBOM_ARTIFACT_TYPE, blob)
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    layer_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    referrers = responses["list_image_referrers"]["referrers"]  # type: ignore[index]
    item = next(
        value
        for value in referrers
        if value["artifactType"] == SBOM_ARTIFACT_TYPE
    )
    item["digest"] = manifest_digest
    item["size"] = len(manifest)
    responses["batch_get_image"] = {  # type: ignore[index]
        **responses["batch_get_image"],  # type: ignore[index]
        manifest_digest: {
            "failures": [],
            "images": [
                {
                    "imageId": {"imageDigest": manifest_digest},
                    "imageManifest": manifest.decode("utf-8"),
                    "imageManifestMediaType": (
                        "application/vnd.oci.image.manifest.v1+json"
                    ),
                    "registryId": ACCOUNT,
                    "repositoryName": "personal-operator/bridge",
                }
            ],
        },
    }
    responses["get_download_url_for_layer"] = {  # type: ignore[index]
        **responses["get_download_url_for_layer"],  # type: ignore[index]
        layer_digest: {
            "downloadUrl": "memory://sbom",
            "layerDigest": layer_digest,
        },
    }
    return FakeBlobReader(
        {"memory://sbom": blob, "memory://provenance": PROVENANCE_BLOB}
    )


def test_adapter_collects_exact_immutable_image_evidence_through_fake() -> None:
    fake = FakeEcr()

    evidence = _collect(fake)

    assert evidence.source_commit == COMMIT
    assert evidence.source_tree == TREE
    assert evidence.image_digest == DIGEST
    assert evidence.image_uri == (
        f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
        f"personal-operator/bridge@{DIGEST}"
    )
    assert evidence.sbom_sha256 == SBOM_DIGEST.removeprefix("sha256:")
    assert evidence.provenance_sha256 == PROVENANCE_DIGEST.removeprefix("sha256:")
    assert evidence.signature_status == "SIGNED"
    assert [name for name, _ in fake.calls] == [
        "describe_repositories",
        "describe_images_by_tag",
        "describe_images_by_digest",
        "describe_image_scan_findings",
        "list_image_referrers",
        "batch_get_image",
        "get_download_url_for_layer",
        "batch_get_image",
        "get_download_url_for_layer",
        "get_signing_configuration",
        "describe_image_signing_status",
    ]


def test_mutable_repository_fails_before_image_or_evidence_calls() -> None:
    responses = _responses()
    responses["describe_repositories"]["repositories"][0][  # type: ignore[index]
        "imageTagMutability"
    ] = "MUTABLE"
    fake = FakeEcr(responses)

    with pytest.raises(EcrEvidenceError, match="immutable"):
        _collect(fake)

    assert [name for name, _ in fake.calls] == ["describe_repositories"]


@pytest.mark.parametrize(
    "lookup",
    ["describe_images_by_tag", "describe_images_by_digest"],
)
def test_duplicate_image_resolution_is_explicitly_ambiguous(lookup: str) -> None:
    responses = _responses()
    responses[lookup]["imageDetails"].append(  # type: ignore[index,union-attr]
        deepcopy(responses[lookup]["imageDetails"][0])  # type: ignore[index]
    )

    with pytest.raises(EcrEvidenceAmbiguous, match="one exact image"):
        _collect(FakeEcr(responses))


def test_tag_and_digest_lookups_must_resolve_the_same_subject() -> None:
    responses = _responses()
    responses["describe_images_by_digest"]["imageDetails"][0][  # type: ignore[index]
        "imageDigest"
    ] = "sha256:" + "f" * 64

    with pytest.raises(EcrEvidenceError, match="digest"):
        _collect(FakeEcr(responses))


@pytest.mark.parametrize("status", ["IN_PROGRESS", "ACTIVE", "PENDING"])
def test_scan_not_complete_is_incomplete_not_success(status: str) -> None:
    responses = _responses()
    responses["describe_image_scan_findings"]["imageScanStatus"][  # type: ignore[index]
        "status"
    ] = status

    with pytest.raises(EcrEvidenceIncomplete, match="scan"):
        _collect(FakeEcr(responses))


def test_unreviewed_high_or_critical_findings_fail_closed() -> None:
    responses = _responses()
    responses["describe_image_scan_findings"]["imageScanFindings"][  # type: ignore[index]
        "findingSeverityCounts"
    ] = {"HIGH": 1, "CRITICAL": 0}

    with pytest.raises(EcrImageScanFailed, match="findings") as captured:
        _collect(FakeEcr(responses))

    assert captured.value.failure_reason == "IMAGE_SCAN_FAILED"
    assert captured.value.provider_reason == "SCAN_POLICY_FAILED"


@pytest.mark.parametrize(
    "status",
    [
        "FAILED",
        "UNSUPPORTED_IMAGE",
        "SCAN_ELIGIBILITY_EXPIRED",
        "FINDINGS_UNAVAILABLE",
        "LIMIT_EXCEEDED",
        "IMAGE_ARCHIVED",
    ],
)
def test_terminal_scan_status_has_a_closed_driver_mapping(status: str) -> None:
    responses = _responses()
    responses["describe_image_scan_findings"]["imageScanStatus"][  # type: ignore[index]
        "status"
    ] = status

    with pytest.raises(EcrImageScanFailed, match=status) as captured:
        _collect(FakeEcr(responses))

    assert captured.value.failure_reason == "IMAGE_SCAN_FAILED"
    assert captured.value.provider_reason == "SCAN_POLICY_FAILED"


@pytest.mark.parametrize(
    "artifact_type",
    [SBOM_ARTIFACT_TYPE, PROVENANCE_ARTIFACT_TYPE],
)
def test_missing_or_duplicate_attestation_is_ambiguous(artifact_type: str) -> None:
    responses = _responses()
    referrers = responses["list_image_referrers"]["referrers"]  # type: ignore[index]
    match = next(item for item in referrers if item["artifactType"] == artifact_type)
    referrers.append(deepcopy(match))

    with pytest.raises(EcrEvidenceAmbiguous, match="attestation"):
        _collect(FakeEcr(responses))


def test_attestation_manifest_must_hash_and_name_the_exact_image_subject() -> None:
    responses = _responses()
    entry = responses["batch_get_image"][SBOM_DIGEST]["images"][0]  # type: ignore[index]
    manifest = json.loads(entry["imageManifest"])
    manifest["subject"]["digest"] = "sha256:" + "0" * 64
    entry["imageManifest"] = _json(manifest).decode("utf-8")

    with pytest.raises(EcrEvidenceError, match="manifest digest"):
        _collect(FakeEcr(responses))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.clear()
            or value.update(
                {
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "dataLicense": "CC0-1.0",
                    "name": "empty-runtime-image",
                    "spdxVersion": "SPDX-2.3",
                }
            ),
            "SPDX",
        ),
        (lambda value: value.update(files=[]), "inventory"),
        (
            lambda value: value["packages"][0]["checksums"][0].update(
                checksumValue="0" * 64
            ),
            "subject",
        ),
        (lambda value: value.update(relationships=[]), "relationship"),
    ],
)
def test_sbom_requires_subject_bound_inventory_and_relationship_coverage(
    mutate,
    message: str,
) -> None:
    sbom = json.loads(SBOM_BLOB)
    mutate(sbom)
    blob = _json(sbom)
    responses = _responses()
    reader = _replace_sbom(responses, blob)

    with pytest.raises(EcrEvidenceError, match=message):
        _collect(FakeEcr(responses), reader)


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (
            ("predicate", "buildDefinition", "externalParameters", "sourceCommit"),
            "0" * 40,
            "commit",
        ),
        (
            ("predicate", "buildDefinition", "externalParameters", "sourceTree"),
            "0" * 40,
            "tree",
        ),
        (
            ("predicate", "buildDefinition", "externalParameters", "buildContext"),
            "other",
            "context",
        ),
        (("predicate", "runDetails", "builder", "id"), "unreviewed", "builder"),
    ],
)
def test_provenance_content_must_bind_exact_release_and_builder(
    path: tuple[str, ...],
    replacement: str,
    message: str,
) -> None:
    provenance = json.loads(PROVENANCE_BLOB)
    cursor = provenance
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement
    blob = _json(provenance)
    responses = _responses()
    reader = _replace_provenance(responses, blob)

    with pytest.raises(EcrEvidenceError, match=message):
        _collect(FakeEcr(responses), reader)


@pytest.mark.parametrize("status", ["IN_PROGRESS", "FAILED", "SURPRISE"])
def test_signing_status_never_defaults_to_signed(status: str) -> None:
    responses = _responses()
    responses["describe_image_signing_status"]["signingStatuses"][0][  # type: ignore[index]
        "status"
    ] = status

    error = (
        EcrEvidenceIncomplete
        if status == "IN_PROGRESS"
        else EcrImageSigningFailed
        if status == "FAILED"
        else EcrEvidenceError
    )
    with pytest.raises(error, match="sign"):
        _collect(FakeEcr(responses))


def test_terminal_signing_status_has_a_closed_driver_mapping() -> None:
    responses = _responses()
    responses["describe_image_signing_status"]["signingStatuses"][0][  # type: ignore[index]
        "status"
    ] = "FAILED"

    with pytest.raises(EcrImageSigningFailed, match="FAILED") as captured:
        _collect(FakeEcr(responses))

    assert captured.value.failure_reason == "IMAGE_SIGNING_FAILED"
    assert captured.value.provider_reason == "SIGNATURE_VERIFICATION_FAILED"


def test_multiple_matching_signing_rules_are_ambiguous() -> None:
    responses = _responses()
    rules = responses["get_signing_configuration"]["signingConfiguration"][  # type: ignore[index]
        "rules"
    ]
    rules.append(deepcopy(rules[0]))

    with pytest.raises(EcrEvidenceAmbiguous, match="signing rule"):
        _collect(FakeEcr(responses))


def test_adapter_construction_performs_no_ambient_client_or_blob_access() -> None:
    fake = FakeEcr()
    reader = FakeBlobReader()

    EcrEvidenceAdapter(fake, blob_reader=reader)

    assert fake.calls == []
    assert reader.calls == []


def test_bound_observer_reconciles_the_exact_precomputed_publication_plan() -> None:
    responses = _responses()
    provenance_blob = _bound_provenance_blob()
    reader = _replace_provenance(responses, provenance_blob)
    provenance_manifest = _artifact_manifest(
        PROVENANCE_ARTIFACT_TYPE,
        provenance_blob,
    )
    plan = _publication_plan(
        provenance_blob=provenance_blob,
        provenance_manifest=provenance_manifest,
    )
    fake = FakeEcr(responses)

    evidence = EcrEvidenceAdapter(fake, blob_reader=reader).collect_bound(
        publication_plan=plan.to_bytes(),
        expected_publication_plan_sha256=plan.publication_plan_sha256,
    )

    assert evidence.image_digest == plan.subject_manifest_digest
    assert evidence.sbom_sha256 == plan.sbom_manifest_digest.removeprefix("sha256:")
    assert evidence.provenance_sha256 == (
        plan.provenance_manifest_digest.removeprefix("sha256:")
    )


def test_bound_observer_rejects_provenance_for_a_different_runtime_closure() -> None:
    responses = _responses()
    provenance = json.loads(_bound_provenance_blob())
    provenance["predicate"]["buildDefinition"]["externalParameters"][
        "runtimeBuildClosureSha256"
    ] = "0" * 64
    provenance_blob = _json(provenance)
    reader = _replace_provenance(responses, provenance_blob)
    provenance_manifest = _artifact_manifest(
        PROVENANCE_ARTIFACT_TYPE,
        provenance_blob,
    )
    plan = _publication_plan(
        provenance_blob=provenance_blob,
        provenance_manifest=provenance_manifest,
    )

    with pytest.raises(EcrEvidenceError, match="publication-plan inputs"):
        EcrEvidenceAdapter(
            FakeEcr(responses), blob_reader=reader
        ).collect_bound(
            publication_plan=plan.to_bytes(),
            expected_publication_plan_sha256=plan.publication_plan_sha256,
        )


def test_bound_observer_rejects_plan_substitution_before_live_calls() -> None:
    plan = _publication_plan(
        provenance_blob=_bound_provenance_blob(),
        provenance_manifest=_artifact_manifest(
            PROVENANCE_ARTIFACT_TYPE,
            _bound_provenance_blob(),
        ),
    )
    fake = FakeEcr()

    with pytest.raises(EcrEvidenceError, match="publication plan"):
        EcrEvidenceAdapter(fake, blob_reader=FakeBlobReader()).collect_bound(
            publication_plan=plan.to_bytes(),
            expected_publication_plan_sha256="f" * 64,
        )

    assert fake.calls == []


@pytest.mark.parametrize("subject", ["image", "SBOM"])
def test_bound_observer_rejects_subject_or_referrer_substitution(subject: str) -> None:
    responses = _responses()
    provenance_blob = _bound_provenance_blob()
    reader = _replace_provenance(responses, provenance_blob)
    provenance_manifest = _artifact_manifest(
        PROVENANCE_ARTIFACT_TYPE,
        provenance_blob,
    )
    plan = _publication_plan(
        provenance_blob=provenance_blob,
        provenance_manifest=provenance_manifest,
    )
    if subject == "image":
        plan = replace(
            plan,
            subject=OciDescriptor(
                OCI_MANIFEST_MEDIA_TYPE,
                "sha256:" + "f" * 64,
                plan.subject.size,
            ),
        )
    else:
        plan = replace(
            plan,
            sbom_manifest=OciDescriptor(
                OCI_MANIFEST_MEDIA_TYPE,
                "sha256:" + "f" * 64,
                plan.sbom_manifest.size,
            ),
        )

    with pytest.raises(EcrEvidenceError, match="planned"):
        EcrEvidenceAdapter(FakeEcr(responses), blob_reader=reader).collect_bound(
            publication_plan=plan.to_bytes(),
            expected_publication_plan_sha256=plan.publication_plan_sha256,
        )
