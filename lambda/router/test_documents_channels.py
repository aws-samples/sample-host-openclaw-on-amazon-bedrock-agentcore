"""Documents (PDFs, text files, Markdown, ZIP) on Slack, Feishu, WhatsApp, LINE and Discord.

Each channel stores an accepted document under the user's _uploads/ prefix and
gives the agent the file header, with the text of a small text file inlined or,
for a PDF, a pointer to the agent's read_pdf_text tool, as Telegram does
(tests/test_router_documents.py). The size a platform declares is checked
before the download, and the transfer is capped at the 20 MB limit. The Slack
bot token is sent only to https Slack hosts. Channel APIs, S3 and AWS calls are
fakes; nothing goes over the network.
"""

import importlib.util
import json
import logging
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import request as urllib_request

# Imported first: it sets the Router's environment and fakes boto3.
from test_unsupported_messages import (  # noqa: I001
    DISCORD_USER,
    LINE_CONFIG,
    LINE_USER,
    REPLY_TOKEN,
    WA_CONFIG,
    WA_ID,
    BOT_USER_ID,
    ChannelTest,
    DedupeTable,
    FakeClientError,
    discord_command,
    feishu_event,
    index,
    line_event,
    slack_event,
    wa_message,
    wa_payload,
)
import discord_channel  # noqa: E402
import documents  # noqa: E402
import line_channel  # noqa: E402
import unsupported_media  # noqa: E402
import whatsapp  # noqa: E402

MAX = documents.MAX_DOCUMENT_BYTES
PDF_BYTES = b"%PDF-1.4\n% fixture, never parsed by the router\n%%EOF\n"
CSV_BYTES = b"id,name\n1,alpha\n2,beta\n"
NO_MESSAGE = "The user sent a file without a message."


class FakeResponse:
    """A urlopen response. With length above len(body), read(n) returns n filler bytes."""

    def __init__(self, body=b"", headers=None, length=None):
        self.body = body
        self.length = len(body) if length is None else length
        self.headers = headers or {}

    def read(self, n=-1):
        if self.length > len(self.body):
            return b"\0" * (self.length if n is None or n < 0 else min(n, self.length))
        return self.body if n is None or n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class DocumentTest(ChannelTest):
    """S3 is a fake; every log record is kept so a test can check file names stay out."""

    def setUp(self):
        super().setUp()
        self.s3 = patch.object(index, "s3_client", MagicMock()).start()
        patch.object(index, "USER_FILES_BUCKET", "user-files-test").start()
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, handler)

    def stored(self):
        return [(c.kwargs["Key"], c.kwargs["Body"], c.kwargs["ContentType"]) for c in self.s3.put_object.call_args_list]

    def assert_document_turn(self, namespace, ext, content_type, body, request, name, label):
        """One document stored under namespace and one turn about it; returns the turn's text."""
        (key, stored_body, stored_type), = self.stored()
        self.assertRegex(key, rf"^{re.escape(namespace)}/_uploads/doc_\d+_[0-9a-f]{{8}}\.{ext}$")
        self.assertEqual((stored_body, stored_type), (body, content_type))
        (message,) = self.agent_messages()
        self.assertIsInstance(message, str)
        self.assertTrue(message.startswith(request + "\n"), message)
        self.assertIn(f"- Name: {name}", message)
        self.assertIn(f"- Type: {label}", message)
        self.assertIn(f"- Stored in the user's file storage as: {key[len(namespace) + 1:]}", message)
        logged = [r.getMessage() for r in self.records if name in r.getMessage()]
        self.assertEqual(logged, [], "file names stay out of the logs")
        return message

    def assert_nothing_stored_or_run(self):
        self.s3.put_object.assert_not_called()
        self.invoke.assert_not_called()


def skipped_note(what):
    return unsupported_media.skipped_note(what)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

SLACK_PDF = {"mimetype": "application/pdf", "name": "q3-report.pdf", "size": len(PDF_BYTES),
             "url_private_download": "https://files.slack.com/files-pri/T1-F1/q3-report.pdf"}
SLACK_CSV = {"mimetype": "text/csv", "name": "data.csv", "size": len(CSV_BYTES),
             "url_private_download": "https://files.slack.com/files-pri/T1-F2/data.csv"}
SLACK_PNG = {"mimetype": "image/png", "name": "chart.png", "size": 3,
             "url_private_download": "https://files.slack.com/files-pri/T1-F3/chart.png"}


