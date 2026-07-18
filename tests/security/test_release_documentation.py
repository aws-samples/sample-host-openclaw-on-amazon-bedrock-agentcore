from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_docs_are_explicitly_preproduction_and_effect_free() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    assert "not authorization to deploy" in operations
    assert "prepare, do not deploy" in operations
    assert "Do not\nrun it as a preflight" in operations
    assert "never replay a write merely because a request timed out" in operations
    assert "Raw message bodies are transient" in privacy
    assert "terminal and no-resend `UNCERTAIN` records expire after 90 days" in privacy
    assert "No deployed completion SLA is claimed" in " ".join(privacy.split())
    assert "**NOT RELEASED / NOT DEPLOYABLE YET.**" in evidence
    assert "No Lambda deployment bundle proof" in evidence
    assert "No image build, scan, SBOM, signature, push, or immutable ECR digest" in evidence
    assert "pre-production local\nprototype" in evidence
