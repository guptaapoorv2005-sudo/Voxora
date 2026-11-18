"""Deterministic LiveKit streaming adapters for tests; audio is synthetic silence."""
from collections import deque

from livekit import rtc
from livekit.agents import APIConnectionError, stt, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS


class FakeSTT(stt.STT):
    def __init__(self, transcripts=(), *, fail=False):
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=False))
        self.transcripts = deque(transcripts)
        self.fail = fail

    async def _recognize_impl(self, buffer, *, language=None, conn_options):
        raise NotImplementedError("The fake supports streaming only")

    def stream(self, *, language=None, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return FakeSTTStream(stt=self, conn_options=conn_options)


class FakeSTTStream(stt.RecognizeStream):
    async def _run(self):
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                continue
            if self._stt.fail:
                raise APIConnectionError("synthetic STT failure")
            if self._stt.transcripts:
                transcript = self._stt.transcripts.popleft()
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="en", text=transcript)]))
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))


class FakeTTS(tts.TTS):
    def __init__(self, *, fail=False):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=True), sample_rate=16000, num_channels=1)
        self.fail = fail

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return FakeTTSStream(tts=self, conn_options=conn_options)


class FakeTTSStream(tts.SynthesizeStream):
    async def _run(self, output_emitter):
        output_emitter.initialize(request_id="fake-tts", sample_rate=16000, num_channels=1,
                                  mime_type="audio/pcm", stream=True)
        started = False
        async for token in self._input_ch:
            if isinstance(token, str) and token:
                if self._tts.fail:
                    raise APIConnectionError("synthetic TTS failure")
                if not started:
                    output_emitter.start_segment(segment_id="fake-segment")
                    started = True
                output_emitter.push_frame(rtc.AudioFrame(data=bytes(320), sample_rate=16000,
                    num_channels=1, samples_per_channel=160))
        if started:
            output_emitter.end_segment()
