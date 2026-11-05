"""Voice messages transcribed with Amazon Transcribe (voice_transcription "transcribe", off by default).

With the setting on, a voice note, audio file or video note on Telegram, and
an audio message on WhatsApp or LINE, is transcribed and answered like a text
message. With it off, such a message gets the notice that the bot cannot read
it (unsupported_media), as before.

For each message the Router:

1. refuses audio longer than VOICE_MAX_SECONDS (where the platform reports the
   duration, before downloading) or larger than MAX_BYTES;
2. stores the audio under the user's namespace in the user-files bucket,
   ``<namespace>/_uploads/voice_<ts>_<hex>.<ext>``, and keeps its VersionId;
3. starts a batch job ``oc-<uuid4 hex>`` with IdentifyLanguage and no
   OutputBucketName (the transcript stays in Transcribe's service-managed
   storage), and polls it every POLL_SECONDS from FIRST_POLL_SECONDS on, for up
   to VOICE_TRANSCRIBE_TIMEOUT_SECONDS;
4. reads at most MAX_TRANSCRIPT_BYTES of the transcript from its temporary
   https URI, which must be on an amazonaws.com host;
5. always deletes the job and then that version of the audio object, so the
   versioned bucket keeps no copy.

The clients are made by an injected factory and the clock is passed in, so all
of this is tested without AWS. Logs carry the duration, size, job status,
elapsed time and error types only, never the transcript, the S3 key or the job
name.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from urllib import request as urllib_request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TRANSCRIBE = "transcribe"
MAX_BYTES = 10 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 1024 * 1024
FIRST_POLL_SECONDS = 2
POLL_SECONDS = 1
JOB_PREFIX = "oc-"

DEFAULT_MAX_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 45

# Telegram message kinds that are transcribed, and how the agent is told what it was.
TELEGRAM_LABELS = {"voice": "Voice message", "audio": "Audio file", "video_note": "Video message"}
VOICE_LABEL = "Voice message"  # WhatsApp and LINE audio messages
# Channels whose audio is transcribed when the setting is on.
CHANNELS = frozenset({"telegram", "telegram_group", "whatsapp", "line"})

# Content type -> (Transcribe MediaFormat, file extension). Anything else gets the notice.
FORMATS = {
    "audio/ogg": ("ogg", "ogg"),
    "audio/opus": ("ogg", "ogg"),
    "audio/mpeg": ("mp3", "mp3"),
    "audio/mp3": ("mp3", "mp3"),
    "audio/mp4": ("m4a", "m4a"),
    "audio/m4a": ("m4a", "m4a"),
    "audio/x-m4a": ("m4a", "m4a"),
    "audio/amr": ("amr", "amr"),
    "audio/webm": ("webm", "webm"),
    "audio/wav": ("wav", "wav"),
    "audio/wave": ("wav", "wav"),
    "audio/x-wav": ("wav", "wav"),
    "audio/flac": ("flac", "flac"),
    "audio/x-flac": ("flac", "flac"),
    "video/mp4": ("mp4", "mp4"),
}
CONTENT_TYPES = frozenset(FORMATS)

FAILED_REPLY = "Sorry, I could not transcribe that voice message. Please try again or type it."
EMPTY_REPLY = "Sorry, I could not make out any words in that voice message."
TOO_LARGE_REPLY = "Sorry, that voice message is too large to transcribe. The limit is 10 MB."

_LANGUAGE_CODE_RE = re.compile(r"[a-z]{2,3}-[A-Z]{2}")
_AMAZONAWS_SUFFIX = ".amazonaws.com"


@dataclass(frozen=True)
class Settings:
    """The Router's VOICE_* environment (set by the stack only when the setting is on)."""

    enabled: bool = False
    max_seconds: int = DEFAULT_MAX_SECONDS
    language_options: tuple = ()
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env):
        """Read the settings. Anything malformed turns transcription off (the stack validates first)."""
        if env.get("VOICE_TRANSCRIPTION", "off").strip().lower() != TRANSCRIBE:
            return cls()
        try:
            max_seconds = int(env.get("VOICE_MAX_SECONDS", DEFAULT_MAX_SECONDS))
            timeout_seconds = int(env.get("VOICE_TRANSCRIBE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
            options = json.loads(env.get("VOICE_LANGUAGE_OPTIONS") or "[]")
        except (TypeError, ValueError):
            logger.error("Voice: VOICE_* settings are malformed; transcription is off")
            return cls()
        if (max_seconds < 1 or timeout_seconds < 1 or not isinstance(options, list)
                or not all(isinstance(code, str) and _LANGUAGE_CODE_RE.fullmatch(code) for code in options)
                or len(options) == 1):
            logger.error("Voice: VOICE_* settings are out of range; transcription is off")
            return cls()
        return cls(True, max_seconds, tuple(options), timeout_seconds)


def seconds(value, per_second=1):
    """A platform-reported duration (in units of 1/per_second s) as whole seconds, or None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return round(value / per_second)


def ogg_seconds(data):
    """The duration of an Ogg Opus stream, from its last page's granule position, or None.

    Opus granule positions count 48 kHz samples. Used where the platform does not
    report a duration (WhatsApp voice notes are Ogg Opus).
    """
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(b"OggS"):
        return None
    last = data.rfind(b"OggS")
    if last < 0 or len(data) < last + 14 or data[last + 4] != 0:
        return None
    granule = int.from_bytes(data[last + 6:last + 14], "little")
    if granule <= 0 or granule >= 2 ** 63:
        return None
    return round(granule / 48000)


def too_long_reply(settings):
    limit = settings.max_seconds
    if limit % 60:
        span = f"{limit} seconds"
    else:
        span = "1 minute" if limit == 60 else f"{limit // 60} minutes"
    return f"Sorry, I can only transcribe voice messages up to {span} long."


def refusal(settings, duration=None, size=None):
    """The reply for audio over the limits, checked before downloading, or None."""
    if duration is not None and duration > settings.max_seconds:
        return too_long_reply(settings)
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_BYTES:
        return TOO_LARGE_REPLY
    return None


def media_format(content_type):
    """(Transcribe MediaFormat, extension) for a content type, or None when it cannot be transcribed."""
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    return FORMATS.get(mime)


def duration_label(duration):
    minutes, rest = divmod(int(duration), 60)
    return f"{minutes}:{rest:02d}"


def agent_text(caption, label, duration, transcript):
    """The agent's text: the caption if any, then a line naming the transcript as such, then it."""
    when = f", {duration_label(duration)}" if duration is not None else ""
    body = f"[{label}{when}, transcribed automatically; it may contain errors]\n{transcript}"
    return f"{caption}\n\n{body}" if caption else body


def transcript_uri_allowed(uri):
    """True for the https URI of a transcript on an amazonaws.com host (no credentials, default port)."""
    if not isinstance(uri, str):
        return False
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and host.endswith(_AMAZONAWS_SUFFIX) and port in (None, 443)
            and parsed.username is None and parsed.password is None)


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib_request.build_opener(_NoRedirect)


def fetch_transcript(uri, timeout=10):
    """GET a transcript from its temporary URI (no redirects). Returns at most MAX_TRANSCRIPT_BYTES + 1 bytes."""
    with _NO_REDIRECT_OPENER.open(urllib_request.Request(uri), timeout=timeout) as response:
        return response.read(MAX_TRANSCRIPT_BYTES + 1)


@dataclass(frozen=True)
class Result:
    """text: the transcript; reply: what to tell the user instead of running a turn."""

    text: str = ""
    reply: str = ""


class Transcriber:
    """Store, transcribe and clean up one audio message.

    client(service) returns the boto3 client for "s3" or "transcribe". fetch(uri)
    reads a transcript; clock has time() and sleep().
    """

    def __init__(self, bucket, settings, client, fetch=None, clock=None):
        self.bucket = bucket
        self.settings = settings
        self._client = client
        self._fetch = fetch
        self.clock = clock or time

    def transcribe(self, audio, content_type, namespace, duration=None):
        started = self.clock.time()
        media = media_format(content_type)
        if not self.bucket or media is None or not audio or len(audio) > MAX_BYTES:
            self._log("refused", audio, duration, started)
            return Result(reply=FAILED_REPLY)
        media_format_name, extension = media
        key = f"{namespace}/_uploads/voice_{int(started)}_{uuid.uuid4().hex[:8]}.{extension}"
        job_name = JOB_PREFIX + uuid.uuid4().hex
        s3 = self._client("s3")
        transcribe = None
        stored, version_id, job_started = False, None, False
        status = "error"
        try:
            response = s3.put_object(Bucket=self.bucket, Key=key, Body=audio, ContentType=content_type)
            stored, version_id = True, (response or {}).get("VersionId")
            transcribe = self._client(TRANSCRIBE)
            request = {
                "TranscriptionJobName": job_name,
                "Media": {"MediaFileUri": f"s3://{self.bucket}/{key}"},
                "MediaFormat": media_format_name,
                "IdentifyLanguage": True,
            }
            if self.settings.language_options:
                request["LanguageOptions"] = list(self.settings.language_options)
            transcribe.start_transcription_job(**request)
            job_started = True
            status, uri = self._wait(transcribe, job_name, started)
            if status != "COMPLETED":
                return Result(reply=FAILED_REPLY)
            if not transcript_uri_allowed(uri):
                status = "bad_uri"
                return Result(reply=FAILED_REPLY)
            text = self._transcript(uri)
            if text is None:
                status = "bad_transcript"
                return Result(reply=FAILED_REPLY)
            if not text:
                status = "empty"
                return Result(reply=EMPTY_REPLY)
            return Result(text=text)
        except Exception as error:  # noqa: BLE001 -- any failure is the same reply; the type is logged
            status = "error"
            logger.warning("Voice: transcription failed: %s", type(error).__name__)
            return Result(reply=FAILED_REPLY)
        finally:
            if job_started:
                self._cleanup("job", lambda: transcribe.delete_transcription_job(TranscriptionJobName=job_name))
            if stored:
                delete = {"Bucket": self.bucket, "Key": key, **({"VersionId": version_id} if version_id else {})}
                self._cleanup("audio", lambda: s3.delete_object(**delete))
            self._log(status, audio, duration, started)

    def _wait(self, transcribe, job_name, started):
        """Poll until the job ends or the timeout passes. Returns (status, transcript URI)."""
        deadline = started + self.settings.timeout_seconds
        self.clock.sleep(FIRST_POLL_SECONDS)
        while True:
            job = transcribe.get_transcription_job(TranscriptionJobName=job_name).get("TranscriptionJob") or {}
            status = job.get("TranscriptionJobStatus")
            if status in ("COMPLETED", "FAILED"):
                return status, (job.get("Transcript") or {}).get("TranscriptFileUri")
            if self.clock.time() + POLL_SECONDS > deadline:
                return "timeout", None
            self.clock.sleep(POLL_SECONDS)

    def _transcript(self, uri):
        """The transcript text ("" when there were no words), or None when it cannot be read."""
        data = (self._fetch or fetch_transcript)(uri)
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_TRANSCRIPT_BYTES:
            return None
        try:
            transcripts = json.loads(data.decode("utf-8"))["results"]["transcripts"]
            return str(transcripts[0].get("transcript") or "").strip() if transcripts else ""
        except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def _cleanup(step, action):
        try:
            action()
        except Exception as error:  # noqa: BLE001 -- the reply still goes out; the failure is logged
            logger.error("Voice: cleanup failed step=%s error=%s", step, type(error).__name__)

    def _log(self, status, audio, duration, started):
        logger.info("Voice: transcription status=%s seconds=%s size=%d elapsed_ms=%d", status,
                    duration if duration is not None else "unknown", len(audio or b""),
                    round((self.clock.time() - started) * 1000))
