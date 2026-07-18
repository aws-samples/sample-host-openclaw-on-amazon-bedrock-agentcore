"""Injected, credential-free collector for immutable ECR release evidence."""

from __future__ import annotations

import re
from typing import Any, Protocol

from release_tools.contracts import RuntimeImageEvidence


REPOSITORY_NAME = "personal-operator/bridge"
REQUIRED_REGION = "eu-west-1"
SIGNING_PROFILE_NAME = "personal_operator_bridge"
SBOM_ARTIFACT_TYPE = "application/spdx+json"
PROVENANCE_ARTIFACT_TYPE = "application/vnd.in-toto+json"

_COMMIT = re.compile(r"[0-9a-f]{40}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class EcrClient(Protocol):
    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_images(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_image_scan_findings(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_image_referrers(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_signing_configuration(self, **kwargs: Any) -> dict[str, Any]: ...

    def describe_image_signing_status(self, **kwargs: Any) -> dict[str, Any]: ...


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


class EcrEvidenceAdapter:
    """Collect one strict RuntimeImageEvidence using only an injected client."""

    def __init__(self, ecr: EcrClient) -> None:
        self._ecr = ecr

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
    def _identity(source_commit: str, source_tree: str, account: str, region: str) -> None:
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
            raise EcrEvidenceAmbiguous("repository lookup must return one exact repository")
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

    def _attestations(self, *, account: str, digest: str) -> tuple[str, str]:
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
        by_type: dict[str, list[str]] = {
            SBOM_ARTIFACT_TYPE: [],
            PROVENANCE_ARTIFACT_TYPE: [],
        }
        for raw in referrers:
            item = _object(raw, label="attestation")
            artifact_type = item.get("artifactType")
            if artifact_type not in by_type or item.get("artifactStatus") != "ACTIVE":
                raise EcrEvidenceError("attestation set contains an unexpected artifact")
            artifact_digest = item.get("digest")
            if not isinstance(artifact_digest, str) or _DIGEST.fullmatch(artifact_digest) is None:
                raise EcrEvidenceError("attestation digest is malformed")
            by_type[artifact_type].append(artifact_digest.removeprefix("sha256:"))
        if any(len(values) != 1 for values in by_type.values()):
            raise EcrEvidenceAmbiguous(
                "attestation evidence must contain one SBOM and one provenance artifact"
            )
        return by_type[SBOM_ARTIFACT_TYPE][0], by_type[PROVENANCE_ARTIFACT_TYPE][0]

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
    ) -> RuntimeImageEvidence:
        """Return complete evidence for one exact commit-tagged image digest."""

        self._identity(source_commit, source_tree, account, region)
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
        sbom, provenance = self._attestations(account=account, digest=digest)
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
