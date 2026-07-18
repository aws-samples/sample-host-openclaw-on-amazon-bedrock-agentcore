#!/usr/bin/env python3
"""Generate a deterministic, local-only dependency evidence inventory.

The output intentionally describes only repository declarations and npm lock
records. It neither imports the active Python environment nor calls a package
registry, so usernames, absolute paths, credentials, and machine-specific
resolution state cannot enter release evidence.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import re
import shlex
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "bridge/package-lock.json",
    "lambda/requirements.txt",
    "requirements.txt",
    "web/package-lock.json",
)
_REQUIREMENT = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<spec>.*)")


def _license(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        values = sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})
        if values:
            return " OR ".join(values)
    return "NOASSERTION"


def _component(
    *,
    ecosystem: str,
    name: str,
    version: str,
    license_name: str,
    scope: str,
    source: str,
) -> dict[str, object]:
    if ecosystem == "npm":
        purl_name = quote(name, safe="/")
        purl = f"pkg:npm/{purl_name}@{quote(version, safe='.-_+')}"
    else:
        purl = f"pkg:pypi/{quote(name.casefold(), safe='.-_')}"
        if re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
            purl += f"@{quote(version, safe='.-_+')}"
    reference = quote(f"{source}|{ecosystem}|{name}|{version}", safe=".-_@/")
    return {
        "bom-ref": f"declared:{reference}",
        "licenses": [{"license": {"name": license_name}}],
        "name": name,
        "properties": [
            {"name": "personal-operator:source", "value": source},
            {"name": "personal-operator:ecosystem", "value": ecosystem},
            {"name": "personal-operator:scope", "value": scope},
        ],
        "purl": purl,
        "type": "library",
        "version": version,
    }


def _requirements(source: str) -> list[dict[str, object]]:
    components: list[dict[str, object]] = []
    logical: list[str] = []
    pending = ""
    for raw_line in (ROOT / source).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    if pending:
        raise ValueError(f"unterminated requirement declaration in {source}")

    for line in logical:
        tokens = shlex.split(line)
        if not tokens:
            continue
        if any(
            re.fullmatch(r"--hash=sha256:[0-9a-fA-F]{64}", token) is None
            for token in tokens[1:]
        ):
            raise ValueError(f"unsupported requirement option in {source}")
        match = _REQUIREMENT.fullmatch(tokens[0])
        if match is None:
            raise ValueError(f"unsupported requirement declaration in {source}")
        name = match.group("name")
        spec = match.group("spec").strip() or "UNSPECIFIED"
        version = spec[2:] if spec.startswith("==") else spec
        components.append(
            _component(
                ecosystem="pypi",
                name=name,
                version=version,
                license_name="NOASSERTION",
                scope="runtime",
                source=source,
            )
        )
    return components


def _npm(source: str) -> list[dict[str, object]]:
    document = json.loads((ROOT / source).read_text(encoding="utf-8"))
    packages = document.get("packages")
    if document.get("lockfileVersion") != 3 or not isinstance(packages, dict):
        raise ValueError(f"{source} is not a supported npm lockfile v3")
    root = packages.get("")
    if not isinstance(root, dict):
        raise ValueError(f"{source} has no root package record")
    direct_runtime = set(root.get("dependencies", {}))
    direct_development = set(root.get("devDependencies", {}))

    components: list[dict[str, object]] = []
    for path, record in packages.items():
        if not path or not path.startswith("node_modules/") or not isinstance(record, dict):
            continue
        name = path.rsplit("node_modules/", 1)[1]
        version = record.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{source} contains an unversioned npm package")
        if name in direct_runtime:
            scope = "runtime-direct"
        elif name in direct_development:
            scope = "development-direct"
        elif record.get("dev") is True:
            scope = "development-transitive"
        else:
            scope = "runtime-transitive"
        components.append(
            _component(
                ecosystem="npm",
                name=name,
                version=version,
                license_name=_license(record.get("license")),
                scope=scope,
                source=source,
            )
        )
    return components


def build_inventory() -> tuple[dict[str, object], list[dict[str, str]]]:
    components: list[dict[str, object]] = []
    for source in SOURCES:
        if source.endswith("package-lock.json"):
            components.extend(_npm(source))
        else:
            components.extend(_requirements(source))
    components.sort(
        key=lambda item: (
            item["properties"][0]["value"],
            item["name"],
            item["version"],
        )
    )
    references = [item["bom-ref"] for item in components]
    if len(references) != len(set(references)):
        raise ValueError("dependency inventory contains duplicate component identities")

    bom = {
        "bomFormat": "CycloneDX",
        "components": components,
        "metadata": {
            "component": {
                "name": "personal-operator",
                "type": "application",
                "version": "0.0.0-local",
            },
            "properties": [
                {
                    "name": "personal-operator:evidence-scope",
                    "value": "declared-and-npm-locked-dependencies-only",
                }
            ],
        },
        "specVersion": "1.5",
        "version": 1,
    }
    rows = [
        {
            "ecosystem": item["properties"][1]["value"],
            "name": item["name"],
            "version_or_constraint": item["version"],
            "license": item["licenses"][0]["license"]["name"],
            "scope": item["properties"][2]["value"],
            "source": item["properties"][0]["value"],
        }
        for item in components
    ]
    return bom, rows


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    fields = (
        "ecosystem",
        "name",
        "version_or_constraint",
        "license",
        "scope",
        "source",
    )
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    options = parser.parse_args()
    bom, rows = build_inventory()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    (options.output_dir / "personal-operator.cdx.json").write_bytes(
        (json.dumps(bom, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    )
    (options.output_dir / "dependency-licenses.csv").write_bytes(_csv_bytes(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
