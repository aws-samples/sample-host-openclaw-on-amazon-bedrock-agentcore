"""Unit tests for scripts/agentcore-exec.py (operator CLI for InvokeAgentRuntimeCommand).

Zero AWS calls: the bedrock-agentcore client is a MagicMock that returns fabricated
event streams. Runs with plain unittest or pytest:

    python3 -m unittest tests.test_agentcore_exec -v
    pytest tests/test_agentcore_exec.py -v
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agentcore-exec.py"
_spec = importlib.util.spec_from_file_location("agentcore_exec", _SCRIPT)
ax = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ax)

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/openclaw_agent-TEST"
SESSION_ID = "exec_" + "0" * 32


# --- helpers -----------------------------------------------------------------


def _client_error(code, message="boom", status=409):
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "InvokeAgentRuntimeCommand",
    )


def _stream(*events):
    """Fabricate an event stream: deltas are (stdout, stderr) tuples; the stop event is a dict."""
    return [{"chunk": e} for e in events]


def _ok_stream(stdout_chunks=("hello\n",), stderr_chunks=(), exit_code=0, status="COMPLETED"):
    events = [{"contentStart": {}}]
    for s in stdout_chunks:
        events.append({"contentDelta": {"stdout": s}})
    for s in stderr_chunks:
        events.append({"contentDelta": {"stderr": s}})
    events.append({"contentStop": {"exitCode": exit_code, "status": status}})
    return _stream(*events)


def _client(side_effect):
    client = MagicMock()
    client.invoke_agent_runtime_command = MagicMock(side_effect=side_effect)
    return client


def _run(client, **overrides):
    out, err, log = io.StringIO(), io.StringIO(), io.StringIO()
    kwargs = dict(
        runtime_arn=RUNTIME_ARN,
        session_id=SESSION_ID,
        command="echo hello",
        timeout=30,
        qualifier="DEFAULT",
        out=out,
        err=err,
        log=log,
        sleep=lambda _s: None,
    )
    kwargs.update(overrides)
    result = ax.run_command(client, **kwargs)
    return result, out.getvalue(), err.getvalue(), log.getvalue()


# --- request shape -------------------------------------------------------------


class TestRequestShape(unittest.TestCase):
    def test_request_matches_api_contract(self):
        client = _client([{"stream": _ok_stream()}])
        _run(client)
        client.invoke_agent_runtime_command.assert_called_once()
        req = client.invoke_agent_runtime_command.call_args.kwargs
        self.assertEqual(req["agentRuntimeArn"], RUNTIME_ARN)
        self.assertEqual(req["runtimeSessionId"], SESSION_ID)
        self.assertEqual(req["qualifier"], "DEFAULT")
        self.assertEqual(req["body"], {"command": "echo hello", "timeout": 30})
        self.assertEqual(req["contentType"], "application/json")

    def test_qualifier_omitted_when_none(self):
        client = _client([{"stream": _ok_stream()}])
        _run(client, qualifier=None)
        self.assertNotIn("qualifier", client.invoke_agent_runtime_command.call_args.kwargs)

    def test_request_validates_against_installed_botocore_model(self):
        """The dict we send must satisfy botocore's own input shape (no network)."""
        import botocore.session
        from botocore import validate

        model = botocore.session.get_session().get_service_model("bedrock-agentcore")
        if "InvokeAgentRuntimeCommand" not in model.operation_names:
            self.skipTest("installed botocore predates InvokeAgentRuntimeCommand")
        op = model.operation_model("InvokeAgentRuntimeCommand")
        client = _client([{"stream": _ok_stream()}])
        _run(client)
        req = client.invoke_agent_runtime_command.call_args.kwargs
        report = validate.ParamValidator().validate(req, op.input_shape)
        self.assertFalse(report.has_errors(), report.generate_report())

    def test_missing_operation_gives_upgrade_hint(self):
        client = MagicMock(spec=[])  # no invoke_agent_runtime_command attribute
        with self.assertRaises(ax.ExecError) as cm:
            _run(client)
        self.assertIn("Upgrade", str(cm.exception))
        self.assertIn("boto3", str(cm.exception))


# --- streaming / exit codes ------------------------------------------------------


