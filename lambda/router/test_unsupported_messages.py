"""Messages the bot cannot read yet get a notice instead of silence.

On every channel, a voice note, video, sticker, location, or a file of a type
the bot does not read gets unsupported_reply(channel) and runs no agent turn.
Media sent with text runs the text turn, and the agent is told what else was
sent. The notice comes after resolve_user (a sender who is not allowlisted
gets the invitation instead) and before route resolution. Channel APIs and AWS
calls are fakes; nothing goes over the network.
"""

import contextlib
import json
import logging
import os
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
import telegram_album  # noqa: E402
import unsupported_media  # noqa: E402
import whatsapp  # noqa: E402
from test_telegram_album import FakeAlbumTable, FakeClock  # noqa: E402

NOW = int(time.time())
USER = "user_abc123"
TELEGRAM_TOKEN = "123:abc"  # bot ID 123
TG_USER = 1001
TG_GROUP = -1001234567890
GROUP_USER = "user_group1"
INVITATION = "Sorry, this bot is private and requires an invitation."


def route(actor, namespace=None):
    return SimpleNamespace(platform_version="V2", user_id=USER, actor_id=actor,
                           namespace=namespace or actor.replace(":", "_"))


def caption_note(label):
    return f"\n\n[The user also sent {label}, which cannot be read yet. Only the text above was received.]"


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class DedupeTable:
    """put_item with attribute_not_exists(PK), as the channels' dedupe claims use it."""

    def __init__(self):
        self.keys = set()

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 (AWS kwargs)
        if ConditionExpression == "attribute_not_exists(PK)" and Item["PK"] in self.keys:
            raise FakeClientError("ConditionalCheckFailedException")
        self.keys.add(Item["PK"])


class ChannelTest(unittest.TestCase):
    """resolve_user, routes, invocation and model lookups patched for every channel."""

    actor = "telegram:1001"

    def setUp(self):
        self.addCleanup(patch.stopall)
        self.resolve = patch.object(index, "resolve_user", return_value=(USER, False)).start()
        self.route = patch.object(index, "get_or_create_session", return_value=route(self.actor)).start()
        patch.object(index, "authorize_channel_actor").start()
        patch.object(index, "get_model_override", return_value=None).start()
        self.invoke = patch.object(index, "invoke_agent_runtime", return_value={"response": "agent reply"}).start()

    def agent_messages(self):
        return [call.args[4] for call in self.invoke.call_args_list]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_MEDIA = {
    "voice": {"file_id": "AwAC", "duration": 4, "mime_type": "audio/ogg", "file_size": 9000},
    "audio": {"file_id": "CQAC", "duration": 60, "mime_type": "audio/mpeg"},
    "video": {"file_id": "BAAC", "duration": 9, "mime_type": "video/mp4"},
    "video_note": {"file_id": "DQAC", "duration": 5, "length": 240},
    "sticker": {"file_id": "CAAC", "emoji": "x", "type": "regular"},
    "location": {"latitude": 1.0, "longitude": 2.0},
    "venue": {"location": {"latitude": 1.0, "longitude": 2.0}, "title": "Cafe", "address": "Main St"},
    "contact": {"phone_number": "+15550000000", "first_name": "B"},
    "poll": {"id": "1", "question": "q", "options": []},
    "dice": {"emoji": "🎲", "value": 3},
}


def tg_update(extra, chat_id=TG_USER, chat_type="private", message_id=5, sender=TG_USER):
    message = {"message_id": message_id, "date": NOW, "chat": {"id": chat_id, "type": chat_type},
               "from": {"id": sender, "first_name": "Alex"}}
    message.update(extra)
    if chat_type != "private":
        message["chat"]["title"] = "Team"
    return json.dumps({"update_id": 900 + message_id, "message": message})


def tg_media(kind):
    extra = {kind: TELEGRAM_MEDIA[kind]}
    if kind == "venue":  # Telegram sets location on a venue message too
        extra["location"] = TELEGRAM_MEDIA["location"]
    return extra


class TelegramTest(ChannelTest):
    def setUp(self):
        super().setUp()
        patch.object(index, "_get_telegram_token", return_value=TELEGRAM_TOKEN).start()
        patch.object(index, "send_telegram_typing").start()
        patch.object(index, "_periodic_typing").start()
        self.send = patch.object(index, "send_telegram_message").start()
        self.chunks = patch.object(index, "_send_telegram_chunks").start()

    def sent(self):
        return [(call.args[0], call.args[1], call.kwargs.get("reply_to")) for call in self.send.call_args_list]


