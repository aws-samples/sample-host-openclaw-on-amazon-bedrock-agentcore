/**
 * Tests for lightweight-agent.js — cron tool additions.
 *
 * Covers: TOOLS definitions, SCRIPT_MAP, TOOL_ENV, and buildToolArgs logic.
 * Run: cd bridge && node --test lightweight-agent.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { TOOLS, SCRIPT_MAP, TOOL_ENV, buildToolArgs } = require("./lightweight-agent");

// --- TOOLS array ---

describe("TOOLS", () => {
  const EXPECTED_TOOLS = [
    "read_user_file",
    "write_user_file",
    "list_user_files",
    "delete_user_file",
    "create_schedule",
    "list_schedules",
    "update_schedule",
    "delete_schedule",
  ];

  it("contains all 8 expected tools", () => {
    const names = TOOLS.map((t) => t.function.name);
    assert.deepStrictEqual(names, EXPECTED_TOOLS);
  });

  it("every tool has valid OpenAI function-calling schema", () => {
    for (const tool of TOOLS) {
      assert.equal(tool.type, "function", `${tool.function?.name} type`);
      assert.ok(tool.function.name, "function.name required");
      assert.ok(tool.function.description, "function.description required");
      assert.equal(
        tool.function.parameters.type,
        "object",
        `${tool.function.name} parameters.type`,
      );
      assert.ok(
        Array.isArray(tool.function.parameters.required),
        `${tool.function.name} required array`,
      );
    }
  });

  it("create_schedule requires cron_expression, timezone, message", () => {
    const tool = TOOLS.find((t) => t.function.name === "create_schedule");
    assert.deepStrictEqual(tool.function.parameters.required, [
      "cron_expression",
      "timezone",
      "message",
    ]);
    const props = Object.keys(tool.function.parameters.properties);
    assert.ok(props.includes("cron_expression"));
    assert.ok(props.includes("timezone"));
    assert.ok(props.includes("message"));
    assert.ok(props.includes("schedule_name"));
  });

  it("list_schedules has no required params", () => {
    const tool = TOOLS.find((t) => t.function.name === "list_schedules");
    assert.deepStrictEqual(tool.function.parameters.required, []);
  });

  it("update_schedule requires schedule_id only", () => {
    const tool = TOOLS.find((t) => t.function.name === "update_schedule");
    assert.deepStrictEqual(tool.function.parameters.required, ["schedule_id"]);
    const props = Object.keys(tool.function.parameters.properties);
    assert.ok(props.includes("schedule_id"));
    assert.ok(props.includes("expression"));
    assert.ok(props.includes("timezone"));
    assert.ok(props.includes("message"));
    assert.ok(props.includes("name"));
    assert.ok(props.includes("enable"));
    assert.ok(props.includes("disable"));
  });

  it("delete_schedule requires schedule_id", () => {
    const tool = TOOLS.find((t) => t.function.name === "delete_schedule");
    assert.deepStrictEqual(tool.function.parameters.required, ["schedule_id"]);
  });
});

// --- SCRIPT_MAP ---

describe("SCRIPT_MAP", () => {
  it("has an entry for every tool in TOOLS", () => {
    for (const tool of TOOLS) {
      const name = tool.function.name;
      assert.ok(SCRIPT_MAP[name], `SCRIPT_MAP missing entry for ${name}`);
    }
  });

  it("cron scripts point to /skills/eventbridge-cron/", () => {
    assert.equal(SCRIPT_MAP.create_schedule, "/skills/eventbridge-cron/create.js");
    assert.equal(SCRIPT_MAP.list_schedules, "/skills/eventbridge-cron/list.js");
    assert.equal(SCRIPT_MAP.update_schedule, "/skills/eventbridge-cron/update.js");
    assert.equal(SCRIPT_MAP.delete_schedule, "/skills/eventbridge-cron/delete.js");
  });

  it("s3 scripts point to /skills/s3-user-files/", () => {
    assert.equal(SCRIPT_MAP.read_user_file, "/skills/s3-user-files/read.js");
    assert.equal(SCRIPT_MAP.write_user_file, "/skills/s3-user-files/write.js");
    assert.equal(SCRIPT_MAP.list_user_files, "/skills/s3-user-files/list.js");
    assert.equal(SCRIPT_MAP.delete_user_file, "/skills/s3-user-files/delete.js");
  });
});

// --- TOOL_ENV ---

describe("TOOL_ENV", () => {
  it("includes base env vars", () => {
    assert.ok("PATH" in TOOL_ENV);
    assert.ok("HOME" in TOOL_ENV);
    assert.ok("NODE_PATH" in TOOL_ENV);
    assert.ok("NODE_OPTIONS" in TOOL_ENV);
    assert.ok("AWS_REGION" in TOOL_ENV);
    assert.ok("S3_USER_FILES_BUCKET" in TOOL_ENV);
  });

  it("includes cron env vars", () => {
    assert.ok("EVENTBRIDGE_SCHEDULE_GROUP" in TOOL_ENV);
    assert.ok("CRON_LAMBDA_ARN" in TOOL_ENV);
    assert.ok("EVENTBRIDGE_ROLE_ARN" in TOOL_ENV);
    assert.ok("IDENTITY_TABLE_NAME" in TOOL_ENV);
  });

  it("defaults cron env vars to empty string when not set", () => {
    // In test environment, these env vars are not set
    // TOOL_ENV should default them to "" rather than undefined
    assert.equal(typeof TOOL_ENV.EVENTBRIDGE_SCHEDULE_GROUP, "string");
    assert.equal(typeof TOOL_ENV.CRON_LAMBDA_ARN, "string");
    assert.equal(typeof TOOL_ENV.EVENTBRIDGE_ROLE_ARN, "string");
    assert.equal(typeof TOOL_ENV.IDENTITY_TABLE_NAME, "string");
  });
});

// --- buildToolArgs ---

describe("buildToolArgs", () => {
  const USER_ID = "telegram_12345";

  it("returns null for unknown tool", () => {
    assert.equal(buildToolArgs("nonexistent_tool", {}, USER_ID), null);
  });

  // --- s3-user-files (existing, verify no regression) ---

  it("read_user_file: script, userId, filename", () => {
    const result = buildToolArgs("read_user_file", { filename: "notes.md" }, USER_ID);
    assert.deepStrictEqual(result, [
      "/skills/s3-user-files/read.js",
      USER_ID,
      "notes.md",
    ]);
  });

  it("read_user_file: defaults filename to empty string", () => {
    const result = buildToolArgs("read_user_file", {}, USER_ID);
    assert.equal(result[2], "");
  });

  it("list_user_files: script, userId only", () => {
    const result = buildToolArgs("list_user_files", {}, USER_ID);
    assert.deepStrictEqual(result, ["/skills/s3-user-files/list.js", USER_ID]);
  });

  it("delete_user_file: script, userId, filename", () => {
    const result = buildToolArgs("delete_user_file", { filename: "old.txt" }, USER_ID);
    assert.deepStrictEqual(result, [
      "/skills/s3-user-files/delete.js",
      USER_ID,
      "old.txt",
    ]);
  });

  // --- create_schedule ---

  it("create_schedule: positional args (expression, timezone, message)", () => {
    const result = buildToolArgs(
      "create_schedule",
      {
        cron_expression: "cron(0 9 * * ? *)",
        timezone: "Asia/Shanghai",
        message: "Check email",
      },
      USER_ID,
    );
    assert.deepStrictEqual(result, [
      "/skills/eventbridge-cron/create.js",
      USER_ID,
      "cron(0 9 * * ? *)",
      "Asia/Shanghai",
      "Check email",
    ]);
  });

  it("create_schedule: includes schedule_name with channel placeholders", () => {
    const result = buildToolArgs(
      "create_schedule",
      {
        cron_expression: "cron(0 17 ? * MON-FRI *)",
        timezone: "America/New_York",
        message: "Log hours",
        schedule_name: "Work reminder",
      },
      USER_ID,
    );
    assert.deepStrictEqual(result, [
      "/skills/eventbridge-cron/create.js",
      USER_ID,
      "cron(0 17 ? * MON-FRI *)",
      "America/New_York",
      "Log hours",
      "", // channel placeholder
      "", // channelTarget placeholder
      "Work reminder",
    ]);
  });

  it("create_schedule: omits placeholders when no schedule_name", () => {
    const result = buildToolArgs(
      "create_schedule",
      {
        cron_expression: "rate(1 hour)",
        timezone: "UTC",
        message: "Ping",
      },
      USER_ID,
    );
    // Should be exactly 5 elements — no placeholders
    assert.equal(result.length, 5);
  });

  it("create_schedule: defaults missing required args to empty string", () => {
    const result = buildToolArgs("create_schedule", {}, USER_ID);
    assert.equal(result[2], ""); // cron_expression
    assert.equal(result[3], ""); // timezone
    assert.equal(result[4], ""); // message
  });

  // --- list_schedules ---

  it("list_schedules: script, userId only", () => {
    const result = buildToolArgs("list_schedules", {}, USER_ID);
    assert.deepStrictEqual(result, ["/skills/eventbridge-cron/list.js", USER_ID]);
  });

  // --- update_schedule ---

  it("update_schedule: minimal (schedule_id only)", () => {
    const result = buildToolArgs(
      "update_schedule",
      { schedule_id: "a1b2c3d4" },
      USER_ID,
    );
    assert.deepStrictEqual(result, [
      "/skills/eventbridge-cron/update.js",
      USER_ID,
      "a1b2c3d4",
    ]);
  });

  it("update_schedule: all optional flags", () => {
    const result = buildToolArgs(
      "update_schedule",
      {
        schedule_id: "a1b2c3d4",
        expression: "cron(30 8 * * ? *)",
        timezone: "Europe/London",
        message: "New message",
        name: "Morning alert",
      },
      USER_ID,
    );
    assert.deepStrictEqual(result, [
      "/skills/eventbridge-cron/update.js",
      USER_ID,
      "a1b2c3d4",
      "--expression",
      "cron(30 8 * * ? *)",
      "--timezone",
      "Europe/London",
      "--message",
      "New message",
      "--name",
      "Morning alert",
    ]);
  });

  it("update_schedule: --enable flag", () => {
    const result = buildToolArgs(
      "update_schedule",
      { schedule_id: "abc", enable: true },
      USER_ID,
    );
    assert.ok(result.includes("--enable"));
    assert.ok(!result.includes("--disable"));
  });

  it("update_schedule: --disable flag", () => {
    const result = buildToolArgs(
      "update_schedule",
      { schedule_id: "abc", disable: true },
      USER_ID,
    );
    assert.ok(result.includes("--disable"));
    assert.ok(!result.includes("--enable"));
  });

  it("update_schedule: enable+disable conflict — neither flag passed", () => {
    const result = buildToolArgs(
      "update_schedule",
      { schedule_id: "abc", enable: true, disable: true },
      USER_ID,
    );
    assert.ok(!result.includes("--enable"), "should not include --enable");
    assert.ok(!result.includes("--disable"), "should not include --disable");
  });

  it("update_schedule: enable=false does not push --enable", () => {
    const result = buildToolArgs(
      "update_schedule",
      { schedule_id: "abc", enable: false },
      USER_ID,
    );
    assert.ok(!result.includes("--enable"));
    assert.ok(!result.includes("--disable"));
  });

  it("update_schedule: defaults missing schedule_id to empty string", () => {
    const result = buildToolArgs("update_schedule", {}, USER_ID);
    assert.equal(result[2], "");
  });

  // --- delete_schedule ---

  it("delete_schedule: script, userId, schedule_id", () => {
    const result = buildToolArgs(
      "delete_schedule",
      { schedule_id: "deadbeef" },
      USER_ID,
    );
    assert.deepStrictEqual(result, [
      "/skills/eventbridge-cron/delete.js",
      USER_ID,
      "deadbeef",
    ]);
  });

  it("delete_schedule: defaults missing schedule_id to empty string", () => {
    const result = buildToolArgs("delete_schedule", {}, USER_ID);
    assert.equal(result[2], "");
  });
});

// --- Argument position alignment with actual scripts ---

describe("CLI arg alignment with script argv positions", () => {
  // These tests verify the argument positions match what each script expects.
  // create.js: argv[2]=userId, argv[3]=expression, argv[4]=timezone,
  //            argv[5]=message, argv[6]=channel, argv[7]=channelTarget,
  //            argv[8+]=scheduleName

  it("create_schedule args align with create.js argv expectations", () => {
    const args = buildToolArgs(
      "create_schedule",
      {
        cron_expression: "cron(0 9 * * ? *)",
        timezone: "UTC",
        message: "Test",
        schedule_name: "My Schedule",
      },
      "telegram_999",
    );
    // args[0] = script path (becomes argv[1] when prefixed with "node")
    // In execFile("node", args), argv = [node, args[0], args[1], ...]
    // So: argv[2] = args[1] = userId
    assert.equal(args[1], "telegram_999"); // argv[2]
    assert.equal(args[2], "cron(0 9 * * ? *)"); // argv[3]
    assert.equal(args[3], "UTC"); // argv[4]
    assert.equal(args[4], "Test"); // argv[5]
    assert.equal(args[5], ""); // argv[6] - channel placeholder
    assert.equal(args[6], ""); // argv[7] - channelTarget placeholder
    assert.equal(args[7], "My Schedule"); // argv[8] - scheduleName
  });

  // update.js: argv[2]=userId, argv[3]=scheduleId, argv[4+]=flags
  it("update_schedule args align with update.js parseArgs expectations", () => {
    const args = buildToolArgs(
      "update_schedule",
      {
        schedule_id: "abc12345",
        expression: "cron(0 10 * * ? *)",
        message: "Updated msg",
      },
      "slack_U123",
    );
    assert.equal(args[1], "slack_U123"); // argv[2]
    assert.equal(args[2], "abc12345"); // argv[3]
    // Flags start at argv[4+], which is args[3+]
    assert.equal(args[3], "--expression");
    assert.equal(args[4], "cron(0 10 * * ? *)");
    assert.equal(args[5], "--message");
    assert.equal(args[6], "Updated msg");
  });

  // list.js: argv[2]=userId
  it("list_schedules args align with list.js argv expectations", () => {
    const args = buildToolArgs("list_schedules", {}, "telegram_999");
    assert.equal(args.length, 2); // [script, userId]
    assert.equal(args[1], "telegram_999"); // argv[2]
  });

  // delete.js: argv[2]=userId, argv[3]=scheduleId
  it("delete_schedule args align with delete.js argv expectations", () => {
    const args = buildToolArgs("delete_schedule", { schedule_id: "ff00ff00" }, "telegram_999");
    assert.equal(args[1], "telegram_999"); // argv[2]
    assert.equal(args[2], "ff00ff00"); // argv[3]
  });
});
