"""Tests for the Discord Interactions channel in the Router Lambda.

Payloads are signed locally with a throwaway Ed25519 key (via the same system
OpenSSL the Lambda uses). All IDs and tokens are synthetic placeholders.
"""

import ctypes
import ctypes.util
import json
import logging
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")
os.environ["DISCORD_TOKEN_SECRET_ID"] = "openclaw/channels/discord"

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")
discord_channel = importlib.import_module("discord_channel")
index.DISCORD_TOKEN_SECRET_ID = "openclaw/channels/discord"


# --- local Ed25519 signer (test only) ---------------------------------------

_lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
_lib.EVP_PKEY_new_raw_private_key.restype = ctypes.c_void_p
_lib.EVP_PKEY_new_raw_private_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.EVP_PKEY_get_raw_public_key.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)]
_lib.EVP_MD_CTX_new.restype = ctypes.c_void_p
_lib.EVP_DigestSignInit.argtypes = [ctypes.c_void_p] * 5
_lib.EVP_DigestSign.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t),
                                ctypes.c_char_p, ctypes.c_size_t]
_lib.EVP_MD_CTX_free.argtypes = [ctypes.c_void_p]


class Signer:
    def __init__(self, seed):
        self.pkey = _lib.EVP_PKEY_new_raw_private_key(1087, None, seed, 32)
        buf, n = ctypes.create_string_buffer(32), ctypes.c_size_t(32)
        assert _lib.EVP_PKEY_get_raw_public_key(self.pkey, buf, ctypes.byref(n)) == 1
        self.public_hex = buf.raw.hex()

    def sign(self, message):
        ctx = _lib.EVP_MD_CTX_new()
        assert _lib.EVP_DigestSignInit(ctx, None, None, None, self.pkey) == 1
        buf, n = ctypes.create_string_buffer(64), ctypes.c_size_t(64)
        assert _lib.EVP_DigestSign(ctx, buf, ctypes.byref(n), message, len(message)) == 1
        _lib.EVP_MD_CTX_free(ctx)
        return buf.raw.hex()


SIGNER = Signer(b"\x01" * 32)
OTHER = Signer(b"\x02" * 32)
APP_ID = "400000000000000001"
USER_ID = "500000000000000001"
TOKEN = "aW50ZXJhY3Rpb246dGVzdA.synthetic-token"
SECRET = json.dumps({"applicationId": APP_ID, "publicKey": SIGNER.public_hex, "botToken": "synthetic.bot.token"})
ROUTE = SimpleNamespace(platform_version="V2", user_id="user_abc123", actor_id=f"discord:{USER_ID}")


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _command(name="ask", options=None, guild=False, resolved=None, app_id=APP_ID, interaction_id="600000000000000001"):
    data = {"id": "700000000000000001", "name": name, "type": 1, "options": options or []}
    if resolved:
        data["resolved"] = resolved
    body = {"type": 2, "id": interaction_id, "application_id": app_id, "token": TOKEN, "data": data}
    user = {"id": USER_ID, "username": "synthetic", "global_name": "Synthetic User"}
    if guild:
        body["guild_id"] = "800000000000000001"
        body["member"] = {"user": user}
    else:
        body["user"] = user
    return body


def _ask(prompt="hello", **kw):
    return _command("ask", [{"name": "prompt", "type": 3, "value": prompt}], **kw)


def _headers(body, signer=SIGNER, ts=None):
    ts = str(int(time.time() if ts is None else ts))
    return {"x-signature-ed25519": signer.sign(ts.encode() + body.encode()), "x-signature-timestamp": ts}


def _event(body, headers):
    return {"requestContext": {"http": {"method": "POST", "path": "/webhook/discord", "sourceIp": "192.0.2.1"}},
            "rawPath": "/webhook/discord", "headers": headers, "body": body, "isBase64Encoded": False}


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector(self):
        pk = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
        sig = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertTrue(discord_channel.ed25519_verify(pk, sig, b"\x72"))
        self.assertFalse(discord_channel.ed25519_verify(pk, sig, b"\x73"))
        self.assertFalse(discord_channel.ed25519_verify(pk, sig[:63], b"\x72"))
        self.assertFalse(discord_channel.ed25519_verify(pk[:31], sig, b"\x72"))

    def test_libcrypto_missing_fails_closed(self):
        with patch.object(discord_channel, "_libcrypto", side_effect=RuntimeError("libcrypto not found")):
            self.assertFalse(discord_channel.ed25519_verify(b"\x00" * 32, b"\x00" * 64, b"x"))


