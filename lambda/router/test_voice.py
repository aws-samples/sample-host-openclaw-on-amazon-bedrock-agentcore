"""Voice messages transcribed with Amazon Transcribe (voice.py), off by default.

The S3 and Transcribe clients are stubs from the injected client factory, the
transcript is read from a stub fetch, and the clock is fake, so the polling is
deterministic and nothing goes over the network or to AWS.
"""

import contextlib
import json
import logging
import os
import re
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib  # noqa: E402

index = importlib.import_module("index")
import line_channel  # noqa: E402
import voice  # noqa: E402
import whatsapp  # noqa: E402
from test_telegram_album import FakeClock  # noqa: E402

BUCKET = "openclaw-user-files-123456789012-us-west-2"
NAMESPACE = "ns_" + "a2" * 13
TRANSCRIPT = "hello there, please remind me about the dentist"
TRANSCRIPT_URI = ("https://s3.us-west-2.amazonaws.com/aws-transcribe-us-west-2-prod/123456789012/"
                  "job/asrOutput.json?X-Amz-Signature=abc")
ON = voice.Settings(enabled=True)
NOW = int(time.time())


def transcript_json(text=TRANSCRIPT):
    return json.dumps({"jobName": "x", "results": {"transcripts": [{"transcript": text}], "items": []}}).encode()


def ogg(seconds):
    """A minimal Ogg Opus stream: a first page, and a last page at `seconds` (48 kHz granules)."""
    def page(granule, header_type):
        return (b"OggS\x00" + bytes([header_type]) + granule.to_bytes(8, "little") + b"\x01\x00\x00\x00"
                + b"\x00" * 8 + b"\x01\x05" + b"audio")
    return page(0, 2) + page(seconds * 48000, 4)


class FakeS3:
    def __init__(self, fail_put=None, fail_delete=None):
        self.puts, self.deletes = [], []
        self.fail_put, self.fail_delete = fail_put, fail_delete

    def put_object(self, **request):
        if self.fail_put:
            raise self.fail_put
        self.puts.append(request)
        return {"ETag": '"e"', "VersionId": "v-111"}

    def delete_object(self, **request):
        self.deletes.append(request)
        if self.fail_delete:
            raise self.fail_delete
        return {}


class FakeTranscribe:
    """Answers GetTranscriptionJob from a list of statuses, one per poll; the last one repeats."""

    def __init__(self, clock, statuses=("IN_PROGRESS", "COMPLETED"), uri=TRANSCRIPT_URI,
                 fail_start=None, fail_delete=None):
        self.clock, self.statuses, self.uri = clock, list(statuses), uri
        self.starts, self.polls, self.deletes = [], [], []
        self.fail_start, self.fail_delete = fail_start, fail_delete

    def start_transcription_job(self, **request):
        if self.fail_start:
            raise self.fail_start
        self.starts.append(request)
        return {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}}

    def get_transcription_job(self, TranscriptionJobName):  # noqa: N803 (AWS kwarg)
        self.polls.append(round(self.clock.elapsed, 3))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        job = {"TranscriptionJobName": TranscriptionJobName, "TranscriptionJobStatus": status}
        if status == "COMPLETED":
            job["Transcript"] = {"TranscriptFileUri": self.uri}
        return {"TranscriptionJob": job}

    def delete_transcription_job(self, TranscriptionJobName):  # noqa: N803
        self.deletes.append(TranscriptionJobName)
        if self.fail_delete:
            raise self.fail_delete


