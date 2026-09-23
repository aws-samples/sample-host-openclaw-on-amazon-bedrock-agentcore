#!/usr/bin/env python3
"""Run one shell command inside a live AgentCore Runtime session (OPERATOR TOOL).

Wraps the AgentCore Runtime data-plane API ``InvokeAgentRuntimeCommand`` via
boto3 (the AWS CLI does not expose this verb yet). The command runs in the same
microVM and filesystem as the OpenClaw session identified by the session id, so
``/mnt/workspace`` and the ``~/.openclaw`` symlink are visible exactly as the
agent left them. stdout/stderr are streamed back as they arrive and this script
exits with the command's own exit code. No LLM turn is involved.

SECURITY -- READ BEFORE USE
---------------------------
A command sent through this API spawns a FRESH bash directly inside the
container. That shell inherits the container's FULL AgentCore EXECUTION-ROLE
credentials (``AWS_CONTAINER_CREDENTIALS_*``). It does NOT pass through the
per-user ``scoped-credentials.js`` boundary that OpenClaw's own ``exec`` tool
runs behind, and it is NOT evaluated by Bedrock Guardrails or OpenClaw's tool
policy. Anyone who can call this API with a command string effectively has the
execution role.

Therefore:

* This is an OPERATOR / CI tool only. Grant ``bedrock-agentcore:InvokeAgentRuntimeCommand``
  to human operators or a CI/E2E principal -- never to the router or cron Lambda roles.
* It must NEVER be wired to a chat surface (Telegram/Slack/Feishu slash-commands)
  or fed any command string that originates from an end-user message.
* Every invocation is recorded in CloudTrail (caller, time, source IP) and the
  input command is logged to CloudWatch by the service. stdout/stderr are not.

See ``docs/execute-command.md`` for the operator workflow and the IAM statement.

Examples
--------
    # Health check in a NEW throwaway session
    python3 scripts/agentcore-exec.py --command 'node --version && df -h /mnt/workspace'

    # Inspect a specific user's persistent workspace (touches user data -- audit it)
    python3 scripts/agentcore-exec.py --session-id ses_user_9dc5386ba1124fbd_0a1b2c3d4e5f \\
        --command 'ls -la /mnt/workspace/.openclaw'

    # Machine-readable output for CI assertions
    python3 scripts/agentcore-exec.py --json --command 'test -L ~/.openclaw && echo ok'
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

# --- Constants (mirror the API contract) -------------------------------------

API_OPERATION = "invoke_agent_runtime_command"
MIN_SESSION_ID_LEN = 33
MAX_SESSION_ID_LEN = 256
MAX_COMMAND_BYTES = 65536  # 64 KB, per API reference
MIN_TIMEOUT = 1
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 300

# The API is rate limited to 25 TPS per account/region. This tool issues one
# request per run, so the limit only matters when many copies run in parallel
# (e.g. a CI matrix) -- ThrottlingException is retried with backoff below.
RETRYABLE_ERROR_CODES = frozenset({"RetryableConflictException", "ThrottlingException"})
DEFAULT_MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 20.0

# Process exit codes for conditions that are not the remote command's own code.
EXIT_USAGE = 2  # bad arguments / validation (argparse convention)
EXIT_AWS_ERROR = 1  # API or credential failure, nothing ran (or outcome unknown)
EXIT_TIMED_OUT = 124  # remote command hit --timeout (same as coreutils `timeout`)
EXIT_PLATFORM_ERROR = 125  # service reported exitCode -1 (platform error)

STATUS_COMPLETED = "COMPLETED"
STATUS_TIMED_OUT = "TIMED_OUT"

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_JSON = REPO_ROOT / "cdk.json"


class ExecError(Exception):
    """Fatal, user-facing error. ``exit_code`` is the process exit status."""

    def __init__(self, message: str, exit_code: int = EXIT_AWS_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


# --- Argument handling --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentcore-exec.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "OPERATOR TOOL: run one shell command inside a live AgentCore Runtime session\n"
            "via InvokeAgentRuntimeCommand and stream its stdout/stderr back.\n\n"
            "The command runs as a fresh bash in the container with the FULL execution-role\n"
            "credentials, bypassing OpenClaw's scoped-credentials boundary and Bedrock\n"
            "Guardrails. Never wire this to a chat surface. See docs/execute-command.md."
        ),
        epilog=(
            "exit status:\n"
            "  <n>   the remote command's own exit code (status COMPLETED)\n"
            "  124   remote command TIMED_OUT (hit --timeout)\n"
            "  125   service reported a platform error (exitCode -1)\n"
            "  1     AWS/API error -- the command did not run, or its outcome is unknown\n"
            "  2     invalid arguments\n"
        ),
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Shell command to run (1 byte .. 64 KB). Executed by a fresh bash in the container; "
        "there is no shell history or env carry-over between calls, so chain steps with '&&'.",
    )
    parser.add_argument(
        "--session-id",
        help=(
            "AgentCore runtime session id (33..256 chars). Pass the SAME id the router/cron "
            "use (ses_<user>_<hex>) to operate on that user's live session and /mnt/workspace. "
            "WHEN OMITTED, A NEW THROWAWAY SESSION IS CREATED (exec_<uuid>) -- it does NOT "
            "target any user's session and starts with an empty /mnt/workspace."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--runtime-arn",
        help="Full agent runtime ARN (arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>).",
    )
    target.add_argument(
        "--runtime-id",
        help="Agent runtime id (the part after 'runtime/'). The ARN is composed from --region "
        "and the STS caller account. Default: 'runtime_id' from cdk.json.",
    )
    parser.add_argument(
        "--qualifier",
        help="Runtime endpoint name (qualifier). Default: 'runtime_endpoint_id' from cdk.json, "
        "else the service default endpoint.",
    )
    parser.add_argument(
        "--region",
        help="AWS region. Default: AWS_REGION / AWS_DEFAULT_REGION env, then cdk.json 'region', "
        "then the boto3 session default.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds the service waits for the command ({MIN_TIMEOUT}..{MAX_TIMEOUT}, "
        f"default {DEFAULT_TIMEOUT}). On expiry the command is killed and status is TIMED_OUT.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Max attempts when the service answers RetryableConflictException (session "
        f"spinning up/down) or ThrottlingException (default {DEFAULT_MAX_ATTEMPTS}). "
        "A failure after output has started is never retried (the command is not idempotent).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON object on stdout (sessionId, runtimeArn, status, exitCode, "
        "stdout, stderr, attempts) instead of streaming. Exit status is unchanged.",
    )
    return parser


def validate_command(command: Optional[str]) -> str:
    """Enforce the API's 1 byte .. 64 KB bound. Returns the command unchanged."""
    if command is None or command.strip() == "":
        raise ExecError("--command must not be empty", EXIT_USAGE)
    size = len(command.encode("utf-8"))
    if size > MAX_COMMAND_BYTES:
        raise ExecError(
            f"--command is {size} bytes; the API limit is {MAX_COMMAND_BYTES} bytes (64 KB)",
            EXIT_USAGE,
        )
    return command


