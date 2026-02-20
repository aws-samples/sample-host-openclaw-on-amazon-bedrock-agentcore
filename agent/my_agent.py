"""OpenClaw Strands Agent — deployed on Bedrock AgentCore Runtime.

Handles AI reasoning for the OpenClaw messaging bridge. Accepts user messages
with actor_id, session_id, and channel metadata. Uses AgentCore Memory for
conversation context and user preferences.
"""

import json
import logging
import os

import boto3
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from strands import Agent
from strands.models.bedrock import BedrockModel

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

tracer = trace.get_tracer("openclaw-agent")

# --- Configuration ---
DEFAULT_MODEL_ID = os.environ["DEFAULT_MODEL_ID"]
AWS_REGION = os.environ["AWS_REGION"]
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

SYSTEM_PROMPT = """You are a helpful personal assistant powered by OpenClaw. You are friendly,
concise, and knowledgeable. You help users with a wide range of tasks including answering
questions, providing information, having conversations, and assisting with daily tasks.

Key behaviors:
- Be conversational and natural in your responses
- Keep responses concise unless the user asks for detail
- If you don't know something, say so honestly
- Remember context from the current conversation
- Use any relevant user preferences or facts you've learned from previous interactions

You are accessed through messaging channels (WhatsApp, Telegram, Discord, Slack, or a web UI).
Keep your responses appropriate for chat-style messaging — avoid very long paragraphs unless
specifically asked for detailed explanations."""

# --- Memory helpers ---
bedrock_agentcore = boto3.client("bedrock-agentcore", region_name=AWS_REGION)


def load_memory_context(actor_id: str, session_id: str) -> str:
    """Load short-term events and long-term memories for the given actor."""
    context_parts = []

    if not MEMORY_ID:
        return ""

    # Retrieve long-term memories (semantic + preferences)
    try:
        response = bedrock_agentcore.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=actor_id,
            searchCriteria={
                "searchQuery": "user preferences and important facts",
            },
            maxResults=10,
        )
        records = response.get("memoryRecordSummaries", [])
        if records:
            facts = [r.get("content", {}).get("text", "") for r in records if r.get("content")]
            if facts:
                context_parts.append(
                    "Relevant memories from previous interactions:\n"
                    + "\n".join(f"- {f}" for f in facts if f)
                )
    except Exception:
        logger.exception("Failed to retrieve long-term memories")

    # List recent short-term events for this session
    try:
        response = bedrock_agentcore.list_events(
            memoryId=MEMORY_ID,
            sessionId=session_id,
            actorId=actor_id,
            includePayloads=True,
            maxResults=20,
        )
        events = response.get("events", [])
        if events:
            recent = []
            for evt in events[-10:]:
                for item in evt.get("payload", []):
                    conv = item.get("conversational", {})
                    text = conv.get("content", {}).get("text", "")
                    role = conv.get("role", "")
                    if text:
                        recent.append(f"{role}: {text}" if role else text)
            if recent:
                context_parts.append(
                    "Recent conversation context:\n" + "\n".join(recent)
                )
    except Exception:
        logger.exception("Failed to list short-term events")

    return "\n\n".join(context_parts)


def store_memory_event(actor_id: str, session_id: str, user_message: str, agent_response: str):
    """Store the interaction as a memory event."""
    if not MEMORY_ID:
        return

    try:
        from datetime import datetime, timezone

        bedrock_agentcore.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "content": {"text": user_message},
                        "role": "user",
                    }
                },
                {
                    "conversational": {
                        "content": {"text": agent_response},
                        "role": "assistant",
                    }
                },
            ],
        )
    except Exception:
        logger.exception("Failed to store memory event")


# --- Model (reused across requests) ---
_bedrock_model = BedrockModel(
    model_id=DEFAULT_MODEL_ID,
    region_name=AWS_REGION,
)


# --- Request handler ---
def handle_request(payload: dict) -> dict:
    """Process an incoming agent invocation request.

    Expected payload:
        {
            "prompt": "user message text",
            "actor_id": "user-123",
            "session_id": "session-abc",
            "channel": "whatsapp|telegram|discord|slack|webui"
        }
    """
    prompt = payload.get("prompt", "")
    actor_id = payload.get("actor_id", "default-user")
    session_id = payload.get("session_id", "default-session")
    channel = payload.get("channel", "webui")

    with tracer.start_as_current_span("openclaw-agent-invocation") as span:
        span.set_attribute("openclaw.actor_id", actor_id)
        span.set_attribute("openclaw.session_id", session_id)
        span.set_attribute("openclaw.channel", channel)

        try:
            # Load memory context
            memory_context = load_memory_context(actor_id, session_id)

            # Augment system prompt with memory context
            system_prompt = SYSTEM_PROMPT
            if memory_context:
                system_prompt += f"\n\n## Relevant Context\n{memory_context}"

            # Create agent with augmented system prompt and invoke
            agent = Agent(model=_bedrock_model, system_prompt=system_prompt)
            result = agent(prompt)
            response_text = str(result)

            # Store the interaction
            store_memory_event(actor_id, session_id, prompt, response_text)

            span.set_status(StatusCode.OK)

            return {
                "response": response_text,
                "actor_id": actor_id,
                "session_id": session_id,
                "channel": channel,
            }

        except Exception as e:
            span.set_status(StatusCode.ERROR, str(e))
            span.record_exception(e)
            logger.exception("Agent invocation failed")
            return {
                "error": str(e),
                "actor_id": actor_id,
                "session_id": session_id,
            }


# --- BedrockAgentCoreApp entrypoint ---
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore Runtime entrypoint."""
    return handle_request(payload)


if __name__ == "__main__":
    app.run()
