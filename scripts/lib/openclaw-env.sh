#!/usr/bin/env bash

load_project_env() {
    local project_dir="$1"
    local env_file="${OPENCLAW_ENV_FILE:-$project_dir/.env}"

    if [ -f "$env_file" ]; then
        echo "INFO: Loading environment from $env_file"
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a
    fi
}

resolve_env_suffix() {
    local project_dir="$1"
    python3 - "$project_dir" "${OPENCLAW_ENV_SUFFIX:-}" <<'PY'
import json
import pathlib
import re
import sys

project_dir = pathlib.Path(sys.argv[1])
raw = sys.argv[2]
if not raw:
    try:
        with open(project_dir / "cdk.json", encoding="utf-8") as fh:
            raw = str(json.load(fh).get("context", {}).get("environment_suffix", "") or "")
    except FileNotFoundError:
        raw = ""
suffix = raw.strip().lower().strip("-")
if suffix and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", suffix):
    raise SystemExit(
        "ERROR: OPENCLAW_ENV_SUFFIX/environment_suffix must use lowercase letters, digits, and hyphens only."
    )
print(suffix)
PY
}

with_suffix() {
    local base="$1"
    if [ -n "${OPENCLAW_ENV_SUFFIX:-}" ]; then
        printf '%s-%s\n' "$base" "$OPENCLAW_ENV_SUFFIX"
    else
        printf '%s\n' "$base"
    fi
}
