"use client";
import { useEffect, useState } from "react";
import { AudioLines, ArrowRight } from "lucide-react";
import { ConnectionState, RemoteParticipant, RoomEvent } from "livekit-client";
import { BarVisualizer, LiveKitRoom, RoomAudioRenderer, StartAudio, useConnectionState, useRemoteParticipants, useRoomContext, useVoiceAssistant, VoiceAssistantControlBar } from "@livekit/components-react";
import { Button } from "@/components/ui/button";
interface TokenResponse { token: string; livekit_url: string; room_name: string; }
export function VoiceRoom() {
  const [details, setDetails] = useState<TokenResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function connect() {
    setLoading(true); setError(null);
    try {
      const response = await fetch("/api/sessions/token", { method: "POST", headers: { "Content-Type": "application/json" } });
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`); }
      setDetails(await response.json() as TokenResponse);
    } catch (err) { setError(err instanceof Error ? err.message : "Please check your connection."); }
    finally { setLoading(false); }
  }
  return <div className="space-y-8">
    <div className="text-center space-y-4"><p className="voice-status">BrightSmile / Voice reception</p><h1 className="voice-title">Let’s talk about your visit.</h1><p className="voice-muted text-sm">Ask a question or manage an appointment, in your own words.</p></div>
    {error && <div role="alert" className="voice-alert"><strong>We couldn’t start your call.</strong><p className="mt-1">{error}</p><p className="mt-1">Please try again in a moment.</p></div>}
    {!details ? <div className="voice-card text-center"><div className="voice-orb mt-8"><AudioLines size={42} strokeWidth={1.3} /></div><div className="mt-14"><h2 className="text-xl font-semibold">Ready when you are.</h2><p className="voice-muted mx-auto mt-3 max-w-sm text-sm">You’ll be connected to our AI receptionist. Please allow microphone access when your browser asks.</p></div><Button onClick={connect} disabled={loading} size="lg" className="mt-8">{loading ? "Connecting…" : "Start a conversation"} {!loading && <ArrowRight size={16} />}</Button><p className="voice-muted mt-5 text-xs">For urgent medical concerns, contact a healthcare professional.</p></div> :
    <LiveKitRoom serverUrl={details.livekit_url} token={details.token} connect={true} audio={true} video={false} onDisconnected={() => setDetails(null)} onError={(err) => { setError(err.message || "Please check your microphone and connection."); setDetails(null); }} data-lk-theme="default" className="voice-card"><ActiveVoiceSession onLeave={() => setDetails(null)} /></LiveKitRoom>}
  </div>;
}
function ActiveVoiceSession({ onLeave }: { onLeave: () => void }) {
  const room = useRoomContext();
  const connectionState = useConnectionState();
  const participants = useRemoteParticipants();
  const { state, audioTrack } = useVoiceAssistant();
  const present = participants.some(p => p.isAgent);
  const [hasJoined, setHasJoined] = useState(() => Array.from(room.remoteParticipants.values()).some(p => p.isAgent));
  const [departed, setDeparted] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  useEffect(() => {
    const connected = (p: RemoteParticipant) => { if (p.isAgent) { setHasJoined(true); setDeparted(false); setTimedOut(false); } };
    const disconnected = (p: RemoteParticipant) => { if (p.isAgent) setDeparted(true); };
    room.on(RoomEvent.ParticipantConnected, connected); room.on(RoomEvent.ParticipantDisconnected, disconnected);
    return () => { room.off(RoomEvent.ParticipantConnected, connected); room.off(RoomEvent.ParticipantDisconnected, disconnected); };
  }, [room]);
  useEffect(() => {
    if (connectionState !== ConnectionState.Connected || hasJoined || present) return;
    const timer = setTimeout(() => setTimedOut(true), 45000);
    return () => clearTimeout(timer);
  }, [connectionState, hasJoined, present]);
  const status = connectionState !== ConnectionState.Connected ? "Connecting your call" : departed ? "Receptionist disconnected" : !present ? "Connecting to the receptionist" : state === "speaking" ? "Receptionist is speaking" : state === "listening" ? "Listening to you" : state === "thinking" ? "One moment" : "Connected";
  return <div className="flex w-full flex-col items-center text-center"><RoomAudioRenderer /><StartAudio label="Allow audio playback" />
    <p className="voice-status" role="status" aria-live="polite">{status}</p><div className="voice-orb mt-12"><AudioLines size={42} strokeWidth={1.3} /></div>
    <div className="mt-12 flex h-16 items-center justify-center" aria-hidden="true">{audioTrack ? <BarVisualizer state={state} trackRef={audioTrack} barCount={7} className="flex h-12 w-40 items-center justify-center gap-1.5 text-[#51816d]" /> : <span className="voice-muted text-sm">{present ? "Getting audio ready…" : "Please stay on the line…"}</span>}</div>
    {timedOut && !present && !departed && <div role="status" className="voice-alert mt-4 w-full">The receptionist is taking longer than expected. Please wait a little longer, or end the call and try again.</div>}
    {departed && <div role="alert" className="voice-alert mt-4 w-full">The receptionist disconnected. Please end the call and try again.</div>}
    <p className="voice-muted mt-5 text-sm">Speak naturally. You can mute your microphone or end the call at any time.</p><div className="voice-controls mt-8 flex w-full flex-wrap items-center justify-center gap-4 border-t border-[#e5ece3] pt-6"><VoiceAssistantControlBar controls={{ leave: false }} /><Button variant="destructive" onClick={() => { void room.disconnect(); onLeave(); }}>End call</Button></div>
  </div>;
}
