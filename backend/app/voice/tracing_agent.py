"""LiveKit pipeline spans without retaining text or audio."""
import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from livekit import rtc
from livekit.agents import Agent, llm

from app.voice.tracing import CallTraceRecorder

logger = logging.getLogger(__name__)


class TracingAgent(Agent):
    def __init__(self, *, trace: CallTraceRecorder, **kwargs: Any):
        super().__init__(**kwargs)
        self.trace = trace

    def _start_trace(self, stage: str) -> tuple[int, float] | None:
        try:
            return self.trace.start_stage(stage)
        except Exception:
            logger.warning("Unable to record pipeline start")
            return None

    def _end_trace(self, stage: str, started: tuple[int, float] | None, status: str) -> None:
        if started is None:
            return
        try:
            self.trace.end_stage(stage, started, status)
        except Exception:
            logger.warning("Unable to record pipeline completion")

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: Any) -> AsyncIterator[Any]:
        started = self._start_trace("llm")
        status = "completed"
        try:
            async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
                yield chunk
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            self._end_trace("llm", started, status)

    async def tts_node(self, text: AsyncIterable[str], model_settings: Any) -> AsyncIterator[rtc.AudioFrame]:
        started = self._start_trace("tts")
        status = "completed"
        try:
            async for frame in Agent.default.tts_node(self, text, model_settings):
                yield frame
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            self._end_trace("tts", started, status)
