# Feishu external groups in `channel` mode

With `channel_identity_mode` set to `channel` (see [Slack channels and Feishu groups](channels.md#slack-channels-and-feishu-groups)), the router serves a Feishu group chat only when Feishu confirms that the group is internal. This page explains why the check covers the whole group rather than each sender, and lists the Feishu documents it is based on (read on 2026-11-05).

## What the router does

- Before it serves an allowlisted group, it reads `GET /open-apis/im/v1/chats/:chat_id` with the app's tenant access token. It serves the group only when the answer has `external: false`.
- `external: true`: the group is refused. It gets one reply, "Sorry, this bot isn't available in external groups.", and no further replies. The identity table records the reply as `NOTICE#feishu_group:<chat_id>` / `EXTERNAL_GROUP`. If the reply cannot be sent, the record is removed and a later message tries again.
- Any other answer is refused without a reply: a failed read, an answer without an `external` boolean, or a missing permission. When Feishu answers with code 99991672 because the app lacks `im:chat:readonly`, the router also logs one line per Router container naming the permission.
- Each Router container caches the answer: an internal group for one hour, any other answer for five minutes, so a failed read is retried soon.

## Why not compare tenant keys per sender

The receive-message event (`im.message.receive_v1`) carries `header.tenant_key` and `event.sender.tenant_key`. Feishu's documentation does not support refusing a sender by comparing them:

- Neither field is marked as required. The official Go SDK models the sender's `tenant_key` as optional (`*string` with `omitempty`).
- Both fields are described only as the unique identifier of a tenant. No document says that the sender's value equals the header's for a member of the app's own tenant, or that it differs for a member of another tenant.
- The official samples show different header and member tenant keys: in the receive-message sample, and in the user-joined-group sample for a group marked `external: false`.

The group's `external` flag is documented as "whether the group is an external group", so the router checks that instead. This is stricter than the Slack rule, which refuses only members from another organization: in an external Feishu group, members of the app's own tenant are refused too.

A bot can only be in an external group if its custom app has external sharing turned on, with "allow the bot to be added to external groups". That needs organization certification or personal identity verification, and an app version approved by an administrator.

## Sources

- Receive message, `im.message.receive_v1` ([Feishu](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive), updated 2026-08-05; [Lark](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive))
- [User joined a group, `im.chat.member.user.added_v1`](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-member-user/events/added), updated 2024-10-30
- [Get chat information, `GET /open-apis/im/v1/chats/:chat_id`](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat/get), updated 2025-02-19
- [Bots in external groups and one-to-one chats with external users](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/develop-robots/add-bot-to-external-group), updated 2026-05-07
- [Common error codes](https://open.feishu.cn/document/ukTMukTMukTM/ugjM14COyUjL4ITN) (99991672: the app has not been granted the API's permission)
- [larksuite/oapi-sdk-go `service/im/v1/model.go`](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/im/v1/model.go) (`EventSender`)