class TestStreaming(unittest.TestCase):
    def test_streams_stdout_and_stderr_in_order(self):
        client = _client([{"stream": _stream(
            {"contentStart": {}},
            {"contentDelta": {"stdout": "one\n"}},
            {"contentDelta": {"stderr": "warn\n"}},
            {"contentDelta": {"stdout": "two\n"}},
            {"contentStop": {"exitCode": 0, "status": "COMPLETED"}},
        )}])
        result, out, err, _ = _run(client)
        self.assertEqual(out, "one\ntwo\n")
        self.assertEqual(err, "warn\n")
        self.assertEqual(result.stdout, "one\ntwo\n")
        self.assertEqual(result.stderr, "warn\n")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(ax.exit_status_for(result), 0)

    def test_json_mode_collects_without_passthrough(self):
        client = _client([{"stream": _ok_stream(("a",), ("b",))}])
        result, _, _, _ = _run(client, out=None, err=None)
        self.assertEqual((result.stdout, result.stderr), ("a", "b"))

    def test_non_zero_exit_code_propagates(self):
        client = _client([{"stream": _ok_stream((), ("ls: no such file\n",), exit_code=2)}])
        result, out, err, _ = _run(client)
        self.assertEqual(out, "")
        self.assertIn("no such file", err)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(ax.exit_status_for(result), 2)

    def test_timed_out_is_distinguished_from_non_zero_exit(self):
        client = _client([{"stream": _ok_stream(("partial",), exit_code=137, status="TIMED_OUT")}])
        result, out, _, _ = _run(client)
        self.assertEqual(out, "partial")
        self.assertEqual(result.status, "TIMED_OUT")
        self.assertEqual(ax.exit_status_for(result), ax.EXIT_TIMED_OUT)
        self.assertNotEqual(ax.exit_status_for(result), 137)

    def test_platform_error_exit_code_minus_one(self):
        client = _client([{"stream": _ok_stream((), exit_code=-1)}])
        result, _, _, _ = _run(client)
        self.assertEqual(ax.exit_status_for(result), ax.EXIT_PLATFORM_ERROR)

    def test_exit_code_clamped_to_255(self):
        r = ax.StreamResult()
        r.status, r.exit_code = "COMPLETED", 300
        self.assertEqual(ax.exit_status_for(r), 255)

    def test_stream_without_stop_is_an_error(self):
        client = _client([{"stream": _stream({"contentStart": {}}, {"contentDelta": {"stdout": "x"}})}])
        with self.assertRaises(ax.ExecError) as cm:
            _run(client)
        self.assertIn("contentStop", str(cm.exception))

    def test_in_stream_exception_member_is_surfaced(self):
        client = _client([{"stream": [{"chunk": {"contentStart": {}}},
                                      {"validationException": {"message": "bad command"}}]}])
        with self.assertRaises(ax.ExecError) as cm:
            _run(client)
        self.assertIn("validationException: bad command", str(cm.exception))


# --- retries -------------------------------------------------------------------------


class TestRetries(unittest.TestCase):
    def test_retryable_conflict_then_success(self):
        client = _client([
            _client_error("RetryableConflictException", "session starting"),
            _client_error("RetryableConflictException", "session starting"),
            {"stream": _ok_stream(("ready\n",))},
        ])
        sleeps = []
        result, out, _, log = _run(client, sleep=sleeps.append)
        self.assertEqual(out, "ready\n")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(client.invoke_agent_runtime_command.call_count, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 <= s <= ax.BACKOFF_CAP_SECONDS for s in sleeps))
        self.assertEqual(log.count("retrying in"), 2)

    def test_throttling_is_retried(self):
        client = _client([
            _client_error("ThrottlingException", "Rate exceeded", 429),
            {"stream": _ok_stream()},
        ])
        result, _, _, _ = _run(client)
        self.assertEqual(result.attempts, 2)

    def test_retries_are_bounded(self):
        client = _client([_client_error("RetryableConflictException")] * 10)
        with self.assertRaises(ax.ExecError) as cm:
            _run(client, max_attempts=3)
        self.assertEqual(client.invoke_agent_runtime_command.call_count, 3)
        self.assertIn("RetryableConflictException", str(cm.exception))

    def test_no_retry_once_output_started(self):
        """A mid-stream failure must not re-run a possibly non-idempotent command."""

        def failing_stream():
            yield {"chunk": {"contentStart": {}}}
            yield {"chunk": {"contentDelta": {"stdout": "started\n"}}}
            raise _client_error("ThrottlingException", "mid-stream", 429)

        client = _client([{"stream": failing_stream()}, {"stream": _ok_stream()}])
        with self.assertRaises(ax.ExecError) as cm:
            _run(client)
        self.assertIn("not retrying", str(cm.exception))
        self.assertEqual(client.invoke_agent_runtime_command.call_count, 1)

    def test_access_denied_is_not_retried_and_names_the_action(self):
        client = _client([_client_error("AccessDeniedException", "nope", 403)])
        with self.assertRaises(ax.ExecError) as cm:
            _run(client)
        self.assertEqual(client.invoke_agent_runtime_command.call_count, 1)
        self.assertIn("bedrock-agentcore:InvokeAgentRuntimeCommand", str(cm.exception))
        self.assertEqual(cm.exception.exit_code, ax.EXIT_AWS_ERROR)

    def test_backoff_is_full_jitter_and_capped(self):
        self.assertEqual(ax.backoff_delay(0, rng=lambda: 1.0), 1.0)
        self.assertEqual(ax.backoff_delay(3, rng=lambda: 1.0), 8.0)
        self.assertEqual(ax.backoff_delay(10, rng=lambda: 1.0), ax.BACKOFF_CAP_SECONDS)
        self.assertEqual(ax.backoff_delay(10, rng=lambda: 0.0), 0.0)


