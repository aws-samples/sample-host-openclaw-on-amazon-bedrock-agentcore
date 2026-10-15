/**
 * Bedrock model defaults and fallback policy for streaming proxy requests.
 *
 * A route allows one fallback after an allowlisted temporary rejection, only
 * before content, tool, terminal, metadata, or unrecognized stream events.
 * Cancellation and evidence of multiple SDK attempts also prevent fallback.
 * Other failures distinguish a known rejection from observed output or an
 * unknown request outcome, so callers can avoid blindly repeating effects.
 *
 * The caller sends requests and feeds their results into this tracker; it
 * records attempt metadata and exposes cancellation without issuing requests.
 */
const { osRandomUUID } = require("./runtime-lifecycle");

const PRIMARY_MODEL_ID = "global.anthropic.claude-opus-5-5";
const FALLBACK_MODEL_ID = "global.anthropic.claude-sonnet-5-5";
const SUBAGENT_MODEL_ID = "global.anthropic.claude-sonnet-5-5";
const UNKNOWN_OUTCOME_ERROR = Object.freeze({
  code: "PROVIDER_POST_DISPATCH_AMBIGUITY",
  type: "invalid_request_error",
  message: "Bedrock request outcome is unknown. Reconcile effects before another request.",
});
const TEMPORARY_ERRORS = Object.freeze({
  InternalServerException: 500,
  ServiceUnavailableException: 503,
  ThrottlingException: 429,
  ModelNotReadyException: 429,
});
const STREAM_ERRORS = Object.freeze({
  internalServerException: "InternalServerException",
  serviceUnavailableException: "ServiceUnavailableException",
  throttlingException: "ThrottlingException",
  modelStreamErrorException: "ModelStreamErrorException",
  validationException: "ValidationException",
});

// Per-model additionalModelRequestFields. Claude Haiku 5.5 (any profile prefix)
// runs with thinking disabled. Opus 5.5 and Sonnet 5.5 reject thinking.type
// "disabled" (Opus only allows adaptive), so they send nothing.
const THINKING_DISABLED_MODEL = /(?:^|[./])anthropic\.claude-haiku-5-5(?=[-.:]|$)/;

function requestFields(modelId) {
  return typeof modelId === "string" && THINKING_DISABLED_MODEL.test(modelId)
    ? { additionalModelRequestFields: { thinking: { type: "disabled" } } }
    : {};
}

function sdkMetadata(value) {
  const metadata = value?.$metadata;
  return {
    sdk_request_id: typeof metadata?.requestId === "string" &&
      /^[A-Za-z0-9_-]{1,256}$/.test(metadata.requestId) ? metadata.requestId : null,
    sdk_attempts: Number.isSafeInteger(metadata?.attempts) && metadata.attempts > 0
      ? metadata.attempts : null,
  };
}

function createStreamingRoute({ primaryModelId, fallbackModelId, response,
  emit = (record) => console.log(JSON.stringify(record)) }) {
  const controller = new AbortController();
  const routeId = osRandomUUID();
  const attempts = [];
  let outputObserved = false, fallbackSelected = false, finished = false;
  let failure;
  const cancel = () => { if (!response.writableEnded) controller.abort(); };
  response.once("close", cancel);
  if (response.destroyed) cancel();
  const active = () => {
    if (response.destroyed && !response.writableEnded) cancel();
    controller.signal.throwIfAborted();
  };
  return {
    signal: controller.signal,
    get failure() { return failure; },
    maxAttempts: fallbackModelId && fallbackModelId !== primaryModelId ? 2 : 1,
    active,
    modelId(index) {
      if (index === 0) return primaryModelId;
      if (index === 1 && fallbackSelected) return fallbackModelId;
      throw new Error("Model fallback has not been authorized.");
    },
    sent(index, params) {
      active();
      if (index !== attempts.length || params.modelId !== this.modelId(index)) {
        throw new Error("Model attempt does not match its payload.");
      }
      attempts.push({ attempt: index + 1, model_id: params.modelId, status: "sending",
        started_ns: process.hrtime.bigint().toString(), resolved_ns: null });
    },
    resolved(index, response) {
      Object.assign(attempts[index], sdkMetadata(response));
      attempts[index].resolved_ns = process.hrtime.bigint().toString();
      attempts[index].status = "streaming";
    },
    event(event) {
      active();
      // Fallback is safe only before any content, tool, guardrail or terminal event.
      if (event.contentBlockStart || event.contentBlockDelta || event.contentBlockStop ||
          event.messageStop || event.metadata) outputObserved = true;
      for (const [key, name] of Object.entries(STREAM_ERRORS)) {
        if (event[key]) throw Object.assign(new Error("Bedrock stream exception"), { name });
      }
      if (Object.keys(event).some((key) => !["messageStart", "contentBlockStart",
        "contentBlockDelta", "contentBlockStop", "messageStop", "metadata"].includes(key))) {
        outputObserved = true;
      }
    },
    failed(index, error) {
      const attempt = attempts[index];
      const status = error?.$metadata?.httpStatusCode;
      const expected = TEMPORARY_ERRORS[error?.name];
      const allowed = !!attempt && index === 0 && !fallbackSelected &&
        this.maxAttempts === 2 && !controller.signal.aborted && !response.destroyed &&
        error?.name !== "AbortError" && !outputObserved &&
        !(error?.$metadata?.attempts > 1) && expected !== undefined &&
        (status === expected || (status == null && attempt.resolved_ns !== null));
      if (attempt) {
        if (!attempt.resolved_ns) Object.assign(attempt, sdkMetadata(error));
        attempt.status = "failed";
        attempt.fallback = allowed;
        attempt.error_code = Object.hasOwn(TEMPORARY_ERRORS, error?.name) ||
          Object.values(STREAM_ERRORS).includes(error?.name) ? error.name : "OTHER";
      }
      if (allowed) fallbackSelected = true;
      else if (attempt) {
        const rejected = !(error?.$metadata?.attempts > 1) && !outputObserved &&
          ((expected !== undefined && (status === expected ||
            (status == null && attempt.resolved_ns !== null))) ||
           (error?.name === "AccessDeniedException" && status === 403) ||
           (error?.name === "ValidationException" && status === 400));
        failure = {
          code: outputObserved ? "PROVIDER_FAILURE_WITH_OUTPUT" :
            rejected ? "BEDROCK_REQUEST_REJECTED" : UNKNOWN_OUTCOME_ERROR.code,
          type: UNKNOWN_OUTCOME_ERROR.type,
          message: rejected ? "Bedrock request rejected." :
            UNKNOWN_OUTCOME_ERROR.message,
        };
      }
      return allowed;
    },
    completed(index) {
      active();
      attempts[index].status = "completed";
    },
    finish() {
      if (finished) return;
      finished = true;
      response.off("close", cancel);
      try { emit({ schema: "openclaw.model-route.v1", route_id: routeId,
        pid: process.pid, fallback_selected: fallbackSelected, attempts }); } catch {}
    },
  };
}

module.exports = { PRIMARY_MODEL_ID, FALLBACK_MODEL_ID, SUBAGENT_MODEL_ID, UNKNOWN_OUTCOME_ERROR, requestFields, createStreamingRoute };
