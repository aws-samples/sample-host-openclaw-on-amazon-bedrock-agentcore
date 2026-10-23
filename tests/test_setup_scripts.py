"""Offline tests for the operator setup scripts (setup-telegram.sh, setup-feishu.sh,
setup-slack.sh, manage-allowlist.sh).

No AWS calls: the scripts run against fake aws/curl executables that record
their argv, so the tests can check that secrets stay off command lines and
that user-supplied IDs cannot break the DynamoDB JSON documents.

    pytest tests/test_setup_scripts.py -v
"""

import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent

_TOKEN = "123456789:AAFakeBotTokenForTests_only-0123456789"
_WEBHOOK_SECRET = "FakeWebhookSecret0123456789abcdefFakeWebhookSecret0123456789abcd"
_API_URL = "https://abcdefghij.execute-api.us-west-2.amazonaws.com/"

_FAKE = r'''
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
stdin = sys.stdin.read() if tool == "curl" else ""
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "args": args, "stdin": stdin}) + "\n")
if tool == "curl":
    target = os.environ.get("CURL_TARGET")
    if target:
        import subprocess
        config = stdin.replace("https://api.telegram.org", target)
        sys.exit(subprocess.run([os.environ["REAL_CURL"], *args], input=config, text=True).returncode)
    print('{"ok":true,"result":true}')
    sys.exit(0)
if args[:2] == ["cloudformation", "describe-stacks"]:
    print(os.environ["FAKE_API_URL"])
elif args[:2] == ["secretsmanager", "get-secret-value"]:
    secret_id = args[args.index("--secret-id") + 1]
    print({"openclaw/webhook-secret": os.environ["FAKE_WEBHOOK_SECRET"],
           "openclaw/channels/telegram": os.environ["FAKE_TOKEN"]}[secret_id])
elif args[:2] == ["secretsmanager", "update-secret"]:
    value = args[args.index("--secret-string") + 1]
    if value.startswith("file://"):
        shutil.copy(value[len("file://"):], os.environ["CAPTURED_SECRET"])
'''


@pytest.fixture
def env(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "fake"
    fake.write_text(f"#!{sys.executable}\n" + _FAKE)
    fake.chmod(0o755)
    for name in ("aws", "curl"):
        (binaries / name).symlink_to(fake)
    environment = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CALL_LOG": str(tmp_path / "calls.jsonl"),
        "CAPTURED_SECRET": str(tmp_path / "secret.json"),
        "FAKE_API_URL": _API_URL, "FAKE_TOKEN": _TOKEN, "FAKE_WEBHOOK_SECRET": _WEBHOOK_SECRET,
        "TMPDIR": str(tmp_path),
    }
    environment.pop("AWS_PROFILE", None)
    return tmp_path, environment


def _calls(root):
    path = root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _run(script, environment, *args, stdin=""):
    return subprocess.run(["bash", str(_REPO / "scripts" / script), *args], env=environment,
                          input=stdin, capture_output=True, text=True)


def _argv_text(root):
    return "\n".join(" ".join(c["args"]) for c in _calls(root))


# --------------------------------------------------------------------------- telegram


