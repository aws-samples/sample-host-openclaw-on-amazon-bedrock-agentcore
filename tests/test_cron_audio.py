"""V1 cron Lambda: send the brief's mp3 from S3 with Telegram sendAudio.

Backport of #150 (cron side). A brief ends its reply with one line
``BRIEF_AUDIO: <namespace>/<file>.mp3``. The Lambda always strips that line,
checks the key belongs to the run's own namespace, and sends the object with
Telegram sendAudio after the text. Every failure falls back to text only, and
the marker line never reaches the user.
"""

import io
import json
import logging
import re
from unittest.mock import patch

import pytest

from tests.cron_v1_helpers import READY, FakeRuntime, cron_event, load_cron_lambda, run_app

BUCKET = "test-user-files-bucket"
NAMESPACE = "telegram_123"  # ACTOR "telegram:123" with ':' -> '_'
KEY = f"{NAMESPACE}/morning-brief-2026-01-01-en.mp3"
AUDIO = b"ID3\x04\x00fake-mp3-bytes" * 64
BRIEF = "Good morning.\n\nHere is your brief."
MARKER = "BRIEF_AUDIO"
UNREADABLE_WARNING = "Brief audio skipped: marker line present but its key could not be read"


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
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


def _cron_module(s3, bucket=BUCKET):
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime()
    module.s3_client = s3
    module.USER_FILES_BUCKET = bucket
    module._get_telegram_token = lambda: "test-token"
    return module


def run_brief(response, s3=None, telegram=None, bucket=BUCKET):
    s3 = s3 if s3 is not None else FakeS3({KEY: AUDIO})
    telegram = telegram or FakeTelegram()
    module = _cron_module(s3, bucket)
    module.agentcore_client = FakeRuntime([READY, {"response": response}])
    with patch.object(module.urllib_request, "urlopen", telegram):
        result = module.handler(cron_event(), None)
    assert result["statusCode"] == 200
    return telegram, s3


def assert_no_marker(texts):
    assert texts, "the brief text must still be delivered"
    for text in texts:
        assert MARKER not in text


def test_marker_label_is_not_a_secret_name_to_openclaw_redaction():
    """OpenClaw redacts values after labels that look like secret names (see #150)."""
    module = load_cron_lambda()
    env_assignment = re.compile(r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)\b\s*[=:]\s*")
    prefilter = re.compile(
        r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|COOKIE|SIGNATURE|CREDENTIAL|CARD|CVC|CVV|PAYMENT", re.I)
    assert module.AUDIO_MARKER_LABEL == MARKER
    assert env_assignment.search("AUDIO_S3_KEY: x"), "control: the old label matches"
    assert not env_assignment.search(f"{MARKER}: {KEY}")
    assert not prefilter.search(f"{MARKER}: {KEY}")


def test_reply_without_marker_is_delivered_exactly_as_before():
    telegram, s3 = run_brief(BRIEF)
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []


def test_valid_key_sends_text_without_marker_then_the_audio():
    telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {KEY}\n")
    assert telegram.methods == ["sendMessage", "sendAudio"]
    (text,) = telegram.texts()
    assert MARKER not in text and "Here is your brief." in text
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
    blocks = json.dumps([{"type": "text", "text": f"{BRIEF}\n\n**{MARKER}:** `{KEY}`"}])
    telegram, _ = run_brief(blocks)
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())


def test_marker_not_on_the_last_line_is_still_stripped():
    telegram, _ = run_brief(f"{BRIEF}\n{MARKER}: {KEY}\nHave a good day.")
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())
    assert "Have a good day." in telegram.texts()[0]


def test_old_audio_s3_key_label_is_not_a_marker():
    module = load_cron_lambda()
    text = f"{BRIEF}\nAUDIO_S3_KEY: {KEY}"
    assert module.split_audio_marker(text) == (text, None)


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
    "telegr\u2026.mp3",                          # OpenClaw's masked form of a long value
])
def test_invalid_key_sends_no_audio_and_never_touches_s3(key, caplog):
    with caplog.at_level(logging.INFO):
        telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {key}")
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []
    assert_no_marker(telegram.texts())
    assert "Brief audio skipped: key rejected" in caplog.text
    assert key not in caplog.text


