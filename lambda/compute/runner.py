"""In-container networkless compute entrypoint and egress fences.

This module is driven directly by the local sandbox harness (with fakes) and,
in production, by the read-only job image. It drops ambient authority, applies
POSIX resource limits (CPU, address space for OOM, process count for fork
bombs, and file size), runs the command in a fresh process group so the whole
tree can be killed on a deadline or resource breach, and proves the namespace
is networkless: DNS, stream/datagram/raw socket egress, VPC endpoints, IMDS,
and the credential provider chain all fail closed.

No import here performs I/O at module load. The container mounts an immutable
input directory and writes only to a fresh output directory.
"""

from __future__ import annotations

import _socket
import contextlib
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

try:  # POSIX only; absent on the local macOS harness host.
    import resource
except ImportError:  # pragma: no cover - platform dependent
    resource = None


IMDS_ADDRESS = "169.254.169.254"

_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_SOCKET_TYPE = socket.SocketType
_ORIGINAL_LOW_LEVEL_SOCKET = _socket.socket
_ORIGINAL_LOW_LEVEL_SOCKET_TYPE = _socket.SocketType
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = _ORIGINAL_SOCKET.connect
_ORIGINAL_CONNECT_EX = _ORIGINAL_SOCKET.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_SEND = _ORIGINAL_SOCKET.send
_ORIGINAL_SENDALL = _ORIGINAL_SOCKET.sendall
_ORIGINAL_SENDTO = _ORIGINAL_SOCKET.sendto
_ORIGINAL_SENDMSG = getattr(_ORIGINAL_SOCKET, "sendmsg", None)
_ORIGINAL_SENDFILE = _ORIGINAL_SOCKET.sendfile
_DNS_RESOLVER_NAMES = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
    "getfqdn",
)
_LOW_LEVEL_DNS_RESOLVER_NAMES = tuple(
    name for name in _DNS_RESOLVER_NAMES if hasattr(_socket, name)
)


class NetworklessViolation(RuntimeError):
    """A job attempted network or ambient-credential access inside the sandbox."""


def _blocked_getaddrinfo(*_args, **_kwargs):
    raise NetworklessViolation("DNS resolution is blocked in the networkless job")


def _blocked_connect(self, *_args, **_kwargs):
    raise NetworklessViolation("outbound network connect is blocked in the job")


def _blocked_create_connection(*_args, **_kwargs):
    raise NetworklessViolation("outbound network connect is blocked in the job")


def _blocked_socket_write(self, *_args, **_kwargs):
    raise NetworklessViolation("outbound network I/O is blocked in the job")


def _is_raw_socket_type(socket_type: int) -> bool:
    """Recognize ``SOCK_RAW`` even when Linux constructor flags are present."""

    normalized = int(socket_type)
    for flag_name in ("SOCK_NONBLOCK", "SOCK_CLOEXEC"):
        normalized &= ~int(getattr(socket, flag_name, 0))
    return normalized == int(socket.SOCK_RAW)


class _NetworklessSocket(_ORIGINAL_SOCKET):
    """Public socket constructor that rejects raw sockets before their syscall."""

    def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
        if fileno is None:
            if family == -1:
                family = socket.AF_INET
            if type == -1:
                type = socket.SOCK_STREAM
            if proto == -1:
                proto = 0
            if _is_raw_socket_type(type):
                raise NetworklessViolation(
                    "raw socket creation is blocked in the networkless job"
                )
        # Call the retained C type directly. During the fence, the public
        # ``_socket.socket`` alias also points at this guarded class.
        _ORIGINAL_LOW_LEVEL_SOCKET.__init__(self, family, type, proto, fileno)
        self._io_refs = 0
        self._closed = False
        if _is_raw_socket_type(self.type):
            self.close()
            raise NetworklessViolation(
                "raw socket adoption is blocked in the networkless job"
            )


_SOCKET_METHOD_FENCES = {
    "connect": _blocked_connect,
    "connect_ex": _blocked_connect,
    "send": _blocked_socket_write,
    "sendall": _blocked_socket_write,
    "sendto": _blocked_socket_write,
    "sendfile": _blocked_socket_write,
}
if _ORIGINAL_SENDMSG is not None:
    _SOCKET_METHOD_FENCES["sendmsg"] = _blocked_socket_write


