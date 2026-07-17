"""Static contract tests for the frozen Personal Operator runtime defaults."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_cdk_product_defaults_are_frozen() -> None:
    context = _json("cdk.json")["context"]

    assert context["region"] == "eu-west-1"
    assert context["default_model_id"] == "eu.anthropic.claude-sonnet-4-6"
    assert context["session_idle_timeout"] == 1800
    assert context["session_max_lifetime"] == 28800
    assert context["workspace_sync_interval_seconds"] == 300
    assert context["user_files_ttl_days"] == 30
    assert context["registration_open"] is False
    assert context["enable_browser"] is False
    assert context["runtime_id"] == ""
    assert context["runtime_endpoint_id"] == ""
    assert context["image_version"] == "71"


def test_bridge_runtime_versions_are_frozen() -> None:
    package = _json("bridge/package.json")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert package["engines"]["node"] == ">=24.15.0"
    assert re.findall(r"^FROM .*node:(\S+)", dockerfile, flags=re.MULTILINE) == [
        "24.15.0-slim",
        "24.15.0-slim",
    ]
    assert re.findall(
        r"^ARG OPENCLAW_VERSION=([^\s]+)", dockerfile, flags=re.MULTILINE
    ) == ["2026.7.2"]
    assert re.findall(
        r"^ARG OPENCLAW_SOURCE_COMMIT=([0-9a-f]+)", dockerfile, flags=re.MULTILINE
    ) == ["4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"]
    assert 'git fetch --depth 1 origin "$OPENCLAW_SOURCE_COMMIT"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "$OPENCLAW_SOURCE_COMMIT"' in dockerfile
    assert (
        "test \"$(node -p 'require(\"./package.json\").version')\" "
        '= "$OPENCLAW_VERSION"'
    ) in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "COPY --from=builder /opt/openclaw /opt/openclaw" in dockerfile
    assert "ln -s /opt/openclaw/openclaw.mjs /usr/local/bin/openclaw" in dockerfile
    assert "npm install -g openclaw@" not in dockerfile


def test_bridge_image_does_not_install_clawhub() -> None:
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "clawhub" not in dockerfile.casefold()


def test_bridge_dependency_install_is_locked() -> None:
    package = _json("bridge/package.json")
    lock_path = ROOT / "bridge/package-lock.json"
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert lock_path.is_file(), "bridge/package-lock.json must be committed"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"][""]["engines"] == package["engines"]
    assert "COPY package.json package-lock.json /app/" in dockerfile
    assert "npm ci --omit=dev" in dockerfile
    assert "npm init" not in dockerfile
    assert "npm install --omit=dev" not in dockerfile


def test_readme_distinguishes_target_constraints_from_imported_behavior() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Current imported runtime behavior" in readme
    assert "until Task 2" in readme
    assert "`api-keys`" in readme
    assert "`clawhub-manage`" in readme
    assert "## Target product boundaries (not yet fully enforced)" in readme


def test_cdk_nag_guard_rejects_missing_reports(tmp_path: Path) -> None:
    script = (ROOT / "scripts/test-local.sh").read_text(encoding="utf-8")
    match = re.search(
        r"check_cdk_nag\(\) \{\n\s+\"\$PYTHON\" - \"\$SYNTH_OUT\" <<'PY'\n"
        r"(?P<checker>.*?)\nPY\n\}",
        script,
        flags=re.DOTALL,
    )
    assert match is not None, "could not locate the embedded cdk-nag checker"

    completed = subprocess.run(
        [sys.executable, "-c", match.group("checker"), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "No AwsSolutions NagReport CSV files found" in completed.stderr
