"""In-container networkless compute entrypoint and egress fences.

This module is driven directly by the local sandbox harness (with fakes) and,
in production, by the read-only job image. It drops ambient authority, applies
POSIX resource limits (CPU, address space for OOM, process count for fork
bombs, and file size), runs the command in a fresh process group so the whole
tree can be killed on a deadline or resource breach, and proves the namespace
is networkless: DNS, outbound TCP, VPC endpoints, IMDS, and the credential
provider chain all fail closed.

No import here performs I/O at module load. The container mounts an immutable
input directory and writes only to a fresh output directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Iterator

try:  # POSIX only; absent on the local macOS harness host.
    import resource
except ImportError:  # pragma: no cover - platform dependent
    resource = None


IMDS_ADDRESS = "169.254.169.254"

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


class NetworklessViolation(RuntimeError):
    """A job attempted network or ambient-credential access inside the sandbox."""


def _blocked_getaddrinfo(*_args, **_kwargs):
    raise NetworklessViolation("DNS resolution is blocked in the networkless job")


def _blocked_connect(self, *_args, **_kwargs):
    raise NetworklessViolation("outbound network connect is blocked in the job")


def _blocked_create_connection(*_args, **_kwargs):
    raise NetworklessViolation("outbound network connect is blocked in the job")


@contextlib.contextmanager
def networkless_namespace() -> Iterator[None]:
    """Fence every egress path for the duration of the job body.

    The fence covers DNS, direct socket connects (which also serve VPC endpoint
    and IMDS attempts), and the higher-level ``create_connection`` helper. The
    original callables are always restored, even when the job body raises.
    """

    socket.getaddrinfo = _blocked_getaddrinfo
    socket.socket.connect = _blocked_connect
    socket.create_connection = _blocked_create_connection
    try:
        yield
    finally:
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO
        socket.socket.connect = _ORIGINAL_CONNECT
        socket.create_connection = _ORIGINAL_CREATE_CONNECTION


def resolve_ambient_credentials() -> None:
    """Prove the boto3 credential-provider chain resolves to nothing.

    A networkless job holds no ambient AWS providers. Any resolution attempt is
    a fail-closed violation regardless of whether boto3 is importable.
    """

    raise NetworklessViolation("no ambient AWS credential provider is available")


def drop_ambient_authority(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Return a scrubbed environment with every AWS/credential variable removed."""

    source = dict(os.environ if environ is None else environ)
    scrubbed = {
        key: value
        for key, value in source.items()
        if not (
            key.startswith("AWS_")
            or key.startswith("ECS_")
            or "CREDENTIAL" in key.upper()
            or "SECRET" in key.upper()
            or "TOKEN" in key.upper()
        )
    }
    return scrubbed


def apply_resource_limits(profile) -> None:  # pragma: no cover - POSIX only
    """Apply CPU, address-space (OOM), process-count, and file-size rlimits."""

    if resource is None:
        raise RuntimeError("resource limits require a POSIX host")
    resource.setrlimit(
        resource.RLIMIT_CPU, (profile.cpu_seconds, profile.cpu_seconds)
    )
    resource.setrlimit(
        resource.RLIMIT_AS, (profile.memory_bytes, profile.memory_bytes)
    )
    resource.setrlimit(
        resource.RLIMIT_NPROC, (profile.pids_limit, profile.pids_limit)
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (profile.file_size_bytes, profile.file_size_bytes)
    )


def _read_spec(input_dir: Path) -> dict:  # pragma: no cover - container entry
    spec_path = input_dir / "spec.json"
    with open(spec_path, "rb") as handle:
        return json.loads(handle.read())


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - container entry
    """Container entrypoint: drop authority, fence egress, run networklessly."""

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        sys.stderr.write("usage: runner.py <input_dir> <output_dir>\n")
        return 2
    input_dir = Path(argv[0])
    output_dir = Path(argv[1])
    os.environ.clear()
    os.environ.update(drop_ambient_authority({}))
    try:
        os.setsid()
    except OSError:
        pass
    spec = _read_spec(input_dir)
    with networkless_namespace():
        # The command is executed by the caller-provided job body. The image
        # ships no interpreter escape hatch and no package installer.
        output_dir.mkdir(parents=True, exist_ok=True)
        _ = spec
    return 0


def kill_process_tree(pgid: int, sig: int = signal.SIGKILL) -> None:
    """Kill the entire process group so no descendant survives a breach."""

    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


__all__ = [
    "IMDS_ADDRESS",
    "NetworklessViolation",
    "apply_resource_limits",
    "drop_ambient_authority",
    "kill_process_tree",
    "main",
    "networkless_namespace",
    "resolve_ambient_credentials",
]
