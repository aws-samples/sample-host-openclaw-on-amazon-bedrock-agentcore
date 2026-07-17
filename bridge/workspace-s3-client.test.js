"use strict";

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");

const {
  RefreshingScopedS3,
  RUNTIME_REGION,
} = require("./workspace-s3-client");

const VALID = Object.freeze({
  accessKeyId: "AKIA_SCOPED",
  secretAccessKey: "scoped-secret",
  sessionToken: "scoped-token",
  expiration: new Date("2030-01-01T00:00:00.000Z"),
});

describe("RefreshingScopedS3", () => {
  it("refuses all S3 operations before explicit scoped credentials exist", async () => {
    const adapter = new RefreshingScopedS3({
      S3ClientConstructor: class {},
      now: () => new Date("2029-01-01T00:00:00.000Z"),
    });

    await assert.rejects(adapter.send({ input: {} }), /scoped credentials/i);
  });

  it("constructs the client with only exact-region explicit credentials", async () => {
    const constructions = [];
    class FakeS3Client {
      constructor(options) {
        constructions.push(options);
      }
      async send(command) {
        return { command };
      }
    }
    const adapter = new RefreshingScopedS3({
      S3ClientConstructor: FakeS3Client,
      now: () => new Date("2029-01-01T00:00:00.000Z"),
    });

    adapter.setCredentials(VALID);
    const command = { input: { Bucket: "bucket" } };
    assert.deepEqual(await adapter.send(command), { command });
    assert.deepEqual(constructions, [
      {
        region: RUNTIME_REGION,
        credentials: {
          accessKeyId: VALID.accessKeyId,
          secretAccessKey: VALID.secretAccessKey,
          sessionToken: VALID.sessionToken,
        },
      },
    ]);
  });

  it("atomically rotates the client and rejects malformed replacement credentials", async () => {
    let nextClient = 0;
    class FakeS3Client {
      constructor(options) {
        this.id = ++nextClient;
        this.options = options;
      }
      async send() {
        return this.id;
      }
    }
    const adapter = new RefreshingScopedS3({
      S3ClientConstructor: FakeS3Client,
      now: () => new Date("2029-01-01T00:00:00.000Z"),
    });

    adapter.setCredentials(VALID);
    assert.equal(await adapter.send({}), 1);
    assert.throws(
      () => adapter.setCredentials({ ...VALID, sessionToken: "" }),
      /scoped credentials/i,
    );
    assert.equal(await adapter.send({}), 1);
    adapter.setCredentials({
      ...VALID,
      accessKeyId: "AKIA_ROTATED",
      expiration: "2031-01-01T00:00:00.000Z",
    });
    assert.equal(await adapter.send({}), 2);
  });

  it("fails closed once the active credential expires", async () => {
    let now = new Date("2029-01-01T00:00:00.000Z");
    let sends = 0;
    class FakeS3Client {
      async send() {
        sends += 1;
        return {};
      }
    }
    const adapter = new RefreshingScopedS3({
      S3ClientConstructor: FakeS3Client,
      now: () => now,
    });
    adapter.setCredentials({
      ...VALID,
      expiration: new Date("2029-01-01T00:01:00.000Z"),
    });

    await adapter.send({});
    now = new Date("2029-01-01T00:01:00.000Z");
    await assert.rejects(adapter.send({}), /expired scoped credentials/i);
    assert.equal(sends, 1);
  });

  it("rejects region overrides and non-Date clocks", () => {
    assert.throws(
      () =>
        new RefreshingScopedS3({
          S3ClientConstructor: class {},
          region: "us-west-2",
        }),
      /eu-west-1/i,
    );
    const adapter = new RefreshingScopedS3({
      S3ClientConstructor: class {},
      now: () => 0,
    });
    assert.throws(() => adapter.setCredentials(VALID), /clock/i);
  });
});
