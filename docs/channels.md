# Channel Setup: Slack, Feishu and WhatsApp

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