class TestParseConfig(unittest.TestCase):
    def test_complete(self):
        self.assertEqual(discord_channel.parse_config(SECRET)["applicationId"], APP_ID)

    def test_placeholder_and_bad_values_disable(self):
        for raw in ("q8YH2kTn0WmZp4rLx7Vb3sDf9GhJ1cNe", "", "[]",
                    json.dumps({"applicationId": APP_ID}),
                    json.dumps({"applicationId": "abc", "publicKey": SIGNER.public_hex}),
                    json.dumps({"applicationId": APP_ID, "publicKey": "zz" * 32})):
            self.assertIsNone(discord_channel.parse_config(raw), raw)


class TestSignature(unittest.TestCase):
    def test_valid(self):
        body = json.dumps({"type": 1})
        self.assertTrue(discord_channel.verify_signature(_headers(body), body.encode(), SIGNER.public_hex))

    def test_missing_headers(self):
        body = b'{"type": 1}'
        self.assertFalse(discord_channel.verify_signature({}, body, SIGNER.public_hex))
        h = _headers(body.decode())
        self.assertFalse(discord_channel.verify_signature({"x-signature-ed25519": h["x-signature-ed25519"]}, body, SIGNER.public_hex))
        self.assertFalse(discord_channel.verify_signature({"x-signature-timestamp": h["x-signature-timestamp"]}, body, SIGNER.public_hex))

    def test_wrong_key_and_tampered_body(self):
        body = json.dumps({"type": 1})
        self.assertFalse(discord_channel.verify_signature(_headers(body, OTHER), body.encode(), SIGNER.public_hex))
        self.assertFalse(discord_channel.verify_signature(_headers(body), b'{"type": 2}', SIGNER.public_hex))

    def test_timestamp_is_signed(self):
        body = json.dumps({"type": 1})
        h = _headers(body)
        h["x-signature-timestamp"] = str(int(h["x-signature-timestamp"]) + 1)
        self.assertFalse(discord_channel.verify_signature(h, body.encode(), SIGNER.public_hex))

    def test_replayed_old_timestamp(self):
        body = json.dumps({"type": 1})
        old = _headers(body, ts=time.time() - discord_channel.MAX_TIMESTAMP_SKEW_SECONDS - 10)
        self.assertFalse(discord_channel.verify_signature(old, body.encode(), SIGNER.public_hex))

    def test_malformed_signature(self):
        body = b'{"type": 1}'
        h = _headers(body.decode())
        h["x-signature-ed25519"] = "not-hex"
        self.assertFalse(discord_channel.verify_signature(h, body, SIGNER.public_hex))


class TestEntry(unittest.TestCase):
    def setUp(self):
        index._token_cache.clear()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value="q8YH2kTn0WmZp4rLx7Vb3sDf9GhJ1cNe")
    def test_unconfigured_404(self, _s, mock_async):
        body = json.dumps({"type": 1})
        self.assertEqual(index.handler(_event(body, _headers(body)), None)["statusCode"], 404)
        mock_async.assert_not_called()

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_ping_pong(self, _s):
        body = json.dumps({"type": 1, "id": "1", "application_id": APP_ID})
        result = index.handler(_event(body, _headers(body)), None)
        self.assertEqual((result["statusCode"], json.loads(result["body"])), (200, {"type": 1}))

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_bad_missing_and_replayed_signatures_401(self, _s, mock_async):
        body = json.dumps({"type": 1})
        for headers in ({}, _headers(body, OTHER), _headers(body, ts=time.time() - 3600)):
            self.assertEqual(index.handler(_event(body, headers), None)["statusCode"], 401)
        mock_async.assert_not_called()

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_body_not_parsed_before_verify(self, _s):
        real_loads = json.loads
        body = '{"type": 1}'

        def guarded(value, *a, **kw):
            if value == body:
                raise AssertionError("body parsed before signature check")
            return real_loads(value, *a, **kw)
        with patch.object(index.json, "loads", side_effect=guarded):
            self.assertEqual(index.handler(_event(body, {"x-signature-ed25519": "00" * 64,
                                                         "x-signature-timestamp": str(int(time.time()))}), None)["statusCode"], 401)

    @patch.object(index, "_claim_discord_interaction", return_value=True)
    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_command_deferred_and_dispatched(self, _s, mock_async, _claim):
        body = json.dumps(_ask())
        result = index.handler(_event(body, _headers(body)), None)
        self.assertEqual(json.loads(result["body"]), {"type": 5, "data": {"flags": 0}})
        mock_async.assert_called_once_with("discord", body, {})
        body = json.dumps(_ask(guild=True))
        result = index.handler(_event(body, _headers(body)), None)
        self.assertEqual(json.loads(result["body"])["data"]["flags"], 64)

    @patch.object(index, "_claim_discord_interaction", return_value=False)
    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_duplicate_interaction_not_dispatched(self, _s, mock_async, _claim):
        body = json.dumps(_ask())
        result = index.handler(_event(body, _headers(body)), None)
        self.assertEqual(json.loads(result["body"])["type"], 4)
        mock_async.assert_not_called()

    @patch.object(index, "_self_invoke_async")
    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_unknown_command_and_other_app(self, _s, mock_async):
        for payload in (_command("weather"), _ask(app_id="400000000000000099")):
            body = json.dumps(payload)
            result = index.handler(_event(body, _headers(body)), None)
            self.assertEqual(json.loads(result["body"])["type"], 4)
        mock_async.assert_not_called()

    @patch.object(index, "handle_discord")
    def test_async_dispatch(self, mock_handle):
        index.handler({"_async_dispatch": True, "_channel": "discord", "_body": "{}", "_headers": {}}, None)
        mock_handle.assert_called_once_with("{}")


