# Code Review: feature/agentcore-browser

## Summary

The AgentCore Browser feature adds an optional headless Chromium browser skill to the OpenClaw container, with CDK infrastructure, session lifecycle management, screenshot delivery to Telegram/Slack, and comprehensive tests. The overall design is solid — the feature is cleanly gated behind `enable_browser=true`, follows existing patterns (skill scripts, S3 uploads, marker-based delivery), and is non-fatal on failure. However, there are **two security issues** (S3 key injection, missing namespace validation), **one bug** (undefined variable reference), and several minor improvements needed before merge.

**Diff scope:** 24 files, +1,709 / −22 lines across CDK, bridge skill, contract server, router lambda, and tests.

---

## Critical / Major Issues (must fix before merge)

### [CRITICAL] S3 key injection via `[SCREENSHOT:]` marker — no namespace validation
**File:** `lambda/router/index.py:639-648`
**Issue:** `_fetch_s3_image(s3_key)` passes the S3 key extracted from the `[SCREENSHOT:key]` marker directly to `s3_client.get_object()` with **zero validation**. The marker content comes from the AI model's response text. If the model is manipulated (prompt injection) or a future code path produces a crafted marker, an attacker could read **any object** in the `S3_USER_FILES_BUCKET` bucket — including other users' files, workspace backups, and API key data.

**Risk:** Cross-user data exfiltration. The S3 bucket is shared across all users with namespace-prefix isolation. An injected key like `[SCREENSHOT:other_user_namespace/secret.txt]` would fetch and deliver that file to the requesting user's chat.

**Fix:** Validate that the S3 key starts with the current user's namespace and is within the `_screenshots/` prefix. Reject keys containing `..`:

```python
def _fetch_s3_image(s3_key: str, namespace: str):
    """Fetch image bytes from S3. Returns None on error or invalid key."""
    # Validate key belongs to user's screenshot namespace
    if ".." in s3_key:
        logger.error("Rejected S3 key with path traversal: %s", s3_key)
        return None
    expected_prefix = f"{namespace}/_screenshots/"
    if not s3_key.startswith(expected_prefix):
        logger.error("Rejected S3 key outside user namespace: %s (expected prefix: %s)", s3_key, expected_prefix)
        return None
    # ... existing fetch logic
```

Then pass `namespace` from both `handle_telegram()` and `handle_slack()` where it's already resolved.

---

### [MAJOR] Undefined variable `currentUserId` in `stopBrowserSessions()`
**File:** `bridge/agentcore-contract.js:552-554`
**Issue:** `stopBrowserSessions()` references `currentUserId` in log messages, but the function is a standalone async function. While `currentUserId` is a module-level variable (line 48), this works in the current per-user container model. However, if `stopBrowserSessions()` is ever called before `init()` sets `currentUserId`, the log will print `null`. More importantly, the test file `browser-lifecycle.test.js` re-implements the stop function with a `userSessions` Map parameter — this divergence means the tests don't actually test the production code's variable scoping.

**Risk:** If the contract server is terminated before init completes (e.g., during scaling), the log line references unset state. The behavioral mismatch between test and production code means bugs in the real function are not caught.

**Fix:** Either pass `userId` as a parameter to `stopBrowserSessions()`, or use `currentBrowserSessionId` in the log (which is already checked):

```javascript
console.log(`[browser] Stopped session: ${currentBrowserSessionId}`);
```

---

### [MAJOR] `uploadScreenshotToS3` does not validate `S3_USER_FILES_BUCKET`
**File:** `bridge/skills/agentcore-browser/common.js:50-62`
**Issue:** If `S3_USER_FILES_BUCKET` env var is missing/empty, `uploadScreenshotToS3` will call `PutObjectCommand` with `Bucket: undefined`, producing a confusing AWS SDK error rather than a clear skill-level error message.

**Risk:** Bad developer experience and confusing error messages when the env var is misconfigured.

**Fix:** Add an early guard:

```javascript
if (!bucket) {
  throw new Error("S3_USER_FILES_BUCKET environment variable is not set — cannot upload screenshot");
}
```

---

### [MAJOR] `ap-southeast-2` missing from `BROWSER_SUPPORTED_REGIONS`
**File:** `stacks/agentcore_stack.py:25`
**Issue:** `BROWSER_SUPPORTED_REGIONS = {"us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"}` — the project's default deployment region is `ap-southeast-2` (per `cdk.json` and CLAUDE.md). If someone sets `enable_browser=true` and deploys to the default region, the browser resource will silently not be deployed (only a CDK warning annotation).

