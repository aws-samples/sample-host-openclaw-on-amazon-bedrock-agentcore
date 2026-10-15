/**
 * Bedrock Converse inferenceConfig for agentcore-proxy.js.
 *
 * Claude Opus 5 and Sonnet 5 and later (and Haiku 5.5) reject `temperature` on
 * Converse/ConverseStream ("ValidationException: temperature is deprecated for
 * this model"), so the key must be left out of inferenceConfig for those
 * models. Every other model keeps the historical temperature of 0.7.
 *
 * BEDROCK_TEMPERATURE overrides the per-model default for every model:
 *   unset / ""  -> per-model default (above)
 *   "none"      -> never send temperature
 *   a number    -> send that temperature (must be finite and >= 0)
 * An invalid value is ignored (per-model default) and reported as `invalid`.
 */

const MAX_TOKENS = 16384;
const DEFAULT_TEMPERATURE = 0.7;

// Anthropic Opus/Sonnet major version at or above which temperature is rejected.
const NO_TEMPERATURE_MIN_MAJOR = 5;
// Other models that reject a non-default temperature, under any profile prefix.
const NO_TEMPERATURE_MODEL = /(?:^|[./])anthropic\.claude-haiku-5-5(?=[-.:]|$)/;

/**
 * True when the model rejects `temperature`. Matches the model name after any
 * region/global inference-profile prefix ("global.", "us.", "apac.") or ARN
 * path ("arn:...:inference-profile/global.anthropic...").
 */
function modelRejectsTemperature(modelId) {
  if (typeof modelId !== "string" || !modelId) return false;
  if (NO_TEMPERATURE_MODEL.test(modelId)) return true;
  const m = modelId.match(/(?:^|[./])anthropic\.claude-(?:opus|sonnet)-(\d+)(?=[-.:]|$)/);
  if (!m) return false;
  return Number(m[1]) >= NO_TEMPERATURE_MIN_MAJOR;
}

/**
 * Parse the BEDROCK_TEMPERATURE override.
 * Returns { mode: "auto" | "omit" | "set", value?: number, invalid?: string }.
 */
function parseTemperatureOverride(raw) {
  if (raw === undefined || raw === null) return { mode: "auto" };
  const text = String(raw).trim();
  if (text === "") return { mode: "auto" };
  if (text.toLowerCase() === "none") return { mode: "omit" };
  const value = Number(text);
  if (Number.isFinite(value) && value >= 0) return { mode: "set", value };
  return { mode: "auto", invalid: text };
}

/**
 * Temperature to send for a model, or undefined when it must be omitted.
 */
function resolveTemperature(modelId, override) {
  const o = override || { mode: "auto" };
  if (o.mode === "omit") return undefined;
  if (o.mode === "set") return o.value;
  return modelRejectsTemperature(modelId) ? undefined : DEFAULT_TEMPERATURE;
}

/**
 * Build the Converse inferenceConfig. The temperature key is absent (not
 * null/undefined) when it must not be sent.
 */
function buildInferenceConfig(modelId, override) {
  const temperature = resolveTemperature(modelId, override);
  return {
    maxTokens: MAX_TOKENS,
    ...(temperature !== undefined && { temperature }),
  };
}

module.exports = {
  MAX_TOKENS,
  DEFAULT_TEMPERATURE,
  modelRejectsTemperature,
  parseTemperatureOverride,
  resolveTemperature,
  buildInferenceConfig,
};
