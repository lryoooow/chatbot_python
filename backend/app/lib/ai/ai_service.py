import json
import asyncio
import logging
from typing import AsyncIterator

from app.lib.ai.config import resolve_ai_config
from app.lib.ai.errors import AIError, map_provider_error
from app.lib.ai.normalizer import normalize_chat_response
from app.lib.ai.persistence import (
    mark_assistant_failed,
    persistence_meta,
    prepare_persistence,
    request_for_context,
    save_assistant_response,
    save_streamed_assistant,
    schedule_after_response,
)
from app.lib.ai.provider import create_chat_client
from app.lib.ai.request_builder import build_provider_request_context
from app.lib.ai.stream import sse_event, stream_initial_sse_events, stream_sse_events
from app.schemas.chat import ChatRequest, ChatResponse
from app.shared.logging import log_event

logger = logging.getLogger(__name__)


class AIService:
    async def chat(self, request: ChatRequest) -> ChatResponse:
        config = resolve_ai_config(
            request_model=request.model,
            provider_config=request.provider_config,
        )
        persistence = await prepare_persistence(request, model_name=config.model)
        context_request = request_for_context(request, persistence)
        provider_context = await build_provider_request_context(
            context_request,
            user_id=persistence.user_id,
        )
        client = create_chat_client(config)

        try:
            response = await client.chat.completions.create(
                model=config.model,
                messages=provider_context.messages,
                stream=False,
            )
        except Exception as exc:
            await mark_assistant_failed(persistence, exc)
            raise map_provider_error(exc) from exc

        result = normalize_chat_response(response, config)
        result.conversation_id = persistence.conversation_id
        result.user_message_id = persistence.user_message_id
        result.retrieved_chunks = provider_context.retrieved_chunks
        result.rag_trace = provider_context.rag_trace
        persistence.assistant_message_id = await save_assistant_response(
            persistence,
            content=result.content,
            usage=result.usage.model_dump(exclude_none=True) if result.usage else {},
            finish_reason=result.finish_reason,
        )
        result.assistant_message_id = persistence.assistant_message_id
        schedule_after_response(persistence, assistant_content=result.content)
        log_event(
            logger,
            "chat.response",
            model=config.model,
            retrieved_chunks=provider_context.retrieved_chunks,
            finish_reason=result.finish_reason,
            stream=False,
        )
        return result

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        try:
            config = resolve_ai_config(
                request_model=request.model,
                provider_config=request.provider_config,
            )
            persistence = await prepare_persistence(
                request,
                model_name=config.model,
                create_streaming_assistant=True,
            )
            context_request = request_for_context(request, persistence)
            provider_context = await build_provider_request_context(
                context_request,
                user_id=persistence.user_id,
            )
            client = create_chat_client(config)
        except Exception as exc:
            error = exc if isinstance(exc, AIError) else map_provider_error(exc)
            yield sse_event("error", {"code": error.code, "message": error.message})
            return

        finalized = False
        assistant_parts: list[str] = []
        done_payload: dict | None = None
        try:
            async for event in stream_initial_sse_events(config, persistence_meta(persistence)):
                yield event

            try:
                stream = await client.chat.completions.create(
                    model=config.model,
                    messages=provider_context.messages,
                    stream=True,
                )
            except Exception as exc:
                error = exc if isinstance(exc, AIError) else map_provider_error(exc)
                await mark_assistant_failed(persistence, exc)
                finalized = True
                yield sse_event("error", {"code": error.code, "message": error.message})
                return

            async for event in stream_sse_events(stream):
                parsed = self._parse_sse_event(event)
                if parsed["event"] == "delta":
                    content = parsed["data"].get("content")
                    if isinstance(content, str):
                        assistant_parts.append(content)
                if parsed["event"] == "done":
                    done_payload = parsed["data"]
                    event = sse_event(
                        "done",
                        {
                            **done_payload,
                            "retrieved_chunks": provider_context.retrieved_chunks,
                            "rag_trace": provider_context.rag_trace,
                        },
                    )
                yield event

            assistant_content = "".join(assistant_parts)
            if done_payload and done_payload.get("finish_reason") == "error":
                await mark_assistant_failed(persistence, RuntimeError("Stream ended with provider error."))
                finalized = True
                return
            await save_streamed_assistant(
                persistence,
                content=assistant_content,
                done_payload=done_payload or {},
            )
            finalized = True
            schedule_after_response(persistence, assistant_content=assistant_content)
            log_event(
                logger,
                "chat.response",
                model=config.model,
                retrieved_chunks=provider_context.retrieved_chunks,
                finish_reason=(done_payload or {}).get("finish_reason"),
                stream=True,
            )
        finally:
            if not finalized:
                await asyncio.shield(
                    mark_assistant_failed(persistence, RuntimeError("Stream interrupted"))
                )

    def _parse_sse_event(self, event: str) -> dict:
        event_name = "message"
        data: dict = {}
        for line in event.splitlines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    data = {}
        return {"event": event_name, "data": data}
