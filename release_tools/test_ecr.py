from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from release_tools.ecr import (
    EcrEvidenceAdapter,
    EcrEvidenceAmbiguous,
    EcrEvidenceError,
    EcrEvidenceIncomplete,
    PROVENANCE_ARTIFACT_TYPE,
    SBOM_ARTIFACT_TYPE,
)


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64
PROFILE = (
    f"arn:aws:signer:{REGION}:{ACCOUNT}:/signing-profiles/"
    "personal_operator_bridge"
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
                    "digest": "sha256:" + "d" * 64,
                    "artifactType": SBOM_ARTIFACT_TYPE,
                    "artifactStatus": "ACTIVE",
                },
                {
                    "digest": "sha256:" + "e" * 64,
                    "artifactType": PROVENANCE_ARTIFACT_TYPE,
                    "artifactStatus": "ACTIVE",
                },
            ]
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

    def describe_image_signing_status(self, **kwargs):
        return self._return("describe_image_signing_status", kwargs)

    def get_signing_configuration(self, **kwargs):
        return self._return("get_signing_configuration", kwargs)


def _collect(fake: FakeEcr):
    return EcrEvidenceAdapter(fake).collect(
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
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
    assert evidence.sbom_sha256 == "d" * 64
    assert evidence.provenance_sha256 == "e" * 64
    assert evidence.signature_status == "SIGNED"
    assert [name for name, _ in fake.calls] == [
        "describe_repositories",
        "describe_images_by_tag",
        "describe_images_by_digest",
        "describe_image_scan_findings",
        "list_image_referrers",
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


@pytest.mark.parametrize("lookup", ["describe_images_by_tag", "describe_images_by_digest"])
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

    with pytest.raises(EcrEvidenceError, match="findings"):
        _collect(FakeEcr(responses))


@pytest.mark.parametrize("artifact_type", [SBOM_ARTIFACT_TYPE, PROVENANCE_ARTIFACT_TYPE])
def test_missing_or_duplicate_attestation_is_ambiguous(artifact_type: str) -> None:
    responses = _responses()
    referrers = responses["list_image_referrers"]["referrers"]  # type: ignore[index]
    match = next(item for item in referrers if item["artifactType"] == artifact_type)
    referrers.append(deepcopy(match))

    with pytest.raises(EcrEvidenceAmbiguous, match="attestation"):
        _collect(FakeEcr(responses))


@pytest.mark.parametrize("status", ["IN_PROGRESS", "FAILED", "SURPRISE"])
def test_signing_status_never_defaults_to_signed(status: str) -> None:
    responses = _responses()
    responses["describe_image_signing_status"]["signingStatuses"][0][  # type: ignore[index]
        "status"
    ] = status

    error = EcrEvidenceIncomplete if status == "IN_PROGRESS" else EcrEvidenceError
    with pytest.raises(error, match="sign"):
        _collect(FakeEcr(responses))


def test_multiple_matching_signing_rules_are_ambiguous() -> None:
    responses = _responses()
    rules = responses["get_signing_configuration"]["signingConfiguration"][  # type: ignore[index]
        "rules"
    ]
    rules.append(deepcopy(rules[0]))

    with pytest.raises(EcrEvidenceAmbiguous, match="signing rule"):
        _collect(FakeEcr(responses))


def test_adapter_source_has_no_sdk_or_credential_construction() -> None:
    source = (Path(__file__).parent / "ecr.py").read_text(encoding="utf-8")

    assert "boto3" not in source
    assert "botocore" not in source
    assert "Session(" not in source
    assert "client(" not in source