@pytest.mark.parametrize("line", [
    f"{MARKER}:",                                # empty
    f"{MARKER}: ***",                            # redacted value
    f"**{MARKER}:** `***`",                      # redacted, with markdown
    f"{MARKER}: {KEY} and more words",           # not a single key
])
def test_unreadable_marker_is_stripped_and_logged_without_its_text(line, caplog):
    with caplog.at_level(logging.INFO):
        telegram, s3 = run_brief(f"{BRIEF}\n{line}")
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []
    assert_no_marker(telegram.texts())
    assert caplog.text.count(UNREADABLE_WARNING) == 1
    assert KEY not in caplog.text and "***" not in caplog.text


def test_reply_without_marker_logs_no_audio_warning(caplog):
    with caplog.at_level(logging.INFO):
        run_brief(BRIEF)
    assert "Brief audio" not in caplog.text


def test_object_over_50_mb_is_not_downloaded_or_sent():
    s3 = FakeS3({KEY: 50 * 1024 * 1024 + 1})
    telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", s3=s3)
    assert telegram.methods == ["sendMessage"]
    assert [name for name, _ in s3.calls] == ["head_object"]
    assert_no_marker(telegram.texts())


def test_object_of_exactly_50_mb_is_sent():
    s3 = FakeS3({KEY: 50 * 1024 * 1024})
    telegram, _ = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", s3=s3)
    assert telegram.methods == ["sendMessage", "sendAudio"]


def test_empty_object_is_not_sent():
    telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", s3=FakeS3({KEY: 0}))
    assert telegram.methods == ["sendMessage"]
    assert [name for name, _ in s3.calls] == ["head_object"]


def test_missing_object_sends_text_only_and_logs_without_the_key(caplog):
    with caplog.at_level(logging.INFO):
        telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", s3=FakeS3({}))
    assert telegram.methods == ["sendMessage"]
    assert [name for name, _ in s3.calls] == ["head_object"]
    assert_no_marker(telegram.texts())
    assert "NoSuchKey" in caplog.text
    assert KEY not in caplog.text and "not found" not in caplog.text.lower()


def test_sendaudio_failure_still_delivers_the_text():
    telegram = FakeTelegram(fail_methods={"sendAudio"})
    telegram, _ = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", telegram=telegram)
    assert telegram.methods == ["sendMessage", "sendAudio"]
    assert_no_marker(telegram.texts())


def test_unconfigured_bucket_strips_marker_and_sends_text_only():
    telegram, s3 = run_brief(f"{BRIEF}\n{MARKER}: {KEY}", bucket="")
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


def test_handler_audio_deadline_comes_from_the_lambda_context():
    """With little time left in the invocation, the audio is skipped, the text is not."""
    s3 = FakeS3({KEY: AUDIO})
    telegram = FakeTelegram()
    module = _cron_module(s3)
    module.agentcore_client = FakeRuntime([READY, {"response": f"{BRIEF}\n{MARKER}: {KEY}"}])

    class Context:
        @staticmethod
        def get_remaining_time_in_millis():
            return 5_000

    with patch.object(module.urllib_request, "urlopen", telegram):
        assert module.handler(cron_event(), Context())["statusCode"] == 200
    assert telegram.methods == ["sendMessage"]
    assert s3.calls == []


@pytest.mark.parametrize("channel", ["slack", "feishu"])
def test_non_telegram_channel_strips_marker_and_sends_no_audio(channel):
    s3 = FakeS3({KEY: AUDIO})
    module = _cron_module(s3)
    module.agentcore_client = FakeRuntime([READY, {"response": f"{BRIEF}\n{MARKER}: {KEY}"}])
    sent = []
    with patch.object(module, "send_slack_message", lambda target, text, token: sent.append(text)), \
            patch.object(module, "send_feishu_message", lambda target, text: sent.append(text)), \
            patch.object(module, "_get_slack_tokens", lambda: ("token", "")):
        assert module.handler(cron_event(channel=channel, channelTarget="target"), None)["statusCode"] == 200
    assert sent and MARKER not in sent[0]
    assert s3.calls == []


def test_deliver_response_strips_a_marker_on_any_path():
    """Even a caller that skips the brief path must not leak the marker."""
    module = _cron_module(FakeS3({KEY: AUDIO}))
    telegram = FakeTelegram()
    with patch.object(module.urllib_request, "urlopen", telegram):
        module.deliver_response("telegram", "123", f"{BRIEF}\n{MARKER}: {KEY}")
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
