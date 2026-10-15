#!/usr/bin/env python3
"""Register the OpenClaw slash commands (/ask, /link) for a Discord application.

Reads applicationId and botToken from the openclaw/channels/discord secret and
overwrites the application's global commands. Run it once after storing the
secret, and again if the command definitions change.

Usage:
    ./scripts/setup-discord.py [--region us-west-2] [--print-url API_URL]

Global commands can take a few minutes to appear in Discord clients.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

import boto3

SECRET_ID = "openclaw/channels/discord"
USER_AGENT = "DiscordBot (https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore, 1.0)"

# integration_types: 0 = installed to a server, 1 = installed to a user.
# contexts: 0 = server channel, 1 = DM with the bot, 2 = group DM / other DMs.
COMMANDS = [
    {
        "name": "ask",
        "type": 1,
        "description": "Ask OpenClaw",
        "integration_types": [0, 1],
        "contexts": [0, 1, 2],
        "options": [
            {"type": 3, "name": "prompt", "description": "Your message", "required": False, "max_length": 6000},
            {"type": 11, "name": "image", "description": "An image to include (JPEG, PNG, GIF, WebP)", "required": False},
        ],
    },
    {
        "name": "link",
        "type": 1,
        "description": "Link this Discord account with your other OpenClaw channels",
        "integration_types": [0, 1],
        "contexts": [0, 1, 2],
        "options": [
            {"type": 3, "name": "code", "description": "Link code from another channel (leave empty to get one)",
             "required": False, "min_length": 8, "max_length": 8},
        ],
    },
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=None, help="AWS region of the deployment")
    parser.add_argument("--print-url", default=None, metavar="API_URL",
                        help="Router API URL; prints the Interactions Endpoint URL to paste into Discord")
    args = parser.parse_args()

    client = boto3.client("secretsmanager", region_name=args.region)
    try:
        config = json.loads(client.get_secret_value(SecretId=SECRET_ID)["SecretString"])
    except (json.JSONDecodeError, KeyError):
        sys.exit(f"{SECRET_ID} is not JSON yet; store applicationId, publicKey and botToken first.")
    app_id, bot_token = config.get("applicationId"), config.get("botToken")
    if not app_id or not bot_token:
        sys.exit(f"{SECRET_ID} needs applicationId and botToken to register commands.")

    req = urllib.request.Request(
        f"https://discord.com/api/v10/applications/{app_id}/commands",
        data=json.dumps(COMMANDS).encode(), method="PUT",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            names = [c["name"] for c in json.loads(resp.read())]
    except urllib.error.HTTPError as e:
        sys.exit(f"Discord rejected the command registration: HTTP {e.code}")
    print("Registered commands:", ", ".join(f"/{n}" for n in names))
    if args.print_url:
        print("Interactions Endpoint URL:", args.print_url.rstrip("/") + "/webhook/discord")


if __name__ == "__main__":
    main()
