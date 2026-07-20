"use strict";

const { describe, it, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const temporaryRoots = [];

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function mountInfoEscape(value) {
  return value
    .replaceAll("\\", "\\134")
    .replaceAll(" ", "\\040")
    .replaceAll("\t", "\\011")
    .replaceAll("\n", "\\012");
}

function fixture({ writable = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "po-lifecycle-"));
  temporaryRoots.push(root);
  const mountedDir = path.join(root, "mounted workspace");
  const seedDir = path.join(root, "seed");
  const homeDir = path.join(root, "home");
  const homeLinkPath = path.join(homeDir, ".openclaw");
  const mountInfoPath = path.join(root, "mountinfo");
  fs.mkdirSync(mountedDir, { recursive: true });
  fs.mkdirSync(seedDir, { recursive: true });
  fs.mkdirSync(homeDir, { recursive: true });
  fs.writeFileSync(path.join(seedDir, "seed.txt"), "immutable seed");
  fs.writeFileSync(
    mountInfoPath,
    `42 31 0:99 / ${mountInfoEscape(mountedDir)} ${
      writable ? "rw" : "ro"
    },nosuid,nodev - ext4 /dev/test ${writable ? "rw" : "ro"}\n`,
  );
  return {
    root,
    mountedDir,
    seedDir,
    homeLinkPath,
    mountInfoPath,
    namespace: "user_01",
  };
}

function committedHead(overrides = {}) {
  return {
    generation: "g-123e4567-e89b-42d3-a456-426614174000",
    manifestSha256: "a".repeat(64),
    parent: null,
    newUser: false,
    ...overrides,
  };
}

async function loadLifecycle() {
  return require("./workspace-lifecycle");
}

