"""Span and metric attribute names.

The ``gen_ai.*`` names come from the OpenTelemetry GenAI semantic conventions (the incubating
module of ``opentelemetry-semantic-conventions``), so they cannot drift from the spec by typo.
Those conventions are still marked *development* upstream; this package pins a minor version of
the semantic-conventions distribution through ``opentelemetry-sdk``.

The ``keelgate.*`` names are ours: tenancy, runs, policy, approvals and cost, none of which the
GenAI conventions cover.

Nothing here ever names an attribute that carries tool arguments, tool output, prompts, or model
text. Those are untrusted and may be sensitive; see ``Telemetry.capture_content`` for the one
opt-in way they leave the process.
"""

from __future__ import annotations

from typing import Final

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as _g

# ------------------------------------------------------------------ GenAI conventions
GEN_AI_OPERATION_NAME: Final = _g.GEN_AI_OPERATION_NAME
GEN_AI_PROVIDER_NAME: Final = _g.GEN_AI_PROVIDER_NAME
GEN_AI_REQUEST_MODEL: Final = _g.GEN_AI_REQUEST_MODEL
GEN_AI_REQUEST_MAX_TOKENS: Final = _g.GEN_AI_REQUEST_MAX_TOKENS
GEN_AI_REQUEST_TEMPERATURE: Final = _g.GEN_AI_REQUEST_TEMPERATURE
GEN_AI_RESPONSE_MODEL: Final = _g.GEN_AI_RESPONSE_MODEL
GEN_AI_RESPONSE_ID: Final = _g.GEN_AI_RESPONSE_ID
GEN_AI_RESPONSE_FINISH_REASONS: Final = _g.GEN_AI_RESPONSE_FINISH_REASONS
GEN_AI_USAGE_INPUT_TOKENS: Final = _g.GEN_AI_USAGE_INPUT_TOKENS
GEN_AI_USAGE_OUTPUT_TOKENS: Final = _g.GEN_AI_USAGE_OUTPUT_TOKENS
GEN_AI_TOOL_NAME: Final = _g.GEN_AI_TOOL_NAME
GEN_AI_TOOL_CALL_ID: Final = _g.GEN_AI_TOOL_CALL_ID
GEN_AI_TOOL_TYPE: Final = _g.GEN_AI_TOOL_TYPE
GEN_AI_AGENT_ID: Final = _g.GEN_AI_AGENT_ID
GEN_AI_AGENT_NAME: Final = _g.GEN_AI_AGENT_NAME
GEN_AI_CONVERSATION_ID: Final = _g.GEN_AI_CONVERSATION_ID
GEN_AI_TOKEN_TYPE: Final = _g.GEN_AI_TOKEN_TYPE
GEN_AI_INPUT_MESSAGES: Final = _g.GEN_AI_INPUT_MESSAGES
GEN_AI_OUTPUT_MESSAGES: Final = _g.GEN_AI_OUTPUT_MESSAGES

OP_CHAT: Final = _g.GenAiOperationNameValues.CHAT.value
OP_INVOKE_AGENT: Final = _g.GenAiOperationNameValues.INVOKE_AGENT.value
OP_EXECUTE_TOOL: Final = _g.GenAiOperationNameValues.EXECUTE_TOOL.value

# The metric names defined by the GenAI conventions.
METRIC_TOKEN_USAGE: Final = "gen_ai.client.token.usage"  # noqa: S105 - a metric name

# ------------------------------------------------------------------ Keelgate
TENANT_ID: Final = "keelgate.tenant.id"
AGENT_ID: Final = "keelgate.agent.id"
RUN_ID: Final = "keelgate.run.id"
LOOP_TYPE: Final = "keelgate.loop.type"
ITERATION: Final = "keelgate.loop.iteration"
STOP_REASON: Final = "keelgate.loop.stop_reason"
RESUMED: Final = "keelgate.loop.resumed"
AS_OF: Final = "keelgate.as_of"
REPLAY: Final = "keelgate.replay"
REPLAY_OF: Final = "keelgate.replay.of_trace_id"

TOOL_SIDE_EFFECT: Final = "keelgate.tool.side_effect"
TOOL_CAPABILITY: Final = "keelgate.tool.capability"
TOOL_STATUS: Final = "keelgate.tool.status"
TOOL_ERROR_CODE: Final = "keelgate.tool.error_code"
TOOL_REPLAYED: Final = "keelgate.tool.idempotent_replay"
ARGS_HASH: Final = "keelgate.tool.args_sha256"

POLICY_EFFECT: Final = "keelgate.policy.effect"
POLICY_ENGINE: Final = "keelgate.policy._engine"
POLICY_VERSION: Final = "keelgate.policy.version"
POLICY_REASONS: Final = "keelgate.policy.reasons"
APPROVAL_TIER: Final = "keelgate.approval.tier"
APPROVAL_ID: Final = "keelgate.approval.id"
APPROVAL_ACTION: Final = "keelgate.approval.action"
APPROVAL_STATUS: Final = "keelgate.approval.status"

MEMORY_TIER: Final = "keelgate.memory.tier"
MEMORY_OP: Final = "keelgate.memory.operation"
MEMORY_RESULTS: Final = "keelgate.memory.results"

COST_USD: Final = "keelgate.cost.usd"
COST_KNOWN: Final = "keelgate.cost.known"
METRIC_COST: Final = "keelgate.llm.cost"

# Attribute values longer than this are truncated: spans are for navigation, not for storage.
MAX_ATTRIBUTE_CHARS: Final = 256
