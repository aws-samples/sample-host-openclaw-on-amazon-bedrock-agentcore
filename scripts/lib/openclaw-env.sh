#!/usr/bin/env bash

validate_env_name() {
    local env_name="${1:-}"
    if [ -z "$env_name" ]; then
        return 0
    fi

    if [[ ! "$env_name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
        echo "ERROR: --env/OPENCLAW_ENV_NAME must use lowercase letters, digits, and hyphens only."
        return 1
    fi
}

resolve_env_file() {
    local project_dir="$1"
    local env_name="${2:-${OPENCLAW_ENV_NAME:-}}"

    if [ -n "${OPENCLAW_ENV_FILE:-}" ]; then
        printf '%s\n' "$OPENCLAW_ENV_FILE"
    elif [ -n "$env_name" ]; then
        printf '%s/.env.%s\n' "$project_dir" "$env_name"
    else
        printf '%s/.env\n' "$project_dir"
    fi
}

load_project_env() {
    local project_dir="$1"
    local env_name="${2:-${OPENCLAW_ENV_NAME:-}}"
    local env_file=""

    validate_env_name "$env_name"
    env_file="$(resolve_env_file "$project_dir" "$env_name")"
    export OPENCLAW_SELECTED_ENV_FILE="$env_file"

    if [ -n "${OPENCLAW_ENV_FILE:-}" ] && [ ! -f "$env_file" ]; then
        echo "ERROR: OPENCLAW_ENV_FILE points to a file that does not exist: $env_file"
        return 1
    fi

    if [ -n "$env_name" ] && [ -z "${OPENCLAW_ENV_FILE:-}" ] && [ ! -f "$env_file" ]; then
        echo "ERROR: --env $env_name expects an env file at $env_file"
        return 1
    fi

    if [ -f "$env_file" ]; then
        echo "INFO: Loading environment from $env_file"
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a
    fi
}

apply_named_environment() {
    local env_name="${1:-${OPENCLAW_ENV_NAME:-}}"
    if [ -z "$env_name" ]; then
        return 0
    fi

    validate_env_name "$env_name"

    if [ -n "${OPENCLAW_ENV_SUFFIX:-}" ] && [ "$OPENCLAW_ENV_SUFFIX" != "$env_name" ]; then
        echo "ERROR: --env $env_name conflicts with OPENCLAW_ENV_SUFFIX=$OPENCLAW_ENV_SUFFIX"
        if [ -n "${OPENCLAW_SELECTED_ENV_FILE:-}" ]; then
            echo "Fix ${OPENCLAW_SELECTED_ENV_FILE} or remove --env."
        fi
        return 1
    fi

    export OPENCLAW_ENV_SUFFIX="$env_name"
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
