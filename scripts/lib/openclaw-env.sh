#!/usr/bin/env bash
# Shared env-file loading and environment-suffix naming for the deploy,
# undeploy and setup scripts. Source it; do not execute it.
#
# Env file selection (first match wins):
#   OPENCLAW_ENV_FILE=<path>   explicit file (must exist)
#   --env <name> / OPENCLAW_ENV_NAME=<name>   .env.<name> (must exist)
#   otherwise                  .env, if present
#
# Suffix: OPENCLAW_ENV_SUFFIX (from the shell or the env file), else the
# environment_suffix key in cdk.json context. Empty (the default) keeps every
# stack and resource name unchanged. Must match stacks/__init__.py:
# lowercase letters, digits and single hyphens, at most 12 characters.
#
# Works with the macOS system bash (3.2): no bash 4+ features.

validate_env_name() {
    local env_name="${1:-}"
    if [ -z "$env_name" ]; then
        return 0
    fi
    if [[ ! "$env_name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || [ "${#env_name}" -gt 12 ]; then
        echo "ERROR: --env/OPENCLAW_ENV_NAME must be at most 12 lowercase letters, digits and single hyphens." >&2
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

    validate_env_name "$env_name" || return 1
    env_file="$(resolve_env_file "$project_dir" "$env_name")"
    export OPENCLAW_SELECTED_ENV_FILE="$env_file"

    if [ -n "${OPENCLAW_ENV_FILE:-}" ] && [ ! -f "$env_file" ]; then
        echo "ERROR: OPENCLAW_ENV_FILE points to a file that does not exist: $env_file" >&2
        return 1
    fi
    if [ -n "$env_name" ] && [ -z "${OPENCLAW_ENV_FILE:-}" ] && [ ! -f "$env_file" ]; then
        echo "ERROR: --env $env_name expects an env file at $env_file (copy .env.template)." >&2
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

# --env <name> selects .env.<name> and implies OPENCLAW_ENV_SUFFIX=<name>.
apply_named_environment() {
    local env_name="${1:-${OPENCLAW_ENV_NAME:-}}"
    if [ -z "$env_name" ]; then
        return 0
    fi
    validate_env_name "$env_name" || return 1
    if [ -n "${OPENCLAW_ENV_SUFFIX:-}" ] && [ "$OPENCLAW_ENV_SUFFIX" != "$env_name" ]; then
        echo "ERROR: --env $env_name conflicts with OPENCLAW_ENV_SUFFIX=$OPENCLAW_ENV_SUFFIX" >&2
        if [ -n "${OPENCLAW_SELECTED_ENV_FILE:-}" ]; then
            echo "Fix ${OPENCLAW_SELECTED_ENV_FILE} or remove --env." >&2
        fi
        return 1
    fi
    export OPENCLAW_ENV_SUFFIX="$env_name"
}

# normalize_env_suffix <raw>: print the lowercased, trimmed suffix (may be
# empty); fails on an invalid value. Same rules as stacks/__init__.py.
normalize_env_suffix() {
    local suffix
    # tr, not ${var,,}: the macOS system bash is 3.2.
    suffix="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | sed -e 's/^[[:space:]-]*//' -e 's/[[:space:]-]*$//')"
    if [ -n "$suffix" ]; then
        if [[ ! "$suffix" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || [ "${#suffix}" -gt 12 ]; then
            echo "ERROR: OPENCLAW_ENV_SUFFIX/environment_suffix must be at most 12 lowercase letters, digits and single hyphens." >&2
            return 1
        fi
    fi
    printf '%s\n' "$suffix"
}

# resolve_env_suffix <project_dir>: OPENCLAW_ENV_SUFFIX, else cdk.json
# context environment_suffix (read with python3), normalized.
resolve_env_suffix() {
    local project_dir="$1"
    local raw="${OPENCLAW_ENV_SUFFIX:-}"
    if [ -z "$raw" ] && [ -f "$project_dir/cdk.json" ]; then
        raw="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("context", {}).get("environment_suffix", "") or "")' "$project_dir/cdk.json")" || return 1
    fi
    normalize_env_suffix "$raw"
}

# with_suffix <base> [separator]: <base><sep><suffix>, or <base> when no suffix.
with_suffix() {
    local base="$1"
    local sep="${2:--}"
    local suffix="${OPENCLAW_ENV_SUFFIX:-}"
    if [ -z "$suffix" ]; then
        printf '%s\n' "$base"
    elif [ "$sep" = "_" ]; then
        printf '%s_%s\n' "$base" "$(printf '%s' "$suffix" | tr '-' '_')"
    else
        printf '%s%s%s\n' "$base" "$sep" "$suffix"
    fi
}
