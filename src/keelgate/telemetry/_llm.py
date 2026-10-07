"""Wrap any ``LLMClient`` so each call becomes a GenAI ``chat`` span with usage and cost."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from opentelemetry.trace import SpanKind

from keelgate.telemetry import attributes as attr
from keelgate.telemetry._core import active, current_run, set_attributes, span

if TYPE_CHECKING:
    from keelgate.llm._types import LLMClient, LLMRequest, LLMResponse


class InstrumentedLLM:
    """An ``LLMClient`` that records a span and cost for every ``complete`` call.

    Cost is attributed to the tenant and agent of the run bound by the loop (see
    ``keelgate.telemetry.bind_run``); outside a run it is recorded as ``unattributed``.
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.name = client.name

    @property
    def wrapped(self) -> LLMClient:
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        with span(
            f"{attr.OP_CHAT} {request.model}",
            kind=SpanKind.CLIENT,
            attributes={
                attr.GEN_AI_OPERATION_NAME: attr.OP_CHAT,
                attr.GEN_AI_PROVIDER_NAME: self._client.name,
                attr.GEN_AI_REQUEST_MODEL: request.model,
                attr.GEN_AI_REQUEST_MAX_TOKENS: request.max_tokens,
                attr.GEN_AI_REQUEST_TEMPERATURE: request.temperature,
            },
        ) as current:
            telemetry = active()
            if telemetry.capture_content:
                current.add_event(
                    "gen_ai.client.inference.operation.details",
                    {attr.GEN_AI_INPUT_MESSAGES: _messages_json(request)},
                )
            response = await self._client.complete(request)
            usage = response.usage
            set_attributes(
                current,
                {
                    attr.GEN_AI_RESPONSE_MODEL: response.model or request.model,
                    attr.GEN_AI_RESPONSE_ID: response.response_id,
                    attr.GEN_AI_USAGE_INPUT_TOKENS: usage.input_tokens,
                    attr.GEN_AI_USAGE_OUTPUT_TOKENS: usage.output_tokens,
                    attr.COST_KNOWN: usage.cost_usd is not None,
                    attr.COST_USD: usage.cost_usd,
                },
            )
            current.set_attribute(
                attr.GEN_AI_RESPONSE_FINISH_REASONS, [response.finish_reason.value]
            )
            if telemetry.capture_content:
                current.add_event(
                    "gen_ai.client.inference.operation.details",
                    {attr.GEN_AI_OUTPUT_MESSAGES: response.message.model_dump_json()},
                )
            run = current_run()
            telemetry.costs.record(
                usage,
                tenant_id=run.tenant_id if run else None,
                agent_id=run.agent_id if run else None,
                model=response.model or request.model,
            )
            return response


def _messages_json(request: LLMRequest) -> str:
    return json.dumps(
        [{"role": m.role.value, "content": m.content} for m in request.messages], sort_keys=True
    )