class TelegramPrivate(TelegramTest):
    def test_each_kind_gets_the_notice_and_no_turn(self):
        self.assertEqual(set(TELEGRAM_MEDIA), set(unsupported_media.TELEGRAM_KINDS))
        for kind in TELEGRAM_MEDIA:
            with self.subTest(kind=kind):
                self.send.reset_mock()
                index.handle_telegram(tg_update(tg_media(kind)))
                self.assertEqual(self.sent(), [(TG_USER, index.unsupported_reply("telegram"), None)])
        self.invoke.assert_not_called()
        self.chunks.assert_not_called()
        self.route.assert_not_called()  # a notice needs no route

    def test_the_notice_names_what_telegram_accepts(self):
        self.assertEqual(index.unsupported_reply("telegram"),
                         "Sorry, I can only read text, images (JPEG, PNG, GIF, WebP), Markdown (.md) files "
                         "and ZIP (.zip) archives for now.")
        for channel in ("slack", "slack_channel", "feishu", "feishu_group", "whatsapp", "line", "discord"):
            self.assertEqual(index.unsupported_reply(channel),
                             "Sorry, I can only read text and images (JPEG, PNG, GIF, WebP) for now.")

    def test_service_and_empty_messages_get_nothing(self):
        for extra in ({"pinned_message": {"message_id": 4}}, {"new_chat_members": [{"id": 7}]},
                      {"write_access_allowed": {"from_request": True}}, {}):
            index.handle_telegram(tg_update(extra))
        index.handle_telegram(json.dumps({"update_id": 1}))
        self.send.assert_not_called()
        self.invoke.assert_not_called()

    def test_a_caption_runs_the_turn_with_a_note(self):
        index.handle_telegram(tg_update({**tg_media("video"), "caption": "what is in this video?"}))
        index.handle_telegram(tg_update({**tg_media("voice"), "caption": "transcribe this"}, message_id=6))
        self.assertEqual(self.agent_messages(), [
            "what is in this video?" + caption_note("a video"),
            "transcribe this" + caption_note("a voice message"),
        ])
        self.send.assert_not_called()

    def test_a_sender_who_is_not_allowlisted_gets_the_invitation(self):
        self.resolve.return_value = (None, False)
        index.handle_telegram(tg_update(tg_media("voice")))
        (chat, text, _), = self.sent()
        self.assertTrue(text.startswith(INVITATION))
        self.assertIn("telegram:1001", text)
        self.invoke.assert_not_called()


class TelegramGroup(TelegramTest):
    actor = f"telegram_group:{TG_GROUP}"

    def setUp(self):
        super().setUp()
        self.resolve.return_value = (GROUP_USER, False)
        patch.object(index, "TELEGRAM_GROUPS_ENABLED", True).start()
        self.username = patch.object(index, "_telegram_bot_username", return_value="TestBot").start()
        patch.object(index, "_is_telegram_group_allowed", return_value=True).start()
        patch.object(index, "_is_disabled_telegram_sender", return_value=False).start()

    @staticmethod
    def reply_to_bot():
        return {"reply_to_message": {"message_id": 4, "from": {"id": 123, "is_bot": True, "first_name": "Bot"}}}

    def group_update(self, extra, message_id=21):
        return tg_update(extra, chat_id=TG_GROUP, chat_type="supergroup", message_id=message_id)

    def test_a_voice_note_replying_to_the_bot_gets_a_threaded_notice(self):
        index.handle_telegram(self.group_update({**tg_media("voice"), **self.reply_to_bot()}))
        self.assertEqual(self.sent(), [(str(TG_GROUP), index.unsupported_reply("telegram_group"), 21)])
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_unaddressed_media_and_sticker_or_dice_replies_get_nothing(self):
        index.handle_telegram(self.group_update(tg_media("voice")))
        index.handle_telegram(self.group_update({**tg_media("sticker"), **self.reply_to_bot()}, message_id=22))
        index.handle_telegram(self.group_update({**tg_media("dice"), **self.reply_to_bot()}, message_id=23))
        self.send.assert_not_called()
        self.invoke.assert_not_called()
        self.resolve.assert_not_called()

    def test_an_addressed_video_caption_runs_a_group_turn_with_the_note(self):
        index.handle_telegram(self.group_update({**tg_media("video"), "caption": "@TestBot what is this?"}))
        self.assertEqual(self.agent_messages(),
                         ["Telegram group message from Alex (telegram:1001):\nwhat is this?" + caption_note("a video")])
        self.send.assert_not_called()


