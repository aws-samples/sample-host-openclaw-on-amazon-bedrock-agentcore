/**
 * Tests for workspace-sync.js — credential configuration, skip patterns,
 * and credential detection guard for S3 isolation.
 *
 * Covers: configureCredentials(), shouldSkip(), detectCredentials(),
 *         credential validation, client replacement.
 * Note: S3Client creation is tested implicitly (SDK only in Docker image).
 * Run: cd bridge && node --test workspace-sync.test.js
 */
const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");

describe("workspace-sync credentials", () => {
  let workspaceSync;

  beforeEach(() => {
    // Fresh module on each test
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    workspaceSync = require("./workspace-sync");
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("exports configureCredentials function", () => {
    assert.equal(typeof workspaceSync.configureCredentials, "function");
  });

  it("exports getS3Client function", () => {
    assert.equal(typeof workspaceSync.getS3Client, "function");
  });

  it("configureCredentials accepts valid credentials without throwing", () => {
    // Should not throw (S3Client created lazily, not at configureCredentials time)
    workspaceSync.configureCredentials({
      accessKeyId: "AKIAIOSFODNN7EXAMPLE",
      secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
      sessionToken: "FwoGZXIvYXdzEBYaDH...",
    });
  });

  it("rejects configureCredentials with missing accessKeyId", () => {
    assert.throws(
      () =>
        workspaceSync.configureCredentials({
          secretAccessKey: "secret",
          sessionToken: "token",
        }),
      /accessKeyId/i,
    );
  });

  it("rejects configureCredentials with missing secretAccessKey", () => {
    assert.throws(
      () =>
        workspaceSync.configureCredentials({
          accessKeyId: "AKIAEXAMPLE",
          sessionToken: "token",
        }),
      /secretAccessKey/i,
    );
  });

  it("rejects configureCredentials with null credentials", () => {
    assert.throws(
      () => workspaceSync.configureCredentials(null),
      /accessKeyId/i,
    );
  });

  it("rejects configureCredentials with empty object", () => {
    assert.throws(
      () => workspaceSync.configureCredentials({}),
      /accessKeyId/i,
    );
  });
});

// --- shouldSkip ---

describe("shouldSkip", () => {
  let shouldSkip;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    shouldSkip = require("./workspace-sync").shouldSkip;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  // Original patterns
  it("skips node_modules/ directory", () => {
    assert.ok(shouldSkip("node_modules/some-package/index.js"));
  });

  it("skips .cache/ directory", () => {
    assert.ok(shouldSkip(".cache/data"));
  });

  it("skips *.log files", () => {
    assert.ok(shouldSkip("debug.log"));
    assert.ok(shouldSkip("subdir/error.log"));
  });

  it("skips *.lock files", () => {
    assert.ok(shouldSkip("yarn.lock"));
  });

  it("skips openclaw.json", () => {
    assert.ok(shouldSkip("openclaw.json"));
  });

  // New security patterns
  it("skips .env files", () => {
    assert.ok(shouldSkip(".env"));
  });

  it("skips .secrets/ directory", () => {
    assert.ok(shouldSkip(".secrets/api-key.txt"));
  });

  it("skips *.pem files", () => {
    assert.ok(shouldSkip("cert.pem"));
    assert.ok(shouldSkip("subdir/private.pem"));
  });

  it("skips *.key files", () => {
    assert.ok(shouldSkip("server.key"));
    assert.ok(shouldSkip("tls/private.key"));
  });

  // Should NOT skip
  it("does not skip user-api-keys.json", () => {
    assert.ok(!shouldSkip("user-api-keys.json"));
  });

  it("does not skip regular files", () => {
    assert.ok(!shouldSkip("notes.md"));
    assert.ok(!shouldSkip("AGENTS.md"));
    assert.ok(!shouldSkip("data/config.yaml"));
  });
});

// --- detectCredentials ---

describe("detectCredentials", () => {
  let detectCredentials;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    detectCredentials = require("./workspace-sync").detectCredentials;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("detects AWS access key IDs", () => {
    const content = 'aws_access_key_id = AKIAIOSFODNN7EXAMPLE';
    assert.ok(detectCredentials(content));
  });

  it("detects OpenAI API keys", () => {
    const content = 'OPENAI_API_KEY=sk-proj1234567890abcdefghij';
    assert.ok(detectCredentials(content));
  });

  it("detects Slack tokens", () => {
    const content = 'token: xoxb-123456789012-abcdefgh';
    assert.ok(detectCredentials(content));
  });

  it("detects Telegram bot tokens", () => {
    // Telegram tokens: 8-10 digit bot ID + colon + exactly 35 alphanumeric/dash/underscore chars
    const content = '123456789:ABCdefGHI_jklMNOpqrSTUvwxYZ01234567';
    assert.ok(detectCredentials(content));
  });

  it("detects private key headers", () => {
    const content = '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAI...';
    assert.ok(detectCredentials(content));
  });

  it("detects GitHub personal access tokens", () => {
    const content = 'GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij';
    assert.ok(detectCredentials(content));
  });

  it("detects GitLab personal access tokens", () => {
    const content = 'GL_TOKEN=glpat-abcdef1234567890abcdef';
    assert.ok(detectCredentials(content));
  });

  it("returns null for safe content", () => {
    assert.equal(detectCredentials("Hello, this is a normal document."), null);
  });

  it("returns null for empty content", () => {
    assert.equal(detectCredentials(""), null);
  });

  it("returns null for JSON without secrets", () => {
    const content = JSON.stringify({ name: "test", value: 42 });
    assert.equal(detectCredentials(content), null);
  });

  it("works with Buffer input", () => {
    const content = Buffer.from('OPENAI_API_KEY=sk-proj1234567890abcdefghij');
    assert.ok(detectCredentials(content));
  });

  it("works with large Buffer (scans first 64KB only)", () => {
    // Create a buffer larger than 64KB with a secret near the start
    const secret = 'AKIAIOSFODNN7EXAMPLE';
    const padding = "x".repeat(100 * 1024);
    const content = Buffer.from(secret + padding);
    assert.ok(detectCredentials(content));
  });
});

// --- CREDENTIAL_SCAN_EXEMPT ---

describe("CREDENTIAL_SCAN_EXEMPT", () => {
  let CREDENTIAL_SCAN_EXEMPT;

  beforeEach(() => {
    delete require.cache[require.resolve("./workspace-sync")];
    process.env.AWS_REGION = "us-west-2";
    process.env.S3_USER_FILES_BUCKET = "test-bucket";
    CREDENTIAL_SCAN_EXEMPT = require("./workspace-sync").CREDENTIAL_SCAN_EXEMPT;
  });

  afterEach(() => {
    delete process.env.S3_USER_FILES_BUCKET;
  });

  it("is user-api-keys.json", () => {
    assert.equal(CREDENTIAL_SCAN_EXEMPT, "user-api-keys.json");
  });
});