class TestParseCommand(unittest.TestCase):
    def test_ask_dm_and_guild(self):
        dm = discord_channel.parse_command(_ask("hi"), APP_ID)
        self.assertEqual((dm["user_id"], dm["text"], dm["in_guild"], dm["name"]), (USER_ID, "hi", False, "Synthetic User"))
        guild = discord_channel.parse_command(_ask("hi", guild=True), APP_ID)
        self.assertEqual((guild["user_id"], guild["in_guild"]), (USER_ID, True))

    def test_link_variants(self):
        self.assertEqual(discord_channel.parse_command(_command("link"), APP_ID)["text"], "link accounts")
        coded = _command("link", [{"name": "code", "type": 3, "value": "ABCD1234"}])
        self.assertEqual(discord_channel.parse_command(coded, APP_ID)["text"], "link ABCD1234")

    def test_bot_user_and_bad_token_rejected(self):
        bot = _ask()
        bot["user"]["bot"] = True
        self.assertIsNone(discord_channel.parse_command(bot, APP_ID))
        bad = _ask()
        bad["token"] = "../../x y"
        self.assertIsNone(discord_channel.parse_command(bad, APP_ID))

    def test_attachment_resolved(self):
        att = {"id": "900000000000000001", "url": "https://cdn.discordapp.com/attachments/1/2/a.png",
               "content_type": "image/png", "size": 10}
        payload = _command("ask", [{"name": "image", "type": 11, "value": att["id"]}],
                           resolved={"attachments": {att["id"]: att}})
        self.assertEqual(discord_channel.parse_command(payload, APP_ID)["attachment"], att)