class TelegramAlbum(TelegramTest):
    """Album items of a kind the bot cannot read go through the album: one notice or one note."""

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.table = FakeAlbumTable()
        patch.object(index, "identity_table", self.table).start()
        patch.object(telegram_album, "time", self.clock).start()
        patch.object(index, "_download_telegram_image", side_effect=lambda message, token: (
            message["photo"][-1]["file_id"].encode(), "image/jpeg", "f")).start()
        patch.object(index, "_upload_image_to_s3",
                     side_effect=lambda data, ns, content_type: f"{ns}/_uploads/img_{data.decode()}.jpeg").start()

    def item(self, message_id, media, caption=None):
        extra = {**media, "media_group_id": "24680"}
        if caption is not None:
            extra["caption"] = caption
        return tg_update(extra, message_id=message_id)

    def deliver(self, first, *later):
        for offset, body in later:
            self.clock.at(offset, lambda body=body: index.handle_telegram(body))
        index.handle_telegram(first)
        self.clock.run_remaining()

    def test_videos_without_a_caption_get_one_notice(self):
        self.deliver(self.item(10, tg_media("video")), (0.1, self.item(11, tg_media("video"))))
        self.assertEqual(self.sent(), [(TG_USER, index.unsupported_reply("telegram"), None)])
        self.invoke.assert_not_called()

    def test_photos_and_a_video_get_one_turn_with_a_note(self):
        photo = {"photo": [{"file_id": "photo11", "width": 90, "height": 90}]}
        self.deliver(self.item(10, tg_media("video"), "compare these"), (0.1, self.item(11, photo)))
        self.assertEqual(self.agent_messages(), [{
            "text": "compare these\n\n[The user also sent a video, which cannot be read yet.]",
            "images": [{"s3Key": "telegram_1001/_uploads/img_photo11.jpeg", "contentType": "image/jpeg"}],
        }])
        self.send.assert_not_called()

    def test_a_video_after_the_album_closed_gets_its_own_notice(self):
        photo = {"photo": [{"file_id": "photo10", "width": 90, "height": 90}]}
        self.deliver(self.item(10, photo, "look"), (30.0, self.item(11, tg_media("video"))))
        self.assertEqual(len(self.agent_messages()), 1)
        self.assertEqual(self.sent(), [(TG_USER, index.unsupported_reply("telegram"), None)])


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

BOT_USER_ID = "UBOT123"
AUTHORIZATIONS = [{"team_id": "T1", "user_id": BOT_USER_ID, "is_bot": True}]
PDF = {"mimetype": "application/pdf", "name": "q3-report.pdf", "size": 9000,
       "url_private_download": "https://files.slack.com/x/q3-report.pdf"}
AUDIO_CLIP = {"mimetype": "audio/webm", "name": "audio_message.webm", "size": 9000,
              "url_private_download": "https://files.slack.com/x/audio.webm"}
PNG = {"mimetype": "image/png", "name": "chart.png", "size": 9000,
       "url_private_download": "https://files.slack.com/x/chart.png"}


def slack_event(text, files, channel="D12345DM", channel_type="im", event_id="Ev1", ts="1700000000.000100"):
    event = {"type": "message", "subtype": "file_share", "user": "U12345USER", "channel": channel,
             "text": text, "ts": ts, "files": files}
    if channel_type:
        event["channel_type"] = channel_type
    return json.dumps({"type": "event_callback", "event_id": event_id, "authorizations": AUTHORIZATIONS,
                       "event": event})


class SlackTest(ChannelTest):
    actor = "slack:U12345USER"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_slack_tokens", return_value=("xoxb-test", "secret")).start()
        self.claim = patch.object(index, "_claim_slack_event", return_value=True).start()
        patch.object(index, "_slack_progress_notify").start()
        self.download = patch.object(index, "_download_slack_file", return_value=(b"png", "image/png", "c")).start()
        patch.object(index, "_upload_image_to_s3", return_value="slack_U12345USER/_uploads/img_1.png").start()
        self.send = patch.object(index, "send_slack_message").start()

    def sent(self):
        return [(call.args[0], call.args[1], call.kwargs.get("thread_ts")) for call in self.send.call_args_list]


