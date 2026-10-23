# Channel Setup: Telegram groups, Slack and Feishu

Detailed setup for Telegram groups and the Slack and Feishu channels. Telegram private chats, the quick-start channel, are covered in the [README](../README.md#telegram).

## Telegram groups

The bot can answer in Telegram groups and supergroups. This is **off by default** (`telegram_groups_enabled: false`): every group message is ignored, commands included, and nothing is sent to the group. Private chats work the same whether the setting is on or off.

### Privacy model

- **A group is its own identity.** The router answers a group as actor `telegram_group:<chat_id>`, with its own namespace (`telegram_group_<chat_id>`), session, memory and files. The sender's private chat with the bot, and the sender's memory and files, are never used in a group.
- **Everyone in the group shares the group's memory.** Anything a member tells the bot in the group can come back in a reply to any other member. Keep private things in your private chat with the bot.
- **Every member of an allowlisted group can use the bot there**, including members who are not on the user allowlist. Allowlist only groups whose members you trust with the bot.
- **The sender is context only.** Each message reaches the agent with a first line like `Telegram group message from Alex Example (telegram:<user_id>):`. The name is reduced to letters, digits, spaces and `.'-`, so it cannot pose as a header or as another identity. It never changes the namespace or what the bot may do.
- **Only addressed messages.** In an allowlisted group the bot answers a message only if it @mentions the bot (the mention is removed), replies to one of the bot's own messages, or is `/command@YourBot` (sent to the agent as `/command`). A bare `/command`, a command for another bot, and all other messages are ignored without a reply. The bot's answer is a reply to the message that triggered it.
- **Files and images** follow the same rules and limits as private chats and are stored under the group's namespace.
- **Not available in groups:** account linking (`link`, `link <code>`) and scheduled tasks (the agent's cron tools refuse to create a schedule for a group).
- **Token usage** for a group's turns is recorded under the group's actor, `telegram_group:<chat_id>`.

### Turn it on

1. In [@BotFather](https://t.me/BotFather), send `/setjoingroups`, pick the bot and choose **Enable**, so it can be added to groups. Privacy mode (`/setprivacy`) can stay **Enabled**: Telegram then delivers only messages that mention the bot, replies to it, and commands, which is all the bot answers.
2. Set `"telegram_groups_enabled": true` in `cdk.json` (or pass `-c telegram_groups_enabled=true`) and redeploy the `OpenClawRouter` stack. Groups stay ignored until you allowlist one.
3. Add the bot to the group.

### Allowlist a group

A group is served only if the identity table holds `ALLOW#telegram_group:<chat_id>`. `registration_open` does not apply to groups. There are two ways to add the record:

**From the group (recommended).** Send `/allowgroup@YourBot` in the group. The router writes the record only if both are true:
- you are on the bot's user allowlist (`ALLOW#telegram:<your_user_id>`, added with `manage-allowlist.sh add telegram:<your_user_id>`);
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

When Telegram upgrades a group to a supergroup, the chat ID changes: allowlist the new ID. The new ID starts with a fresh session, memory and files.

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
