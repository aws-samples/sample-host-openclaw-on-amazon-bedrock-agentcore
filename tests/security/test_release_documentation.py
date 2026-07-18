from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_docs_are_explicitly_preproduction_and_effect_free() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    assert "not authorization to deploy" in operations
    assert "prepare, do not deploy" in operations
    boundary = "staging deployment path implemented and locally verified; not deployed"
    assert boundary in operations
    assert boundary in evidence
    assert "never replay a write merely because a request timed out" in operations
    assert "Raw message bodies are transient" in privacy
    assert "terminal and no-resend `UNCERTAIN` records expire after 90 days" in privacy
    assert "No deployed completion SLA is claimed" in " ".join(privacy.split())
    assert "**NOT RELEASED / NOT DEPLOYABLE YET.**" in evidence
    assert "Runtime provisioning is intentionally absent" not in evidence
    assert "Runtime deployment is intentionally **not implemented**" not in operations
    assert "pre-production local\nprototype" in evidence


def test_release_docs_keep_every_external_staging_gate_explicitly_open() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")
    combined = operations + evidence

    for gate in (
        "runtime image push",
        "managed signing",
        "authoritative image scan",
        "CloudFormation change-set execution",
        "AgentCore runtime readiness",
        "consumer application",
        "moderated pilot",
    ):
        assert f"OPEN — {gate}" in combined
    assert "No cloud resource was created or changed" in evidence
    assert "No image was pushed" in evidence


def test_release_docs_count_the_shared_lambda_asset_surface_correctly() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    expected = "five unique handler modules across six Lambda functions"
    assert expected in operations
    assert expected in evidence
    assert "all four Lambda handlers" not in operations