class SlackDirectMessages(SlackTest):
    def test_an_audio_clip_alone_gets_the_notice(self):
        index.handle_slack(slack_event("", [AUDIO_CLIP]))
        self.assertEqual(self.sent(), [("D12345DM", index.unsupported_reply("slack"), None)])
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_a_pdf_with_text_runs_one_turn_with_the_note(self):
        index.handle_slack(slack_event("summarize this", [PDF]))
        self.assertEqual(self.agent_messages(), ["summarize this" + caption_note("a file (application/pdf)")])
        self.download.assert_not_called()

    def test_an_image_and_a_pdf_without_text(self):
        index.handle_slack(slack_event("", [PNG, PDF]))
        self.assertEqual(self.agent_messages(), [{
            "text": "[The user also sent a file (application/pdf), which cannot be read yet.]",
            "images": [{"s3Key": "slack_U12345USER/_uploads/img_1.png", "contentType": "image/png"}],
        }])

    def test_an_unaddressed_channel_message_with_a_file_gets_nothing(self):
        index.handle_slack(slack_event("", [PDF], channel="C12345CHAN", channel_type="channel"))
        index.handle_slack(slack_event("look at this", [PDF], channel="C12345CHAN", channel_type="channel"))
        self.send.assert_not_called()
        self.invoke.assert_not_called()
        self.claim.assert_not_called()

    def test_a_sender_who_is_not_allowlisted_gets_the_invitation(self):
        self.resolve.return_value = (None, False)
        index.handle_slack(slack_event("", [AUDIO_CLIP]))
        (channel, text, _), = self.sent()
        self.assertTrue(text.startswith(INVITATION))


class SlackChannelMode(SlackTest):
    """#243's channel identity: the channel answers as itself, in the message's thread."""

    actor = "slack_channel:C12345CHAN"

    def setUp(self):
        super().setUp()
        patch.object(index, "CHANNEL_IDENTITY_MODE", "channel").start()
        patch.object(index, "_allow_record_exists", return_value=True).start()
        patch.object(index, "_is_disabled_sender", return_value=False).start()
        patch.object(index, "_slack_member_name", return_value="Alex").start()
        self.resolve.return_value = ("user_channel1", False)

    def channel_event(self, text, files):
        return slack_event(f"<@{BOT_USER_ID}> {text}".strip(), files, channel="C12345CHAN", channel_type="channel")

    def test_a_file_alone_gets_a_threaded_notice(self):
        index.handle_slack(self.channel_event("", [AUDIO_CLIP]))
        self.assertEqual(self.sent(), [("C12345CHAN", index.unsupported_reply("slack_channel"), "1700000000.000100")])
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_a_file_with_text_runs_the_channel_turn_with_the_note(self):
        index.handle_slack(self.channel_event("summarize this", [PDF]))
        self.assertEqual(self.agent_messages(), [
            "Slack channel message from Alex:\nsummarize this" + caption_note("a file (application/pdf)")])


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------

def feishu_event(msg_type, content, chat_type="p2p", event_id="evt1", mentions=None, chat_id="oc_chat456"):
    message = {"message_id": "om_1", "chat_id": chat_id, "chat_type": chat_type, "message_type": msg_type,
               "content": json.dumps(content)}
    if mentions:
        message["mentions"] = mentions
    return json.dumps({"schema": "2.0", "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
                       "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_test123"}},
                                 "message": message}})


FEISHU_UNREADABLE = {
    "file": {"file_key": "file_v3_00cd", "file_name": "q3-report.pdf"},
    "audio": {"file_key": "file_v3_00ab", "duration": 3000},
    "media": {"file_key": "file_v3_00ef", "image_key": "img_v3_01", "file_name": "clip.mp4", "duration": 4000},
    "sticker": {"file_key": "file_v3_00gh"},
    "location": {"name": "Cafe", "longitude": "1.0", "latitude": "2.0"},
    "share_chat": {"chat_id": "oc_other"},
    "share_user": {"user_id": "ou_other"},
    "interactive": {"elements": []},
    "merge_forward": {},
}
POST = {"title": "", "content": [[{"tag": "text", "text": "Please review "},
                                  {"tag": "a", "href": "https://example.com/d", "text": "this doc"}]]}


class FeishuTest(ChannelTest):
    actor = "feishu:ou_test123"

    def setUp(self):
        super().setUp()
        patch.object(index, "_claim_feishu_event", return_value=True).start()
        patch.object(index, "_feishu_progress_notify").start()
        self.send = patch.object(index, "send_feishu_message").start()

    def sent(self):
        return [call.args for call in self.send.call_args_list]