**Risk:** The default deployment path fails silently for the new feature. Users will see "Browser session not available" at runtime with no clear indication that the region is the problem.

**Fix:** Either add `ap-southeast-2` if the service is available there, or make the warning more prominent (e.g., `add_error` instead of `add_warning` to fail the synth). At minimum, document the region limitation in the SKILL.md.

---

## Minor Issues (recommended improvements)

### [MINOR] No timeout on `StopBrowserSession` API call during SIGTERM
**File:** `bridge/agentcore-contract.js:536-555`
**Issue:** `stopBrowserSessions()` calls `StopBrowserSessionCommand` without a timeout. AgentCore gives 15 seconds total for SIGTERM shutdown. If the browser API call hangs, it will consume the entire grace period, potentially preventing workspace save (which runs before it) — wait, workspace save runs before browser stop, so this is OK. But it could still delay process exit.

**Risk:** Slow shutdown if the browser API is unresponsive.

**Fix:** Add an `AbortController` timeout or `Promise.race` with a 3-second deadline:

```javascript
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 3000);
try {
  await client.send(new StopBrowserSessionCommand({...}), { abortSignal: controller.signal });
} finally {
  clearTimeout(timeout);
}
```

---

### [MINOR] `document.cloneNode(true)` in `navigate.js` — potential memory pressure
**File:** `bridge/skills/agentcore-browser/navigate.js:14-17`
**Issue:** `document.cloneNode(true)` creates a full deep copy of the DOM to remove script/style elements before extracting text. On very large pages, this doubles memory usage briefly inside the browser context.

**Risk:** Low — the browser runs in the AgentCore microVM with limited memory. A very large page could cause an OOM in the browser process.

**Fix:** Consider removing elements from the live DOM within an evaluate block that doesn't clone, or use a more targeted extraction:

```javascript
const content = await page.evaluate(() => {
  const sel = 'script, style, noscript, iframe';
  document.querySelectorAll(sel).forEach(el => el.remove());
  return document.body?.innerText || "";
});
```

This mutates the live DOM, but since the browser session persists, the user would see a modified page on subsequent interactions. The clone approach is safer but heavier. Consider documenting this trade-off.

---

### [MINOR] `scroll` action hardcodes 500px with no configurability
**File:** `bridge/skills/agentcore-browser/interact.js:39`
**Issue:** The `scroll` action ignores the `selector` parameter and always scrolls by 500px. The SKILL.md documents `selector` as "optional" for scroll, which is technically correct, but doesn't mention the fixed scroll amount.

**Risk:** User confusion when asking to scroll to a specific element.

**Fix:** Document the 500px behavior in SKILL.md, or better, support an optional `amount` parameter and scroll-to-element when `selector` is provided:

```javascript
case "scroll":
  if (selector) {
    await page.locator(selector).scrollIntoViewIfNeeded({ timeout: INTERACT_TIMEOUT_MS });
    return JSON.stringify({ success: true, message: `Scrolled to: ${selector}` });
  }
  await page.evaluate((px) => window.scrollBy(0, px), amount || 500);
  return JSON.stringify({ success: true, message: `Scrolled down ${amount || 500}px` });
```

---

### [MINOR] `_send_telegram_photo` boundary could collide
**File:** `lambda/router/index.py:668`
**Issue:** `boundary = "----FormBoundary" + str(int(time.time()))` uses a timestamp with 1-second granularity. If two screenshots are sent in the same second, they'd share a boundary. This is extremely unlikely to cause issues in practice since each is a separate HTTP request, but using a more unique boundary is better practice.

**Fix:** Use `uuid.uuid4().hex[:16]` or add a random suffix.

---

### [MINOR] `playwright-core` added to Dockerfile but not used during warm-up
**File:** `bridge/Dockerfile:43`
**Issue:** `playwright-core` is installed in the container image even when `enable_browser=false` (the default). This adds ~10MB to the image size for a feature that most deployments won't use.

**Risk:** Slightly larger container image, slightly longer cold start.

**Fix:** Consider making the npm install conditional on a build arg, or accept the trade-off and document it. This is low priority given the image already includes multiple AWS SDKs.

---

### [MINOR] Test divergence — `browser-lifecycle.test.js` re-implements functions
**File:** `bridge/browser-lifecycle.test.js`
**Issue:** The test file re-implements `initBrowserSession` and `stopBrowserSessions` with a `userSessions` Map parameter instead of testing the actual functions from `agentcore-contract.js`. The production code uses module-level `currentBrowserSessionId`/`currentBrowserEndpoint` variables, not a Map. This means tests could pass while the production code has bugs.

