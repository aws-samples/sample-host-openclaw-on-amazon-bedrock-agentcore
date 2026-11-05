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
elif args[:2] == ["dynamodb", "get-item"] and os.environ.get("FAKE_USER_ID"):
    print(os.environ["FAKE_USER_ID"])
elif args[:2] == ["dynamodb", "query"] and os.environ.get("FAKE_QUERY_ITEMS"):
    print(os.environ["FAKE_QUERY_ITEMS"])
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
@pytest.mark.parametrize("command", ["add", "remove", "disable", "enable"])
def test_allowlist_rejects_keys_that_would_break_the_json_before_any_aws_call(env, command, key):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert not any(c["tool"] == "aws" for c in _calls(root))


@pytest.mark.parametrize("key", [
    "telegram:123456", "slack:U0XXXXXXXX2", "feishu:ou_0123abcd", "whatsapp:15551234567",
    "discord:123456789012345678", "line:U0123456789abcdef0123456789abcdef",
])
@pytest.mark.parametrize("command", ["disable", "enable"])
def test_allowlist_disable_and_enable_accept_every_documented_channel_key(env, command, key):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode == 0, result.stderr
    lookup, update = (c["args"] for c in _calls(root) if c["tool"] == "aws")
    assert json.loads(lookup[lookup.index("--key") + 1]) == {
        "PK": {"S": f"CHANNEL#{key}"}, "SK": {"S": "PROFILE"}}
    assert json.loads(update[update.index("--key") + 1]) == {
        "PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "PROFILE"}}


def test_allowlist_list_still_works_without_a_profile(env):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, "list")
    assert result.returncode == 0, result.stderr
    assert [c["args"][:2] for c in _calls(root)] == [["dynamodb", "scan"]]


