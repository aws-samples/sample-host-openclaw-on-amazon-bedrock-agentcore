"""Injected, credential-free collector for immutable ECR release evidence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol, Sequence

from release_tools.contracts import RuntimeImageEvidence


REPOSITORY_NAME = "personal-operator/bridge"
REQUIRED_REGION = "eu-west-1"
SIGNING_PROFILE_NAME = "personal_operator_bridge"
SBOM_ARTIFACT_TYPE = "application/spdx+json"
PROVENANCE_ARTIFACT_TYPE = "application/vnd.in-toto+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
PROVENANCE_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PROVENANCE_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BRIDGE_BUILD_TYPE = "https://personal-operator.invalid/build/bridge-v1"
MAX_ATTESTATION_MANIFEST_BYTES = 1024 * 1024
MAX_ATTESTATION_BLOB_BYTES = 16 * 1024 * 1024

_COMMIT = re.compile(r"[0-9a-f]{40}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class EcrClient(Protocol):
    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_images(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_image_scan_findings(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_image_referrers(self, **kwargs: Any) -> dict[str, Any]: ...

    def batch_get_image(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_download_url_for_layer(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_signing_configuration(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_image_signing_status(self, **kwargs: Any) -> dict[str, Any]: ...


class ArtifactBlobReader(Protocol):
    def read(self, url: str, *, maximum_bytes: int) -> bytes: ...


class EcrEvidenceError(RuntimeError):
    """ECR evidence disproves or cannot satisfy the release contract."""


class EcrEvidenceIncomplete(EcrEvidenceError):
    """A bounded asynchronous evidence step has not completed."""


class EcrEvidenceAmbiguous(EcrEvidenceError):
    """Live evidence cannot prove one exact immutable subject."""


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EcrEvidenceError(f"{label} response is malformed")
    return value


def _list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EcrEvidenceError(f"{label} response is malformed")
    return value


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload:
        raise EcrEvidenceError(f"{label} is empty")

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EcrEvidenceError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=exact_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EcrEvidenceError(f"{label} is not valid JSON") from error
    return _object(value, label=label)


class EcrEvidenceAdapter:
    """Collect one strict RuntimeImageEvidence using only an injected client."""

    def __init__(
        self,
        ecr: EcrClient,
        *,
        blob_reader: ArtifactBlobReader,
    ) -> None:
        self._ecr = ecr
        self._blob_reader = blob_reader

    def _call(self, method_name: str, **arguments: Any) -> dict[str, Any]:
        method = getattr(self._ecr, method_name, None)
        if method is None or not callable(method):
            raise EcrEvidenceError(f"injected ECR adapter lacks {method_name}")
        try:
            return _object(method(**arguments), label=method_name)
        except (TimeoutError, ConnectionError) as error:
            raise EcrEvidenceAmbiguous(
                f"{method_name} ended without authoritative evidence"
            ) from error

    @staticmethod
    def _identity(
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
    ) -> None:
        if _COMMIT.fullmatch(source_commit) is None:
            raise EcrEvidenceError("source commit is not canonical")
        if _COMMIT.fullmatch(source_tree) is None:
            raise EcrEvidenceError("source tree is not canonical")
        if _ACCOUNT.fullmatch(account) is None or account == "000000000000":
            raise EcrEvidenceError("release account is not canonical")
        if region != REQUIRED_REGION:
            raise EcrEvidenceError(f"release region must be exactly {REQUIRED_REGION}")

    def _repository(self, *, account: str) -> None:
        response = self._call(
            "describe_repositories",
            registryId=account,
            repositoryNames=[REPOSITORY_NAME],
        )
        if response.get("nextToken"):
            raise EcrEvidenceAmbiguous("repository lookup was paginated")
        repositories = _list(response.get("repositories"), label="repository")
        if len(repositories) != 1:
            raise EcrEvidenceAmbiguous(
                "repository lookup must return one exact repository"
            )
        repository = _object(repositories[0], label="repository")
        if (
            repository.get("registryId") != account
            or repository.get("repositoryName") != REPOSITORY_NAME
        ):
            raise EcrEvidenceError("repository identity crosses the release account")
        if repository.get("imageTagMutability") != "IMMUTABLE":
            raise EcrEvidenceError("runtime image repository is not immutable")
        scanning = _object(
            repository.get("imageScanningConfiguration"), label="repository scanning"
        )
        if scanning.get("scanOnPush") is not True:
            raise EcrEvidenceError("runtime image repository does not scan on push")

    def _one_image(
        self,
        *,
        account: str,
        image_id: dict[str, str],
        expected_tag: str,
    ) -> dict[str, Any]:
        response = self._call(
            "describe_images",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageIds=[image_id],
        )
        if response.get("nextToken"):
            raise EcrEvidenceAmbiguous("image lookup was paginated")
        details = _list(response.get("imageDetails"), label="image")
        if len(details) != 1:
            raise EcrEvidenceAmbiguous("image lookup must return one exact image")
        detail = _object(details[0], label="image")
        if (
            detail.get("registryId") != account
            or detail.get("repositoryName") != REPOSITORY_NAME
        ):
            raise EcrEvidenceError("image identity crosses the release repository")
        digest = detail.get("imageDigest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise EcrEvidenceError("image digest is malformed")
        tags = detail.get("imageTags")
        if not isinstance(tags, list) or expected_tag not in tags:
            raise EcrEvidenceError("image is not bound to the immutable commit tag")
        size = detail.get("imageSizeInBytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise EcrEvidenceError("image size evidence is malformed")
        return detail

    def _scan(self, *, account: str, digest: str) -> tuple[int, int]:
        response = self._call(
            "describe_image_scan_findings",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageId={"imageDigest": digest},
        )
        if (
            response.get("registryId") != account
            or response.get("repositoryName") != REPOSITORY_NAME
            or response.get("imageId") != {"imageDigest": digest}
        ):
            raise EcrEvidenceError("image scan identity differs from the image")
        status = _object(response.get("imageScanStatus"), label="image scan").get(
            "status"
        )
        if status in {"IN_PROGRESS", "ACTIVE", "PENDING"}:
            raise EcrEvidenceIncomplete(f"image scan is not complete: {status}")
        if status != "COMPLETE":
            raise EcrEvidenceError(f"image scan failed closed: {status}")
        findings = _object(response.get("imageScanFindings"), label="image findings")
        counts = _object(findings.get("findingSeverityCounts"), label="finding counts")
        critical = counts.get("CRITICAL", 0)
        high = counts.get("HIGH", 0)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (critical, high)
        ):
            raise EcrEvidenceError("image finding counts are malformed")
        if critical or high:
            raise EcrEvidenceError("image has unreviewed high or critical findings")
        return critical, high

    def _artifact_blob(
        self,
        *,
        account: str,
        subject_digest: str,
        artifact_type: str,
        referrer: dict[str, Any],
    ) -> tuple[str, bytes]:
        manifest_digest = referrer.get("digest")
        manifest_size = referrer.get("size")
        if (
            not isinstance(manifest_digest, str)
            or _DIGEST.fullmatch(manifest_digest) is None
            or not isinstance(manifest_size, int)
            or isinstance(manifest_size, bool)
            or not 1 <= manifest_size <= MAX_ATTESTATION_MANIFEST_BYTES
            or referrer.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
        ):
            raise EcrEvidenceError("attestation referrer metadata is malformed")
        response = self._call(
            "batch_get_image",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageIds=[{"imageDigest": manifest_digest}],
            acceptedMediaTypes=[OCI_MANIFEST_MEDIA_TYPE],
        )
        if response.get("failures") != []:
            raise EcrEvidenceError("attestation manifest lookup failed")
        images = _list(response.get("images"), label="attestation manifest")
        if len(images) != 1:
            raise EcrEvidenceAmbiguous(
                "attestation manifest lookup must return one exact artifact"
            )
        image = _object(images[0], label="attestation manifest")
        if (
            image.get("registryId") != account
            or image.get("repositoryName") != REPOSITORY_NAME
            or image.get("imageId") != {"imageDigest": manifest_digest}
            or image.get("imageManifestMediaType") != OCI_MANIFEST_MEDIA_TYPE
        ):
            raise EcrEvidenceError("attestation manifest identity is not exact")
        manifest_text = image.get("imageManifest")
        if not isinstance(manifest_text, str):
            raise EcrEvidenceError("attestation manifest bytes are missing")
        manifest_bytes = manifest_text.encode("utf-8")
        if (
            len(manifest_bytes) != manifest_size
            or "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            != manifest_digest
        ):
            raise EcrEvidenceError("attestation manifest digest or size differs")
        manifest = _strict_json(manifest_bytes, label="attestation manifest")
        allowed = {
            "annotations",
            "artifactType",
            "config",
            "layers",
            "mediaType",
            "schemaVersion",
            "subject",
        }
        if set(manifest) - allowed or not {
            "artifactType",
            "layers",
            "mediaType",
            "schemaVersion",
            "subject",
        }.issubset(manifest):
            raise EcrEvidenceError("attestation manifest schema is invalid")
        if (
            manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
            or manifest.get("artifactType") != artifact_type
        ):
            raise EcrEvidenceError("attestation manifest type is invalid")
        subject = _object(manifest.get("subject"), label="attestation subject")
        if (
            subject.get("digest") != subject_digest
            or subject.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
            or not isinstance(subject.get("size"), int)
            or isinstance(subject.get("size"), bool)
            or subject.get("size") <= 0
        ):
            raise EcrEvidenceError("attestation manifest subject differs from image")
        layers = _list(manifest.get("layers"), label="attestation layers")
        if len(layers) != 1:
            raise EcrEvidenceAmbiguous(
                "attestation manifest must contain one exact content blob"
            )
        layer = _object(layers[0], label="attestation layer")
        if set(layer) != {"digest", "mediaType", "size"}:
            raise EcrEvidenceError("attestation layer schema is invalid")
        layer_digest = layer.get("digest")
        layer_size = layer.get("size")
        if (
            not isinstance(layer_digest, str)
            or _DIGEST.fullmatch(layer_digest) is None
            or layer.get("mediaType") != artifact_type
            or not isinstance(layer_size, int)
            or isinstance(layer_size, bool)
            or not 1 <= layer_size <= MAX_ATTESTATION_BLOB_BYTES
        ):
            raise EcrEvidenceError("attestation layer metadata is malformed")
        download = self._call(
            "get_download_url_for_layer",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            layerDigest=layer_digest,
        )
        url = download.get("downloadUrl")
        if download.get("layerDigest") != layer_digest or not isinstance(url, str):
            raise EcrEvidenceError("attestation layer download identity differs")
        try:
            blob = self._blob_reader.read(
                url,
                maximum_bytes=MAX_ATTESTATION_BLOB_BYTES,
            )
        except (TimeoutError, ConnectionError) as error:
            raise EcrEvidenceAmbiguous(
                "attestation blob read ended without authoritative evidence"
            ) from error
        if (
            not isinstance(blob, bytes)
            or len(blob) != layer_size
            or "sha256:" + hashlib.sha256(blob).hexdigest() != layer_digest
        ):
            raise EcrEvidenceError("attestation blob digest or size differs")
        return manifest_digest.removeprefix("sha256:"), blob

    @staticmethod
    def _validate_sbom(payload: bytes) -> None:
        value = _strict_json(payload, label="SBOM")
        if (
            value.get("spdxVersion") != "SPDX-2.3"
            or value.get("SPDXID") != "SPDXRef-DOCUMENT"
            or value.get("dataLicense") != "CC0-1.0"
            or not isinstance(value.get("name"), str)
            or not value.get("name")
        ):
            raise EcrEvidenceError("SBOM content is not a complete SPDX document")

    @staticmethod
    def _validate_provenance(
        payload: bytes,
        *,
        subject_digest: str,
        source_commit: str,
        source_tree: str,
        build_context: str,
        builder_id: str,
        builder_inputs: Sequence[str],
    ) -> None:
        value = _strict_json(payload, label="provenance")
        if value.get("_type") != PROVENANCE_STATEMENT_TYPE:
            raise EcrEvidenceError("provenance statement type is invalid")
        if value.get("predicateType") != PROVENANCE_PREDICATE_TYPE:
            raise EcrEvidenceError("provenance predicate type is invalid")
        expected_subject = [
            {
                "digest": {"sha256": subject_digest.removeprefix("sha256:")},
                "name": REPOSITORY_NAME,
            }
        ]
        if value.get("subject") != expected_subject:
            raise EcrEvidenceError("provenance subject differs from the image")
        predicate = _object(value.get("predicate"), label="provenance predicate")
        definition = _object(
            predicate.get("buildDefinition"),
            label="provenance build definition",
        )
        if definition.get("buildType") != BRIDGE_BUILD_TYPE:
            raise EcrEvidenceError("provenance build type is invalid")
        external = _object(
            definition.get("externalParameters"),
            label="provenance external parameters",
        )
        if external.get("sourceCommit") != source_commit:
            raise EcrEvidenceError("provenance source commit differs")
        if external.get("sourceTree") != source_tree:
            raise EcrEvidenceError("provenance source tree differs")
        if external.get("buildContext") != build_context:
            raise EcrEvidenceError("provenance build context differs")
        run_details = _object(predicate.get("runDetails"), label="provenance run")
        builder = _object(run_details.get("builder"), label="provenance builder")
        if builder.get("id") != builder_id:
            raise EcrEvidenceError("provenance builder identity differs")
        dependencies = _list(
            definition.get("resolvedDependencies"),
            label="provenance builder inputs",
        )
        observed_inputs: list[str] = []
        for raw in dependencies:
            dependency = _object(raw, label="provenance builder input")
            digest = _object(
                dependency.get("digest"),
                label="provenance builder input digest",
            )
            sha256 = digest.get("sha256")
            if (
                set(dependency) != {"digest", "uri"}
                or set(digest) != {"sha256"}
                or not isinstance(dependency.get("uri"), str)
                or not dependency.get("uri")
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            ):
                raise EcrEvidenceError("provenance builder input is malformed")
            observed_inputs.append(f"sha256:{sha256}")
        if sorted(observed_inputs) != sorted(builder_inputs):
            raise EcrEvidenceError("provenance builder inputs differ")

    def _attestations(
        self,
        *,
        account: str,
        digest: str,
        source_commit: str,
        source_tree: str,
        build_context: str,
        builder_id: str,
        builder_inputs: Sequence[str],
    ) -> tuple[str, str]:
        response = self._call(
            "list_image_referrers",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            subjectId={"imageDigest": digest},
            filter={
                "artifactTypes": [SBOM_ARTIFACT_TYPE, PROVENANCE_ARTIFACT_TYPE],
                "artifactStatus": "ACTIVE",
            },
        )
        if response.get("nextToken"):
            raise EcrEvidenceAmbiguous("attestation lookup was paginated")
        referrers = _list(response.get("referrers"), label="attestation")
        by_type: dict[str, list[dict[str, Any]]] = {
            SBOM_ARTIFACT_TYPE: [],
            PROVENANCE_ARTIFACT_TYPE: [],
        }
        for raw in referrers:
            item = _object(raw, label="attestation")
            artifact_type = item.get("artifactType")
            if artifact_type not in by_type or item.get("artifactStatus") != "ACTIVE":
                raise EcrEvidenceError(
                    "attestation set contains an unexpected artifact"
                )
            artifact_digest = item.get("digest")
            if (
                not isinstance(artifact_digest, str)
                or _DIGEST.fullmatch(artifact_digest) is None
            ):
                raise EcrEvidenceError("attestation digest is malformed")
            by_type[artifact_type].append(item)
        if any(len(values) != 1 for values in by_type.values()):
            raise EcrEvidenceAmbiguous(
                "attestation evidence must contain one SBOM and one provenance artifact"
            )
        sbom_digest, sbom_blob = self._artifact_blob(
            account=account,
            subject_digest=digest,
            artifact_type=SBOM_ARTIFACT_TYPE,
            referrer=by_type[SBOM_ARTIFACT_TYPE][0],
        )
        provenance_digest, provenance_blob = self._artifact_blob(
            account=account,
            subject_digest=digest,
            artifact_type=PROVENANCE_ARTIFACT_TYPE,
            referrer=by_type[PROVENANCE_ARTIFACT_TYPE][0],
        )
        self._validate_sbom(sbom_blob)
        self._validate_provenance(
            provenance_blob,
            subject_digest=digest,
            source_commit=source_commit,
            source_tree=source_tree,
            build_context=build_context,
            builder_id=builder_id,
            builder_inputs=builder_inputs,
        )
        return sbom_digest, provenance_digest

    def _signing(self, *, account: str, digest: str, profile: str) -> None:
        configuration = self._call("get_signing_configuration")
        if configuration.get("registryId") != account:
            raise EcrEvidenceError("signing configuration crosses the release account")
        body = _object(
            configuration.get("signingConfiguration"), label="signing configuration"
        )
        rules = _list(body.get("rules"), label="signing rules")
        expected_filter = [
            {"filter": REPOSITORY_NAME, "filterType": "WILDCARD_MATCH"}
        ]
        matching = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("signingProfileArn") == profile
            and rule.get("repositoryFilters") == expected_filter
        ]
        if len(matching) != 1:
            raise EcrEvidenceAmbiguous(
                "registry must contain one exact repository-filtered signing rule"
            )
        response = self._call(
            "describe_image_signing_status",
            registryId=account,
            repositoryName=REPOSITORY_NAME,
            imageId={"imageDigest": digest},
        )
        if (
            response.get("registryId") != account
            or response.get("repositoryName") != REPOSITORY_NAME
            or response.get("imageId") != {"imageDigest": digest}
        ):
            raise EcrEvidenceError("image signing identity differs from the image")
        statuses = _list(response.get("signingStatuses"), label="image signing")
        matching_statuses = [
            item
            for item in statuses
            if isinstance(item, dict) and item.get("signingProfileArn") == profile
        ]
        if len(matching_statuses) != 1:
            raise EcrEvidenceAmbiguous("image signing evidence is ambiguous")
        status = matching_statuses[0].get("status")
        if status == "IN_PROGRESS":
            raise EcrEvidenceIncomplete("image signing is still in progress")
        if status != "COMPLETE":
            raise EcrEvidenceError(f"image signing failed closed: {status}")

    def collect(
        self,
        *,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
        build_context: str,
        builder_id: str,
        builder_inputs: Sequence[str],
    ) -> RuntimeImageEvidence:
        """Return complete evidence for one exact commit-tagged image digest."""

        self._identity(source_commit, source_tree, account, region)
        if (
            not isinstance(build_context, str)
            or not build_context
            or len(build_context) > 256
            or not isinstance(builder_id, str)
            or not builder_id
            or len(builder_id) > 512
            or not isinstance(builder_inputs, Sequence)
            or isinstance(builder_inputs, (str, bytes))
            or not builder_inputs
            or any(
                not isinstance(value, str)
                or _DIGEST.fullmatch(value) is None
                for value in builder_inputs
            )
            or len(set(builder_inputs)) != len(builder_inputs)
        ):
            raise EcrEvidenceError("reviewed build provenance inputs are invalid")
        self._repository(account=account)
        commit_tag = f"commit-{source_commit}"
        tagged = self._one_image(
            account=account,
            image_id={"imageTag": commit_tag},
            expected_tag=commit_tag,
        )
        digest = str(tagged["imageDigest"])
        by_digest = self._one_image(
            account=account,
            image_id={"imageDigest": digest},
            expected_tag=commit_tag,
        )
        if by_digest.get("imageDigest") != digest:
            raise EcrEvidenceError("tag and digest lookups resolve different digests")
        critical, high = self._scan(account=account, digest=digest)
        sbom, provenance = self._attestations(
            account=account,
            digest=digest,
            source_commit=source_commit,
            source_tree=source_tree,
            build_context=build_context,
            builder_id=builder_id,
            builder_inputs=tuple(builder_inputs),
        )
        profile = (
            f"arn:aws:signer:{region}:{account}:/signing-profiles/"
            f"{SIGNING_PROFILE_NAME}"
        )
        self._signing(account=account, digest=digest, profile=profile)
        image_uri = (
            f"{account}.dkr.ecr.{region}.amazonaws.com/"
            f"{REPOSITORY_NAME}@{digest}"
        )
        return RuntimeImageEvidence.from_mapping(
            {
                "schema": RuntimeImageEvidence.SCHEMA,
                "sourceCommit": source_commit,
                "sourceTree": source_tree,
                "account": account,
                "region": region,
                "repositoryName": REPOSITORY_NAME,
                "commitTag": commit_tag,
                "imageDigest": digest,
                "imageUri": image_uri,
                "imageSizeBytes": tagged["imageSizeInBytes"],
                "scanStatus": "COMPLETE",
                "criticalFindings": critical,
                "highFindings": high,
                "sbomSha256": sbom,
                "provenanceSha256": provenance,
                "signingProfileArn": profile,
                "signatureStatus": "SIGNED",
            }
        )
