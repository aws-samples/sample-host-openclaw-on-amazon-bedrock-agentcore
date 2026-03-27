# Spec: Proxy Guardrail Integration (agentcore-proxy.js)

## Goal
Inject Bedrock Guardrail ID into all Bedrock ConverseStream / Converse API calls in `bridge/agentcore-proxy.js`.

## Environment variables (set by AgentCoreStack)

| Var | Source | Example |
|-----|--------|---------|
| `BEDROCK_GUARDRAIL_ID` | CfnOutput of GuardrailsStack | `abc123def456` |
| `BEDROCK_GUARDRAIL_VERSION` | Always `"DRAFT"` for now | `DRAFT` |

Both vars are **absent / undefined** when `enable_guardrails: false`. Code must handle undefined gracefully.

## Change pattern

At top of file (near other env var reads):
```js
const GUARDRAIL_ID = process.env.BEDROCK_GUARDRAIL_ID;
const GUARDRAIL_VERSION = process.env.BEDROCK_GUARDRAIL_VERSION || 'DRAFT';
const guardrailConfig = GUARDRAIL_ID
  ? { guardrailIdentifier: GUARDRAIL_ID, guardrailVersion: GUARDRAIL_VERSION }
  : undefined;
```

## Locations to update in agentcore-proxy.js

Find all `ConverseStreamCommand` and `ConverseCommand` parameter objects and add:
```js
...(guardrailConfig && { guardrailConfig }),
```

Specifically:
1. Non-streaming Converse call (`invokeBedrock` function or equivalent)
2. Streaming ConverseStream call (`invokeBedrockStreaming` function or equivalent)

## Guardrail trace logging (optional, recommended)

When guardrail fires, Bedrock returns `trace.guardrail` in the response. Log it at DEBUG level:
```js
if (response?.trace?.guardrail) {
  console.debug('[guardrail]', JSON.stringify(response.trace.guardrail));
}
```

## Handling guardrail interventions

When guardrail blocks a response, Bedrock returns `stopReason: "guardrail_intervened"`.
The proxy should:
- Pass through the response as-is (OpenClaw will surface it to the user)
- Log the intervention at WARN level: `[guardrail] intervention: <action>`
- Do NOT retry — the guardrail decision is final

## Unit tests (bridge/tests/ or similar)

Add tests:
1. `guardrailConfig` is included in params when `BEDROCK_GUARDRAIL_ID` is set
2. `guardrailConfig` is absent when env var is not set
3. `stopReason: "guardrail_intervened"` is handled without error