describe("prepareWorkspace", () => {
  it("fails closed when the configured path is not an exact writable mount", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const readOnly = fixture({ writable: false });
    const missing = fixture();
    fs.writeFileSync(
      missing.mountInfoPath,
      "42 31 0:99 / /somewhere-else rw - ext4 /dev/test rw\n",
    );

    for (const current of [readOnly, missing]) {
      let restored = false;
      await assert.rejects(
        prepareWorkspace({
          ...current,
          snapshotStore: {
            restore: async () => {
              restored = true;
              return committedHead();
            },
          },
        }),
        /mount|writable/i,
      );
      assert.equal(restored, false);
      assert.equal(fs.existsSync(path.join(current.mountedDir, "live")), false);
    }
  });

  it("probes create, fsync, read, and unlink before restore", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    const calls = [];
    const instrumentedFs = Object.create(fs);
    let probeFd = null;
    for (const method of ["openSync", "writeFileSync", "fsyncSync", "readFileSync", "unlinkSync"]) {
      instrumentedFs[method] = (...args) => {
        const candidate = String(args[0]);
        if (
          candidate.includes(".personal-operator-mount-probe") ||
          (probeFd !== null && args[0] === probeFd)
        ) {
          calls.push(method);
        }
        const result = fs[method](...args);
        if (
          method === "openSync" &&
          candidate.includes(".personal-operator-mount-probe")
        ) {
          probeFd = result;
        }
        if (
          method === "unlinkSync" &&
          candidate.includes(".personal-operator-mount-probe")
        ) {
          probeFd = null;
        }
        return result;
      };
    }

    await prepareWorkspace({
      ...current,
      fs: instrumentedFs,
      snapshotStore: {
        restore: async ({ targetDir }) => {
          fs.mkdirSync(targetDir, { mode: 0o700 });
          fs.writeFileSync(path.join(targetDir, "restored.txt"), "ok");
          return committedHead();
        },
      },
    });

    assert.deepEqual(calls, [
      "openSync",
      "writeFileSync",
      "fsyncSync",
      "readFileSync",
      "unlinkSync",
    ]);
    assert.equal(
      fs.existsSync(path.join(current.mountedDir, ".personal-operator-mount-probe")),
      false,
    );
  });

  it("removes a partial probe and never restores after probe failure", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    let restored = false;
    const failingFs = Object.create(fs);
    failingFs.writeFileSync = (target, ...args) => {
      if (typeof target === "number") throw new Error("probe write denied");
      return fs.writeFileSync(target, ...args);
    };

    await assert.rejects(
      prepareWorkspace({
        ...current,
        fs: failingFs,
        snapshotStore: {
          restore: async () => {
            restored = true;
            return committedHead();
          },
        },
      }),
      /probe write denied/,
    );
    assert.equal(restored, false);
    assert.equal(
      fs.existsSync(path.join(current.mountedDir, ".personal-operator-mount-probe")),
      false,
    );
  });

  it("restores to private staging, writes the canonical marker, activates, then links", async () => {
    const { prepareWorkspace, READY_MARKER_BASENAME } = await loadLifecycle();
    const current = fixture();
    const observations = [];
    const head = committedHead();

    const result = await prepareWorkspace({
      ...current,
      snapshotStore: {
        restore: async ({ targetDir, seedDir }) => {
          observations.push({
            targetDir,
            seedDir,
            linkExists: fs.existsSync(current.homeLinkPath),
            liveExists: fs.existsSync(path.join(current.mountedDir, "live")),
          });
          assert.equal(fs.existsSync(targetDir), false);
          fs.mkdirSync(targetDir, { mode: 0o700 });
          fs.mkdirSync(path.join(targetDir, "workspace"), { recursive: true });
          fs.writeFileSync(path.join(targetDir, "workspace", "notes.md"), "restored");
          return head;
        },
      },
    });

    const liveDir = path.join(current.mountedDir, "live");
    assert.deepEqual(observations, [
      {
        targetDir: observations[0].targetDir,
        seedDir: current.seedDir,
        linkExists: false,
        liveExists: false,
      },
    ]);
    assert.match(path.basename(observations[0].targetDir), /^\.staging-/);
    assert.equal(fs.readlinkSync(current.homeLinkPath), liveDir);
    assert.equal(fs.readFileSync(path.join(liveDir, "workspace", "notes.md"), "utf8"), "restored");
    assert.equal(
      fs.readFileSync(path.join(liveDir, READY_MARKER_BASENAME), "utf8"),
      JSON.stringify({
        format: "personal-operator.workspace-ready.v1",
        generation: head.generation,
        manifestSha256: head.manifestSha256,
        namespace: current.namespace,
      }),
    );
    assert.equal(result.liveDir, liveDir);
    assert.deepEqual(result.head, head);
  });

  it("commits an immutable seed before activating a new user", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    const order = [];
    const initial = committedHead({
      generation: null,
      manifestSha256: null,
      newUser: true,
    });
    const committed = committedHead({ newUser: false });

    const result = await prepareWorkspace({
      ...current,
      snapshotStore: {
        restore: async ({ targetDir, seedDir }) => {
          order.push("restore");
          fs.mkdirSync(targetDir, { mode: 0o700 });
          fs.writeFileSync(
            path.join(targetDir, "seed.txt"),
            fs.readFileSync(path.join(seedDir, "seed.txt")),
          );
          return initial;
        },
        commit: async ({ liveDir, assertWritable }) => {
          order.push("commit-seed");
          assert.equal(fs.readFileSync(path.join(liveDir, "seed.txt"), "utf8"), "immutable seed");
          await assertWritable();
          return committed;
        },
      },
      assertWritable: async () => order.push("writable"),
    });

    assert.deepEqual(order, ["restore", "commit-seed", "writable"]);
    assert.deepEqual(result.head, committed);
  });

  it("leaves the last good live tree and home link untouched on restore failure", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    const liveDir = path.join(current.mountedDir, "live");
    fs.mkdirSync(liveDir);
    fs.writeFileSync(path.join(liveDir, "old.txt"), "last good");
    fs.symlinkSync(liveDir, current.homeLinkPath);

    await assert.rejects(
      prepareWorkspace({
        ...current,
        snapshotStore: {
          restore: async ({ targetDir }) => {
            fs.mkdirSync(targetDir, { mode: 0o700 });
            fs.writeFileSync(path.join(targetDir, "partial.txt"), "partial");
            throw new Error("hash mismatch");
          },
        },
      }),
      /hash mismatch/,
    );

    assert.equal(fs.readFileSync(path.join(liveDir, "old.txt"), "utf8"), "last good");
    assert.equal(fs.readlinkSync(current.homeLinkPath), liveDir);
    assert.deepEqual(
      fs.readdirSync(current.mountedDir).filter((entry) => entry.startsWith(".staging-")),
      [],
    );
  });

  it("rolls the previous tree back if atomic activation fails", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    const liveDir = path.join(current.mountedDir, "live");
    fs.mkdirSync(liveDir);
    fs.writeFileSync(path.join(liveDir, "old.txt"), "last good");
    let failedOnce = false;
    const failingFs = Object.create(fs);
    failingFs.renameSync = (source, target) => {
      if (!failedOnce && path.basename(source).startsWith(".staging-") && target === liveDir) {
        failedOnce = true;
        throw new Error("activation interrupted");
      }
      return fs.renameSync(source, target);
    };

    await assert.rejects(
      prepareWorkspace({
        ...current,
        fs: failingFs,
        snapshotStore: {
          restore: async ({ targetDir }) => {
            fs.mkdirSync(targetDir, { mode: 0o700 });
            fs.writeFileSync(path.join(targetDir, "new.txt"), "new");
            return committedHead();
          },
        },
      }),
      /activation interrupted/,
    );

    assert.equal(fs.readFileSync(path.join(liveDir, "old.txt"), "utf8"), "last good");
    assert.equal(fs.existsSync(path.join(liveDir, "new.txt")), false);
  });

  it("refuses to replace a non-symlink home state path", async () => {
    const { prepareWorkspace } = await loadLifecycle();
    const current = fixture();
    fs.mkdirSync(current.homeLinkPath);
    fs.writeFileSync(path.join(current.homeLinkPath, "do-not-delete"), "owned");

    await assert.rejects(
      prepareWorkspace({
        ...current,
        snapshotStore: {
          restore: async ({ targetDir }) => {
            fs.mkdirSync(targetDir, { mode: 0o700 });
            fs.writeFileSync(path.join(targetDir, "restored.txt"), "ok");
            return committedHead();
          },
        },
      }),
      /symlink|home state/i,
    );
    assert.equal(
      fs.readFileSync(path.join(current.homeLinkPath, "do-not-delete"), "utf8"),
      "owned",
    );
  });

  it("removes a newly created home link and restores old live on marker verification failure", async () => {
    const { prepareWorkspace, READY_MARKER_BASENAME } = await loadLifecycle();
    const current = fixture();
    const liveDir = path.join(current.mountedDir, "live");
    fs.mkdirSync(liveDir);
    fs.writeFileSync(path.join(liveDir, "old.txt"), "last good");
    const failingFs = Object.create(fs);
    failingFs.readFileSync = (candidate, ...args) => {
      if (
        String(candidate).endsWith(READY_MARKER_BASENAME) &&
        String(candidate).includes(`${path.sep}live${path.sep}`)
      ) {
        throw new Error("marker read interrupted");
      }
      return fs.readFileSync(candidate, ...args);
    };

    await assert.rejects(
      prepareWorkspace({
        ...current,
        fs: failingFs,
        snapshotStore: {
          restore: async ({ targetDir }) => {
            fs.mkdirSync(targetDir, { mode: 0o700 });
            fs.writeFileSync(path.join(targetDir, "new.txt"), "new");
            return committedHead();
          },
        },
      }),
      /marker read interrupted/,
    );

    assert.equal(fs.existsSync(current.homeLinkPath), false);
    assert.equal(fs.readFileSync(path.join(liveDir, "old.txt"), "utf8"), "last good");
    assert.equal(fs.existsSync(path.join(liveDir, "new.txt")), false);
  });
});