**Risk:** False confidence in test coverage. The undefined variable bug in `stopBrowserSessions` (using `currentUserId` in logs) is not caught because the test's version doesn't reference it.

**Fix:** Either extract the browser lifecycle functions from `agentcore-contract.js` into a separate module that can be properly tested, or use module mocking to test the actual contract server functions.

---

### [MINOR] No test for S3 key injection in `test_screenshot_handling.py`
**File:** `lambda/router/test_screenshot_handling.py`
**Issue:** The test suite covers marker extraction, S3 fetch, and photo delivery, but does **not** test the security case where a `[SCREENSHOT:../other_user/file]` key is injected. Once the namespace validation fix (Critical issue #1) is implemented, add a test.

**Fix:** Add a test case:

```python
def test_rejects_path_traversal(self):
    result = _fetch_s3_image("../../etc/passwd", namespace="telegram_123")
    self.assertIsNone(result)

def test_rejects_cross_namespace(self):
    result = _fetch_s3_image("other_user/_screenshots/shot.png", namespace="telegram_123")
    self.assertIsNone(result)
```

---

## Nitpicks (optional)

### [NIT] Model ID default changed in `agentcore-proxy.js` and `agentcore_stack.py`
**Files:** `bridge/agentcore-proxy.js:20`, `stacks/agentcore_stack.py:276`
**Issue:** The default model ID was changed from `global.anthropic.claude-opus-4-6-v1` to `minimax.minimax-m2.1`. This is unrelated to the browser feature and should ideally be a separate commit/PR for clean git history.

### [NIT] `bridge/package.json` added
**File:** `bridge/package.json`
**Issue:** New file duplicates the dependency list already maintained in the Dockerfile's `npm install` command. Either the Dockerfile should `COPY package.json` and `npm install` from it, or this file should be removed to avoid drift.

### [NIT] `enable_browser_raw in (True, "true", "True")` — doesn't handle `"TRUE"` or `"yes"`
**File:** `stacks/agentcore_stack.py:364`
**Issue:** CDK context values from CLI `-c enable_browser=TRUE` would not match. Consider `str(enable_browser_raw).lower() == "true"` for robustness.

### [NIT] Unused import in `conftest.py`
**File:** `tests/e2e/conftest.py:2`
**Issue:** `from pathlib import Path` and `import json` are added, which is fine for the fixture, but if `enable_browser` is later moved to a config module, these become unused.

---

## Positives

- **Clean feature gating**: The `enable_browser` flag cleanly controls CDK resource creation, container env var injection, and runtime behavior. No browser code runs when disabled.
- **Non-fatal design**: Browser init failures don't break the container. The `catch` + log pattern in `initBrowserSession` and the "Browser is not available" error messages in skill scripts are well-designed.
- **Good CDK region guard**: The `BROWSER_SUPPORTED_REGIONS` check with a CDK warning prevents silent deployment failures in unsupported regions.
- **Comprehensive E2E test skip**: The `browser_enabled` pytest fixture cleanly skips browser tests when the feature is not configured, preventing false failures.
- **Consistent patterns**: The new skill follows the exact same structure as existing skills (SKILL.md, common.js, individual action scripts). The screenshot marker pattern (`[SCREENSHOT:key]`) mirrors the existing image upload pattern.
- **IAM scoping**: Browser permissions are scoped to `self.browser.attr_browser_arn` — not a wildcard.
- **Good test coverage**: 241-line unit test for the skill, 324-line lifecycle test, 224-line screenshot handling test, and E2E tests with navigate/screenshot/interact flows.
- **Slack v2 file upload**: Correctly uses the modern three-step Slack file upload API instead of the deprecated `files.upload`.

---

## Verdict

**[X] REQUEST CHANGES** — fix critical/major issues first:

1. **[CRITICAL]** Add S3 key namespace validation in `_fetch_s3_image()` to prevent cross-user data access
2. **[MAJOR]** Fix undefined variable reference in `stopBrowserSessions()` log messages
3. **[MAJOR]** Add `S3_USER_FILES_BUCKET` guard in `uploadScreenshotToS3()`
4. **[MAJOR]** Address `ap-southeast-2` missing from `BROWSER_SUPPORTED_REGIONS` (verify availability or fail loudly)

After these four fixes + corresponding test additions, this is ready to merge.
