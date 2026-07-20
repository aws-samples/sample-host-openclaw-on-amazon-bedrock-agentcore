"use strict";

const RUNTIME_REGION = "eu-west-1";

function requireDate(value, label) {
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) {
    throw new Error(`${label} must return a valid Date`);
  }
  return value;
}

function normalizeCredentials(credentials, now) {
  if (
    !credentials ||
    typeof credentials.accessKeyId !== "string" ||
    credentials.accessKeyId.length === 0 ||
    typeof credentials.secretAccessKey !== "string" ||
    credentials.secretAccessKey.length === 0 ||
    typeof credentials.sessionToken !== "string" ||
    credentials.sessionToken.length === 0
  ) {
    throw new Error("Complete explicit scoped credentials are required");
  }
  const expiration = new Date(credentials.expiration);
  if (!Number.isFinite(expiration.getTime()) || expiration.getTime() <= now.getTime()) {
    throw new Error("Expired scoped credentials are forbidden");
  }
  return Object.freeze({
    accessKeyId: credentials.accessKeyId,
    secretAccessKey: credentials.secretAccessKey,
    sessionToken: credentials.sessionToken,
    expiration,
  });
}

class RefreshingScopedS3 {
  constructor(options = {}) {
    const region = options.region || RUNTIME_REGION;
    if (region !== RUNTIME_REGION) {
      throw new Error(`Workspace S3 region must be exactly ${RUNTIME_REGION}`);
    }
    this.S3ClientConstructor =
      options.S3ClientConstructor || require("@aws-sdk/client-s3").S3Client;
    this.now = options.now || (() => new Date());
    this.client = null;
    this.credentials = null;
  }

  setCredentials(credentials) {
    const now = requireDate(this.now(), "Workspace credential clock");
    const nextCredentials = normalizeCredentials(credentials, now);
    const nextClient = new this.S3ClientConstructor({
      region: RUNTIME_REGION,
      credentials: {
        accessKeyId: nextCredentials.accessKeyId,
        secretAccessKey: nextCredentials.secretAccessKey,
        sessionToken: nextCredentials.sessionToken,
      },
    });
    this.credentials = nextCredentials;
    this.client = nextClient;
  }

  async send(command) {
    if (!this.client || !this.credentials) {
      throw new Error("Explicit scoped credentials are required before S3 access");
    }
    const now = requireDate(this.now(), "Workspace credential clock");
    if (this.credentials.expiration.getTime() <= now.getTime()) {
      throw new Error("Expired scoped credentials forbid S3 access");
    }
    return this.client.send(command);
  }
}

module.exports = {
  RUNTIME_REGION,
  RefreshingScopedS3,
};
