#!/usr/bin/env bash
# Compatibility entry point. All validation, state transitions, and release
# behavior live in the typed Python staging-release package.

set -euo pipefail

SCRIPT_DIR="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(unset CDPATH; cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-${REPOSITORY_ROOT}/.venv/bin/python}"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'deploy shim: Python environment is missing at %s\n' "${PYTHON}" >&2
  exit 2
fi

exec "${PYTHON}" "${SCRIPT_DIR}/staging-release.py" "$@"
