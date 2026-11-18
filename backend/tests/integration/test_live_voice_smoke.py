"""Opt-in synthetic audio smoke test against configured live voice providers.

Uses no LiveKit room. Database and local viewer checks require separate opt-ins.
It is never part of quota-free CI.
"""
import asyncio
import base64
import os
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from livekit import rtc
from livekit.agents.utils import http_context
from livekit.agents.voice import io
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes_calls import router as calls_router
from app.db.models.call import Call, CallTraceEvent
from app.db.session import get_db
from app.voice.tracing import CallTraceRecorder
from app.voice.tracing_agent import TracingAgent
from worker import VoiceSessionCoordinator, build_agent_session, get_worker_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("RUN_LIVE_VOICE_SMOKE") != "1", reason="explicit opt-in required for live providers")]


class SyntheticInput(io.AudioInput):
    def __init__(self):
        super().__init__(label="synthetic input")
        self.frames = asyncio.Queue()

    async def __anext__(self):
        return await self.frames.get()


class SyntheticOutput(io.AudioOutput):
    def __init__(self):
        super().__init__(label="synthetic output", capabilities=io.AudioOutputCapabilities(pause=False))
        self.frames = []
        self.finished = asyncio.Event()

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.frames.append(frame)

    def flush(self):
        super().flush()
        self.on_playback_finished(playback_position=0.01, interrupted=False)
        self.finished.set()

    def clear_buffer(self):
        pass


async def test_live_provider_pipeline_with_synthetic_audio():
    async with http_context.open():
        await _run_live_pipeline()


def _local_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _check_viewer(app, call_id, secret):
    """Opt-in local HTTP smoke test, requiring an already-built Next.js app."""
    api_port, web_port = _local_port(), _local_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error", lifespan="off"))
    api_task = asyncio.create_task(server.serve())
    web_dir = Path(__file__).resolve().parents[3] / "web"
    child_env = os.environ.copy()
    child_env.update({"TRACE_ACCESS_SECRET": secret, "VOXORA_TRACE_API_URL": f"http://127.0.0.1:{api_port}"})
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the opt-in viewer smoke test"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen([node, str(web_dir / "node_modules/next/dist/bin/next"), "start",
        "--hostname", "127.0.0.1", "--port", str(web_port)], cwd=web_dir, env=child_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        async with AsyncClient(timeout=3) as client:
            url = f"http://127.0.0.1:{web_port}/calls/{call_id}"
            for _ in range(50):
                try:
                    response = await client.get(url)
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("Next.js viewer did not start")
            assert response.status_code == 401
            encoded = base64.b64encode(f"trace:{secret}".encode()).decode()
            response = await client.get(url, headers={"Authorization": f"Basic {encoded}"})
            assert response.status_code == 200
            assert "Call details" in response.text
            assert "Pipeline latency waterfall" in response.text
            assert "llm" in response.text and "tts" in response.text
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, 10)
        server.should_exit = True
        await asyncio.wait_for(api_task, timeout=10)


async def _run_live_pipeline():
    settings = get_worker_settings().model_copy(update={"inactivity_nudge_timeout_s": 120.0, "inactivity_disconnect_timeout_s": 120.0})
    session = build_agent_session(settings)
    # Generate a fictional question; nothing is written to a room or a database.
    async with session.tts.synthesize("What are the clinic opening hours?") as synthesis:
        question = [event.frame async for event in synthesis]
    assert question
    source, sink = SyntheticInput(), SyntheticOutput()
    session.input.audio = source
    session.output.audio = sink
    call_id = f"sess-{uuid4().hex[:12]}"
    persist = os.getenv("RUN_LIVE_VOICE_TRACE_DATABASE") == "1"
    engine = create_async_engine(settings.database_url) if persist else None
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else MagicMock()
    trace = CallTraceRecorder(factory, "test-room", call_id)
    coordinator = VoiceSessionCoordinator(session=session, settings=settings,
        room_name="test-room", session_id=call_id, trace=trace)
    coordinator.attach()
    try:
        await session.start(TracingAgent(trace=trace,
            instructions="You are a demo receptionist. Say the clinic opens at 9 AM."), record=False)
        for frame in question:
            await source.frames.put(frame)
            await asyncio.sleep(frame.duration)
        # Deepgram Flux needs trailing silence to detect the synthetic utterance's end.
        sample_rate = question[0].sample_rate
        sample_count = sample_rate // 50
        silence = rtc.AudioFrame(data=bytes(sample_count * 2), sample_rate=sample_rate,
                                 num_channels=1, samples_per_channel=sample_count)
        for _ in range(150):
            await source.frames.put(silence)
            await asyncio.sleep(silence.duration)
        await asyncio.wait_for(sink.finished.wait(), timeout=45)
        assert sink.frames
        assert any(event.event == "user_transcript_received" for event in trace._pending)
        stages = {event.details.get("stage") for event in trace._pending if event.event == "stage_finished"}
        assert {"llm", "tts"} <= stages
        assert all("clinic opening hours" not in str(event.details) for event in trace._pending)
        if engine:
            await trace.flush(outcome="completed")
            async with factory() as db:
                call = await db.get(Call, call_id)
                events = (await db.execute(select(CallTraceEvent).where(
                    CallTraceEvent.call_id == call_id).order_by(CallTraceEvent.sequence))).scalars().all()
                assert call is not None and call.outcome == "completed"
                assert {"llm", "tts"} <= {event.details.get("stage") for event in events if event.event == "stage_finished"}
                assert all("clinic opening hours" not in str(event.details) for event in events)
            # Verify the actual API serializes the persisted trace and enforces access.
            app = FastAPI()
            app.include_router(calls_router)
            async def database_session():
                async with factory() as db:
                    yield db
            app.dependency_overrides[get_db] = database_session
            secret = "test-only-" + "x" * 32
            with patch("app.api.routes_calls.get_settings", return_value=SimpleNamespace(environment="development", trace_access_secret=secret)):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    assert (await client.get(f"/api/calls/{call_id}")).status_code == 404
                    response = await client.get(f"/api/calls/{call_id}", headers={"X-Trace-Key": secret})
                    assert response.status_code == 200
                    payload = response.json()
                    assert payload["outcome"] == "completed"
                    assert {"llm", "tts"} <= {event["details"].get("stage") for event in payload["events"] if event["event"] == "stage_finished"}
                if os.getenv("RUN_LIVE_VOICE_WEB") == "1":
                    await _check_viewer(app, call_id, secret)
    finally:
        await coordinator.inactivity.aclose()
        await session.aclose()
        if engine:
            try:
                async with factory() as db:
                    await db.execute(delete(Call).where(Call.id == call_id))
                    await db.commit()
            finally:
                await engine.dispose()