describe("WorkspaceLifecycle", () => {
  function lifecycleFixture(overrides = {}) {
    const calls = [];
    const store = {
      commit: async ({ assertWritable }) => {
        calls.push("commit");
        await assertWritable();
        return committedHead();
      },
      ...overrides.snapshotStore,
    };
    const prepare = async () => {
      calls.push("prepare");
      return {
        liveDir: "/mnt/workspace/live",
        head: committedHead(),
      };
    };
    return {
      calls,
      store,
      options: {
        snapshotStore: store,
        namespace: "user_01",
        seedDir: "/opt/personal-operator/seed",
        mountedDir: "/mnt/workspace",
        homeLinkPath: "/run/personal-operator/home/.openclaw",
        prepareWorkspace: prepare,
        stopOpenClaw: async () => calls.push("stop-openclaw"),
        stopSupportProcesses: async () => calls.push("stop-support"),
        ...overrides.options,
      },
    };
  }

  it("initializes exactly once before admitting turns", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const current = lifecycleFixture();
    const lifecycle = new WorkspaceLifecycle(current.options);

    assert.equal(lifecycle.status().state, "NEW");
    assert.throws(() => lifecycle.beginTurn(), /not ready/i);
    await lifecycle.initialize();
    await lifecycle.initialize();
    assert.equal(lifecycle.status().state, "READY");
    assert.deepEqual(current.calls, ["prepare"]);
    assert.ok(lifecycle.beginTurn());
  });

  it("does not resolve a successful turn until the durable commit resolves", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const commit = deferred();
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async ({ assertWritable }) => {
          current.calls.push("commit-start");
          await assertWritable();
          const result = await commit.promise;
          current.calls.push("commit-end");
          return result;
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    const turn = lifecycle.beginTurn();
    let settled = false;
    const persisted = lifecycle.commitAfterTurn(turn).then(() => {
      settled = true;
    });
    await Promise.resolve();
    assert.equal(settled, false);
    assert.equal(
      lifecycle.status().activeTurns,
      1,
      "the turn remains exclusive until its durable commit settles",
    );
    commit.resolve(committedHead());
    await persisted;
    assert.equal(settled, true);
  });

  it("does not admit a turn until an already-running periodic commit finishes", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const gates = [deferred(), deferred()];
    let active = 0;
    let maximum = 0;
    let invocation = 0;
    const order = [];
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          const index = invocation++;
          order.push(`start-${index}`);
          active++;
          maximum = Math.max(maximum, active);
          await gates[index].promise;
          active--;
          order.push(`end-${index}`);
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    const first = lifecycle.requestPeriodicCommit();
    await Promise.resolve();
    assert.deepEqual(order, ["start-0"]);
    let admitted = false;
    const turnPromise = lifecycle.acquireTurn().then((turn) => {
      admitted = true;
      return turn;
    });
    await Promise.resolve();
    assert.equal(admitted, false);
    gates[0].resolve();
    await first;
    const turn = await turnPromise;
    const second = lifecycle.commitAfterTurn(turn);
    await Promise.resolve();
    assert.deepEqual(order, ["start-0", "end-0", "start-1"]);
    gates[1].resolve();
    await second;
    assert.equal(maximum, 1);
  });

  it("holds one exclusive turn through its durable commit before admitting the next", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const firstCommit = deferred();
    let commits = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          commits += 1;
          if (commits === 1) await firstCommit.promise;
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const firstTurn = await lifecycle.acquireTurn();
    let secondAdmitted = false;
    const secondTurnPromise = lifecycle.acquireTurn().then((turn) => {
      secondAdmitted = true;
      return turn;
    });
    const firstPersisted = lifecycle.commitAfterTurn(firstTurn);
    await Promise.resolve();
    assert.equal(secondAdmitted, false);
    assert.equal(lifecycle.status().activeTurns, 1);

    firstCommit.resolve();
    await firstPersisted;
    const secondTurn = await secondTurnPromise;
    assert.equal(secondAdmitted, true);
    assert.equal(lifecycle.status().activeTurns, 1);
    await lifecycle.commitAfterTurn(secondTurn);
    assert.equal(commits, 2);
  });

  it("never runs a periodic snapshot while a turn is active or queued", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    let commits = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          commits += 1;
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const first = await lifecycle.acquireTurn();
    const queued = lifecycle.acquireTurn();
    const skipped = await lifecycle.requestPeriodicCommit();
    assert.equal(commits, 0);
    assert.equal(skipped.generation, committedHead().generation);

    await lifecycle.commitAfterTurn(first);
    const second = await queued;
    assert.equal(commits, 1);
    await lifecycle.commitAfterTurn(second);
    assert.equal(commits, 2);
  });

  it("coalesces overlapping periodic requests into one bounded commit", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const gate = deferred();
    let commits = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          commits++;
          await gate.promise;
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    const first = lifecycle.requestPeriodicCommit();
    const second = lifecycle.requestPeriodicCommit();
    await Promise.resolve();
    assert.equal(commits, 1);
    gate.resolve();
    assert.deepEqual(await Promise.all([first, second]), [
      committedHead(),
      committedHead(),
    ]);
    assert.equal(commits, 1);
  });

  it("serializes a manual snapshot fairly between already-queued workspace turns", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const gates = [deferred(), deferred(), deferred()];
    const started = [deferred(), deferred(), deferred()];
    const order = [];
    let commitIndex = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async ({ reason }) => {
          const index = commitIndex++;
          order.push(`start-${reason}-${index}`);
          started[index].resolve();
          await gates[index].promise;
          order.push(`end-${reason}-${index}`);
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const firstTurn = await lifecycle.acquireTurn();
    const manualSnapshot = lifecycle.requestManualSnapshot();
    let secondAdmitted = false;
    const secondTurnPromise = lifecycle.acquireTurn().then((turn) => {
      secondAdmitted = true;
      return turn;
    });
    const firstPersisted = lifecycle.commitAfterTurn(firstTurn);
    await started[0].promise;
    assert.deepEqual(order, ["start-post-turn-0"]);
    assert.equal(secondAdmitted, false);

    gates[0].resolve();
    await firstPersisted;
    await started[1].promise;
    assert.deepEqual(order, [
      "start-post-turn-0",
      "end-post-turn-0",
      "start-manual-1",
    ]);
    assert.equal(secondAdmitted, false);

    gates[1].resolve();
    assert.deepEqual(await manualSnapshot, committedHead());
    const secondTurn = await secondTurnPromise;
    assert.equal(secondAdmitted, true);

    const secondPersisted = lifecycle.commitAfterTurn(secondTurn);
    await started[2].promise;
    assert.deepEqual(order, [
      "start-post-turn-0",
      "end-post-turn-0",
      "start-manual-1",
      "end-manual-1",
      "start-post-turn-2",
    ]);
    gates[2].resolve();
    await secondPersisted;
  });

  it("waits for a legacy active turn instead of racing its manual snapshot", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const firstCommit = deferred();
    const manualCommit = deferred();
    const started = [deferred(), deferred()];
    let commitIndex = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async ({ reason }) => {
          const index = commitIndex++;
          started[index].resolve(reason);
          await [firstCommit, manualCommit][index].promise;
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const turn = lifecycle.beginTurn();
    const snapshot = lifecycle.requestManualSnapshot();
    let snapshotSettled = false;
    void snapshot.then(
      () => {
        snapshotSettled = true;
      },
      () => {
        snapshotSettled = true;
      },
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(snapshotSettled, false);
    const persisted = lifecycle.commitAfterTurn(turn);
    assert.equal(await started[0].promise, "post-turn");
    assert.equal(commitIndex, 1);

    firstCommit.resolve();
    await persisted;
    assert.equal(await started[1].promise, "manual");
    manualCommit.resolve();
    await snapshot;
    assert.equal(commitIndex, 2);
  });

  it("skips periodic work while an exclusive manual snapshot is pending", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const gate = deferred();
    const started = deferred();
    let commits = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          commits += 1;
          started.resolve();
          await gate.promise;
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const manualSnapshot = lifecycle.requestManualSnapshot();
    const periodic = await lifecycle.requestPeriodicCommit();
    await started.promise;
    assert.equal(commits, 1);
    assert.deepEqual(periodic, committedHead());

    gate.resolve();
    await manualSnapshot;
    assert.equal(commits, 1);
  });

  it("quarantines after a manual snapshot persistence failure", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          throw new Error("manual S3 CAS failed");
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    await assert.rejects(
      lifecycle.requestManualSnapshot(),
      (error) =>
        error.code === "WORKSPACE_PERSISTENCE_FAILED" && error.retryable === true,
    );
    assert.equal(lifecycle.status().state, "QUARANTINED");
    assert.equal(
      lifecycle.status().quarantine.code,
      "WORKSPACE_PERSISTENCE_FAILED",
    );
  });

  it("quarantines after a post-turn persistence failure", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          throw new Error("S3 CAS failed");
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    const turn = lifecycle.beginTurn();

    await assert.rejects(
      lifecycle.commitAfterTurn(turn),
      (error) =>
        error.code === "WORKSPACE_PERSISTENCE_FAILED" && error.retryable === true,
    );
    assert.equal(lifecycle.status().state, "QUARANTINED");
    assert.throws(() => lifecycle.beginTurn(), /quarantined/i);
    await assert.rejects(lifecycle.requestPeriodicCommit(), /quarantined/i);
  });

  it("drains an active turn, stops OpenClaw, snapshots, then stops support", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const current = lifecycleFixture();
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    const turn = lifecycle.beginTurn();
    const shutdown = lifecycle.shutdown();
    await Promise.resolve();
    assert.equal(lifecycle.status().state, "DRAINING");
    assert.deepEqual(current.calls, ["prepare"]);

    await lifecycle.commitAfterTurn(turn);
    await shutdown;
    assert.deepEqual(current.calls, [
      "prepare",
      "commit",
      "stop-openclaw",
      "commit",
      "stop-support",
    ]);
    assert.equal(lifecycle.status().state, "STOPPED");
  });

  it("stops support but fails shutdown when the final snapshot fails", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    let commitCount = 0;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => {
          commitCount++;
          throw new Error("final snapshot unavailable");
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    await assert.rejects(lifecycle.shutdown(), /final snapshot unavailable/);
    assert.equal(commitCount, 1);
    assert.deepEqual(current.calls, ["prepare", "stop-openclaw", "stop-support"]);
    assert.equal(lifecycle.status().state, "FAILED");
  });

  it("bounds every shutdown phase and still attempts child cleanup", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    const never = new Promise(() => {});
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async () => never,
      },
      options: { shutdownTimeoutMs: 5 },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();

    const outcome = await Promise.race([
      lifecycle.shutdown().then(
        () => ({ status: "fulfilled" }),
        (error) => ({ status: "rejected", error }),
      ),
      new Promise((resolve) =>
        setTimeout(() => resolve({ status: "hung" }), 100),
      ),
    ]);

    assert.equal(outcome.status, "rejected");
    assert.match(outcome.error.message, /timed out/i);
    assert.deepEqual(current.calls, [
      "prepare",
      "stop-openclaw",
      "stop-support",
    ]);
    assert.equal(lifecycle.status().state, "FAILED");
  });

  it("binds writable assertions to the initialized namespace and state", async () => {
    const { WorkspaceLifecycle } = await loadLifecycle();
    let writable;
    const current = lifecycleFixture({
      snapshotStore: {
        commit: async ({ assertWritable }) => {
          writable = assertWritable;
          await assertWritable();
          return committedHead();
        },
      },
    });
    const lifecycle = new WorkspaceLifecycle(current.options);
    await lifecycle.initialize();
    await lifecycle.requestPeriodicCommit();
    assert.equal(typeof writable, "function");
    await lifecycle.shutdown();
    await assert.rejects(writable(), /writable|stopped/i);
  });
});