def validate_timeout(timeout: int) -> int:
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise ExecError(
            f"--timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds (got {timeout})",
            EXIT_USAGE,
        )
    return timeout


def validate_session_id(session_id: str) -> str:
    if not MIN_SESSION_ID_LEN <= len(session_id) <= MAX_SESSION_ID_LEN:
        raise ExecError(
            f"--session-id must be {MIN_SESSION_ID_LEN}..{MAX_SESSION_ID_LEN} characters "
            f"(got {len(session_id)})",
            EXIT_USAGE,
        )
    return session_id


def new_throwaway_session_id() -> str:
    """A fresh id that can never collide with router/cron ids (they start with 'ses_')."""
    session_id = f"exec_{uuid.uuid4().hex}"  # 5 + 32 = 37 chars >= 33
    assert len(session_id) >= MIN_SESSION_ID_LEN
    return session_id


def load_cdk_context(path: Path = CDK_JSON) -> Dict[str, Any]:
    """Best-effort read of cdk.json 'context'. Missing/invalid file -> {}."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("context", {}) or {}
    except (OSError, ValueError):
        return {}


def resolve_region(cli_region: Optional[str], ctx: Dict[str, Any], session_region: Optional[str]) -> str:
    region = (
        cli_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ctx.get("region")
        or session_region
    )
    if not region:
        raise ExecError(
            "Could not determine the AWS region. Pass --region or set AWS_REGION.", EXIT_USAGE
        )
    return region


def resolve_runtime_arn(
    runtime_arn: Optional[str],
    runtime_id: Optional[str],
    region: str,
    ctx: Dict[str, Any],
    get_account: Callable[[], str],
) -> str:
    """Return the runtime ARN. ``get_account`` is only called when composing from an id."""
    if runtime_arn:
        if not runtime_arn.startswith("arn:") or ":runtime/" not in runtime_arn:
            raise ExecError(f"--runtime-arn does not look like a runtime ARN: {runtime_arn}", EXIT_USAGE)
        return runtime_arn
    rid = runtime_id or ctx.get("runtime_id")
    if not rid or rid == "PLACEHOLDER":
        raise ExecError(
            "No runtime target: pass --runtime-arn or --runtime-id, or set 'runtime_id' in cdk.json "
            "(deploy.sh writes it after the first deployment).",
            EXIT_USAGE,
        )
    account = get_account()
    return f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{rid}"


# --- Streaming ----------------------------------------------------------------


class StreamResult:
    """Outcome of one InvokeAgentRuntimeCommand call."""

    def __init__(self) -> None:
        self.status: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.received_output = False
        self.attempts = 1
        self.stdout_parts: List[str] = []
        self.stderr_parts: List[str] = []

    @property
    def stdout(self) -> str:
        return "".join(self.stdout_parts)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_parts)


def consume_stream(
    stream: Any,
    result: StreamResult,
    out: Optional[TextIO],
    err: Optional[TextIO],
) -> StreamResult:
    """Walk the event stream, writing deltas through as they arrive.

    ``out``/``err`` may be None to collect only (used by --json). Errors that the
    service delivers *inside* the stream surface from boto3 as EventStreamError
    (a ClientError subclass) while iterating; they propagate to the caller.
    """
    for event in stream:
        chunk = event.get("chunk")
        if not chunk:
            # Non-chunk members are exception events; boto3 normally raises them
            # as EventStreamError before we get here. Be defensive anyway.
            for key, value in event.items():
                if key.endswith("Exception") or key == "runtimeClientError":
                    raise ExecError(f"{key}: {(value or {}).get('message', '')}".strip())
            continue
        if "contentStart" in chunk:
            result.received_output = True
            continue
        delta = chunk.get("contentDelta")
        if delta is not None:
            result.received_output = True
            text = delta.get("stdout")
            if text:
                result.stdout_parts.append(text)
                if out is not None:
                    out.write(text)
                    out.flush()
            text = delta.get("stderr")
            if text:
                result.stderr_parts.append(text)
                if err is not None:
                    err.write(text)
                    err.flush()
            continue
        stop = chunk.get("contentStop")
        if stop is not None:
            result.status = stop.get("status")
            result.exit_code = stop.get("exitCode")
    return result


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff: U(0, min(cap, base * 2**attempt))."""
    return rng() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))


