"""Brief audio: the cron Lambda sends the mp3 a brief uploaded to S3.

The agent can't attach audio itself (tts and read are denied, and the
contract keeps only text blocks), so a brief ends its reply with one line
``AUDIO_S3_KEY: <namespace>/<file>.mp3``. The Lambda strips that line, checks
the key belongs to the run's own namespace, and sends the object with
Telegram sendAudio after the text. Every failure falls back to text only, and
the marker line never reaches the user.
"""

import io
import json
import logging
from unittest.mock import patch

import pytest

from tests.test_runtime_routing import FakeRuntime, cron_event, cron_table, load_lambda
from tests.test_runtime_routing_synth import run_app

BUCKET = "test-user-files-bucket"
NAMESPACE = "telegram_123"  # ACTOR "telegram:123" with ':' -> '_'
KEY = f"{NAMESPACE}/morning-brief-2026-01-01-en.mp3"
AUDIO = b"ID3\x04\x00fake-mp3-bytes" * 64
BRIEF = "Good morning.\n\nHere is your brief."


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self, objects=None, head_error=None):
        self.objects = dict(objects or {})
        self.head_error = head_error
        self.calls = []

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if self.head_error:
            raise self.head_error
        if kwargs["Key"] not in self.objects:
            raise NoSuchKey("Not Found")
        size = self.objects[kwargs["Key"]]
        return {"ContentLength": size if isinstance(size, int) else len(size)}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        body = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(body if isinstance(body, bytes) else b"x" * body)}


class FakeTelegram:
    """urlopen stub that records each Bot API method and its request."""

    def __init__(self, fail_methods=()):
        self.requests = []
        self.fail_methods = set(fail_methods)

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        self.requests.append((method, req))
        if method in self.fail_methods:
            raise OSError("telegram down")
        return io.BytesIO(b'{"ok":true}')

    @property
    def methods(self):
        return [method for method, _ in self.requests]

    def texts(self):
        return [json.loads(req.data)["text"] for method, req in self.requests if method == "sendMessage"]

    def audio_requests(self):
        return [req for method, req in self.requests if method == "sendAudio"]


def _cron_module(s3):
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime()
    module.s3_client = s3
    module.USER_FILES_BUCKET = BUCKET
    module._get_telegram_token = lambda: "test-token"
    return module


def run_brief(response, s3=None, telegram=None, bucket=BUCKET):
    s3 = s3 if s3 is not None else FakeS3({KEY: AUDIO})
    telegram = telegram or FakeTelegram()
    module = _cron_module(s3)
    module.USER_FILES_BUCKET = bucket
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": response}])
    with patch.object(module.urllib_request, "urlopen", telegram):
        result = module.handler(cron_event(), None)
    assert result["statusCode"] == 200
    return telegram, s3


def assert_no_marker(texts):
    assert texts, "the brief text must still be delivered"
    for text in texts:
        assert "AUDIO_S3_KEY" not in text


def test_reply_without_marker_is_delivered_exactly_as_before():
    telegram, s3 = run_brief(BRIEF)
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []


def test_valid_key_sends_text_without_marker_then_the_audio():
    telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}\n")
    assert telegram.methods == ["sendMessage", "sendAudio"]
    (text,) = telegram.texts()
    assert "AUDIO_S3_KEY" not in text and "Here is your brief." in text
    assert [name for name, _ in s3.calls] == ["head_object", "get_object"]
    assert all(call == {"Bucket": BUCKET, "Key": KEY} for _, call in s3.calls)

    (req,) = telegram.audio_requests()
    assert req.full_url == "https://api.telegram.org/bottest-token/sendAudio"
    content_type = req.get_header("Content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = req.data
    assert body.endswith(b"--" + boundary + b"--\r\n")
    assert b'name="chat_id"\r\n\r\n123\r\n' in body
    assert b'name="audio"; filename="morning-brief-2026-01-01-en.mp3"' in body
    assert b"Content-Type: audio/mpeg\r\n\r\n" + AUDIO + b"\r\n" in body


def test_marker_in_content_blocks_with_markdown_decoration_is_parsed_and_stripped():
    blocks = json.dumps([{"type": "text", "text": f"{BRIEF}\n\n**AUDIO_S3_KEY:** `{KEY}`"}])
    telegram, _ = run_brief(blocks)
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())


def test_marker_not_on_the_last_line_is_still_stripped():
    telegram, _ = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}\nHave a good day.")
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())
    assert "Have a good day." in telegram.texts()[0]


