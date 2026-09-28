# Channel Setup: Slack and Feishu

Detailed setup for the Slack and Feishu channels. Telegram, the quick-start channel, is covered in the [README](../README.md#telegram).

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