class FeishuPosts(unittest.TestCase):
    def test_flattening(self):
        cases = [
            ("text and link", POST, ("Please review this doc (https://example.com/d)", "")),
            ("link without a web href, and a bare link",
             {"content": [[{"tag": "a", "href": "lark://x", "text": "app"}, {"tag": "text", "text": " "},
                           {"tag": "a", "href": "https://a.example", "text": "https://a.example"}]]},
             ("app https://a.example", "")),
            ("mention placeholder", {"content": [[{"tag": "at", "user_id": "@_user_1", "user_name": "Bot"},
                                                  {"tag": "text", "text": " hi"}]]}, ("@_user_1 hi", "")),
            ("mention that is not a placeholder", {"content": [[{"tag": "at", "user_id": "ou_123"},
                                                                {"tag": "text", "text": "hi"}]]}, ("hi", "")),
            ("emoji dropped",
             {"content": [[{"tag": "text", "text": "ok "}, {"tag": "emotion", "emoji_type": "SMILE"}]]},
             ("ok", "")),
            ("image left out", {"content": [[{"tag": "text", "text": "see"}], [{"tag": "img", "image_key": "img_1"}]]},
             ("see", "an image")),
            ("two images and a video",
             {"content": [[{"tag": "img", "image_key": "a"}, {"tag": "img", "image_key": "b"}],
                          [{"tag": "media", "file_key": "c"}]]},
             ("", "2 images and a video")),
            ("language key, title, code block, Markdown mention",
             {"zh_cn": {"title": "Notes", "content": [
                 [{"tag": "code_block", "language": "GO", "text": "x := 1"}],
                 [{"tag": "md", "text": 'ask <at user_id="ou_xyz">Tom</at>'}]]}},
             ("Notes\nx := 1\nask @Tom", "")),
            ("not a post", ["x"], ("", "")),
        ]
        for name, content, expected in cases:
            with self.subTest(name):
                self.assertEqual(unsupported_media.flatten_feishu_post(content), expected)


class FeishuMessages(FeishuTest):
    def test_a_post_is_read_as_text(self):
        index.handle_feishu(feishu_event("post", POST))
        self.assertEqual(self.agent_messages(), ["Please review this doc (https://example.com/d)"])

    def test_the_bot_mention_is_removed_from_a_group_post(self):
        post = {"content": [[{"tag": "at", "user_id": "@_user_1", "user_name": "Bot"},
                             {"tag": "text", "text": " summarize the thread"}]]}
        index.handle_feishu(feishu_event("post", post, chat_type="group", mentions=[{"key": "@_user_1"}]))
        self.assertEqual(self.agent_messages(), ["summarize the thread"])

    def test_a_post_with_an_image_and_text_runs_a_turn_with_the_note(self):
        post = {"content": [[{"tag": "text", "text": "what is this?"}], [{"tag": "img", "image_key": "img_1"}]]}
        index.handle_feishu(feishu_event("post", post))
        self.assertEqual(self.agent_messages(), ["what is this?" + caption_note("an image")])

    def test_unreadable_types_and_image_only_posts_get_the_notice(self):
        bodies = [feishu_event(msg_type, content, event_id=f"evt-{msg_type}")
                  for msg_type, content in FEISHU_UNREADABLE.items()]
        bodies.append(feishu_event("post", {"content": [[{"tag": "img", "image_key": "img_1"}]]}, event_id="evt-p"))
        for body in bodies:
            index.handle_feishu(body)
        self.assertEqual(self.sent(), [("oc_chat456", index.unsupported_reply("feishu"))] * len(bodies))
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_no_python_repr_reaches_the_agent(self):
        with patch.object(index, "_download_feishu_image", return_value=(None, None, None)):
            for number, (msg_type, content) in enumerate([*FEISHU_UNREADABLE.items(), ("post", POST),
                                                          ("text", {"text": "hi"}), ("image", {"image_key": "k"})]):
                index.handle_feishu(feishu_event(msg_type, content, event_id=f"evt{number}"))
        for message in self.agent_messages():
            self.assertFalse(str(message).startswith("{'"), message)
            self.assertNotIn("file_key", str(message))
        self.assertEqual(self.agent_messages(), ["Please review this doc (https://example.com/d)", "hi"])

    def test_system_messages_get_nothing(self):
        index.handle_feishu(feishu_event("system", {"template": "x"}))
        self.send.assert_not_called()

    def test_a_sender_who_is_not_allowlisted_gets_the_invitation(self):
        self.resolve.return_value = (None, False)
        index.handle_feishu(feishu_event("audio", FEISHU_UNREADABLE["audio"]))
        (chat, text), = self.sent()
        self.assertTrue(text.startswith(INVITATION))


