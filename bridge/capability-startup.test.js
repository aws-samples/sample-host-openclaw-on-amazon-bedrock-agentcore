"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");

process.env.AWS_REGION = "eu-west-1";
process.env.AWS_DEFAULT_REGION = "eu-west-1";

const contractSource = fs.readFileSync(
  path.join(__dirname, "agentcore-contract.js"),
  "utf8",
);
const dockerfile = fs.readFileSync(path.join(__dirname, "Dockerfile"), "utf8");

describe("release-bound capability startup", () => {
  it("does not construct a plugin, listener, or child after release validation fails", () => {
    const { createCapabilityStartupGate } = require("./agentcore-contract");
    const events = [];
    const gate = createCapabilityStartupGate({
      validateRelease: () => {
        events.push("validate");
        throw new Error("catalog digest drift");
      },
      constructRuntime: () => events.push("construct"),
    });

    assert.throws(() => gate.start(), /catalog digest drift/);
    assert.deepEqual(events, ["validate"]);
    assert.equal(gate.started, false);
  });

  it("validates once before runtime construction and reuses the frozen startup", () => {
    const { createCapabilityStartupGate } = require("./agentcore-contract");
    const events = [];
    const validated = Object.freeze({ catalogDigest: "a".repeat(64) });
    const runtime = Object.freeze({ agent: "reviewed" });
    const gate = createCapabilityStartupGate({
      validateRelease: () => {
        events.push("validate");
        return validated;
      },
      constructRuntime: (release) => {
        events.push(["construct", release]);
        return runtime;
      },
    });

    assert.strictEqual(gate.start(), runtime);
    assert.strictEqual(gate.start(), runtime);
    assert.equal(gate.started, true);
    assert.deepEqual(events, ["validate", ["construct", validated]]);
  });

  it("runs the real startup gate before relay listening and server listening", () => {
    assert.doesNotMatch(
      contractSource,
      /^const agent = require\("\.\/lightweight-agent"\);$/m,
    );
    const start = contractSource.indexOf("function startContractServer");
    const gate = contractSource.indexOf("runtimeCapabilityStartup.start()", start);
    const relay = contractSource.indexOf("ensureCapabilityRelayServer()", start);
    const listener = contractSource.indexOf("server.listen(", start);
    assert.ok(start >= 0 && gate > start && relay > gate && listener > relay);
  });

  it("requires exact image release inputs and verifies generated metadata at build time", () => {
    assert.match(dockerfile, /^ARG PERSONAL_OPERATOR_RELEASE_COMMIT$/m);
    assert.match(dockerfile, /^ARG PERSONAL_OPERATOR_CATALOG_DIGEST$/m);
    assert.match(dockerfile, /personal-operator\.capability-release\.v1/);
    assert.match(dockerfile, /release-v1\.json/);
    assert.match(dockerfile, /loadRuntimeCapabilityRelease/);
  });
});
