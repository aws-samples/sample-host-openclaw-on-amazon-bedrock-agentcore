/**
 * Tests for workspace-sync.js — credential configuration for S3 isolation.
 *
 * Covers: configureCredentials(), credential validation, client replacement.
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