def test_telegram_token_and_webhook_secret_stay_off_the_command_line(env):
    root, environment = env
    result = _run("setup-telegram.sh", environment, stdin="123456789\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _TOKEN not in _argv_text(root)
    assert _WEBHOOK_SECRET not in _argv_text(root)
    curl = next(c for c in _calls(root) if c["tool"] == "curl")
    assert curl["args"] == ["-s", "--config", "-"]
    assert f"/bot{_TOKEN}/setWebhook" in curl["stdin"]
    assert f"secret_token={_WEBHOOK_SECRET}" in curl["stdin"]


@pytest.mark.parametrize("name, value", [
    ("FAKE_TOKEN", 'not-a-token"\nurl = "https://example.invalid'),
    ("FAKE_WEBHOOK_SECRET", "secret&url=https://example.invalid/"),
    ("FAKE_API_URL", 'https://example.invalid/" -o /tmp/x'),
])
def test_telegram_refuses_values_that_would_change_the_curl_config(env, name, value):
    root, environment = env
    environment[name] = value
    result = _run("setup-telegram.sh", environment, stdin="123456789\n")
    assert result.returncode != 0
    assert not any(c["tool"] == "curl" for c in _calls(root))


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.seen.append((self.path, self.headers.get("Content-Type"), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true,"result":true}')

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not shutil.which("curl"), reason="needs a real curl to parse the config")
def test_telegram_config_is_a_valid_set_webhook_form_post_for_real_curl(env):
    root, environment = env
    environment["REAL_CURL"] = shutil.which("curl")
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment["CURL_TARGET"] = f"http://127.0.0.1:{server.server_port}"
    _Recorder.seen.clear()
    try:
        result = _run("setup-telegram.sh", environment, stdin="123456789\n")
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _Recorder.seen == [(
        f"/bot{_TOKEN}/setWebhook", "application/x-www-form-urlencoded",
        f"url={_API_URL}webhook/telegram&secret_token={_WEBHOOK_SECRET}",
    )]


# --------------------------------------------------------------------------- feishu


def test_feishu_credentials_stay_off_the_command_line_and_are_valid_json(env):
    root, environment = env
    secret = 'app"secret\\with}quotes'
    stdin = "\n".join(["", "cli_app_id", secret, "verif-token", "encrypt-key", "ou_abc123", ""])
    result = _run("setup-feishu.sh", environment, stdin=stdin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert secret not in _argv_text(root)
    assert "verif-token" not in _argv_text(root)
    assert secret not in result.stdout
    assert json.loads((root / "secret.json").read_text()) == {
        "appId": "cli_app_id", "appSecret": secret,
        "verificationToken": "verif-token", "encryptKey": "encrypt-key",
    }
    assert not list(root.glob("openclaw-feishu.*"))


def test_feishu_rejects_an_open_id_that_would_break_the_item_json(env):
    root, environment = env
    stdin = "\n".join(["", "cli_app_id", "s", "t", "k", 'ou_x"}, "PK": {"S": "USER#y', "y", ""])
    result = _run("setup-feishu.sh", environment, stdin=stdin)
    assert result.returncode != 0
    assert not any(c["args"][:2] == ["dynamodb", "put-item"] for c in _calls(root))


# --------------------------------------------------------------------------- slack


def test_slack_rejects_a_member_id_that_would_break_the_item_json(env):
    root, environment = env
    result = _run("setup-slack.sh", environment, stdin='\nU1"}, "x": {"S": "y\ny\n')
    assert result.returncode != 0
    assert not any(c["args"][:2] == ["dynamodb", "put-item"] for c in _calls(root))


# --------------------------------------------------------------------------- manage-allowlist


@pytest.mark.parametrize("key", [
    "telegram:123456", "slack:U0XXXXXXXX2", "feishu:ou_0123abcd", "whatsapp:15551234567",
    "discord:123456789012345678", "line:U0123456789abcdef0123456789abcdef",
])
@pytest.mark.parametrize("command", ["add", "remove"])
def test_allowlist_accepts_every_documented_channel_key(env, command, key):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode == 0, result.stderr
    call = next(c["args"] for c in _calls(root) if c["tool"] == "aws")
    document = json.loads(call[call.index("--item" if command == "add" else "--key") + 1])
    assert document["PK"] == {"S": f"ALLOW#{key}"}
    assert document["SK"] == {"S": "ALLOW"}


@pytest.mark.parametrize("key", [
    'telegram:1", "PK": {"S": "USER#x',
    "telegram:1 2", "telegram:", ":123", "Telegram:123", "telegram:1\\u0022", "telegram:a{b}",
])
@pytest.mark.parametrize("command", ["add", "remove"])
def test_allowlist_rejects_keys_that_would_break_the_json_before_any_aws_call(env, command, key):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert not any(c["tool"] == "aws" for c in _calls(root))


def test_allowlist_list_still_works_without_a_profile(env):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, "list")
    assert result.returncode == 0, result.stderr
    assert [c["args"][:2] for c in _calls(root)] == [["dynamodb", "scan"]]
