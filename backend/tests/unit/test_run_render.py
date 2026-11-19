"""Verify Render worker settings stay isolated and operator-configurable."""

import run_render


def test_worker_monitor_defaults_and_explicit_override(monkeypatch):
    monkeypatch.delenv("LIVEKIT_AGENTS_LOOP_BLOCK_WARN_MS", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENTS_LOOP_BLOCK_ERROR_MS", raising=False)
    env = run_render.worker_environment()
    assert env["LIVEKIT_AGENTS_LOOP_BLOCK_WARN_MS"] == "1000"
    assert env["LIVEKIT_AGENTS_LOOP_BLOCK_ERROR_MS"] == "2000"
    monkeypatch.setenv("LIVEKIT_AGENTS_LOOP_BLOCK_WARN_MS", "300")
    monkeypatch.setenv("LIVEKIT_AGENTS_LOOP_BLOCK_ERROR_MS", "500")
    env = run_render.worker_environment()
    assert env["LIVEKIT_AGENTS_LOOP_BLOCK_WARN_MS"] == "300"
    assert env["LIVEKIT_AGENTS_LOOP_BLOCK_ERROR_MS"] == "500"