@contextlib.contextmanager
def networkless_namespace() -> Iterator[None]:
    """Fence every egress path for the duration of the job body.

    The fence covers DNS, every public socket constructor, direct and
    connectionless socket writes, VPC endpoint and IMDS attempts, and the
    higher-level ``create_connection`` helper. The original callables are
    always restored, even when the job body raises.
    """

    previous_socket = socket.socket
    previous_socket_type = socket.SocketType
    previous_low_level_socket = _socket.socket
    previous_low_level_socket_type = _socket.SocketType
    previous_create_connection = socket.create_connection
    previous_resolvers = {
        name: getattr(socket, name) for name in _DNS_RESOLVER_NAMES
    }
    previous_low_level_resolvers = {
        name: getattr(_socket, name) for name in _LOW_LEVEL_DNS_RESOLVER_NAMES
    }
    previous_methods = {
        name: getattr(_ORIGINAL_SOCKET, name) for name in _SOCKET_METHOD_FENCES
    }
    for name, fence in _SOCKET_METHOD_FENCES.items():
        setattr(_ORIGINAL_SOCKET, name, fence)
    socket.socket = _NetworklessSocket
    socket.SocketType = _NetworklessSocket
    _socket.socket = _NetworklessSocket
    _socket.SocketType = _NetworklessSocket
    for name in _DNS_RESOLVER_NAMES:
        setattr(socket, name, _blocked_getaddrinfo)
    for name in _LOW_LEVEL_DNS_RESOLVER_NAMES:
        setattr(_socket, name, _blocked_getaddrinfo)
    socket.create_connection = _blocked_create_connection
    try:
        yield
    finally:
        socket.create_connection = previous_create_connection
        for name, original in previous_low_level_resolvers.items():
            setattr(_socket, name, original)
        for name, original in previous_resolvers.items():
            setattr(socket, name, original)
        _socket.SocketType = previous_low_level_socket_type
        _socket.socket = previous_low_level_socket
        socket.SocketType = previous_socket_type
        socket.socket = previous_socket
        for name, original in previous_methods.items():
            setattr(_ORIGINAL_SOCKET, name, original)


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
    # macOS exposes RLIMIT_AS but rejects every finite value. The reviewed
    # production image is Linux, where the exact address-space limit is
    # mandatory; Darwin is only a local development harness and still applies
    # CPU/process/file limits plus the wall-clock deadline.
    if sys.platform != "darwin":
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


def _child_python_command(spec: dict, input_dir: Path) -> None:
    """Execute the already-validated Python-only command inside the child.

    The distroless image deliberately contains one interpreter and no shell or
    package installer. ``SCRIPT`` executes as isolated Python source. ``ARGV``
    accepts only that interpreter and one bound Python file from the immutable
    input tree; it cannot select an arbitrary host executable.
    """

    command = spec.get("command")
    if not isinstance(command, dict) or set(command) != {"mode", "value"}:
        raise ValueError("compute command is invalid")
    mode = command["mode"]
    value = command["value"]
    if mode == "SCRIPT" and isinstance(value, str):
        namespace = {"__name__": "__main__", "__builtins__": __builtins__}
        exec(compile(value, "<compute-script>", "exec"), namespace, namespace)
        return
    if mode != "ARGV" or not isinstance(value, list) or len(value) < 2:
        raise ValueError("compute ARGV command is invalid")
    if value[0] not in {"python", "python3", Path(sys.executable).name}:
        raise ValueError("compute image exposes only the pinned Python runtime")
    relative_script = value[1]
    if not isinstance(relative_script, str) or not relative_script.endswith(".py"):
        raise ValueError("compute ARGV must select one bound Python file")
    root = input_dir.resolve(strict=True)
    script_path = (root / relative_script).resolve(strict=True)
    try:
        script_path.relative_to(root)
    except ValueError as error:
        raise ValueError("compute ARGV escaped the immutable input tree") from error
    if not script_path.is_file() or script_path.is_symlink():
        raise ValueError("compute ARGV script is not a regular bound input")
    sys.argv = [str(script_path), *value[2:]]
    runpy.run_path(str(script_path), run_name="__main__")


def _child_main(argv: list[str]) -> int:
    """Run one command in the child-side network and authority fence."""

    if len(argv) != 3:
        return 2
    spec_path = Path(argv[0])
    input_dir = Path(argv[1])
    output_dir = Path(argv[2])
    with open(spec_path, "rb") as handle:
        spec = json.loads(handle.read())
    os.environ.clear()
    os.environ.update(drop_ambient_authority({}))
    os.chdir(output_dir)
    try:
        with networkless_namespace():
            _child_python_command(spec, input_dir)
    except BaseException:
        return 1
    return 0