class ServiceError(Exception):
    def __init__(self, code="InternalFailureException"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class VoiceTest(unittest.TestCase):
    """Stub clients, a fake clock and a stub transcript fetch."""

    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = FakeClock()
        self.s3 = FakeS3()
        self.transcribe = FakeTranscribe(self.clock)
        self.fetched = []
        self.transcript = transcript_json()
        patch.object(voice, "time", self.clock).start()
        patch.object(voice, "fetch_transcript", side_effect=self._fetch).start()

    def _fetch(self, uri):
        self.fetched.append(uri)
        return self.transcript

    def client(self, service):
        return {"s3": self.s3, "transcribe": self.transcribe}[service]

    def run_transcriber(self, audio=None, content_type="audio/ogg", settings=ON, duration=14):
        transcriber = voice.Transcriber(BUCKET, settings, self.client, clock=self.clock)
        return transcriber.transcribe(ogg(14) if audio is None else audio, content_type, NAMESPACE, duration)

    def assert_cleaned_up(self):
        """The job, then exactly the stored version of the audio, were deleted."""
        self.assertEqual(self.transcribe.deletes, [self.transcribe.starts[0]["TranscriptionJobName"]])
        self.assertEqual(self.s3.deletes, [{"Bucket": BUCKET, "Key": self.s3.puts[0]["Key"], "VersionId": "v-111"}])


# ---------------------------------------------------------------------------
# The transcriber
# ---------------------------------------------------------------------------

class TranscriberFlow(VoiceTest):
    def test_store_transcribe_read_and_clean_up(self):
        result = self.run_transcriber()
        self.assertEqual(result, voice.Result(text=TRANSCRIPT))
        put, = self.s3.puts
        self.assertEqual((put["Bucket"], put["ContentType"]), (BUCKET, "audio/ogg"))
        self.assertRegex(put["Key"], rf"^{NAMESPACE}/_uploads/voice_\d+_[0-9a-f]{{8}}\.ogg$")
        start, = self.transcribe.starts
        self.assertRegex(start["TranscriptionJobName"], r"^oc-[0-9a-f]{32}$")
        self.assertEqual(start, {
            "TranscriptionJobName": start["TranscriptionJobName"],
            "Media": {"MediaFileUri": f"s3://{BUCKET}/{put['Key']}"},
            "MediaFormat": "ogg",
            "IdentifyLanguage": True,
        })  # no OutputBucketName: the transcript stays in Transcribe's own storage
        self.assertEqual(self.transcribe.polls, [2.0, 3.0])  # first after 2 s, then every second
        self.assertEqual(self.fetched, [TRANSCRIPT_URI])
        self.assert_cleaned_up()

    def test_language_options_when_configured(self):
        self.run_transcriber(settings=voice.Settings(enabled=True, language_options=("en-US", "zh-CN")))
        self.assertEqual(self.transcribe.starts[0]["LanguageOptions"], ["en-US", "zh-CN"])
        self.assertIs(self.transcribe.starts[0]["IdentifyLanguage"], True)

    def test_media_formats(self):
        cases = {"audio/ogg": ("ogg", "ogg"), "audio/mpeg": ("mp3", "mp3"), "audio/x-m4a": ("m4a", "m4a"),
                 "audio/mp4": ("m4a", "m4a"), "video/mp4": ("mp4", "mp4"), "audio/amr": ("amr", "amr"),
                 "audio/webm": ("webm", "webm"), "audio/ogg; codecs=opus": ("ogg", "ogg")}
        for content_type, (media_format, extension) in cases.items():
            with self.subTest(content_type=content_type):
                self.s3.puts.clear()
                self.transcribe.starts.clear()
                self.transcribe.statuses = ["COMPLETED"]
                self.run_transcriber(content_type=content_type)
                self.assertEqual(self.transcribe.starts[0]["MediaFormat"], media_format)
                self.assertTrue(self.s3.puts[0]["Key"].endswith("." + extension))
        self.assertIsNone(voice.media_format("audio/aac"))
        self.assertIsNone(voice.media_format(""))

    def test_failed_job(self):
        self.transcribe.statuses = ["IN_PROGRESS", "FAILED"]
        self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))
        self.assertEqual(self.fetched, [])
        self.assert_cleaned_up()

    def test_timeout(self):
        self.transcribe.statuses = ["IN_PROGRESS"]
        self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))
        self.assertEqual(self.transcribe.polls[0], 2.0)
        self.assertLessEqual(self.transcribe.polls[-1], ON.timeout_seconds)
        self.assertEqual(len(self.transcribe.polls), ON.timeout_seconds - 1)  # at 2, 3, ..., 45 s
        self.assertEqual(self.fetched, [])
        self.assert_cleaned_up()

    def test_cleanup_failures_are_logged_and_the_result_stands(self):
        self.transcribe.fail_delete = ServiceError("BadRequestException")
        self.s3.fail_delete = ServiceError("AccessDenied")
        with self.assertLogs("voice", level="ERROR") as logs:
            result = self.run_transcriber()
        self.assertEqual(result, voice.Result(text=TRANSCRIPT))
        self.assertEqual(logs.output, ["ERROR:voice:Voice: cleanup failed step=job error=ServiceError",
                                       "ERROR:voice:Voice: cleanup failed step=audio error=ServiceError"])
        self.assertEqual(len(self.s3.deletes), 1)  # still attempted after the job delete failed

    def test_transcript_uri_must_be_https_on_amazonaws(self):
        for uri in ("http://s3.us-west-2.amazonaws.com/x", "https://example.com/x",
                    "https://s3.amazonaws.com.example.com/x", "https://s3.us-west-2.amazonaws.com:8443/x",
                    "https://user:pw@s3.us-west-2.amazonaws.com/x", "ftp://s3.amazonaws.com/x", None):
            with self.subTest(uri=uri):
                self.s3.puts.clear()
                self.s3.deletes.clear()
                self.transcribe.starts.clear()
                self.transcribe.deletes.clear()
                self.transcribe.statuses, self.transcribe.uri = ["COMPLETED"], uri
                self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))
                self.assert_cleaned_up()
        self.assertEqual(self.fetched, [])  # refused without a request
        self.assertTrue(voice.transcript_uri_allowed(TRANSCRIPT_URI))
        self.assertTrue(voice.transcript_uri_allowed("https://aws-transcribe-x.s3.us-west-2.amazonaws.com/y"))

    def test_transcript_over_1_mb_or_malformed(self):
        for data in (b"x" * (voice.MAX_TRANSCRIPT_BYTES + 1), b"not json", json.dumps({"results": {}}).encode(),
                     b"\xff\xfe"):
            with self.subTest(size=len(data)):
                self.transcript = data
                self.transcribe.statuses = ["COMPLETED"]
                self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))

    def test_empty_transcript(self):
        for text in ("", "   "):
            self.transcript = transcript_json(text)
            self.transcribe.statuses = ["COMPLETED"]
            self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.EMPTY_REPLY))
        self.transcript = json.dumps({"results": {"transcripts": []}}).encode()
        self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.EMPTY_REPLY))

    def test_store_or_start_failure(self):
        self.s3.fail_put = ServiceError("AccessDenied")
        self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))
        self.assertEqual((self.transcribe.starts, self.s3.deletes, self.transcribe.deletes), ([], [], []))
        self.s3.fail_put = None
        self.transcribe.fail_start = ServiceError("LimitExceededException")
        self.assertEqual(self.run_transcriber(), voice.Result(reply=voice.FAILED_REPLY))
        self.assertEqual(self.transcribe.deletes, [])  # no job was created
        self.assertEqual(len(self.s3.deletes), 1)

    def test_nothing_is_stored_for_audio_it_cannot_transcribe(self):
        for audio, content_type in ((b"", "audio/ogg"), (b"x" * (voice.MAX_BYTES + 1), "audio/ogg"),
                                    (ogg(3), "audio/aac")):
            self.assertEqual(self.run_transcriber(audio=audio, content_type=content_type),
                             voice.Result(reply=voice.FAILED_REPLY))
        self.assertEqual((self.s3.puts, self.transcribe.starts), ([], []))

    def test_logs_carry_no_transcript_key_or_job_name(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.run_transcriber()
            self.transcribe.statuses = ["FAILED"]
            self.run_transcriber()
        text = "\n".join(logs.output)
        for secret in (TRANSCRIPT, "dentist", NAMESPACE, "_uploads/voice_",
                       *[start["TranscriptionJobName"] for start in self.transcribe.starts]):
            self.assertNotIn(secret, text)
        self.assertIn("Voice: transcription status=COMPLETED seconds=14 size=", text)
        self.assertIn("Voice: transcription status=FAILED", text)
        self.assertIn("elapsed_ms=", text)


class Helpers(unittest.TestCase):
    def test_settings_from_env(self):
        self.assertEqual(voice.Settings.from_env({}), voice.Settings())
        self.assertFalse(voice.Settings.from_env({"VOICE_TRANSCRIPTION": "off"}).enabled)
        self.assertEqual(
            voice.Settings.from_env({"VOICE_TRANSCRIPTION": "transcribe", "VOICE_MAX_SECONDS": "120",
                                     "VOICE_TRANSCRIBE_TIMEOUT_SECONDS": "30",
                                     "VOICE_LANGUAGE_OPTIONS": '["en-US","ja-JP"]'}),
            voice.Settings(True, 120, ("en-US", "ja-JP"), 30))
        self.assertEqual(voice.Settings.from_env({"VOICE_TRANSCRIPTION": "transcribe"}), voice.Settings(True))
        for bad in ({"VOICE_MAX_SECONDS": "x"}, {"VOICE_MAX_SECONDS": "0"}, {"VOICE_LANGUAGE_OPTIONS": "[1]"},
                    {"VOICE_LANGUAGE_OPTIONS": '["en-US"]'}, {"VOICE_LANGUAGE_OPTIONS": "{"}):
            with self.subTest(bad=bad), self.assertLogs("voice", level="ERROR"):
                self.assertFalse(voice.Settings.from_env({"VOICE_TRANSCRIPTION": "transcribe", **bad}).enabled)

    def test_limits_and_replies(self):
        self.assertIsNone(voice.refusal(ON, 300, voice.MAX_BYTES))
        self.assertEqual(voice.refusal(ON, 301), "Sorry, I can only transcribe voice messages up to 5 minutes long.")
        self.assertEqual(voice.refusal(ON, None, voice.MAX_BYTES + 1), voice.TOO_LARGE_REPLY)
        self.assertIsNone(voice.refusal(ON, None, None))
        self.assertEqual(voice.too_long_reply(voice.Settings(True, 60)),
                         "Sorry, I can only transcribe voice messages up to 1 minute long.")
        self.assertEqual(voice.too_long_reply(voice.Settings(True, 90)),
                         "Sorry, I can only transcribe voice messages up to 90 seconds long.")
        self.assertEqual((voice.seconds(14), voice.seconds(14200, per_second=1000), voice.seconds("14"),
                          voice.seconds(True), voice.seconds(-1)), (14, 14, None, None, None))

    def test_ogg_duration(self):
        self.assertEqual(voice.ogg_seconds(ogg(14)), 14)
        self.assertEqual(voice.ogg_seconds(ogg(301)), 301)
        for data in (b"", b"ID3...", b"OggS", b"OggS\x01" + b"\x00" * 20, None):
            self.assertIsNone(voice.ogg_seconds(data))

    def test_agent_text(self):
        self.assertEqual(voice.agent_text("", "Voice message", 14, "hi"),
                         "[Voice message, 0:14, transcribed automatically; it may contain errors]\nhi")
        self.assertEqual(voice.agent_text("summary please", "Audio file", 75, "hi"),
                         "summary please\n\n[Audio file, 1:15, transcribed automatically; it may contain errors]\nhi")
        self.assertEqual(voice.agent_text("", "Voice message", None, "hi"),
                         "[Voice message, transcribed automatically; it may contain errors]\nhi")


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def tg_update(extra, chat_id=1001, chat_type="private", message_id=5):
    message = {"message_id": message_id, "date": NOW, "chat": {"id": chat_id, "type": chat_type},
               "from": {"id": 1001, "first_name": "Alex"}}
    message.update(extra)
    if chat_type != "private":
        message["chat"]["title"] = "Team"
    return json.dumps({"update_id": 900 + message_id, "message": message})


TG_VOICE = {"voice": {"file_id": "AwAC", "duration": 14, "mime_type": "audio/ogg", "file_size": 9000}}


class Route(SimpleNamespace):
    """Logs show a route as its session ID, as runtime_routing.RuntimeRoute does."""

    def __str__(self):
        return "ses_" + "1" * 40


class ChannelTest(VoiceTest):
    actor = "telegram:1001"

    def setUp(self):
        super().setUp()
        route = Route(platform_version="V2", user_id="user_abc123", actor_id=self.actor, namespace=NAMESPACE)
        self.resolve = patch.object(index, "resolve_user", return_value=("user_abc123", False)).start()
        self.route = patch.object(index, "get_or_create_session", return_value=route).start()
        patch.object(index, "authorize_channel_actor").start()
        patch.object(index, "get_model_override", return_value=None).start()
        self.invoke = patch.object(index, "invoke_agent_runtime", return_value={"response": "agent reply"}).start()
        patch.object(index, "_voice_client", side_effect=self.client).start()
        patch.object(index, "USER_FILES_BUCKET", BUCKET).start()
        self.settings = patch.object(index, "VOICE", ON).start()

    def agent_messages(self):
        return [call.args[4] for call in self.invoke.call_args_list]


class TelegramTest(ChannelTest):
    def setUp(self):
        super().setUp()
        patch.object(index, "_get_telegram_token", return_value="123:abc").start()
        patch.object(index, "send_telegram_typing").start()
        patch.object(index, "_periodic_typing").start()
        self.send = patch.object(index, "send_telegram_message").start()
        self.chunks = patch.object(index, "_send_telegram_chunks").start()
        self.download = patch.object(index, "_download_telegram_document", return_value=(ogg(14), None)).start()

    def sent(self):
        return [call.args[1] for call in self.send.call_args_list]


class TelegramVoice(TelegramTest):
    def test_a_voice_note_runs_one_turn_with_the_transcript(self):
        index.handle_telegram(tg_update(TG_VOICE))
        self.assertEqual(self.agent_messages(), [
            "[Voice message, 0:14, transcribed automatically; it may contain errors]\n" + TRANSCRIPT])
        self.download.assert_called_once_with("AwAC", "123:abc", voice.MAX_BYTES)
        self.assertEqual(self.transcribe.starts[0]["MediaFormat"], "ogg")
        self.assertTrue(self.s3.puts[0]["Key"].startswith(f"{NAMESPACE}/_uploads/voice_"))
        self.assert_cleaned_up()
        self.assertEqual(self.chunks.call_count, 1)
        self.send.assert_not_called()

    def test_a_caption_comes_first(self):
        index.handle_telegram(tg_update({**TG_VOICE, "caption": "summarize this"}))
        self.assertEqual(self.agent_messages(), [
            "summarize this\n\n[Voice message, 0:14, transcribed automatically; it may contain errors]\n"
            + TRANSCRIPT])

    def test_audio_files_and_video_notes(self):
        self.download.return_value = (b"\xff\xfb audio", None)
        index.handle_telegram(tg_update({"audio": {"file_id": "CQAC", "duration": 75, "mime_type": "audio/mpeg"}}))
        self.transcribe.statuses = ["COMPLETED"]
        index.handle_telegram(tg_update({"video_note": {"file_id": "DQAC", "duration": 5, "length": 240}},
                                        message_id=6))
        self.assertEqual([start["MediaFormat"] for start in self.transcribe.starts], ["mp3", "mp4"])
        self.assertEqual([message.split("\n")[0] for message in self.agent_messages()], [
            "[Audio file, 1:15, transcribed automatically; it may contain errors]",
            "[Video message, 0:05, transcribed automatically; it may contain errors]"])

    def test_too_long_or_too_large_is_refused_before_the_download(self):
        index.handle_telegram(tg_update({"voice": {**TG_VOICE["voice"], "duration": 301}}))
        index.handle_telegram(tg_update({"voice": {**TG_VOICE["voice"], "file_size": voice.MAX_BYTES + 1}},
                                        message_id=6))
        self.assertEqual(self.sent(), ["Sorry, I can only transcribe voice messages up to 5 minutes long.",
                                       voice.TOO_LARGE_REPLY])
        self.download.assert_not_called()
        self.assertEqual((self.s3.puts, self.transcribe.starts), ([], []))
        self.invoke.assert_not_called()

    def test_a_download_over_the_limit_or_failing(self):
        self.download.return_value = (None, "too_large")
        index.handle_telegram(tg_update(TG_VOICE))
        self.download.return_value = (None, "failed")
        index.handle_telegram(tg_update(TG_VOICE, message_id=6))
        self.assertEqual(self.sent(), [voice.TOO_LARGE_REPLY, voice.FAILED_REPLY])
        self.assertEqual(self.s3.puts, [])

    def test_a_failed_job_gets_the_apology_and_no_turn(self):
        self.transcribe.statuses = ["FAILED"]
        index.handle_telegram(tg_update(TG_VOICE))
        self.assertEqual(self.sent(), [voice.FAILED_REPLY])
        self.invoke.assert_not_called()
        self.assert_cleaned_up()

    def test_no_words_gets_its_own_reply(self):
        self.transcript = transcript_json("")
        index.handle_telegram(tg_update(TG_VOICE))
        self.assertEqual(self.sent(), [voice.EMPTY_REPLY])
        self.invoke.assert_not_called()

    def test_flag_off_keeps_the_notice(self):
        patch.object(index, "VOICE", voice.Settings()).start()
        index.handle_telegram(tg_update(TG_VOICE))
        self.assertEqual(self.sent(), [index.unsupported_reply("telegram")])
        self.assertNotIn("voice messages", self.sent()[0])
        self.download.assert_not_called()
        self.assertEqual((self.s3.puts, self.transcribe.starts), ([], []))

    def test_with_the_flag_on_the_notice_lists_voice_messages(self):
        self.assertEqual(index.unsupported_reply("telegram"),
                         "Sorry, I can only read text, voice messages, images (JPEG, PNG, GIF, WebP), PDFs, "
                         "text files (.txt, .csv, .tsv, .log, .json), Markdown (.md) files and ZIP (.zip) "
                         "archives for now.")
        self.assertNotIn("voice", index.unsupported_reply("slack"))
        index.handle_telegram(tg_update({"sticker": {"file_id": "CAAC", "emoji": "x"}}))
        self.assertEqual(self.sent(), [index.unsupported_reply("telegram")])

    def test_an_audio_album_item_stays_with_the_album(self):
        with patch.object(index.telegram_album, "join", return_value=None) as join:
            index.handle_telegram(tg_update({"audio": {"file_id": "CQAC", "duration": 7, "mime_type": "audio/mpeg"},
                                             "media_group_id": "13579"}))
        join.assert_called_once()
        self.download.assert_not_called()
        self.assertEqual(self.transcribe.starts, [])

    def test_a_group_voice_note_replying_to_the_bot_runs_the_group_turn(self):
        group = -1001234567890
        with patch.object(index, "TELEGRAM_GROUPS_ENABLED", True), \
                patch.object(index, "_telegram_bot_username", return_value="TestBot"), \
                patch.object(index, "_is_telegram_group_allowed", return_value=True), \
                patch.object(index, "_is_disabled_telegram_sender", return_value=False):
            index.handle_telegram(tg_update(
                {**TG_VOICE, "reply_to_message": {"message_id": 4, "from": {"id": 123, "is_bot": True}}},
                chat_id=group, chat_type="supergroup", message_id=21))
        self.assertEqual(self.agent_messages(), [
            "Telegram group message from Alex (telegram:1001):\n"
            "[Voice message, 0:14, transcribed automatically; it may contain errors]\n" + TRANSCRIPT])
        self.assertEqual(self.chunks.call_args.kwargs.get("reply_to"), 21)
        self.assert_cleaned_up()


WA_CONFIG = whatsapp.parse_config(json.dumps({
    "accessToken": "EAAtest", "appSecret": "secret", "verifyToken": "verify", "phoneNumberId": "100000000000001"}))
WA_ID = "15550000001"


def wa_audio_body(mime="audio/ogg; codecs=opus", msg_id="wamid.audio1"):
    message = {"from": WA_ID, "id": msg_id, "timestamp": str(NOW), "type": "audio",
               "audio": {"id": "1234567890", "mime_type": mime, "voice": True}}
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "100000000000001"},
        "contacts": [{"profile": {"name": "Test User"}, "wa_id": WA_ID}], "messages": [message]}}]}]})


