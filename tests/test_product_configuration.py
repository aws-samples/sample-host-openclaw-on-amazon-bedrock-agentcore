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


def test_bridge_image_copies_only_the_reviewed_plugin() -> None:
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "clawhub" not in dockerfile.casefold()
    assert "COPY plugins/personal-operator /app/plugins/personal-operator" in dockerfile
    assert "COPY skills/" not in dockerfile
    assert "/skills/" not in dockerfile
    assert "ln -s /opt/openclaw /app/node_modules/openclaw" in dockerfile
    assert "COPY gateway-invocation.js /app/gateway-invocation.js" in dockerfile


def test_legacy_executable_skill_trees_are_absent() -> None:
    assert not (ROOT / "bridge/skills").exists()
    assert not (ROOT / "bridge/agentcore-browser.test.js").exists()
    assert not (ROOT / "bridge/browser-lifecycle.test.js").exists()


def test_personal_operator_plugin_package_is_frozen() -> None:
    package = _json("bridge/plugins/personal-operator/package.json")
    manifest = _json("bridge/plugins/personal-operator/openclaw.plugin.json")

    expected_tools = [
        "po_file_list",
        "po_file_read",
        "po_file_write",
        "po_file_delete",
    ]
    assert package["type"] == "module"
    assert package["openclaw"]["extensions"] == ["./index.js"]
    assert package["openclaw"]["compat"]["pluginApi"] == ">=2026.7.2"
    assert manifest["id"] == "personal-operator"
    assert manifest["contracts"]["tools"] == expected_tools
    assert manifest["activation"]["onStartup"] is True


def test_contract_has_no_channel_delivery_or_executable_tool_runtime() -> None:
    contract = (ROOT / "bridge/agentcore-contract.js").read_text(encoding="utf-8")
    lightweight = (ROOT / "bridge/lightweight-agent.js").read_text(encoding="utf-8")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "TELEGRAM_BOT_TOKEN" not in contract
    assert "api.telegram.org" not in contract
    assert "TELEGRAM_CHANNEL_SECRET_ID" not in contract
    assert "createTelegramStreamer" not in contract
    assert "eventbridge-cron" not in contract.casefold()
    assert "clawhub" not in contract.casefold()
    assert "api-keys" not in contract.casefold()
    assert "agentcore-browser" not in contract.casefold()
    assert 'require("./runtime-policy")' in contract
    assert "buildOpenClawConfig" in contract
    assert "createLocalGatewayToken" in contract
    assert "GATEWAY_TOKEN_SECRET_ID" not in contract
    assert "child_process" not in lightweight
    assert "/skills/" not in lightweight
    assert "playwright-core" not in _json("bridge/package.json")["dependencies"]
    assert "@aws-sdk/client-scheduler" not in _json("bridge/package.json")["dependencies"]
    assert "playwright-core" not in dockerfile


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


def test_readme_records_the_enforced_runtime_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Runtime boundary now enforced" in readme
    assert "`po_file_list`" in readme
    assert "`po_file_delete`" in readme
    assert "arbitrary shell execution is disabled" in readme
    assert "Telegram delivery remains outside the runtime" in readme
    assert "ordinary slash-prefixed input is model text" in readme
    assert "exact `/new` and `/reset`" in readme
    assert "Text commands are disabled" not in readme


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