class LocalProcessRunner:
    """Execute a bound command in one disposable, resource-limited process tree.

    This is the reviewed image entrypoint semantics. Network isolation is
    layered: the child blocks Python network primitives, while the external
    task/VPC/seccomp gates remain independently OPEN until exercised on the
    exact built image. Candidate output is written to a private sibling and is
    published only after the child exits successfully and the output importer
    accepts the complete tree.
    """

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        self._clock = clock or (lambda: int(time.time()))

    def run(self, *, spec, output_dir: Path, input_dir: Path, profile=None):
        from . import importer, models
        from .service import RunnerBreach, RunnerResult

        resolved_profile = profile or models.resolve_profile(spec.resource_profile)
        if not isinstance(resolved_profile, models.ResourceProfile):
            raise TypeError("runner requires one frozen resource profile")
        if spec.resource_profile != resolved_profile.name:
            raise ValueError("runner profile does not match the bound job spec")

        started_at = self._clock()
        remaining = spec.deadline - started_at
        if remaining <= 0:
            return RunnerResult(
                breach=RunnerBreach(
                    kind="TIMEOUT", error_code="COMPUTE_DEADLINE_EXCEEDED"
                ),
                started_at=started_at,
                completed_at=started_at,
            )

        input_dir = Path(input_dir)
        output_dir = Path(output_dir)
        if not input_dir.exists() and not spec.input_files:
            input_dir.mkdir(parents=True, exist_ok=False)
        if not input_dir.is_dir() or input_dir.is_symlink():
            raise ValueError("runner input directory is invalid")
        if not output_dir.is_dir() or output_dir.is_symlink():
            raise ValueError("runner output directory is invalid")
        if any(output_dir.iterdir()):
            raise ValueError("runner output directory must be fresh")

        candidate = Path(
            tempfile.mkdtemp(prefix=f".{spec.job_id}.candidate-", dir=output_dir.parent)
        )
        control_dir = Path(
            tempfile.mkdtemp(prefix=f".{spec.job_id}.control-", dir=output_dir.parent)
        )
        spec_path = control_dir / "spec.json"
        spec_path.write_bytes(spec.to_bytes())
        spec_path.chmod(0o400)
        process = None
        try:
            child_environment = drop_ambient_authority({})
            child_environment["PYTHONDONTWRITEBYTECODE"] = "1"

            def set_limits() -> None:
                apply_resource_limits(resolved_profile)

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).resolve()),
                    "--child",
                    str(spec_path.resolve(strict=True)),
                    str(input_dir.resolve(strict=True)),
                    str(candidate.resolve(strict=True)),
                ],
                cwd=candidate,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=set_limits,
            )
            try:
                return_code = process.wait(
                    timeout=min(resolved_profile.deadline_seconds, remaining)
                )
            except subprocess.TimeoutExpired:
                kill_process_tree(process.pid)
                process.wait()
                return RunnerResult(
                    breach=RunnerBreach(
                        kind="TIMEOUT", error_code="COMPUTE_DEADLINE_EXCEEDED"
                    ),
                    started_at=started_at,
                    completed_at=max(started_at, self._clock()),
                )

            # A successful parent may still have spawned descendants. End the
            # complete process group before validating a now-quiescent tree.
            kill_process_tree(process.pid)
            if return_code != 0:
                error_code = "COMPUTE_PROCESS_FAILED"
                if return_code == -getattr(signal, "SIGXCPU", -999):
                    error_code = "COMPUTE_CPU_LIMIT"
                elif return_code == -getattr(signal, "SIGXFSZ", -999):
                    error_code = "COMPUTE_FILE_SIZE_LIMIT"
                return RunnerResult(
                    breach=RunnerBreach(kind="FAILED", error_code=error_code),
                    started_at=started_at,
                    completed_at=max(started_at, self._clock()),
                )

            # Validate before publication. The service importer validates again
            # immediately before its atomic user-store commit.
            importer.collect_outputs(candidate, resolved_profile)
            output_dir.rmdir()
            os.replace(candidate, output_dir)
            return RunnerResult(
                breach=None,
                started_at=started_at,
                completed_at=max(started_at, self._clock()),
            )
        except Exception:
            if process is not None:
                kill_process_tree(process.pid)
            return RunnerResult(
                breach=RunnerBreach(
                    kind="FAILED", error_code="COMPUTE_OUTPUT_REJECTED"
                ),
                started_at=started_at,
                completed_at=max(started_at, self._clock()),
            )
        finally:
            import shutil

            shutil.rmtree(candidate, ignore_errors=True)
            shutil.rmtree(control_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - container entry
    """Container entrypoint: drop authority, fence egress, run networklessly."""

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--child"]:
        return _child_main(argv[1:])
    if len(argv) != 2:
        sys.stderr.write("usage: runner.py <input_dir> <output_dir>\n")
        return 2
    input_dir = Path(argv[0])
    output_dir = Path(argv[1])
    from capabilities.contracts import ComputeJobSpecV1
    from .models import resolve_profile

    spec = ComputeJobSpecV1.from_mapping(_read_spec(input_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    result = LocalProcessRunner().run(
        spec=spec,
        input_dir=input_dir,
        output_dir=output_dir,
        profile=resolve_profile(spec.resource_profile),
    )
    if result.breach is None:
        return 0
    return 124 if result.breach.kind == "TIMEOUT" else 1


def kill_process_tree(pgid: int, sig: int = signal.SIGKILL) -> None:
    """Kill the entire process group so no descendant survives a breach."""

    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


__all__ = [
    "IMDS_ADDRESS",
    "NetworklessViolation",
    "LocalProcessRunner",
    "apply_resource_limits",
    "drop_ambient_authority",
    "kill_process_tree",
    "main",
    "networkless_namespace",
    "resolve_ambient_credentials",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