class WhatsAppVoice(ChannelTest):
    actor = f"whatsapp:{WA_ID}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_whatsapp_config", return_value=WA_CONFIG).start()
        patch.object(index, "_claim_whatsapp_message", return_value=True).start()
        patch.object(index, "_whatsapp_progress_notify").start()
        self.send = patch.object(whatsapp, "send_text").start()
        self.download = patch.object(whatsapp, "download_audio", return_value=(ogg(14), "audio/ogg", None)).start()

    def sent(self):
        return [call.args[2] for call in self.send.call_args_list]

    def test_the_extracted_item_keeps_the_media_id(self):
        item, = whatsapp.extract_messages(json.loads(wa_audio_body()), "100000000000001")
        self.assertEqual((item["type"], item["unsupported"], item["audio_id"], item["audio_mime"]),
                         ("audio", True, "1234567890", "audio/ogg"))

    def test_an_audio_message_runs_one_turn_with_the_transcript(self):
        index.handle_whatsapp(wa_audio_body())
        self.download.assert_called_once_with(WA_CONFIG, "1234567890", voice.CONTENT_TYPES, voice.MAX_BYTES)
        # WhatsApp reports no duration: the Ogg Opus stream is measured.
        self.assertEqual(self.agent_messages(), [
            "[Voice message, 0:14, transcribed automatically; it may contain errors]\n" + TRANSCRIPT])
        self.assert_cleaned_up()
        self.assertEqual(self.sent(), ["agent reply"])

    def test_limits(self):
        self.download.return_value = (ogg(301), "audio/ogg", None)
        index.handle_whatsapp(wa_audio_body())
        self.download.return_value = (None, None, "too_large")
        index.handle_whatsapp(wa_audio_body(msg_id="wamid.audio2"))
        self.assertEqual(self.sent(), ["Sorry, I can only transcribe voice messages up to 5 minutes long.",
                                       voice.TOO_LARGE_REPLY])
        self.assertEqual((self.s3.puts, self.transcribe.starts), ([], []))

    def test_a_format_transcribe_does_not_read_gets_the_notice(self):
        index.handle_whatsapp(wa_audio_body(mime="audio/aac"))
        self.assertEqual(self.sent(), [index.unsupported_reply("whatsapp")])
        self.download.assert_not_called()

    def test_flag_off_keeps_the_notice(self):
        patch.object(index, "VOICE", voice.Settings()).start()
        index.handle_whatsapp(wa_audio_body())
        self.assertEqual(self.sent(), [index.unsupported_reply("whatsapp")])
        self.download.assert_not_called()


