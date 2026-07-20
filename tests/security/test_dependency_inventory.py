from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/generate-release-inventory.py"


def _run(output_dir: Path) -> tuple[bytes, bytes]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output_dir)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    return (
        (output_dir / "personal-operator.cdx.json").read_bytes(),
        (output_dir / "dependency-licenses.csv").read_bytes(),
    )


def test_release_inventory_is_deterministic_complete_and_path_free(tmp_path: Path) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first == second
    bom = json.loads(first[0])
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.5"
    assert bom["metadata"]["component"] == {
        "name": "personal-operator",
        "type": "application",
        "version": "0.0.0-local",
    }
    assert bom["metadata"]["properties"] == [
        {
            "name": "personal-operator:evidence-scope",
            "value": "declared-and-npm-locked-dependencies-only",
        }
    ]
    components = bom["components"]
    assert components == sorted(
        components,
        key=lambda item: (item["properties"][0]["value"], item["name"], item["version"]),
    )
    sources = {item["properties"][0]["value"] for item in components}
    assert sources == {
        "bridge/package-lock.json",
        "lambda/requirements.txt",
        "requirements.txt",
        "web/package-lock.json",
    }
    assert any(item["name"] == "openai" and item["version"] == "2.46.0" for item in components)
    assert any(item["name"] == "react" and item["version"] == "19.2.7" for item in components)
    assert any(item["name"] == "ipaddr.js" and item["licenses"] for item in components)
    assert len({item["bom-ref"] for item in components}) == len(components)

    rendered = first[0].decode() + first[1].decode()
    assert str(ROOT) not in rendered
    assert str(Path.home()) not in rendered
    assert "node_modules/" not in rendered

    rows = list(csv.DictReader(first[1].decode().splitlines()))
    assert len(rows) == len(components)
    assert set(rows[0]) == {
        "ecosystem",
        "name",
        "version_or_constraint",
        "license",
        "scope",
        "source",
    }
    assert any(
        row["source"] == "requirements.txt"
        and row["name"] == "aws-cdk-lib"
        and row["license"] == "NOASSERTION"
        for row in rows
    )
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")
    assert f"CycloneDX 1.5 components: {len(components)}" in evidence
    assert hashlib.sha256(first[0]).hexdigest() in evidence
    assert hashlib.sha256(first[1]).hexdigest() in evidence