@pytest.mark.parametrize("key", [
    "telegram_999/morning-brief.mp3",            # another user's namespace
    "telegram_1234/morning-brief.mp3",           # prefix collision without the '/'
    "morning-brief.mp3",                         # no namespace at all
    "/telegram_123/morning-brief.mp3",           # leading slash
    f"{NAMESPACE}/morning-brief.wav",            # wrong suffix
    f"{NAMESPACE}/morning-brief.mp3.exe",        # suffix not at the end
    f"{NAMESPACE}/morning-brief.MP3",            # IAM grant is case-sensitive
    f"{NAMESPACE}/../telegram_999/brief.mp3",    # traversal
    f"{NAMESPACE}/a..b.mp3",                     # any '..'
    f"{NAMESPACE}/",                             # nothing after the prefix
    f"{NAMESPACE}/.mp3",                         # empty name
    f"{NAMESPACE}/brief?.mp3",                   # outside the safe character set
])
def test_invalid_key_sends_no_audio_and_never_touches_s3(key, caplog):
    with caplog.at_level(logging.INFO):
        telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {key}")
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []
    assert_no_marker(telegram.texts())
    assert key not in caplog.text


def test_empty_marker_is_stripped_and_sends_no_audio():
    telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY:")
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []
    assert_no_marker(telegram.texts())


def test_object_over_50_mb_is_not_downloaded_or_sent():
    s3 = FakeS3({KEY: 50 * 1024 * 1024 + 1})
    telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}", s3=s3)
    assert telegram.methods == ["sendMessage"]
    assert [name for name, _ in s3.calls] == ["head_object"]
    assert_no_marker(telegram.texts())


def test_object_of_exactly_50_mb_is_sent():
    s3 = FakeS3({KEY: 50 * 1024 * 1024})
    telegram, _ = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}", s3=s3)
    assert telegram.methods == ["sendMessage", "sendAudio"]


def test_missing_object_sends_text_only_and_logs_without_the_key(caplog):
    with caplog.at_level(logging.INFO):
        telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}", s3=FakeS3({}))
    assert telegram.methods == ["sendMessage"]
    assert [name for name, _ in s3.calls] == ["head_object"]
    assert_no_marker(telegram.texts())
    assert "NoSuchKey" in caplog.text
    assert KEY not in caplog.text and "not found" not in caplog.text.lower()


def test_sendaudio_failure_still_delivers_the_text():
    telegram = FakeTelegram(fail_methods={"sendAudio"})
    telegram, _ = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}", telegram=telegram)
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())


def test_unconfigured_bucket_strips_marker_and_sends_text_only():
    telegram, s3 = run_brief(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}", bucket="")
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []
    assert_no_marker(telegram.texts())


def test_audio_is_skipped_when_the_lambda_is_out_of_time():
    s3 = FakeS3({KEY: AUDIO})
    telegram = FakeTelegram()
    module = _cron_module(s3)
    with patch.object(module.urllib_request, "urlopen", telegram):
        module.deliver_brief_audio("telegram", "123", KEY, "telegram:123", deadline=module.time.monotonic() + 1)
    assert telegram.methods == []
    assert s3.calls == []


@pytest.mark.parametrize("channel", ["slack", "feishu"])
def test_non_telegram_channel_strips_marker_and_sends_no_audio(channel):
    s3 = FakeS3({KEY: AUDIO})
    module = _cron_module(s3)
    sent = []
    with patch.object(module, "send_slack_message", lambda target, text, token: sent.append(text)), \
            patch.object(module, "send_feishu_message", lambda target, text: sent.append(text)), \
            patch.object(module, "_get_slack_tokens", lambda: ("token", "")):
        response, key = module.split_audio_marker(f"{BRIEF}\nAUDIO_S3_KEY: {KEY}")
        module.deliver_response(channel, "target", response)
        module.deliver_brief_audio(channel, "target", key, "telegram:123", deadline=module.time.monotonic() + 600)
    assert sent and "AUDIO_S3_KEY" not in sent[0]
    assert s3.calls == []


def test_deliver_response_strips_a_marker_on_any_path():
    """Even a caller that skips the brief path must not leak the marker."""
    module = _cron_module(FakeS3({KEY: AUDIO}))
    telegram = FakeTelegram()
    with patch.object(module.urllib_request, "urlopen", telegram):
        module.deliver_response("telegram", "123", f"{BRIEF}\nAUDIO_S3_KEY: {KEY}")
    assert telegram.methods == ["sendMessage"]
    assert_no_marker(telegram.texts())


def test_cron_role_reads_only_mp3_objects_in_the_user_files_bucket(tmp_path):
    templates = run_app(tmp_path)
    cron = templates["OpenClawCron.template.json"]["Resources"]
    statements = [
        statement
        for resource in cron.values() if resource["Type"] == "AWS::IAM::Policy"
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]
    s3_statements = [
        s for s in statements
        if any(str(action).startswith("s3:") for action in
               (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))
    ]
    assert len(s3_statements) == 1, s3_statements
    (grant,) = s3_statements
    assert grant["Effect"] == "Allow"
    assert grant["Action"] == "s3:GetObject"
    resource = json.dumps(grant["Resource"])
    assert resource.endswith('/*/*.mp3"]]}'), resource
    assert "UserFilesBucket" in resource
    assert '"*"' not in resource

    function = next(
        r["Properties"] for r in cron.values()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"].get("FunctionName") == "openclaw-cron-executor"
    )
    bucket_env = json.dumps(function["Environment"]["Variables"]["USER_FILES_BUCKET"])
    assert "UserFilesBucket" in bucket_env