LINE_CONFIG = line_channel.parse_config(json.dumps({"channelSecret": "0" * 32, "channelAccessToken": "token"}))
LINE_USER = "U" + "0" * 31 + "1"
REPLY_TOKEN = "nHuyWiB7yP5Zw52FIkcQobQuGDXCTA"


def line_audio_body(duration_ms=14000, event_id="01HSYNTHETICEVENT0000000001"):
    event = {"type": "message", "mode": "active", "timestamp": NOW * 1000,
             "source": {"type": "user", "userId": LINE_USER}, "webhookEventId": event_id,
             "deliveryContext": {"isRedelivery": False}, "replyToken": REPLY_TOKEN,
             "message": {"id": "100000000000000001", "type": "audio", "duration": duration_ms,
                         "contentProvider": {"type": "line"}}}
    return json.dumps({"destination": "", "events": [event]})


class LineVoice(ChannelTest):
    actor = f"line:{LINE_USER}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_line_config", return_value=LINE_CONFIG).start()
        patch.object(index, "_claim_line_event", return_value=True).start()
        patch.object(line_channel, "show_loading").start()
        self.send = patch.object(line_channel, "send_text").start()
        self.download = patch.object(line_channel, "download_content",
                                     return_value=(b"m4a audio", "audio/x-m4a")).start()

    def test_an_audio_message_runs_one_turn_and_replies_with_the_reply_token(self):
        index.handle_line(line_audio_body())
        self.download.assert_called_once_with(LINE_CONFIG, "100000000000000001", voice.CONTENT_TYPES,
                                              voice.MAX_BYTES)
        self.assertEqual(self.agent_messages(), [
            "[Voice message, 0:14, transcribed automatically; it may contain errors]\n" + TRANSCRIPT])
        self.assertEqual(self.transcribe.starts[0]["MediaFormat"], "m4a")
        self.assert_cleaned_up()
        self.assertEqual([call.args for call in self.send.call_args_list],
                         [(LINE_CONFIG, LINE_USER, "agent reply", REPLY_TOKEN)])

    def test_too_long_is_refused_before_the_download_with_the_reply_token(self):
        index.handle_line(line_audio_body(duration_ms=301000))
        self.assertEqual([call.args for call in self.send.call_args_list], [(
            LINE_CONFIG, LINE_USER, "Sorry, I can only transcribe voice messages up to 5 minutes long.",
            REPLY_TOKEN)])
        self.download.assert_not_called()
        self.invoke.assert_not_called()

    def test_flag_off_keeps_the_notice(self):
        patch.object(index, "VOICE", voice.Settings()).start()
        index.handle_line(line_audio_body())
        self.assertEqual([call.args[2] for call in self.send.call_args_list], [index.unsupported_reply("line")])
        self.download.assert_not_called()


class LogPrivacy(TelegramTest):
    def test_no_transcript_key_or_job_name_in_any_log_record(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            index.handle_telegram(tg_update(TG_VOICE))
            self.transcribe.statuses = ["FAILED"]
            index.handle_telegram(tg_update(TG_VOICE, message_id=6))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        messages = [record.getMessage() for record in records]
        secrets = [TRANSCRIPT, "dentist", NAMESPACE, "_uploads/voice_", TRANSCRIPT_URI, "AwAC",
                   *[start["TranscriptionJobName"] for start in self.transcribe.starts]]
        for secret in secrets:
            self.assertFalse([m for m in messages if secret in m], secret)
        self.assertTrue([m for m in messages if m.startswith("Voice: transcription status=COMPLETED")])


if __name__ == "__main__":
    unittest.main()