class FeishuGroupMode(FeishuTest):
    """#243's channel identity: a group chat answers as itself; the notice is sent once."""

    actor = "feishu_group:oc_chat456"

    def setUp(self):
        super().setUp()
        patch.object(index, "CHANNEL_IDENTITY_MODE", "channel").start()
        patch.object(index, "_allow_record_exists", return_value=True).start()
        patch.object(index, "_feishu_group_status", return_value="internal").start()
        patch.object(index, "_is_disabled_sender", return_value=False).start()
        patch.object(index, "_feishu_member_name", return_value="Alex").start()
        self.resolve.return_value = ("user_group2", False)

    def test_a_file_gets_one_notice(self):
        index.handle_feishu(feishu_event("file", FEISHU_UNREADABLE["file"], chat_type="group"))
        self.assertEqual(self.sent(), [("oc_chat456", index.unsupported_reply("feishu_group"))])
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_a_post_with_an_image_runs_the_group_turn_with_the_note(self):
        post = {"content": [[{"tag": "at", "user_id": "@_user_1"}, {"tag": "text", "text": " what is this?"}],
                            [{"tag": "img", "image_key": "img_1"}]]}
        index.handle_feishu(feishu_event("post", post, chat_type="group", mentions=[{"key": "@_user_1"}]))
        self.assertEqual(self.agent_messages(),
                         ["Feishu group message from Alex:\nwhat is this?" + caption_note("an image")])


# ---------------------------------------------------------------------------
# WhatsApp and LINE
# ---------------------------------------------------------------------------

WA_CONFIG = whatsapp.parse_config(json.dumps({
    "accessToken": "EAAtest", "appSecret": "secret", "verifyToken": "verify", "phoneNumberId": "100000000000001"}))
WA_ID = "15550000001"


def wa_payload(*messages, statuses=None):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "100000000000001"},
             "contacts": [{"profile": {"name": "Test User"}, "wa_id": WA_ID}]}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": value}]}]}


def wa_message(msg_type, body, msg_id=None):
    return {"from": WA_ID, "id": msg_id or f"wamid.{msg_type}", "timestamp": str(NOW), "type": msg_type,
            msg_type: body}


WA_UNREADABLE = {
    "audio": {"id": "123", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    "video": {"id": "789", "mime_type": "video/mp4"},
    "document": {"id": "456", "mime_type": "application/pdf", "filename": "passport.pdf"},
    "sticker": {"id": "321", "mime_type": "image/webp"},
    "location": {"latitude": 1.0, "longitude": 2.0, "name": "Cafe"},
    "contacts": [{"name": {"formatted_name": "B"}, "phones": [{"phone": "+15550000002"}]}],
}


class WhatsAppMessages(ChannelTest):
    actor = f"whatsapp:{WA_ID}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_whatsapp_config", return_value=WA_CONFIG).start()
        patch.object(index, "ClientError", FakeClientError).start()
        patch.object(index, "identity_table", DedupeTable()).start()
        patch.object(index, "_whatsapp_progress_notify").start()
        self.send = patch.object(whatsapp, "send_text").start()

    def test_the_listed_kinds_are_kept_as_unsupported(self):
        items = whatsapp.extract_messages(
            wa_payload(*[wa_message(kind, body) for kind, body in WA_UNREADABLE.items()]), "100000000000001")
        self.assertEqual([(item["type"], item["unsupported"], item["text"]) for item in items],
                         [(kind, True, "") for kind in WA_UNREADABLE])
        captioned = whatsapp.extract_messages(wa_payload(
            wa_message("video", {"id": "1", "caption": "what is this?"}),
            wa_message("document", {"id": "2", "caption": "sign this", "filename": "a.pdf"})), "100000000000001")
        self.assertEqual([item["text"] for item in captioned], ["what is this?", "sign this"])

    def test_reactions_system_buttons_and_statuses_stay_dropped(self):
        dropped = [wa_message("reaction", {"message_id": "wamid.x", "emoji": "👍"}),
                   wa_message("system", {"body": "changed number"}),
                   wa_message("unsupported", {}),
                   wa_message("button", {"text": "Yes"}),
                   wa_message("interactive", {"type": "button_reply"})]
        self.assertEqual(whatsapp.extract_messages(wa_payload(*dropped), "100000000000001"), [])
        self.assertEqual(whatsapp.extract_messages(wa_payload(statuses=[{"id": "x", "status": "read"}]),
                                                   "100000000000001"), [])

    def test_one_notice_and_a_redelivery_sends_nothing(self):
        body = json.dumps(wa_payload(wa_message("audio", WA_UNREADABLE["audio"])))
        index.handle_whatsapp(body)
        index.handle_whatsapp(body)
        self.assertEqual([call.args for call in self.send.call_args_list],
                         [(WA_CONFIG, WA_ID, index.unsupported_reply("whatsapp"))])
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_a_video_caption_runs_the_turn_with_the_note(self):
        index.handle_whatsapp(json.dumps(wa_payload(wa_message("video", {"id": "1", "caption": "what is this?"}))))
        self.assertEqual(self.agent_messages(), ["what is this?" + caption_note("a video")])

    def test_a_sender_who_is_not_allowlisted_gets_the_invitation(self):
        self.resolve.return_value = (None, False)
        index.handle_whatsapp(json.dumps(wa_payload(wa_message("sticker", WA_UNREADABLE["sticker"]))))
        (_, to, text), = [call.args for call in self.send.call_args_list]
        self.assertTrue(text.startswith(INVITATION))


