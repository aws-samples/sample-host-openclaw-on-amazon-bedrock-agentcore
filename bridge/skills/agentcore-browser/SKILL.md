---
name: agentcore-browser
description: Browse web pages using a headless Chromium browser running inside the AgentCore container. Navigate to URLs, take screenshots, and interact with page elements (click, type, wait, scroll). Requires enable_browser=true in CDK config. Use when the user asks to visit a website, take a screenshot, fill in a form, or interact with a web page.
allowed-tools: Bash(node:*)
---

# AgentCore Browser

Headless Chromium browser running inside the AgentCore container. Navigate to URLs, take screenshots, and interact with page elements.

## Important

This skill requires `enable_browser=true` in CDK configuration. If the browser is not available, the tools will return a clear error message.

## Usage

### browser_navigate

Navigate to a URL and return the page title and text content.

```bash
node {baseDir}/navigate.js '{"url": "https://example.com"}'
```

- `url` (required): The URL to navigate to

Returns JSON: `{"url": "...", "title": "...", "content": "..."}`

Content is truncated to 8000 characters to keep responses manageable.

### browser_screenshot

Take a screenshot of the current browser page and send it to the user.

```bash
node {baseDir}/screenshot.js '{"description": "Homepage after login"}'
```

- `description` (optional): Caption for the screenshot

Returns text with `[SCREENSHOT:{s3key}]` marker that the proxy converts to an image.

### browser_interact

Interact with the current page — click elements, type text, wait for elements, or scroll.

```bash
node {baseDir}/interact.js '{"action": "click", "selector": "#submit-btn"}'
node {baseDir}/interact.js '{"action": "type", "selector": "#search", "text": "hello"}'
node {baseDir}/interact.js '{"action": "wait", "selector": ".results"}'
node {baseDir}/interact.js '{"action": "scroll"}'
```

- `action` (required): One of `click`, `type`, `wait`, `scroll`
- `selector` (optional): CSS selector for the target element (required for click, type, wait)
- `text` (optional): Text to type (required for type action)

Returns JSON: `{"success": true, "message": "..."}`

## From Agent Chat

- "Go to example.com" -> browser_navigate with url
- "Take a screenshot" -> browser_screenshot
- "Click the login button" -> browser_interact with action=click
- "Type my email in the form" -> browser_interact with action=type
- "Scroll down" -> browser_interact with action=scroll
- "Wait for the results to load" -> browser_interact with action=wait

## Security Notes

- Browser runs inside an isolated per-user microVM
- Screenshots are uploaded to the user's S3 namespace (no cross-user access)
- The browser session file is stored at `/tmp/agentcore-browser-session.json`
- Navigation timeout: 30s, interaction timeout: 10s, wait timeout: 15s