# --- validation / resolution -------------------------------------------------------


class TestValidation(unittest.TestCase):
    def test_empty_command_refused(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ax.ExecError) as cm:
                ax.validate_command(bad)
            self.assertEqual(cm.exception.exit_code, ax.EXIT_USAGE)

    def test_command_over_64kb_refused(self):
        ax.validate_command("x" * ax.MAX_COMMAND_BYTES)  # exactly at the limit is fine
        with self.assertRaises(ax.ExecError):
            ax.validate_command("x" * (ax.MAX_COMMAND_BYTES + 1))
        with self.assertRaises(ax.ExecError):  # bytes, not characters
            ax.validate_command("\u00e9" * (ax.MAX_COMMAND_BYTES // 2 + 1))

    def test_command_is_passed_verbatim_not_interpolated(self):
        cmd = "echo '$(whoami)' && ls; rm -- \"$X\""
        client = _client([{"stream": _ok_stream()}])
        _run(client, command=cmd)
        self.assertEqual(client.invoke_agent_runtime_command.call_args.kwargs["body"]["command"], cmd)

    def test_timeout_range(self):
        self.assertEqual(ax.validate_timeout(1), 1)
        self.assertEqual(ax.validate_timeout(3600), 3600)
        for bad in (0, -5, 3601):
            with self.assertRaises(ax.ExecError):
                ax.validate_timeout(bad)

    def test_session_id_length(self):
        self.assertEqual(ax.validate_session_id("a" * 33), "a" * 33)
        with self.assertRaises(ax.ExecError):
            ax.validate_session_id("a" * 32)
        with self.assertRaises(ax.ExecError):
            ax.validate_session_id("a" * 257)

    def test_throwaway_session_id_is_long_enough_and_distinct(self):
        a, b = ax.new_throwaway_session_id(), ax.new_throwaway_session_id()
        self.assertGreaterEqual(len(a), 33)
        self.assertTrue(a.startswith("exec_"))
        self.assertNotEqual(a, b)

    def test_resolve_runtime_arn_prefers_explicit_arn_without_sts(self):
        sts = MagicMock(side_effect=AssertionError("STS must not be called"))
        arn = ax.resolve_runtime_arn(RUNTIME_ARN, None, "us-west-2", {"runtime_id": "other"}, sts)
        self.assertEqual(arn, RUNTIME_ARN)

    def test_resolve_runtime_arn_from_id_uses_account_callback(self):
        arn = ax.resolve_runtime_arn(None, "rt-1", "ap-southeast-2", {}, lambda: "111122223333")
        self.assertEqual(arn, "arn:aws:bedrock-agentcore:ap-southeast-2:111122223333:runtime/rt-1")

    def test_resolve_runtime_arn_falls_back_to_cdk_json(self):
        arn = ax.resolve_runtime_arn(None, None, "us-west-2", {"runtime_id": "from-cdk"}, lambda: "1" * 12)
        self.assertTrue(arn.endswith(":runtime/from-cdk"))

    def test_resolve_runtime_arn_rejects_placeholder_and_missing(self):
        for ctx in ({}, {"runtime_id": "PLACEHOLDER"}):
            with self.assertRaises(ax.ExecError) as cm:
                ax.resolve_runtime_arn(None, None, "us-west-2", ctx, lambda: "1" * 12)
            self.assertEqual(cm.exception.exit_code, ax.EXIT_USAGE)

    def test_resolve_runtime_arn_rejects_malformed(self):
        with self.assertRaises(ax.ExecError):
            ax.resolve_runtime_arn("not-an-arn", None, "us-west-2", {}, lambda: "1" * 12)

    def test_resolve_region_precedence(self):
        with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}, clear=False):
            self.assertEqual(ax.resolve_region("us-east-1", {"region": "x"}, "y"), "us-east-1")
            self.assertEqual(ax.resolve_region(None, {"region": "x"}, "y"), "eu-west-1")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ax.resolve_region(None, {"region": "x"}, "y"), "x")
            self.assertEqual(ax.resolve_region(None, {"region": ""}, "y"), "y")
            with self.assertRaises(ax.ExecError):
                ax.resolve_region(None, {}, None)

    def test_load_cdk_context_tolerates_missing_file(self):
        self.assertEqual(ax.load_cdk_context(Path("/nonexistent/cdk.json")), {})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"context": {"runtime_id": "abc"}}, fh)
        try:
            self.assertEqual(ax.load_cdk_context(Path(fh.name)), {"runtime_id": "abc"})
        finally:
            os.unlink(fh.name)


