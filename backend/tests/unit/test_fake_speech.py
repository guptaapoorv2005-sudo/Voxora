"""Exercise the actual LiveKit streaming adapter contracts without network access."""
import pytest
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, stt

from tests.fake_speech import FakeSTT, FakeTTS


@pytest.fixture
def silence():
    return rtc.AudioFrame(data=bytes(320), sample_rate=16000, num_channels=1, samples_per_channel=160)


async def test_streaming_stt_emits_scripted_turns(silence):
    adapter = FakeSTT(["Hello", "Where is the clinic?"])
    async with adapter.stream() as stream:
        stream.push_frame(silence)
        stream.push_frame(silence)
        stream.end_input()
        events = [event async for event in stream]
    transcripts = [e.alternatives[0].text for e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert transcripts == ["Hello", "Where is the clinic?"]
    assert sum(e.type == stt.SpeechEventType.END_OF_SPEECH for e in events) == 2


async def test_streaming_tts_emits_synthetic_audio():
    async with FakeTTS().stream() as stream:
        stream.push_text("Hello")
        stream.end_input()
        frames = [event.frame async for event in stream]
    assert frames and frames[0].sample_rate == 16000
    assert all(not any(frame.data) for frame in frames)


@pytest.mark.parametrize("stage", ["stt", "tts"])
async def test_speech_provider_failure_is_reported(stage, silence):
    options = APIConnectOptions(max_retry=0)
    adapter = FakeSTT(["hello"], fail=True) if stage == "stt" else FakeTTS(fail=True)
    async with adapter.stream(conn_options=options) as stream:
        if stage == "stt":
            stream.push_frame(silence)
        else:
            stream.push_text("hello")
        stream.end_input()
        with pytest.raises(APIConnectionError):
            async for _ in stream:
                pass
