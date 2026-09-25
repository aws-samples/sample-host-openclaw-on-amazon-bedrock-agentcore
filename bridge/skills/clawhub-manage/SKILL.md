---
name: clawhub-manage
description: Install, uninstall, and list ClawHub community skills. Use when the user asks to install a new skill, remove an existing skill, or see what skills are available. Installed skills are recorded per user and reinstalled automatically a few seconds after each new session starts.
allowed-tools: Bash(node:*)
---

# ClawHub Skill Manager

Install, uninstall, and list ClawHub community skills from the marketplace.

## Usage

### install_skill

Install a community skill from the ClawHub marketplace.

```bash
node {baseDir}/install.js <skill_name>
```

- `skill_name` (required): The skill name from ClawHub (e.g., `baidu-search`, `reddit-readonly`)

### uninstall_skill

Remove a previously installed skill.

```bash
node {baseDir}/uninstall.js <skill_name>
```

- `skill_name` (required): The skill name to remove

### list_skills

List all installed ClawHub skills.

```bash
node {baseDir}/list.js
```

## From Agent Chat

- "Install baidu-search skill" -> install_skill with `baidu-search`
- "Add the reddit-readonly skill" -> install_skill with `reddit-readonly`
- "Remove the transcript skill" -> uninstall_skill with `transcript`
- "What skills are installed?" -> list_skills
- "Show me available skills" -> list_skills

## Notes

- Installs go to `/skills` and are recorded, with the installed version pinned, in the user's persistent state (`~/.openclaw/runtime-skills.json`). `/skills` itself does not survive a cold start, so a few seconds after a new session starts the bridge reinstalls every recorded skill in the background. Until that finishes (a few seconds per skill) a recorded skill may briefly be missing; `list_skills` shows any that are still pending or whose reinstall failed.
- OpenClaw picks a newly installed or removed skill up when it next refreshes its skill list (on file change, or at the latest at the next session start).
- Installs never bypass ClawHub's security review: a skill flagged as suspicious or malicious is refused with an explanation rather than installed. Tell the user why and do not retry with other flags.
- Only valid ClawHub skill names are accepted (letters, numbers, hyphens; no `@owner/` prefix).
- Pre-installed skills (`jina-reader`, `deep-research-pro`, `telegram-compose`, `transcript`, `task-decomposer`) ship with the container image and cannot be uninstalled persistently.