LINE_CONFIG = line_channel.parse_config(json.dumps({"channelSecret": "0" * 32, "channelAccessToken": "token"}))
LINE_USER = "U" + "0" * 31 + "1"
REPLY_TOKEN = "nHuyWiB7yP5Zw52FIkcQobQuGDXCTA"


def line_event(msg_type, extra=None, event_id="01HSYNTHETICEVENT0000000001", event_type="message"):
    message = {"id": "100000000000000001", "type": msg_type, **(extra or {})}
    return {"type": event_type, "mode": "active", "timestamp": NOW * 1000,
            "source": {"type": "user", "userId": LINE_USER}, "webhookEventId": event_id,
            "deliveryContext": {"isRedelivery": False}, "replyToken": REPLY_TOKEN, "message": message}


LINE_UNREADABLE = {
    "audio": {"duration": 4000},
    "video": {"duration": 4000},
    "file": {"fileName": "contract.pdf", "fileSize": 9},
    "sticker": {"packageId": "1", "stickerId": "2"},
    "location": {"title": "Cafe", "latitude": 1.0, "longitude": 2.0},
}


class LineMessages(ChannelTest):
    actor = f"line:{LINE_USER}"

    def setUp(self):
        super().setUp()
        patch.object(index, "_get_line_config", return_value=LINE_CONFIG).start()
        patch.object(index, "ClientError", FakeClientError).start()
        patch.object(index, "identity_table", DedupeTable()).start()
        patch.object(line_channel, "show_loading").start()
        self.send = patch.object(line_channel, "send_text").start()

    def test_the_listed_kinds_are_kept_as_unsupported(self):
        events = [line_event(kind, extra, event_id=f"01HSYNTHETICEVENT00000000{n:02d}")
                  for n, (kind, extra) in enumerate(LINE_UNREADABLE.items())]
        items = line_channel.extract_events({"destination": "", "events": events})
        self.assertEqual([(item["type"], item["unsupported"], item["text"], item["message_id"]) for item in items],
                         [(kind, True, "", "") for kind in LINE_UNREADABLE])

    def test_non_message_events_and_other_types_stay_dropped(self):
        dropped = [{**line_event("text"), "type": event_type} for event_type in ("follow", "unfollow", "postback")]
        dropped.append(line_event("imagemap"))
        self.assertEqual(line_channel.extract_events({"destination": "", "events": dropped}), [])

    def test_one_notice_with_the_reply_token_and_a_redelivery_sends_nothing(self):
        body = json.dumps({"destination": "", "events": [line_event("sticker", LINE_UNREADABLE["sticker"])]})
        index.handle_line(body)
        index.handle_line(body)
        self.assertEqual([call.args for call in self.send.call_args_list],
                         [(LINE_CONFIG, LINE_USER, index.unsupported_reply("line"), REPLY_TOKEN)])
        self.invoke.assert_not_called()
        self.route.assert_not_called()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

DISCORD_USER = "500000000000000001"


def discord_command(text, attachment):
    return {"user_id": DISCORD_USER, "name": "Alex", "text": text, "attachment": attachment}