# --- main() end to end with a patched boto3 -------------------------------------------


class TestMain(unittest.TestCase):
    def _run_main(self, argv, stream_side_effect):
        client = _client(stream_side_effect)
        session = MagicMock()
        session.region_name = "us-west-2"
        session.client = MagicMock(return_value=client)
        fake_boto3 = MagicMock()
        fake_boto3.session.Session.return_value = session
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"boto3": fake_boto3}), \
                patch.object(sys, "stdout", out), patch.object(sys, "stderr", err), \
                patch.object(ax.time, "sleep", lambda _s: None):
            rc = ax.main(argv)
        return rc, out.getvalue(), err.getvalue(), session

    def test_main_streams_and_returns_command_exit_code(self):
        rc, out, err, session = self._run_main(
            ["--command", "false", "--runtime-arn", RUNTIME_ARN],
            [{"stream": _ok_stream(("out\n",), ("err\n",), exit_code=1)}],
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "out\n")
        self.assertIn("err\n", err)
        self.assertIn("NEW throwaway session exec_", err)
        # explicit ARN: STS must never be created
        for call in session.client.call_args_list:
            self.assertNotEqual(call.args[0], "sts")

    def test_main_json_output(self):
        rc, out, _, _ = self._run_main(
            ["--json", "--command", "true", "--runtime-arn", RUNTIME_ARN, "--session-id", SESSION_ID],
            [{"stream": _ok_stream(("ok\n",))}],
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["sessionId"], SESSION_ID)
        self.assertFalse(payload["throwawaySession"])
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["exitCode"], 0)
        self.assertEqual(payload["stdout"], "ok\n")
        self.assertEqual(payload["attempts"], 1)

    def test_main_timed_out_exit_124(self):
        rc, _, err, _ = self._run_main(
            ["--command", "sleep 999", "--runtime-arn", RUNTIME_ARN, "--timeout", "5"],
            [{"stream": _ok_stream((), exit_code=137, status="TIMED_OUT")}],
        )
        self.assertEqual(rc, ax.EXIT_TIMED_OUT)
        self.assertIn("TIMED_OUT", err)

    def test_main_runtime_id_composes_arn_via_sts(self):
        client = _client([{"stream": _ok_stream()}])
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        session = MagicMock()
        session.region_name = "us-west-2"
        session.client = MagicMock(side_effect=lambda name, **kw: sts if name == "sts" else client)
        fake_boto3 = MagicMock()
        fake_boto3.session.Session.return_value = session
        with patch.dict(sys.modules, {"boto3": fake_boto3}), \
                patch.object(sys, "stdout", io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
            rc = ax.main(["--command", "true", "--runtime-id", "openclaw_agent-TEST", "--region", "us-west-2"])
        self.assertEqual(rc, 0)
        self.assertEqual(client.invoke_agent_runtime_command.call_args.kwargs["agentRuntimeArn"], RUNTIME_ARN)

    def test_main_usage_errors_exit_2_without_touching_aws(self):
        fake_boto3 = MagicMock()
        with patch.dict(sys.modules, {"boto3": fake_boto3}), patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(ax.main(["--command", "   ", "--runtime-arn", RUNTIME_ARN]), ax.EXIT_USAGE)
            self.assertEqual(ax.main(["--command", "x", "--timeout", "0", "--runtime-arn", RUNTIME_ARN]), ax.EXIT_USAGE)
            self.assertEqual(ax.main(["--command", "x", "--session-id", "short", "--runtime-arn", RUNTIME_ARN]), ax.EXIT_USAGE)
        fake_boto3.session.Session.return_value.client.assert_not_called()

    def test_main_reports_aws_error_exit_1(self):
        rc, _, err, _ = self._run_main(
            ["--command", "true", "--runtime-arn", RUNTIME_ARN],
            [_client_error("ResourceNotFoundException", "no such runtime", 404)],
        )
        self.assertEqual(rc, ax.EXIT_AWS_ERROR)
        self.assertIn("ResourceNotFoundException", err)
        self.assertIn("2026-03-17", err)


if __name__ == "__main__":
    unittest.main()