def test_allowlist_whois_lists_channel_actors_for_a_user(env):
    root, environment = env
    environment["FAKE_QUERY_ITEMS"] = json.dumps({"Items": [
        {"PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "PROFILE"},
         "enrollmentActorId": {"S": "telegram:123"}},
        {"PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "CHANNEL#telegram:123"}},
        {"PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "CHANNEL#slack:U456"}},
        {"PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "SESSION"}},
    ]})
    result = _run("manage-allowlist.sh", environment, "whois", "user_0123abcd")
    assert result.returncode == 0, result.stderr
    query = next(c["args"] for c in _calls(root) if c["tool"] == "aws")
    assert query[:2] == ["dynamodb", "query"]
    values = json.loads(query[query.index("--expression-attribute-values") + 1])
    assert values[":pk"] == {"S": "USER#user_0123abcd"}
    assert "telegram:123" in result.stdout
    assert "slack:U456" in result.stdout
    assert "SESSION" not in result.stdout


def test_allowlist_whois_rejects_a_non_user_id_before_any_aws_call(env):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, "whois", "telegram:123")
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert not any(c["tool"] == "aws" for c in _calls(root))


# ------------------------------------------- manage-allowlist: Slack channels, Feishu groups


@pytest.mark.parametrize("key", ["slack_channel:C0123ABCDEF", "slack_channel:G0123PRIV", "feishu_group:oc_0123abcd"])
@pytest.mark.parametrize("command", ["add", "remove"])
def test_allowlist_accepts_slack_channel_and_feishu_group_keys(env, command, key):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode == 0, result.stderr
    call = next(c["args"] for c in _calls(root) if c["tool"] == "aws")
    document = json.loads(call[call.index("--item" if command == "add" else "--key") + 1])
    assert document["PK"] == {"S": f"ALLOW#{key}"}
    assert document["SK"] == {"S": "ALLOW"}
    if command == "add":
        assert document["channelKey"] == {"S": key}
        assert 'channel_identity_mode\nis "channel"' in result.stdout
    else:
        assert f"The next message from {key} is ignored." in result.stdout
        assert "disable" not in result.stdout


@pytest.mark.parametrize("key", [
    "slack_channel:general", "slack_channel:#general", "slack_channel:U0123USER", "slack_channel:D0123DM",
    "slack_channel:c0123abc", "slack_channel:C1", "feishu_group:ou_0123abcd", "feishu_group:0123abcd",
    "feishu_group:oc_", "feishu_group:oc_0123-abcd",
])
@pytest.mark.parametrize("command", ["add", "remove", "disable", "enable"])
def test_allowlist_rejects_malformed_conversation_keys_before_any_aws_call(env, command, key):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, command, key)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert not any(c["tool"] == "aws" for c in _calls(root))


@pytest.mark.parametrize("command", ["disable", "enable"])
def test_allowlist_disable_and_enable_accept_a_conversation_key(env, command):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    result = _run("manage-allowlist.sh", environment, command, "slack_channel:C0123ABCDEF")
    assert result.returncode == 0, result.stderr
    lookup, update = (c["args"] for c in _calls(root) if c["tool"] == "aws")
    assert json.loads(lookup[lookup.index("--key") + 1]) == {
        "PK": {"S": "CHANNEL#slack_channel:C0123ABCDEF"}, "SK": {"S": "PROFILE"}}
    assert json.loads(update[update.index("--key") + 1]) == {
        "PK": {"S": "USER#user_0123abcd"}, "SK": {"S": "PROFILE"}}


@pytest.mark.parametrize("key", ["slack:U0XXXXXXXX2", "telegram_group:-1001234567890"])
def test_allowlist_add_and_remove_output_is_unchanged_for_other_keys(env, key):
    _, environment = env
    added = _run("manage-allowlist.sh", environment, "add", key)
    removed = _run("manage-allowlist.sh", environment, "remove", key)
    assert added.returncode == removed.returncode == 0
    assert added.stdout == f"Adding {key} to allowlist...\nDone. {key} is now allowed.\n"
    assert "channel_identity_mode" not in added.stdout
    assert f"run '{_REPO / 'scripts' / 'manage-allowlist.sh'} disable {key}'" in removed.stdout


def test_allowlist_usage_names_the_conversation_keys(env):
    _, environment = env
    result = _run("manage-allowlist.sh", environment)
    assert result.returncode != 0
    assert "slack_channel:<channel_id>" in result.stdout
    assert "feishu_group:<chat_id>" in result.stdout


# ------------------------------------- manage-allowlist: daily cost limit (per_user_daily_cost_usd)


def _aws_calls(root):
    return [c["args"] for c in _calls(root) if c["tool"] == "aws"]


@pytest.mark.parametrize("key", [
    "telegram:123456", "slack:U0XXXXXXXX2", "telegram_group:-1001234567890", "slack_channel:C0123ABCDEF",
    "feishu_group:oc_0123abcd",
])
@pytest.mark.parametrize("usd, stored", [("5", "5"), ("2.50", "2.50"), ("0", "0"), ("10000", "10000"),
                                         ("0.000001", "0.000001")])
def test_set_cap_writes_the_override_of_the_bound_user(env, key, usd, stored):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    result = _run("manage-allowlist.sh", environment, "set-cap", key, usd)
    assert result.returncode == 0, result.stderr
    lookup, put = _aws_calls(root)
    assert json.loads(lookup[lookup.index("--key") + 1]) == {"PK": {"S": f"CHANNEL#{key}"}, "SK": {"S": "PROFILE"}}
    assert put[:2] == ["dynamodb", "put-item"]
    item = json.loads(put[put.index("--item") + 1])
    assert set(item) == {"PK", "SK", "dailyCostUsd", "setAt"}
    assert item["PK"] == {"S": "USAGE#user_0123abcd"} and item["SK"] == {"S": "CAP"}
    assert item["dailyCostUsd"] == {"N": stored}
    assert "per_user_daily_cost_usd" in result.stdout
    if usd == "0":
        assert "has no daily cost limit" in result.stdout


@pytest.mark.parametrize("usd", ["-1", "abc", "1e3", "10000.01", "10001", "5 USD", "", '5"}', ".5", "5.",
                                 "0x10", "5.0000001", " 5"])
def test_set_cap_rejects_an_amount_that_is_not_plain_usd_before_any_aws_call(env, usd):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    result = _run("manage-allowlist.sh", environment, "set-cap", "telegram:123456", usd)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert _aws_calls(root) == []


@pytest.mark.parametrize("key", ['telegram:1", "PK": {"S": "USER#x', "telegram:", "slack_channel:general",
                                 "feishu_group:ou_0123abcd"])
@pytest.mark.parametrize("args", [("set-cap", "5"), ("clear-cap",), ("usage",)])
def test_cap_commands_reject_malformed_keys_before_any_aws_call(env, key, args):
    root, environment = env
    command, *rest = args
    result = _run("manage-allowlist.sh", environment, command, key, *rest)
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert _aws_calls(root) == []


@pytest.mark.parametrize("args", [("set-cap", "telegram:123456", "5"), ("clear-cap", "telegram:123456"),
                                  ("usage", "telegram:123456")])
def test_cap_commands_refuse_an_unregistered_identity(env, args):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, *args)
    assert result.returncode != 0
    assert "is not registered" in result.stderr
    assert [call[:2] for call in _aws_calls(root)] == [["dynamodb", "get-item"]]