def run_command(
    client: Any,
    *,
    runtime_arn: str,
    session_id: str,
    command: str,
    timeout: int,
    qualifier: Optional[str],
    out: Optional[TextIO],
    err: Optional[TextIO],
    log: TextIO,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> StreamResult:
    """Invoke the API with bounded retries and stream the result.

    Only ``RetryableConflictException`` / ``ThrottlingException`` are retried, and
    only while no output has been received yet (a retry re-runs the command, so
    it is never done once the command may have started). Returns the completed
    StreamResult with ``attempts`` set.
    """
    from botocore.exceptions import BotoCoreError, ClientError  # local: keep import-time light

    if not hasattr(client, API_OPERATION):
        raise ExecError(
            "The installed boto3/botocore does not know 'bedrock-agentcore' "
            f"{API_OPERATION}. Upgrade: pip install -U boto3 botocore "
            "(verified working with botocore >= 1.43.22)."
        )

    request: Dict[str, Any] = {
        "agentRuntimeArn": runtime_arn,
        "runtimeSessionId": session_id,
        "contentType": "application/json",
        "accept": "application/json",
        "body": {"command": command, "timeout": timeout},
    }
    if qualifier:
        request["qualifier"] = qualifier

    if max_attempts < 1:
        raise ExecError("--max-attempts must be >= 1", EXIT_USAGE)

    attempt = 0
    while True:
        result = StreamResult()
        try:
            response = getattr(client, API_OPERATION)(**request)
            consume_stream(response.get("stream") or [], result, out, err)
            result.attempts = attempt + 1
            if result.status is None:
                raise ExecError("Stream ended without a contentStop event; command outcome unknown")
            return result
        except ClientError as exc:
            code = _error_code(exc)
            message = (exc.response.get("Error") or {}).get("Message") or str(exc)
            if code in RETRYABLE_ERROR_CODES and not result.received_output and attempt + 1 < max_attempts:
                delay = backoff_delay(attempt)
                log.write(
                    f"[agentcore-exec] {code} (attempt {attempt + 1}/{max_attempts}); "
                    f"retrying in {delay:.1f}s\n"
                )
                log.flush()
                sleep(delay)
                attempt += 1
                continue
            if code in RETRYABLE_ERROR_CODES and result.received_output:
                raise ExecError(
                    f"{code} after output had started; not retrying because the command may "
                    f"have run: {message}"
                ) from exc
            if code == "AccessDeniedException":
                raise ExecError(
                    f"AccessDeniedException: {message}\n"
                    "Your principal needs bedrock-agentcore:InvokeAgentRuntimeCommand on "
                    f"{runtime_arn} and {runtime_arn}/* (see docs/execute-command.md)."
                ) from exc
            if code == "ResourceNotFoundException":
                raise ExecError(
                    f"ResourceNotFoundException: {message}\nCheck --runtime-arn/--runtime-id, "
                    "--qualifier and --region. Runtimes created before 2026-03-17 must be "
                    "redeployed before they accept commands."
                ) from exc
            raise ExecError(f"{code or exc.__class__.__name__}: {message}") from exc
        except BotoCoreError as exc:
            raise ExecError(f"{exc.__class__.__name__}: {exc}") from exc


# --- Entrypoint ---------------------------------------------------------------


def exit_status_for(result: StreamResult) -> int:
    if result.status == STATUS_TIMED_OUT:
        return EXIT_TIMED_OUT
    code = result.exit_code
    if code is None:
        return EXIT_AWS_ERROR
    if code < 0:
        return EXIT_PLATFORM_ERROR
    return code if code <= 255 else 255


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        command = validate_command(args.command)
        timeout = validate_timeout(args.timeout)
        session_id = validate_session_id(args.session_id) if args.session_id else None

        import boto3  # deferred: --help and validation never need AWS

        ctx = load_cdk_context()
        boto_session = boto3.session.Session()
        region = resolve_region(args.region, ctx, boto_session.region_name)

        def _account() -> str:
            # Only reached when composing an ARN from a runtime id.
            return boto_session.client("sts", region_name=region).get_caller_identity()["Account"]

        runtime_arn = resolve_runtime_arn(args.runtime_arn, args.runtime_id, region, ctx, _account)
        qualifier = args.qualifier or ctx.get("runtime_endpoint_id") or None

        throwaway = session_id is None
        if throwaway:
            session_id = new_throwaway_session_id()

        log = sys.stderr
        if throwaway:
            log.write(
                f"[agentcore-exec] no --session-id given: using NEW throwaway session {session_id} "
                "(not a user's session; empty /mnt/workspace)\n"
            )
        log.write(
            f"[agentcore-exec] runtime={runtime_arn} qualifier={qualifier or '(default)'} "
            f"session={session_id} timeout={timeout}s\n"
        )
        log.flush()

        client = boto_session.client("bedrock-agentcore", region_name=region)
        result = run_command(
            client,
            runtime_arn=runtime_arn,
            session_id=session_id,
            command=command,
            timeout=timeout,
            qualifier=qualifier,
            out=None if args.json else sys.stdout,
            err=None if args.json else sys.stderr,
            log=log,
            max_attempts=args.max_attempts,
        )
    except ExecError as exc:
        sys.stderr.write(f"agentcore-exec: error: {exc}\n")
        return exc.exit_code

    status = exit_status_for(result)
    if args.json:
        json.dump(
            {
                "sessionId": session_id,
                "throwawaySession": throwaway,
                "runtimeArn": runtime_arn,
                "qualifier": qualifier,
                "status": result.status,
                "exitCode": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "attempts": result.attempts,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    elif result.status == STATUS_TIMED_OUT:
        sys.stderr.write(f"[agentcore-exec] command TIMED_OUT after {timeout}s (exit {EXIT_TIMED_OUT})\n")
    elif result.exit_code is not None and result.exit_code < 0:
        sys.stderr.write(f"[agentcore-exec] platform error (service exitCode {result.exit_code})\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