class DiscordCommands(ChannelTest):
    actor = f"discord:{DISCORD_USER}"

    def setUp(self):
        super().setUp()
        self.download = patch.object(index.discord_channel, "download_attachment",
                                     return_value=(b"png", "image/png")).start()
        patch.object(index, "_upload_image_to_s3", return_value="discord_500000000000000001/_uploads/img.png").start()
        self.replies = []

    def test_a_pdf_attachment_gets_the_notice_without_a_download(self):
        pdf = {"content_type": "application/pdf", "filename": "a.pdf", "size": 1000,
               "url": "https://cdn.discordapp.com/attachments/1/2/a.pdf"}
        index._handle_discord_command(discord_command("summarize", pdf), self.replies.append)
        self.assertEqual(self.replies, [index.unsupported_reply("discord")])
        self.download.assert_not_called()
        self.invoke.assert_not_called()
        self.route.assert_not_called()

    def test_an_image_attachment_still_runs_a_turn(self):
        png = {"content_type": "image/png", "filename": "c.png", "size": 1000,
               "url": "https://cdn.discordapp.com/attachments/1/2/c.png"}
        index._handle_discord_command(discord_command("what is this?", png), self.replies.append)
        self.assertEqual(self.agent_messages(), [{
            "text": "what is this?",
            "images": [{"s3Key": "discord_500000000000000001/_uploads/img.png", "contentType": "image/png"}]}])


# ---------------------------------------------------------------------------
# Logs: the kind only
# ---------------------------------------------------------------------------

class LogPrivacy(unittest.TestCase):
    """File names, captions and message text never reach the logs; the kind does."""

    SECRETS = ("caption-secret-tg", "slack-secret.pdf", "text-secret-slack", "feishu-secret.pdf",
               "wa-secret.pdf", "caption-secret-wa", "line-secret.pdf", "discord-secret.pdf")

    def test_no_file_names_or_captions_in_log_records(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self._run_every_channel()
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        messages = [record.getMessage() for record in records]
        for secret in self.SECRETS:
            self.assertFalse([message for message in messages if secret in message], secret)
        kinds = [message for message in messages if "unsupported" in message and "kind" in message]
        self.assertGreaterEqual(len(kinds), 6, messages)

    def _run_every_channel(self):
        patches = [
            patch.object(index, "resolve_user", return_value=(USER, False)),
            patch.object(index, "get_or_create_session", return_value=route("telegram:1001")),
            patch.object(index, "authorize_channel_actor"),
            patch.object(index, "get_model_override", return_value=None),
            patch.object(index, "invoke_agent_runtime", return_value={"response": "ok"}),
            patch.object(index, "_get_telegram_token", return_value=TELEGRAM_TOKEN),
            patch.object(index, "send_telegram_typing"), patch.object(index, "_periodic_typing"),
            patch.object(index, "send_telegram_message"), patch.object(index, "_send_telegram_chunks"),
            patch.object(index, "_get_slack_tokens", return_value=("xoxb-test", "secret")),
            patch.object(index, "_claim_slack_event", return_value=True),
            patch.object(index, "_slack_progress_notify"), patch.object(index, "send_slack_message"),
            patch.object(index, "_claim_feishu_event", return_value=True),
            patch.object(index, "_feishu_progress_notify"), patch.object(index, "send_feishu_message"),
            patch.object(index, "_get_whatsapp_config", return_value=WA_CONFIG),
            patch.object(index, "_get_line_config", return_value=LINE_CONFIG),
            patch.object(index, "ClientError", FakeClientError),
            patch.object(index, "identity_table", DedupeTable()),
            patch.object(index, "_whatsapp_progress_notify"), patch.object(whatsapp, "send_text"),
            patch.object(line_channel, "send_text"), patch.object(line_channel, "show_loading"),
        ]
        with contextlib.ExitStack() as stack:
            for active in patches:
                stack.enter_context(active)
            index.handle_telegram(tg_update({**tg_media("video"), "caption": "caption-secret-tg"}))
            index.handle_telegram(tg_update(tg_media("voice"), message_id=6))
            secret_pdf = {**PDF, "name": "slack-secret.pdf"}
            index.handle_slack(slack_event("", [secret_pdf], event_id="Ev2"))
            index.handle_slack(slack_event("text-secret-slack", [secret_pdf], event_id="Ev3"))
            index.handle_feishu(feishu_event("file", {"file_key": "file_v3", "file_name": "feishu-secret.pdf"}))
            index.handle_whatsapp(json.dumps(wa_payload(
                wa_message("document", {"id": "1", "filename": "wa-secret.pdf"}, msg_id="wamid.d1"),
                wa_message("document", {"id": "2", "filename": "wa-secret.pdf", "caption": "caption-secret-wa"},
                           msg_id="wamid.d2"))))
            index.handle_line(json.dumps({"destination": "", "events": [
                line_event("file", {"fileName": "line-secret.pdf", "fileSize": 9})]}))
            index._handle_discord_command(
                discord_command("", {"content_type": "application/pdf", "filename": "discord-secret.pdf",
                                     "url": "https://cdn.discordapp.com/attachments/1/2/discord-secret.pdf"}),
                lambda text: None)


if __name__ == "__main__":
    unittest.main()
