/**
 * Async scheduled turns (cron_start / cron_status).
 *
 * The cron Lambda can wait at most 900 s (Lambda's hard limit), and a long brief
 * needs more. With async cron the Lambda sends `cron_start` and returns; the
 * contract runs the turn with its own job deadline and keeps the result here. A
 * Step Functions poller then asks `cron_status` every minute and the Lambda
 * delivers the result with its existing delivery code.
 *
 * The store keeps each job in memory and, when session storage is mounted, in one
 * JSON file at the mount root (outside the .openclaw mirror, so the mirror never
 * prunes it). After a container restart a job that was still running is reported
 * as lost: nothing in this process is running it any more.
 */
const fs = require("fs");
const path = require("path");

// The per-job deadline. Default 45 min; at most 55 min. The cap comes from the
// turn's scoped S3 credentials: role chaining limits them to 1 h, and a turn
// needs them valid until its deadline plus 60 s (turnValidityMs). That is far
// below the AgentCore session's 8 h maxLifetime.
const DEFAULT_JOB_BUDGET_MS = 45 * 60 * 1000;
const MAX_JOB_BUDGET_MS = 55 * 60 * 1000;
const MIN_JOB_BUDGET_MS = 60 * 1000;
// Kept for the poller and for duplicate detection, then pruned.
const JOB_RETENTION_MS = 24 * 60 * 60 * 1000;
const MAX_JOBS = 50;
// A done job's reply is delivered by the Lambda through Step Functions (256 KB
// state limit), so the stored text is capped well under it.
const MAX_RESPONSE_CHARS = 100000;
const JOB_ID_RE = /^[A-Za-z0-9_-]{1,80}$/;
const STORE_FILE = "openclaw-cron-jobs.json";

function envBudgetMs(value) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) return DEFAULT_JOB_BUDGET_MS;
  return Math.min(MAX_JOB_BUDGET_MS, Math.max(MIN_JOB_BUDGET_MS, parsed));
}

/**
 * Validate a cron_start payload. Returns { ok, jobId, budgetMs } or { ok: false, error }.
 * jobBudgetMs is optional; without it the deployment default applies.
 */
function parseCronStart(payload, { defaultBudgetMs = DEFAULT_JOB_BUDGET_MS } = {}) {
  const { jobId, jobBudgetMs } = payload || {};
  if (typeof jobId !== "string" || !JOB_ID_RE.test(jobId)) {
    return { ok: false, error: "jobId must be 1-80 letters, digits, '-' or '_'" };
  }
  if (payload.callerBudgetMs !== undefined) {
    return { ok: false, error: "cron_start takes jobBudgetMs, not callerBudgetMs" };
  }
  if (jobBudgetMs === undefined) return { ok: true, jobId, budgetMs: defaultBudgetMs };
  if (!Number.isSafeInteger(jobBudgetMs) || jobBudgetMs < MIN_JOB_BUDGET_MS || jobBudgetMs > MAX_JOB_BUDGET_MS) {
    return { ok: false, error: `jobBudgetMs must be an integer from ${MIN_JOB_BUDGET_MS} to ${MAX_JOB_BUDGET_MS}` };
  }
  return { ok: true, jobId, budgetMs: jobBudgetMs };
}

function summarize(result) {
  // Only what the Lambda needs to deliver or report a failure.
  const body = result && typeof result === "object" ? result : {};
  const response = typeof body.response === "string" ? body.response : "";
  const failed = body.error !== undefined || body.status === "error" || body.status === "failed" ||
    body.status === "unknown_outcome" || typeof body.response !== "string" ||
    // release/v1 replies carry no execution block; a V2 reply must say completed.
    (body.execution !== undefined && body.execution?.phase !== "completed");
  return {
    outcome: body.status === "unknown_outcome" ? "unknown_outcome" : failed ? "error" : "completed",
    response: failed ? "" : response.slice(0, MAX_RESPONSE_CHARS),
    truncated: !failed && response.length > MAX_RESPONSE_CHARS,
  };
}

function createCronJobStore({ dir = null, now = Date.now, bootId, log = console } = {}) {
  const jobs = new Map();
  const file = dir ? path.join(dir, STORE_FILE) : null;

  function save() {
    if (!file) return;
    try {
      const tmp = `${file}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify([...jobs.values()]));
      fs.renameSync(tmp, file);
    } catch (error) {
      // Memory still answers while this container lives.
      log.warn("[cron-jobs] Could not save the job store: %s", error.code || error.name);
    }
  }

  function prune() {
    const cutoff = now() - JOB_RETENTION_MS;
    for (const [id, job] of jobs) if (job.acceptedAt < cutoff && job.state !== "running") jobs.delete(id);
    const extra = [...jobs.values()].filter((job) => job.state !== "running")
      .sort((a, b) => a.acceptedAt - b.acceptedAt);
    while (jobs.size > MAX_JOBS && extra.length) jobs.delete(extra.shift().jobId);
  }

  function load() {
    if (!file) return;
    let list;
    try {
      list = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") log.warn("[cron-jobs] Ignoring an unreadable job store: %s", error.code || error.name);
      return;
    }
    if (!Array.isArray(list)) return;
    for (const job of list) {
      if (!job || typeof job.jobId !== "string" || !JOB_ID_RE.test(job.jobId)) continue;
      // A job another boot was running has no runner now.
      if (job.state === "running" && job.bootId !== bootId) job.state = "lost";
      jobs.set(job.jobId, job);
    }
    prune();
  }

  return {
    load,
    /** Register a job. Returns { duplicate: true, job } when the jobId is known. */
    start(jobId, { userId, deadline }) {
      const existing = jobs.get(jobId);
      if (existing) return { duplicate: true, job: existing };
      const job = { jobId, userId, state: "running", bootId, acceptedAt: now(), deadline };
      jobs.set(jobId, job);
      prune();
      save();
      return { duplicate: false, job };
    },
    finish(jobId, result) {
      const job = jobs.get(jobId);
      if (!job || job.state !== "running") return;
      Object.assign(job, { state: "done", finishedAt: now(), ...summarize(result) });
      save();
    },
    /** cron_status answer. Never queued and never waits on a turn. */
    status(jobId, userId) {
      const job = typeof jobId === "string" ? jobs.get(jobId) : undefined;
      if (!job || job.userId !== userId) return { status: "unknown", jobId };
      if (job.state === "lost") return { status: "unknown", jobId, reason: "lost" };
      if (job.state === "running") return { status: "running", jobId, deadline: job.deadline };
      return {
        status: "done", jobId, outcome: job.outcome, response: job.response,
        // Same shape as a turn reply, so the Lambda keeps an unknown outcome's admission.
        execution: { phase: job.outcome },
        ...(job.truncated ? { truncated: true } : {}),
      };
    },
    size: () => jobs.size,
  };
}

module.exports = {
  DEFAULT_JOB_BUDGET_MS, MAX_JOB_BUDGET_MS, MIN_JOB_BUDGET_MS, MAX_JOBS, MAX_RESPONSE_CHARS, STORE_FILE,
  envBudgetMs, parseCronStart, createCronJobStore, summarize,
};
