/**
 * Translate OpenClaw's marked stable system prefix into a Bedrock cache point.
 *
 * The split is accepted only for supported models, pinned source files, and
 * the exact two-block STABLE/DYNAMIC message shape captured for this request.
 * Applying it must preserve the full prompt bytes and leave room within the
 * four-cache-point limit; otherwise the original system blocks are returned.
 *
 * A separate helper appends a cache point after validated tool specifications.
 * Request handles retain split metadata, not model responses.
 */
const { readFileSync } = require("node:fs");
const { createHash } = require("node:crypto");
const { PRIMARY_MODEL_ID, FALLBACK_MODEL_ID, SUBAGENT_MODEL_ID } = require("./model-policy");

const SOURCE_PINS = [
  ["/app/system-messages.js", "aaf0d64a1d0b53585286edbb1fba4bb468afe13c3672054a1a908ce3a89b2060"],
  ["/usr/local/lib/node_modules/openclaw/dist/system-prompt-params-DtkB_Kxm.mjs",
    "3437ad361afcfbc4e9072f430a8f4a04f139f7b61c4f3ffc787b038147c1b7e4"],
  ["/usr/local/lib/node_modules/openclaw/node_modules/@openclaw/ai/dist/openai-completions-DJ1Vm-CD.mjs",
    "7f6385963407435415aed2108c0f88817a13e9a7728aae10beeb6c180af4f550"],
  ["/usr/local/lib/node_modules/openclaw/node_modules/@openclaw/ai/dist/openai-completions-stream-Bu98b0Hv.mjs",
    "761ac664eaf802b14fa0d6f482253f4e8846bc684526ff55ace660fdc568c7ce"],
];
const MAX_ITEMS = 4096, MAX_TEXT_UNITS = 4 * 1024 * 1024;
const data = (value, key) => value && typeof value === "object"
  ? Object.getOwnPropertyDescriptor(value, key)?.value : undefined;
const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
  Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const wrapped = (text, section) => typeof text === "string" &&
  text.startsWith(`<!-- openclaw:attempt:${section} -->\n`) &&
  text.endsWith(`\n<!-- /openclaw:attempt:${section} -->`);
// Models verified to accept system and tool cache points: the policy defaults
// (Opus 5.5 main, Sonnet 5.5 sub-agent/fallback) and the Haiku 5.5 option.
const CACHE_MODEL_IDS = new Set([PRIMARY_MODEL_ID, FALLBACK_MODEL_ID, SUBAGENT_MODEL_ID,
  "global.anthropic.claude-haiku-5-5"]);
const supported = (modelId) => CACHE_MODEL_IDS.has(modelId);

function addToolPrefixCachePoint(toolConfig, modelId) {
  if (!supported(modelId)) return toolConfig;
  if (!Array.isArray(toolConfig?.tools) || !toolConfig.tools.length ||
      !toolConfig.tools.every((tool) => exact(tool, ["toolSpec"]) &&
        tool.toolSpec && typeof tool.toolSpec === "object" && !Array.isArray(tool.toolSpec))) {
    throw new Error("Tool prefix cache requires tool specifications");
  }
  return { ...toolConfig, tools: [...toolConfig.tools, { cachePoint: { type: "default" } }] };
}

function createSystemPrefixCache({ readSource = readFileSync } = {}) {
  const states = new WeakMap();
  let sourcePinMatch = true;
  for (const [file, expected] of SOURCE_PINS) {
    try {
      if (createHash("sha256").update(readSource(file)).digest("hex") !== expected) sourcePinMatch = false;
    } catch { sourcePinMatch = false; }
  }
  function capture(messages, base, full) {
    const handle = Object.freeze({});
    try {
      if (!Array.isArray(messages) || messages.length > MAX_ITEMS ||
          typeof base !== "string" || typeof full !== "string" ||
          base.length > MAX_TEXT_UNITS || full.length > MAX_TEXT_UNITS) return handle;
      const systems = messages.filter((message) => data(message, "role") === "system");
      if (systems.length !== 1) return handle;
      const blocks = data(systems[0], "content");
      if (!Array.isArray(blocks) || blocks.length !== 2 ||
          !exact(blocks[0], ["type", "text", "cache_control"]) ||
          !exact(blocks[1], ["type", "text"]) ||
          !exact(data(blocks[0], "cache_control"), ["type"]) ||
          data(data(blocks[0], "cache_control"), "type") !== "ephemeral" ||
          blocks.some((block) => data(block, "type") !== "text")) return handle;
      const prefix = data(blocks[0], "text"), dynamic = data(blocks[1], "text");
      if (typeof prefix !== "string" || !prefix.length || typeof dynamic !== "string" ||
          !dynamic.length || prefix.length > MAX_TEXT_UNITS || dynamic.length > MAX_TEXT_UNITS ||
          !wrapped(prefix, "STABLE") || !wrapped(dynamic, "DYNAMIC") ||
          base !== prefix + "\n" + dynamic || !full.startsWith(base)) return handle;
      states.set(handle, { prefix, full });
    } catch {}
    return handle;
  }
  function apply(handle, original, full, modelId, toolConfig, messages) {
    const state = states.get(handle);
    if (!state || !sourcePinMatch || !supported(modelId)) return original;
    try {
      if (full !== state.full || !Array.isArray(original) || original.length !== 1 ||
          !exact(original[0], ["text"]) || data(original[0], "text") !== full) return original;
      const system = [{ text: state.prefix }, { cachePoint: { type: "default" } },
        { text: full.slice(state.prefix.length) }];
      const joined = system[0].text + system[2].text;
      if (joined !== full || !Buffer.from(joined).equals(Buffer.from(full))) return original;
      const tools = toolConfig === undefined ? [] : data(toolConfig, "tools");
      if (!Array.isArray(tools) || tools.length > MAX_ITEMS ||
          !Array.isArray(messages) || messages.length > MAX_ITEMS) return original;
      let count = tools.filter((block) => data(block, "cachePoint") !== undefined).length;
      let entries = tools.length;
      for (const message of messages) {
        const content = data(message, "content");
        if (!Array.isArray(content) || (entries += content.length) > MAX_ITEMS) return original;
        count += content.filter((block) => data(block, "cachePoint") !== undefined).length;
      }
      return count < 4 ? system : original;
    } catch { return original; }
  }
  return Object.freeze({ capture, apply });
}

module.exports = { createSystemPrefixCache, addToolPrefixCachePoint };