@patch.object(index, "_get_secret", return_value=SECRET)
@patch.object(index.discord_channel, "send_reply", return_value=True)
class TestHandleDiscord(unittest.TestCase):

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hi there"})
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_happy_path(self, mock_resolve, _session, mock_auth, mock_invoke, mock_reply, _s):
        index.handle_discord(json.dumps(_ask("What's up?")))
        mock_resolve.assert_called_once_with("discord", USER_ID, "Synthetic User")
        mock_auth.assert_called_once()
        self.assertEqual(mock_invoke.call_args.args[3:5], ("discord", "What's up?"))
        mock_reply.assert_called_once_with(APP_ID, TOKEN, "Hi there", ephemeral=False)

    @patch.object(index, "invoke_agent_runtime")
    @patch.object(index, "resolve_user", return_value=(None, False))
    def test_not_allowlisted(self, _r, mock_invoke, mock_reply, _s):
        index.handle_discord(json.dumps(_ask(guild=True)))
        mock_invoke.assert_not_called()
        text = mock_reply.call_args.args[2]
        self.assertIn(f"discord:{USER_ID}", text)
        self.assertTrue(mock_reply.call_args.kwargs["ephemeral"])

    @patch.object(index, "redeem_bind_code", return_value=("user_abc123", True))
    @patch.object(index, "resolve_user")
    def test_link_code_before_allowlist(self, mock_resolve, mock_redeem, mock_reply, _s):
        index.handle_discord(json.dumps(_command("link", [{"name": "code", "type": 3, "value": "ABCD1234"}])))
        mock_redeem.assert_called_once_with("ABCD1234", "discord", USER_ID, "Synthetic User")
        mock_resolve.assert_not_called()

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "A cat"})
    @patch.object(index, "_upload_image_to_s3", return_value="discord_500000000000000001/_uploads/img.png")
    @patch.object(index.discord_channel, "download_attachment", return_value=(b"png", "image/png"))
    @patch.object(index, "authorize_channel_actor")
    @patch.object(index, "get_or_create_session", return_value=ROUTE)
    @patch.object(index, "resolve_user", return_value=("user_abc123", False))
    def test_image(self, _r, _sess, _a, mock_dl, mock_up, mock_invoke, mock_reply, _s):
        att = {"id": "900000000000000001", "url": "https://cdn.discordapp.com/a.png", "content_type": "image/png", "size": 3}
        index.handle_discord(json.dumps(_command("ask", [{"name": "image", "type": 11, "value": att["id"]}],
                                                 resolved={"attachments": {att["id"]: att}})))
        mock_up.assert_called_once_with(b"png", "discord_500000000000000001", "image/png")
        self.assertEqual(mock_invoke.call_args.args[4]["text"], "What is this image?")

    @patch.object(index, "invoke_agent_runtime")
    def test_unconfigured_drops(self, mock_invoke, mock_reply, mock_secret):
        mock_secret.return_value = "placeholder"
        index.handle_discord(json.dumps(_ask()))
        mock_invoke.assert_not_called()
        mock_reply.assert_not_called()


class TestOutbound(unittest.TestCase):
    def test_reply_edits_original_then_follows_up(self):
        with patch.object(discord_channel.urllib_request, "urlopen") as mock_open:
            self.assertTrue(discord_channel.send_reply(APP_ID, TOKEN, "a" * 4000, ephemeral=True))
        calls = [c.args[0] for c in mock_open.call_args_list]
        self.assertEqual(calls[0].get_method(), "PATCH")
        self.assertTrue(calls[0].full_url.endswith(f"/webhooks/{APP_ID}/{TOKEN}/messages/@original"))
        self.assertEqual([c.get_method() for c in calls[1:]], ["POST", "POST"])
        first = json.loads(calls[0].data)
        self.assertEqual(first["allowed_mentions"], {"parse": []})
        self.assertEqual(json.loads(calls[1].data)["flags"], 64)
        self.assertTrue(all(len(json.loads(c.data)["content"]) <= 2000 for c in calls))

    def test_reply_failure_logs_no_token(self):
        with self.assertLogs(level=logging.ERROR) as logs, \
             patch.object(discord_channel.urllib_request, "urlopen", side_effect=OSError("url " + TOKEN)):
            self.assertFalse(discord_channel.send_reply(APP_ID, TOKEN, "x"))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_attachment_host_and_limits(self):
        base = {"url": "https://cdn.discordapp.com/a.png", "content_type": "image/png", "size": 3}
        for bad in ({**base, "url": "https://evil.example/a.png"}, {**base, "url": "http://cdn.discordapp.com/a.png"},
                    {**base, "content_type": "application/pdf"}, {**base, "size": 10_000}):
            with patch.object(discord_channel._NO_REDIRECT_OPENER, "open") as op:
                self.assertEqual(discord_channel.download_attachment(bad, {"image/png"}, 1000), (None, None))
                op.assert_not_called()
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b"png"
        with patch.object(discord_channel._NO_REDIRECT_OPENER, "open", return_value=resp):
            self.assertEqual(discord_channel.download_attachment(base, {"image/png"}, 1000), (b"png", "image/png"))


class TestDedupe(unittest.TestCase):
    def test_claim(self):
        with patch.object(index, "ClientError", FakeClientError), patch.object(index, "identity_table") as table:
            self.assertTrue(index._claim_discord_interaction("600000000000000001"))
            self.assertEqual(table.put_item.call_args.kwargs["Item"]["PK"], "DEDUP#discord:600000000000000001")
            table.put_item.side_effect = FakeClientError("ConditionalCheckFailedException")
            self.assertFalse(index._claim_discord_interaction("600000000000000001"))


if __name__ == "__main__":
    unittest.main()