class SlackTest(DocumentTest):
    actor = "slack:U12345USER"
    namespace = "slack_U12345USER"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_slack_tokens", return_value=("xoxb-test", "secret")).start()
        patch.object(index, "_claim_slack_event", return_value=True).start()
        patch.object(index, "_slack_progress_notify").start()
        self.send = patch.object(index, "send_slack_message").start()
        self.bodies = {SLACK_PDF["url_private_download"]: PDF_BYTES, SLACK_CSV["url_private_download"]: CSV_BYTES}
        self.opener = patch.object(index, "_SLACK_FILE_OPENER").start()
        self.opener.open.side_effect = lambda req, timeout=None: FakeResponse(self.bodies[req.full_url])

    def sent(self):
        return [call.args[1] for call in self.send.call_args_list]

    def requests(self):
        return [call.args[0] for call in self.opener.open.call_args_list]


class SlackDocuments(SlackTest):
    def test_a_pdf_with_text_runs_one_turn_with_the_document_and_the_text(self):
        index.handle_slack(slack_event("summarize this", [SLACK_PDF]))
        message = self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES,
                                            "summarize this", "q3-report.pdf", "PDF")
        self.assertIn("read_pdf_text", message)
        (req,) = self.requests()
        self.assertEqual(req.full_url, SLACK_PDF["url_private_download"])
        self.assertEqual(req.get_header("Authorization"), "Bearer xoxb-test")
        self.assertEqual(self.sent(), ["agent reply"])

    def test_a_text_file_alone_is_inlined(self):
        index.handle_slack(slack_event("", [SLACK_CSV]))
        message = self.assert_document_turn(self.namespace, "txt", "text/plain", CSV_BYTES, NO_MESSAGE,
                                            "data.csv", "Text file")
        self.assertIn("\nid,name\n1,alpha\n2,beta\n", message)

    def test_a_file_url_off_slack_is_refused_before_any_request(self):
        urls = ("https://files.slack.com.evil.example/q3.pdf", "http://files.slack.com/q3.pdf",
                "https://evil.example/files.slack.com/q3.pdf", "https://slack.com@evil.example/q3.pdf",
                "file:///etc/passwd")
        for number, url in enumerate(urls):
            index.handle_slack(slack_event("summarize this", [{**SLACK_PDF, "url_private_download": url}],
                                           event_id=f"Ev{number}"))
        self.opener.open.assert_not_called()
        self.assert_nothing_stored_or_run()
        self.assertEqual(self.sent(), [index.DOCUMENT_DOWNLOAD_FAILED_REPLY] * len(urls))

    def test_a_declared_size_over_the_limit_is_refused_before_the_download(self):
        index.handle_slack(slack_event("summarize", [{**SLACK_PDF, "size": MAX + 1}]))
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.opener.open.assert_not_called()
        self.assert_nothing_stored_or_run()

    def test_a_download_over_the_limit_is_refused(self):
        self.opener.open.side_effect = lambda req, timeout=None: FakeResponse(length=MAX + 1)
        index.handle_slack(slack_event("summarize", [SLACK_PDF]))
        self.assertEqual(self.sent(), [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assert_nothing_stored_or_run()

    def test_an_image_is_read_and_a_document_beside_it_is_named_as_not_read(self):
        self.bodies[SLACK_PNG["url_private_download"]] = b"png"
        patch.object(index, "_upload_image_to_s3", return_value=f"{self.namespace}/_uploads/img_1.png").start()
        index.handle_slack(slack_event("compare these", [SLACK_PDF, SLACK_PNG]))
        self.assertEqual(self.agent_messages(), [{
            "text": "compare these" + skipped_note("a file (application/pdf)"),
            "images": [{"s3Key": f"{self.namespace}/_uploads/img_1.png", "contentType": "image/png"}],
        }])
        self.assertEqual([r.full_url for r in self.requests()], [SLACK_PNG["url_private_download"]])

    def test_the_first_document_is_read_and_the_others_named(self):
        index.handle_slack(slack_event("", [SLACK_CSV, SLACK_PDF]))
        message = self.assert_document_turn(self.namespace, "txt", "text/plain", CSV_BYTES,
                                            skipped_note("a file (application/pdf)").strip(), "data.csv",
                                            "Text file")
        self.assertIn("only one file per message is read", message)

    def test_a_document_is_not_a_model_command(self):
        index.handle_slack(slack_event(" /model opus", [SLACK_PDF]))
        self.assertEqual(len(self.agent_messages()), 1)


class SlackRedirects(unittest.TestCase):
    def test_a_redirect_is_followed_only_to_another_slack_url(self):
        handler = index._SlackRedirect()
        req = urllib_request.Request(SLACK_PDF["url_private_download"], headers={"Authorization": "Bearer xoxb-test"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.com/q3.pdf"))
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "http://files.slack.com/q3.pdf"))
        followed = handler.redirect_request(req, None, 302, "Found", {}, "https://files-origin.slack.com/q3.pdf")
        self.assertEqual(followed.full_url, "https://files-origin.slack.com/q3.pdf")


class SlackChannelModeDocuments(SlackTest):
    """#243's channel identity: the document is stored under the channel's own namespace."""

    actor = "slack_channel:C12345CHAN"
    namespace = "slack_channel_C12345CHAN"

    def setUp(self):
        super().setUp()
        patch.object(index, "CHANNEL_IDENTITY_MODE", "channel").start()
        patch.object(index, "_allow_record_exists", return_value=True).start()
        patch.object(index, "_is_disabled_sender", return_value=False).start()
        patch.object(index, "_slack_member_name", return_value="Alex").start()
        self.resolve.return_value = ("user_channel1", False)

    def channel_event(self, text, files):
        return slack_event(f"<@{BOT_USER_ID}> {text}".strip(), files, channel="C12345CHAN", channel_type="channel")

    def test_a_pdf_runs_the_channel_turn(self):
        index.handle_slack(self.channel_event("summarize this", [SLACK_PDF]))
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES,
                                  "Slack channel message from Alex:\nsummarize this", "q3-report.pdf", "PDF")
        self.assertEqual([call.kwargs.get("thread_ts") for call in self.send.call_args_list], ["1700000000.000100"])

    def test_a_declared_size_over_the_limit_gets_a_threaded_reply(self):
        index.handle_slack(self.channel_event("summarize", [{**SLACK_PDF, "size": MAX + 1}]))
        self.assertEqual([(call.args[1], call.kwargs.get("thread_ts")) for call in self.send.call_args_list],
                         [(index.DOCUMENT_TOO_LARGE_REPLY, "1700000000.000100")])
        self.assert_nothing_stored_or_run()


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------

FEISHU_PDF = {"file_key": "file_v3_00cd_1a2b", "file_name": "q3-report.pdf"}


class FeishuTest(DocumentTest):
    actor = "feishu:ou_test123"
    namespace = "feishu_ou_test123"

    def setUp(self):
        super().setUp()
        patch.object(index, "_claim_feishu_event", return_value=True).start()
        patch.object(index, "_feishu_progress_notify").start()
        patch.object(index, "_get_feishu_tenant_token", return_value="t-tenant").start()
        self.send = patch.object(index, "send_feishu_message").start()
        self.response = FakeResponse(PDF_BYTES, headers={"Content-Length": str(len(PDF_BYTES))})
        self.urlopen = patch.object(index.urllib_request, "urlopen",
                                    side_effect=lambda req, timeout=None: self.response).start()

    def sent(self):
        return [call.args[1] for call in self.send.call_args_list]

    def resource_url(self, file_key):
        return f"{index.FEISHU_API_DOMAIN}/open-apis/im/v1/messages/om_1/resources/{file_key}?type=file"


class FeishuDocuments(FeishuTest):
    def test_a_pdf_file_message_is_downloaded_with_the_message_resource_api(self):
        index.handle_feishu(feishu_event("file", FEISHU_PDF))
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES, NO_MESSAGE,
                                  "q3-report.pdf", "PDF")
        (req,) = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual(req.full_url, self.resource_url(FEISHU_PDF["file_key"]))
        self.assertEqual(req.get_header("Authorization"), "Bearer t-tenant")
        self.assertEqual(self.sent(), ["agent reply"])

    def test_a_text_file_is_inlined(self):
        self.response = FakeResponse(CSV_BYTES)
        index.handle_feishu(feishu_event("file", {"file_key": "file_v3_00ef", "file_name": "data.csv"}))
        message = self.assert_document_turn(self.namespace, "txt", "text/plain", CSV_BYTES, NO_MESSAGE,
                                            "data.csv", "Text file")
        self.assertIn("1,alpha", message)

    def test_a_declared_size_over_the_limit_is_refused_before_reading(self):
        self.response = FakeResponse(length=MAX + 1, headers={"Content-Length": str(MAX + 1)})
        self.response.read = MagicMock(side_effect=AssertionError("the body was read"))
        index.handle_feishu(feishu_event("file", FEISHU_PDF))
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_download_over_the_limit_is_refused(self):
        self.response = FakeResponse(length=MAX + 1)  # no Content-Length
        index.handle_feishu(feishu_event("file", FEISHU_PDF))
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_malformed_file_key_is_refused_before_any_request(self):
        index.handle_feishu(feishu_event("file", {"file_key": "../../auth/v3", "file_name": "q3.pdf"}))
        self.urlopen.assert_not_called()
        self.assertEqual(self.sent(), [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_refused_download_logs_the_status_and_feishu_code_only(self):
        error = index.HTTPError(self.resource_url("x"), 400, "Bad Request", {}, None)
        error.read = lambda n=-1: b'{"code": 99991672, "msg": "Access denied. scope required"}'
        self.urlopen.side_effect = error
        index.handle_feishu(feishu_event("file", FEISHU_PDF))
        self.assertEqual(self.sent(), [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assertIn("Feishu file download failed: status=400 code=99991672",
                      [r.getMessage() for r in self.records])


class FeishuGroupDocuments(FeishuTest):
    """#243's channel identity: a group chat's file is stored under the group's own namespace."""

    actor = "feishu_group:oc_chat456"
    namespace = "feishu_group_oc_chat456"

    def setUp(self):
        super().setUp()
        patch.object(index, "CHANNEL_IDENTITY_MODE", "channel").start()
        patch.object(index, "_allow_record_exists", return_value=True).start()
        patch.object(index, "_feishu_group_status", return_value="internal").start()
        patch.object(index, "_is_disabled_sender", return_value=False).start()
        patch.object(index, "_feishu_member_name", return_value="Alex").start()
        self.resolve.return_value = ("user_group2", False)

    def test_a_pdf_runs_the_group_turn(self):
        index.handle_feishu(feishu_event("file", FEISHU_PDF, chat_type="group"))
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES,
                                  "Feishu group message from Alex:", "q3-report.pdf", "PDF")
        self.assertEqual(self.sent(), ["agent reply"])

    def test_a_download_over_the_limit_is_refused(self):
        self.response = FakeResponse(length=MAX + 1)
        index.handle_feishu(feishu_event("file", FEISHU_PDF, chat_type="group"))
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.assert_nothing_stored_or_run()


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

WA_MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=456"


def wa_document(caption=None, mime_type="application/pdf", filename="passport.pdf", media_id="456"):
    body = {"id": media_id, "mime_type": mime_type, "filename": filename}
    if caption is not None:
        body["caption"] = caption
    return json.dumps(wa_payload(wa_message("document", body, msg_id=f"wamid.doc{media_id}")))


class WhatsAppDocuments(DocumentTest):
    actor = f"whatsapp:{WA_ID}"
    namespace = f"whatsapp_{WA_ID}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_whatsapp_config", return_value=WA_CONFIG).start()
        patch.object(index, "ClientError", FakeClientError).start()
        patch.object(index, "identity_table", DedupeTable()).start()
        patch.object(index, "_whatsapp_progress_notify").start()
        self.send = patch.object(whatsapp, "send_text").start()
        self.meta = {"url": WA_MEDIA_URL, "mime_type": "application/pdf", "file_size": len(PDF_BYTES)}
        self.urlopen = patch.object(whatsapp.urllib_request, "urlopen", side_effect=lambda req, timeout=None:
                                    FakeResponse(json.dumps(self.meta).encode())).start()
        self.media = FakeResponse(PDF_BYTES)
        self.opener = patch.object(whatsapp, "_NO_REDIRECT_OPENER").start()
        self.opener.open.side_effect = lambda req, timeout=None: self.media

    def sent(self):
        return [call.args[2] for call in self.send.call_args_list]

    def test_extract_messages_passes_a_document_on(self):
        (item,) = whatsapp.extract_messages(json.loads(wa_document("sign this")), "100000000000001")
        self.assertEqual((item["type"], item["unsupported"], item["text"], item["document_id"],
                          item["document_mime"], item["document_name"]),
                         ("document", False, "sign this", "456", "application/pdf", "passport.pdf"))
        (bad,) = whatsapp.extract_messages(json.loads(wa_document(media_id="../me")), "100000000000001")
        self.assertEqual((bad["unsupported"], bad["document_id"]), (True, ""))

    def test_a_pdf_with_a_caption_runs_one_turn(self):
        index.handle_whatsapp(wa_document("check the dates"))
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES, "check the dates",
                                  "passport.pdf", "PDF")
        (meta_req,) = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertTrue(meta_req.full_url.endswith("/v23.0/456"), meta_req.full_url)
        (media_req,) = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(media_req.full_url, WA_MEDIA_URL)
        self.assertEqual(media_req.get_header("Authorization"), "Bearer EAAtest")
        self.assertEqual(self.sent(), ["agent reply"])

    def test_a_text_document_is_inlined(self):
        self.meta["mime_type"] = "text/plain"
        self.media = FakeResponse(CSV_BYTES)
        index.handle_whatsapp(wa_document(mime_type="text/plain", filename="data.csv"))
        message = self.assert_document_turn(self.namespace, "txt", "text/plain", CSV_BYTES, NO_MESSAGE,
                                            "data.csv", "Text file")
        self.assertIn("2,beta", message)

    def test_a_declared_size_over_the_limit_is_refused_before_the_media_download(self):
        self.meta["file_size"] = MAX + 1
        index.handle_whatsapp(wa_document())
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.opener.open.assert_not_called()
        self.assert_nothing_stored_or_run()

    def test_a_download_over_the_limit_is_refused(self):
        self.media = FakeResponse(length=MAX + 1)
        index.handle_whatsapp(wa_document())
        self.assertEqual(self.sent(), [index.DOCUMENT_TOO_LARGE_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_media_url_off_meta_is_refused_before_the_token_is_sent(self):
        self.meta["url"] = "https://evil.example/attachments/456"
        index.handle_whatsapp(wa_document())
        self.opener.open.assert_not_called()
        self.assertEqual(self.sent(), [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_document_of_another_type_keeps_the_notice(self):
        index.handle_whatsapp(wa_document(mime_type="application/msword", filename="a.doc"))
        index.handle_whatsapp(wa_document("sign this", mime_type="application/msword", filename="a.doc",
                                          media_id="457"))
        self.assertEqual(self.sent(), [index.unsupported_reply("whatsapp"), "agent reply"])
        self.assertEqual(self.agent_messages(), [
            "sign this\n\n[The user also sent a document, which cannot be read yet. Only the text above was received.]"])
        self.urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------

class LineDocuments(DocumentTest):
    actor = f"line:{LINE_USER}"
    namespace = f"line_{LINE_USER}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_line_config", return_value=LINE_CONFIG).start()
        patch.object(index, "ClientError", FakeClientError).start()
        patch.object(index, "identity_table", DedupeTable()).start()
        patch.object(line_channel, "show_loading").start()
        self.send = patch.object(line_channel, "send_text").start()
        # LINE may serve a file with a generic content type: the name decides.
        self.response = FakeResponse(CSV_BYTES, headers={"Content-Type": "application/octet-stream"})
        self.urlopen = patch.object(line_channel.urllib_request, "urlopen",
                                    side_effect=lambda req, timeout=None: self.response).start()

    def file_event(self, name="data.csv", size=len(CSV_BYTES)):
        return json.dumps({"destination": "", "events": [line_event("file", {"fileName": name, "fileSize": size})]})

    def sent(self):
        return [(call.args[2], call.args[3]) for call in self.send.call_args_list]

    def test_extract_events_passes_a_file_on(self):
        (item,) = line_channel.extract_events(json.loads(self.file_event()))
        self.assertEqual((item["type"], item["unsupported"], item["message_id"], item["file_message_id"],
                          item["file_name"], item["file_size"]),
                         ("file", False, "", "100000000000000001", "data.csv", len(CSV_BYTES)))

    def test_a_text_file_is_classified_by_name_and_inlined(self):
        index.handle_line(self.file_event())
        message = self.assert_document_turn(self.namespace, "txt", "text/plain", CSV_BYTES, NO_MESSAGE,
                                            "data.csv", "Text file")
        self.assertIn("1,alpha", message)
        (req,) = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual(req.full_url, f"{line_channel.DATA_API_BASE}/message/100000000000000001/content")
        self.assertEqual(self.sent(), [("agent reply", REPLY_TOKEN)])

    def test_a_pdf_file(self):
        self.response = FakeResponse(PDF_BYTES, headers={"Content-Type": "application/pdf"})
        index.handle_line(self.file_event("contract.pdf", len(PDF_BYTES)))
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES, NO_MESSAGE,
                                  "contract.pdf", "PDF")

    def test_a_declared_size_over_the_limit_is_refused_before_the_download(self):
        index.handle_line(self.file_event(size=MAX + 1))
        self.assertEqual(self.sent(), [(index.DOCUMENT_TOO_LARGE_REPLY, REPLY_TOKEN)])
        self.urlopen.assert_not_called()
        self.assert_nothing_stored_or_run()

    def test_a_download_over_the_limit_is_refused(self):
        self.response = FakeResponse(length=MAX + 1, headers={"Content-Type": "text/csv"})
        index.handle_line(self.file_event())
        self.assertEqual(self.sent(), [(index.DOCUMENT_DOWNLOAD_FAILED_REPLY, REPLY_TOKEN)])
        self.assert_nothing_stored_or_run()

    def test_a_file_of_another_type_keeps_the_notice(self):
        index.handle_line(self.file_event("contract.docx"))
        self.assertEqual(self.sent(), [(index.unsupported_reply("line"), REPLY_TOKEN)])
        self.urlopen.assert_not_called()
        self.assert_nothing_stored_or_run()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

DISCORD_PDF = {"id": "1", "content_type": "application/pdf", "filename": "q3.pdf", "size": len(PDF_BYTES),
               "url": "https://cdn.discordapp.com/attachments/1/2/q3.pdf"}
SETUP_DISCORD = Path(__file__).resolve().parents[2] / "scripts" / "setup-discord.py"


class DiscordDocuments(DocumentTest):
    actor = f"discord:{DISCORD_USER}"
    namespace = f"discord_{DISCORD_USER}"

    def setUp(self):
        super().setUp()
        self.response = FakeResponse(PDF_BYTES)
        self.opener = patch.object(discord_channel, "_NO_REDIRECT_OPENER").start()
        self.opener.open.side_effect = lambda req, timeout=None: self.response
        self.replies = []

    def run_command(self, text, attachment):
        index._handle_discord_command(discord_command(text, attachment), self.replies.append)

    def test_a_pdf_through_the_image_option(self):
        self.run_command("summarize", DISCORD_PDF)
        self.assert_document_turn(self.namespace, "pdf", "application/pdf", PDF_BYTES, "summarize", "q3.pdf", "PDF")
        (req,) = [call.args[0] for call in self.opener.open.call_args_list]
        self.assertEqual(req.full_url, DISCORD_PDF["url"])
        self.assertEqual(self.replies, ["agent reply"])

    def test_a_markdown_file_without_a_content_type(self):
        self.response = FakeResponse(b"# Notes\n- one\n")
        markdown = {"id": "2", "filename": "notes.md", "size": 14, "url": "https://cdn.discordapp.com/a/1/notes.md"}
        self.run_command("", markdown)
        message = self.assert_document_turn(self.namespace, "md", "text/markdown", b"# Notes\n- one\n", NO_MESSAGE,
                                            "notes.md", "Markdown")
        self.assertIn("# Notes\n- one", message)

    def test_a_declared_size_over_the_limit_is_refused_before_the_download(self):
        self.run_command("summarize", {**DISCORD_PDF, "size": MAX + 1})
        self.assertEqual(self.replies, [index.DOCUMENT_TOO_LARGE_REPLY])
        self.opener.open.assert_not_called()
        self.assert_nothing_stored_or_run()

    def test_a_download_over_the_limit_is_refused(self):
        self.response = FakeResponse(length=MAX + 1)
        self.run_command("summarize", DISCORD_PDF)
        self.assertEqual(self.replies, [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assert_nothing_stored_or_run()

    def test_a_url_off_the_discord_cdn_is_refused_before_any_request(self):
        self.run_command("summarize", {**DISCORD_PDF, "url": "https://evil.example/attachments/1/2/q3.pdf"})
        self.opener.open.assert_not_called()
        self.assertEqual(self.replies, [index.DOCUMENT_DOWNLOAD_FAILED_REPLY])
        self.assert_nothing_stored_or_run()

    def test_the_option_keeps_its_name_and_its_description_names_files(self):
        spec = importlib.util.spec_from_file_location("setup_discord_under_test", SETUP_DISCORD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (ask,) = [command for command in module.COMMANDS if command["name"] == "ask"]
        option = next(o for o in ask["options"] if o["type"] == 11)
        self.assertEqual(option["name"], "image")
        self.assertIn("PDF", option["description"])
        self.assertLessEqual(len(option["description"]), 100)  # Discord's limit for option descriptions


if __name__ == "__main__":
    unittest.main()
