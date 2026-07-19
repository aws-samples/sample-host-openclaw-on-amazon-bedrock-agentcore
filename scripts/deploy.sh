#!/usr/bin/env bash
# Compatibility entry point. All validation, state transitions, and release
# behavior live in the typed Python staging-release package.

set -euo pipefail
readonly PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

SCRIPT_DIR="$(unset CDPATH; cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(unset CDPATH; cd -- "${SCRIPT_DIR}/.." && pwd -P)"
if [[ "${PYTHON+x}" == "x" ]]; then
  printf 'deploy shim: operator Python override is forbidden\n' >&2
  exit 2
fi
readonly PYTHON="${REPOSITORY_ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'deploy shim: Python environment is missing at %s\n' "${PYTHON}" >&2
  exit 2
fi
if [[ ! -O "${PYTHON}" ]] || /usr/bin/find -L "${PYTHON}" -prune \( -perm -002 -o -perm -020 \) -print | /usr/bin/grep -q .; then
  printf 'deploy shim: Python must be owner-controlled and not group/world writable\n' >&2
  exit 2
fi

unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT
exec "${PYTHON}" -I "${SCRIPT_DIR}/staging-release.py" "$@"
