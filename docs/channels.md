# Channel Setup: Telegram groups, Slack, Feishu, WhatsApp, Discord and LINE

Detailed setup for Telegram groups and the other channels. Telegram private chats, the quick-start channel, are covered in the [README](../README.md#telegram).

## Telegram groups

The bot can answer in Telegram groups and supergroups. This is **off by default** (`telegram_groups_enabled: false`): every group message is ignored, commands included, and nothing is sent to the group. Private chats work the same whether the setting is on or off.

### Privacy model

- **A group is its own identity.** The router answers a group as actor `telegram_group:<chat_id>`, with its own namespace (`telegram_group_<chat_id>`), session, memory and files. The sender's private chat with the bot, and the sender's memory and files, are never used in a group.
- **Everyone in the group shares the group's memory.** Anything a member tells the bot in the group can come back in a reply to any other member. Keep private things in your private chat with the bot.
- **Every member of an allowlisted group can use the bot there**, including members who are not on the user allowlist. Allowlist only groups whose members you trust with the bot. A user an operator has disabled (`manage-allowlist.sh disable`) is ignored in groups too.
- **The sender is context only.** Each message reaches the agent with a first line like `Telegram group message from Alex Example (telegram:<user_id>):`. The name is reduced to letters, digits, spaces and `.'-`, so it cannot pose as a header or as another identity. It never changes the namespace or what the bot may do.
- **Only addressed messages.** In an allowlisted group the bot answers a message only if it @mentions the bot (the mention is removed), replies to one of the bot's own messages, or is `/command@YourBot` (sent to the agent as `/command`). A bare `/command`, a command for another bot, and all other messages are ignored without a reply. The bot's answer is a reply to the message that triggered it.
- **Files and images** follow the same rules and limits as private chats and are stored under the group's namespace.
- **Not available in groups:** `/model` (a group uses the default model), account linking (`link`, `link <code>`), and scheduled tasks (the agent's cron tools refuse to create a schedule for a group).
- **Token usage** for a group's turns is recorded under the group's actor, `telegram_group:<chat_id>`.

### Turn it on

1. In [@BotFather](https://t.me/BotFather), send `/setjoingroups`, pick the bot and choose **Enable**, so it can be added to groups. Privacy mode (`/setprivacy`) can stay **Enabled**: Telegram then delivers only messages that mention the bot, replies to it, and commands, which is all the bot answers.
2. Set `"telegram_groups_enabled": true` in `cdk.json` (or pass `-c telegram_groups_enabled=true`) and redeploy the `OpenClawRouter` stack. Groups stay ignored until you allowlist one.
3. Add the bot to the group.

### Allowlist a group

A group is served only if the identity table holds `ALLOW#telegram_group:<chat_id>`. `registration_open` does not apply to groups. There are two ways to add the record:

**From the group (recommended).** Send `/allowgroup@YourBot` in the group. The router writes the record only if both are true:
- you are on the bot's user allowlist (`ALLOW#telegram:<your_user_id>`, added with `manage-allowlist.sh add telegram:<your_user_id>`) and not disabled;
- Telegram's `getChatMember` reports you as the group's **creator** or an **administrator**.

Anyone else gets a refusal and nothing is written. Sending as an anonymous admin (as the group) is not accepted: turn off **Remain anonymous** in your admin rights first.

**With the manage script.** Use the group's chat ID (a negative number such as `-1001234567890`):

```bash
./scripts/manage-allowlist.sh add telegram_group:-1001234567890
./scripts/manage-allowlist.sh list
```

To stop serving a group, remove its record. The router checks the record on every group message, so the next message is ignored:

```bash
./scripts/manage-allowlist.sh remove telegram_group:-1001234567890
```

`disable` / `enable` also work for a group that has already talked to the bot. When Telegram upgrades a group to a supergroup, the chat ID changes: allowlist the new ID. The new ID starts with a fresh session, memory and files.

## Slack

OpenClaw uses **Slack Events API** with the Router Lambda as the webhook endpoint. Incoming requests are validated using Slack's HMAC signing secret.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** > **From scratch**
2. Give it a name (e.g., "OpenClaw") and select your workspace
3. If **Settings** > **Socket Mode** is enabled, turn it **off** (Socket Mode hides the Event Subscriptions URL field)

**Add OAuth Scopes:**

4. Go to **Features** > **OAuth & Permissions** > **Scopes** > **Bot Token Scopes** and add:
   - `chat:write` — send messages
   - `files:read` — download image attachments (required for image upload support)
   - `app_mentions:read` — detect @mentions (optional)
   - `im:history` — read DM history
   - `im:read` — access DMs
   - `im:write` — send DMs
5. Click **Install to Workspace** and authorize

**Enable direct messages:**

6. Go to **Features** > **App Home**
7. Under **Show Tabs**, enable **Messages Tab**
8. Check **Allow users to send Slash commands and messages from the messages tab**

**Configure Event Subscriptions:**

9. Get your API Gateway URL (you'll need this for the Request URL):
    ```bash
    aws cloudformation describe-stacks \
      --stack-name OpenClawRouter \
      --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
      --output text --region $CDK_DEFAULT_REGION
    ```
10. Go to **Features** > **Event Subscriptions** and toggle **Enable Events** on
11. Set the **Request URL** to your API URL followed by `webhook/slack`, e.g.:
    ```
    https://<your-api-id>.execute-api.us-west-2.amazonaws.com/webhook/slack
    ```
    Slack sends a verification challenge — you should see a green checkmark confirming the URL is valid.
12. Under **Subscribe to bot events**, add:
    - `message.im` — receive direct messages
    - `message.channels` — messages in channels the bot is in (optional)
13. Click **Save Changes**

**Store credentials in Secrets Manager:**

14. From **Settings** > **Basic Information** > **App Credentials**, copy the **Signing Secret** (a hex string like `a1b2c3d4...` — this is NOT the app-level token that starts with `xapp-`)
15. From **Features** > **OAuth & Permissions**, copy the **Bot User OAuth Token** (starts with `xoxb-`)
16. Store both values:
    ```bash
    aws secretsmanager update-secret \
      --secret-id openclaw/channels/slack \
      --secret-string '{"botToken":"xoxb-YOUR-BOT-TOKEN","signingSecret":"YOUR-SIGNING-SECRET"}' \
      --region $CDK_DEFAULT_REGION
    ```

The signing secret is used by the Router Lambda to validate `X-Slack-Signature` HMAC on every incoming webhook request (with 5-minute replay attack prevention).

**Add yourself to the allowlist:**

17. Find your Slack member ID: click your profile picture → **Profile** → **⋯** (more) → **Copy member ID**
18. Run the setup script (handles steps 9–11 and the allowlist in one go):
    ```bash
    ./scripts/setup-slack.sh
    ```
    Or add yourself manually:
    ```bash
    ./scripts/manage-allowlist.sh add slack:YOUR_MEMBER_ID
    ```

## Feishu

Feishu (飞书 / Lark) uses the Events API with the Router Lambda as the webhook endpoint (`POST /webhook/feishu`). Requests are validated with the `X-Lark-Signature` SHA-256 check (fail-closed if `encryptKey` is not set). The Lambda calls `open.feishu.cn` by default (`FEISHU_API_DOMAIN`), downloads image messages via `im/v1/images`, and caches the tenant access token.

1. Create an app with **Bot** capability at [open.feishu.cn/app](https://open.feishu.cn/app)
2. Run `./scripts/setup-feishu.sh` — it prints the Request URL for **Event Subscriptions**, stores the app credentials, and adds you to the allowlist
3. Or store the credentials manually (all four fields are read by the Lambda):
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/feishu \
     --secret-string '{"appId":"cli_xxx","appSecret":"...","verificationToken":"...","encryptKey":"..."}' \
     --region $CDK_DEFAULT_REGION
   ./scripts/manage-allowlist.sh add feishu:YOUR_OPEN_ID
   ```

Scheduled-task replies (see [Scheduled Tasks](how-it-works.md#scheduled-tasks-cron-jobs)) are delivered to Feishu users too: `lambda/cron/index.py` routes `feishu` targets to the Feishu sender, using the same `openclaw/channels/feishu` app credentials and a cached tenant access token. Design notes: [docs/design-feishu-channel.md](design-feishu-channel.md).

## WhatsApp

WhatsApp uses the **WhatsApp Business Platform Cloud API** (Meta), with the Router Lambda as the webhook (`GET` and `POST /webhook/whatsapp`). It is **off by default**: both routes return 404 until `openclaw/channels/whatsapp` holds a complete JSON config, so deploying this code changes nothing for existing channels.

Security model:
- `POST` events are accepted only when `X-Hub-Signature-256` equals `sha256=` + HMAC-SHA256(App Secret, raw body), compared in constant time, before the body is parsed. Missing or wrong signatures get 401.
- The `GET` subscription check answers `hub.challenge` only when `hub.verify_token` matches your verify token (constant-time); otherwise 403.
- Meta signs no timestamp, so each message's own `timestamp` must be within 5 minutes, and message IDs are recorded in the identity table for 24 hours so Meta's retries are processed once.
- Events for a phone number ID other than the configured one are ignored. Media is downloaded only from Meta-hosted HTTPS URLs, without following redirects.
- The same allowlist and `link` account-binding flow as other channels applies. User IDs are `whatsapp:<phone number in international format, digits only>`. The WhatsApp code does not log tokens or message text; as for other channels, the shared allowlist check logs the ID of a rejected user so the admin can find it.

1. In [Meta for Developers](https://developers.facebook.com/apps), create an app of type **Business** and add the **WhatsApp** product. Use the test phone number Meta provides or register your own business number.
2. Create a permanent token: in Business Settings, add a **System User**, assign the app, and generate a token with `whatsapp_business_messaging` (and `whatsapp_business_management`). Temporary 24-hour tokens from the API Setup page also work for a quick test.
3. Note the **Phone number ID** (API Setup page, not the phone number itself) and the **App Secret** (App settings > Basic).
4. Choose a random verify token, e.g. `openssl rand -hex 24`.
5. Store the config (all four keys are required; `graphApiVersion` is optional, default `v23.0`):
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/whatsapp \
     --secret-string '{"accessToken":"EAAxxxx","appSecret":"YOUR_APP_SECRET","verifyToken":"YOUR_VERIFY_TOKEN","phoneNumberId":"123456789012345"}' \
     --region $CDK_DEFAULT_REGION
   ```
   The Router Lambda caches secrets for up to 15 minutes, so a change can take that long to apply.
6. In the app's **WhatsApp > Configuration**, set **Callback URL** to your API URL followed by `webhook/whatsapp` (see the Slack section for how to get the API URL) and **Verify token** to the value from step 4, then click **Verify and save**.
7. Under **Webhook fields**, subscribe to `messages`.
8. Send a message to the bot number. The bot replies with your ID; allow it:
   ```bash
   ./scripts/manage-allowlist.sh add whatsapp:15551234567
   ```

Limits: text and images (WhatsApp sends JPEG and PNG) are supported; other message types are ignored. Replies longer than 4,000 characters are split. WhatsApp only allows free-form messages within 24 hours of the user's last message, so scheduled-task results (cron) reach WhatsApp users only inside that window; outside it Meta rejects the send and the cron Lambda logs a delivery failure. Sending business-initiated template messages is not implemented.

To turn the channel off again, put any non-JSON value back in the secret (for example `aws secretsmanager put-secret-value --secret-id openclaw/channels/whatsapp --secret-string disabled`); both routes return 404 once the cache expires.

## Discord

Discord delivers ordinary chat messages only over its Gateway websocket, which a Lambda cannot keep open. OpenClaw therefore uses Discord's **Interactions endpoint** (`POST /webhook/discord`): users talk to the bot with the `/ask` slash command, in a DM with the bot or in a server. Plain messages that don't use `/ask` are not received. The channel is **off by default**: the route returns 404 until `openclaw/channels/discord` holds a JSON config with `applicationId` and `publicKey`.

Security model:
- Every request must carry `X-Signature-Ed25519` over `X-Signature-Timestamp` + raw body, verified with the application's public key (OpenSSL Ed25519 via the system `libcrypto`, no extra packages) before the body is parsed. Missing or invalid signatures get 401, which Discord also tests when you save the endpoint URL.
- Timestamps more than 5 minutes off are rejected, and interaction IDs are recorded for an hour so a replayed request runs once.
- Interactions for another application ID, from bot users, or for unknown commands are refused. Replies go back through the interaction's webhook (no bot token needed at runtime), with mentions disabled so model output can't ping `@everyone`. In servers, replies are visible only to the person who ran the command.
- Same allowlist and `link` account binding as other channels. User IDs are `discord:<user ID>`.

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. On **General Information**, copy the **Application ID** and **Public Key**.
3. On **Bot**, reset and copy the **token** (only `scripts/setup-discord.py` uses it, to register commands). Privileged intents are not needed.
4. Store the config:
   ```bash
   aws secretsmanager update-secret \
     --secret-id openclaw/channels/discord \
     --secret-string '{"applicationId":"123456789012345678","publicKey":"<64 hex chars>","botToken":"<bot token>"}' \
     --region $CDK_DEFAULT_REGION
   ```
5. Register the `/ask` and `/link` commands and print the endpoint URL (use the API URL from the Slack section):
   ```bash
   ./scripts/setup-discord.py --region $CDK_DEFAULT_REGION --print-url https://<api-id>.execute-api.<region>.amazonaws.com/
   ```
6. On **General Information**, paste the printed URL into **Interactions Endpoint URL** and save. Discord sends a signed PING (and some deliberately bad signatures) and saves only if the endpoint answers correctly. If the secret was just changed, wait up to 15 minutes for the Router Lambda's secret cache to expire.
7. On **Installation**, enable **User Install** (to use the bot in DMs anywhere) and/or **Guild Install** with the `applications.commands` scope, then open the install link.
8. Run `/ask prompt: hello`. The bot replies with your ID; allow it:
   ```bash
   ./scripts/manage-allowlist.sh add discord:YOUR_USER_ID
   ```

Limits: one `/ask` is one turn; the reply must arrive within Discord's 15-minute interaction window and is split into 1,900-character messages. Images can be attached with the `image` option (JPEG, PNG, GIF, WebP, up to 3.75 MB). Scheduled-task (cron) results are not delivered to Discord yet, because delivery outside an interaction needs the bot to DM the user with its bot token.

## LINE

LINE uses the **LINE Messaging API** with the Router Lambda as the webhook (`POST /webhook/line`). It is **off by default**: the route returns 404 until `openclaw/channels/line` holds a JSON config with `channelSecret` and `channelAccessToken`.

Security model:
- Every request must carry `X-Line-Signature` = base64 HMAC-SHA256 of the raw body with the channel secret, compared in constant time before the body is parsed. Missing, malformed or wrong signatures get 401.
- LINE signs no timestamp, so each event's own `timestamp` must be within 5 minutes (1 hour for events LINE marks as redelivered, which is inside the signed body). `webhookEventId` values are recorded in the identity table for 24 hours, so redeliveries and replays run once.
- Only 1:1 chats with the bot are handled; group and room messages, standby-mode events and other event types are ignored. If `botUserId` is set in the secret, events addressed to another bot (`destination`) are ignored too.
- Same allowlist and `link` account-binding flow as other channels. User IDs are `line:U<32 hex>`. Logs never include the channel secret, access token, reply token or message text.

1. In the [LINE Developers Console](https://developers.line.biz/console/), create a provider and a **Messaging API** channel (this also creates a LINE Official Account).
2. On **Basic settings**, copy the **Channel secret**. On **Messaging API**, issue a **Channel access token (long-lived)**. Optionally look up the bot's own user ID for `botUserId` with `curl -H "Authorization: Bearer <token>" https://api.line.me/v2/bot/info` (field `userId`). **Your user ID** on Basic settings is your own LINE ID, which you'll allowlist in step 6.
3. Store the config (`botUserId` is optional):
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id openclaw/channels/line \
     --secret-string '{"channelSecret":"YOUR_CHANNEL_SECRET","channelAccessToken":"YOUR_LONG_LIVED_TOKEN","botUserId":"U0123456789abcdef0123456789abcdef"}' \
     --region $CDK_DEFAULT_REGION
   ```
   The secret is created by the OpenClawSecurity stack. The Router Lambda caches secrets for up to 15 minutes, so a change can take that long to apply.
4. On **Messaging API** > **Webhook settings**, set the **Webhook URL** to your API URL followed by `webhook/line` (see the Slack section for how to get the API URL), turn on **Use webhook**, and click **Verify** (LINE sends a signed request with no events; the router answers 200). Turning on **Webhook redelivery** is optional; redelivered events are handled once.
5. In the LINE Official Account Manager (or **Messaging API** > **LINE Official Account features**), turn off **Auto-reply messages** and **Greeting messages** so only OpenClaw answers.
6. Add the bot as a friend (QR code on the **Messaging API** tab) and send a message. The bot replies with your ID; allow it:
   ```bash
   ./scripts/manage-allowlist.sh add line:U0123456789abcdef0123456789abcdef
   ```

Replies: the router answers with the event's reply token when it is still valid. Agent turns often outlast it, so when LINE rejects the token the reply is sent with the push API instead, which counts against your plan's monthly message quota. While the agent works, LINE's loading animation is shown in the chat (free). Text is split into 4,900-character messages. Images (JPEG, PNG, GIF, WebP, up to 3.75 MB) are supported; other message types are ignored. Scheduled-task (cron) results are delivered with the push API.

To turn the channel off again, put a non-JSON value back in the secret, e.g. `--secret-string disabled-placeholder`; the route returns 404 once the cache expires.