def test_set_cap_without_an_amount_prints_the_usage(env):
    root, environment = env
    result = _run("manage-allowlist.sh", environment, "set-cap", "telegram:123456")
    assert result.returncode != 0
    assert "set-cap <channel:user_id> <usd>" in result.stdout
    assert _aws_calls(root) == []


def test_clear_cap_deletes_the_override(env):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    result = _run("manage-allowlist.sh", environment, "clear-cap", "slack_channel:C0123ABCDEF")
    assert result.returncode == 0, result.stderr
    lookup, delete = _aws_calls(root)
    assert json.loads(lookup[lookup.index("--key") + 1])["PK"] == {"S": "CHANNEL#slack_channel:C0123ABCDEF"}
    assert delete[:2] == ["dynamodb", "delete-item"]
    assert json.loads(delete[delete.index("--key") + 1]) == {"PK": {"S": "USAGE#user_0123abcd"}, "SK": {"S": "CAP"}}


def test_usage_prints_todays_item_and_the_override(env):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    today = subprocess.run(["date", "-u", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()
    environment["FAKE_QUERY_ITEMS"] = json.dumps({"Items": [
        {"PK": {"S": "USAGE#user_0123abcd"}, "SK": {"S": "CAP"}, "dailyCostUsd": {"N": "2.5"},
         "setAt": {"S": "2026-11-01T00:00:00Z"}},
        {"PK": {"S": "USAGE#user_0123abcd"}, "SK": {"S": "DAY#2000-01-01"}, "costUsd": {"N": "99"}},
        {"PK": {"S": "USAGE#user_0123abcd"}, "SK": {"S": f"DAY#{today}"}, "costUsd": {"N": "1.24"},
         "cronCostUsd": {"N": "0.1"}, "calls": {"N": "37"}, "cronCalls": {"N": "4"},
         "inputTokens": {"N": "400100"}, "outputTokens": {"N": "12245"}},
    ]})
    result = _run("manage-allowlist.sh", environment, "usage", "telegram:123456")
    assert result.returncode == 0, result.stderr
    lookup, query = _aws_calls(root)
    assert query[:2] == ["dynamodb", "query"]
    assert query[query.index("--key-condition-expression") + 1] == "PK = :pk AND SK BETWEEN :cap AND :day"
    assert json.loads(query[query.index("--expression-attribute-values") + 1]) == {
        ":pk": {"S": "USAGE#user_0123abcd"}, ":cap": {"S": "CAP"}, ":day": {"S": f"DAY#{today}"}}
    assert result.stdout.splitlines() == [
        f"Usage of user_0123abcd on {today} (UTC), estimated:",
        "  cost:        $1.24 (scheduled tasks: $0.1)",
        "  model calls: 37 (scheduled tasks: 4)",
        "  tokens:      400100 in, 12245 out",
        "  limit:       $2.5 (override, set 2026-11-01T00:00:00Z)",
    ]


@pytest.mark.parametrize("items, limit", [
    ([], "  limit:       per_user_daily_cost_usd from cdk.json (no override)"),
    ([{"PK": {"S": "USAGE#user_0123abcd"}, "SK": {"S": "CAP"}, "dailyCostUsd": {"N": "0"},
       "setAt": {"S": "2026-11-01T00:00:00Z"}}], "  limit:       none (override 0, set 2026-11-01T00:00:00Z)"),
])
def test_usage_of_a_day_without_calls(env, items, limit):
    root, environment = env
    environment["FAKE_USER_ID"] = "user_0123abcd"
    environment["FAKE_QUERY_ITEMS"] = json.dumps({"Items": items})
    result = _run("manage-allowlist.sh", environment, "usage", "telegram_group:-1001234567890")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[1:] == ["  cost:        $0 (scheduled tasks: $0)", "  model calls: 0 (scheduled tasks: 0)",
                         "  tokens:      0 in, 0 out", limit]


def test_allowlist_usage_names_the_cap_commands(env):
    _, environment = env
    result = _run("manage-allowlist.sh", environment)
    assert result.returncode != 0
    for command in ("set-cap <channel:user_id> <usd>", "clear-cap <channel:user_id>", "usage   <channel:user_id>"):
        assert command in result.stdout
