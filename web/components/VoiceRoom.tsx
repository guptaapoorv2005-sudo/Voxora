"use client";

import { useEffect, useState } from "react";
import { ConnectionState, RemoteParticipant, RoomEvent } from "livekit-client";
import {
  BarVisualizer,
  LiveKitRoom,
  RoomAudioRenderer,
  StartAudio,
  useConnectionState,
  useRemoteParticipants,
  useRoomContext,
  useVoiceAssistant,
  VoiceAssistantControlBar,
} from "@livekit/components-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

interface TokenResponse {
  token: string;
  livekit_url: string;
  room_name: string;
}

export function VoiceRoom() {
  const [connectionDetails, setConnectionDetails] = useState<TokenResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);

  const handleConnect = async () => {
    setIsLoading(true);
    setApiError(null);
    try {
      const response = await fetch(`${API_BASE}/api/sessions/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(
          errorData.detail || `Server returned ${response.status}: ${response.statusText}`
        );
      }

      const data: TokenResponse = await response.json();
      setConnectionDetails(data);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to obtain session token from backend.";
      setApiError(message);
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisconnect = () => {
    setConnectionDetails(null);
  };

  return (
    <div className="w-full max-w-2xl mx-auto p-6 space-y-6">
      <div className="text-center space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-white">
          Voxora AI Dental Receptionist
        </h1>
        <p className="text-neutral-400 text-sm">
          Phase 1: Real-time WebRTC Voice Spike (LiveKit Cloud, Deepgram Flux, Gemini Flash-Lite, ElevenLabs Flash)
        </p>
      </div>

      {apiError && (
        <div className="p-4 rounded-xl bg-red-950/80 border border-red-700/60 text-red-200 text-sm space-y-1">
          <p className="font-semibold text-red-100">Unable to Start Voice Session</p>
          <p>{apiError}</p>
          <p className="text-xs text-red-300/80 mt-1">
            Note: Token generation is only available when backend is running with{" "}
            <code className="px-1 py-0.5 bg-red-900 rounded text-red-200 font-mono">
              ENVIRONMENT=development
            </code>{" "}
            on localhost.
          </p>
        </div>
      )}

      {!connectionDetails ? (
        <div className="flex flex-col items-center justify-center p-12 rounded-2xl bg-neutral-900/80 border border-neutral-800 space-y-6 text-center shadow-lg">
          <div className="w-16 h-16 rounded-full bg-blue-600/20 border border-blue-500/30 flex items-center justify-center text-blue-400">
            <svg
              className="w-8 h-8"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
              />
            </svg>
          </div>
          <div className="space-y-1">
            <h2 className="text-lg font-medium text-white">Ready to test voice pipeline?</h2>
            <p className="text-sm text-neutral-400">
              Click below to request a short-lived token and connect to LiveKit SFU.
            </p>
          </div>
          <button
            onClick={handleConnect}
            disabled={isLoading}
            className="px-6 py-3 bg-blue-600 hover:bg-blue-500 active:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium rounded-xl transition shadow-md shadow-blue-900/30"
          >
            {isLoading ? "Requesting Session Token..." : "Start Call"}
          </button>
        </div>
      ) : (
        <LiveKitRoom
          serverUrl={connectionDetails.livekit_url}
          token={connectionDetails.token}
          connect={true}
          audio={true}
          video={false}
          onDisconnected={handleDisconnect}
          data-lk-theme="default"
          className="flex flex-col items-center gap-6 p-8 rounded-2xl bg-neutral-900/90 border border-neutral-800 shadow-xl"
        >
          <ActiveVoiceSession
            roomName={connectionDetails.room_name}
            onLeave={handleDisconnect}
          />
        </LiveKitRoom>
      )}
    </div>
  );
}

interface ActiveVoiceSessionProps {
  roomName: string;
  onLeave: () => void;
}

function ActiveVoiceSession({ roomName, onLeave }: ActiveVoiceSessionProps) {
  const room = useRoomContext();
  const connectionState = useConnectionState();
  const participants = useRemoteParticipants();
  const { state: agentState, audioTrack } = useVoiceAssistant();

  // Agent presence based on official isAgent property
  const agentParticipant = participants.find((p) => p.isAgent);
  const isAgentPresent = Boolean(agentParticipant);

  const [hasAgentJoined, setHasAgentJoined] = useState(() =>
    Array.from(room.remoteParticipants.values()).some((p) => p.isAgent)
  );
  const [agentDeparted, setAgentDeparted] = useState(false);
  const [timeoutExpired, setTimeoutExpired] = useState(false);

  // Subscribe to room participant events for reliable departure/arrival detection
  useEffect(() => {
    const onParticipantConnected = (participant: RemoteParticipant) => {
      if (participant.isAgent) {
        setHasAgentJoined(true);
        setAgentDeparted(false);
        setTimeoutExpired(false);
      }
    };

    const onParticipantDisconnected = (participant: RemoteParticipant) => {
      if (participant.isAgent) {
        setAgentDeparted(true);
      }
    };

    room.on(RoomEvent.ParticipantConnected, onParticipantConnected);
    room.on(RoomEvent.ParticipantDisconnected, onParticipantDisconnected);

    return () => {
      room.off(RoomEvent.ParticipantConnected, onParticipantConnected);
      room.off(RoomEvent.ParticipantDisconnected, onParticipantDisconnected);
    };
  }, [room]);

  // 15-second worker join timeout guard
  useEffect(() => {
    if (connectionState !== ConnectionState.Connected || hasAgentJoined || isAgentPresent) {
      return;
    }

    const timer = setTimeout(() => {
      setTimeoutExpired(true);
    }, 15000);

    return () => clearTimeout(timer);
  }, [connectionState, hasAgentJoined, isAgentPresent]);

  const handleEndCall = () => {
    void room.disconnect();
    onLeave();
  };

  return (
    <div className="w-full flex flex-col items-center space-y-6">
      {/* Audio Playback Handlers */}
      <RoomAudioRenderer />
      <StartAudio label="Click to allow audio playback" />

      {/* Top Status Bar: Room State & Agent Presence */}
      <div className="w-full flex items-center justify-between px-4 py-2 rounded-lg bg-neutral-950/60 border border-neutral-800/80 text-xs">
        <div className="flex items-center space-x-2">
          <span
            className={`w-2 h-2 rounded-full ${
              connectionState === ConnectionState.Connected
                ? "bg-emerald-500 animate-pulse"
                : connectionState === ConnectionState.Connecting
                  ? "bg-amber-500 animate-ping"
                  : "bg-neutral-500"
            }`}
          />
          <span className="text-neutral-300 capitalize">
            Room: <strong className="text-white">{connectionState}</strong>
          </span>
        </div>

        <div className="flex items-center space-x-2">
          <span
            className={`w-2 h-2 rounded-full ${
              isAgentPresent
                ? "bg-emerald-500"
                : agentDeparted
                  ? "bg-red-500"
                  : "bg-amber-500"
            }`}
          />
          <span className="text-neutral-300">
            Worker:{" "}
            <strong className="text-white">
              {isAgentPresent
                ? `Active (${agentState})`
                : agentDeparted
                  ? "Disconnected"
                  : "Waiting..."}
            </strong>
          </span>
        </div>
      </div>

      {/* Room Information */}
      <div className="text-center space-y-1">
        <p className="text-xs text-neutral-500 font-mono">Room: {roomName}</p>
        <p className="text-xs text-neutral-400">
          Speak clearly into your microphone to talk with Voxora.
        </p>
      </div>

      {/* Timeout Alert */}
      {timeoutExpired && !isAgentPresent && !agentDeparted && (
        <div className="w-full p-4 rounded-xl bg-amber-950/80 border border-amber-600/70 text-amber-200 text-xs space-y-1 text-center">
          <p className="font-semibold text-amber-100">Agent Join Timeout (15s)</p>
          <p>Voxora worker did not join room within 15 seconds.</p>
          <p className="text-amber-300/80">
            Ensure <code className="px-1 py-0.5 bg-amber-900 rounded font-mono">python worker.py dev</code> is running and connected to the same LiveKit project.
          </p>
        </div>
      )}

      {/* Worker Departed Alert */}
      {agentDeparted && (
        <div className="w-full p-4 rounded-xl bg-red-950/80 border border-red-700/60 text-red-200 text-xs space-y-1 text-center">
          <p className="font-semibold text-red-100">Worker Disconnected</p>
          <p>The Voxora worker has left the room.</p>
        </div>
      )}

      {/* Audio Visualizer */}
      <div className="py-6 flex flex-col items-center justify-center min-h-[100px]">
        {audioTrack ? (
          <BarVisualizer
            state={agentState}
            trackRef={audioTrack}
            barCount={7}
            className="h-16 w-56 flex items-center justify-center gap-1.5"
          />
        ) : (
          <div className="flex flex-col items-center justify-center space-y-2 text-neutral-500 text-xs">
            <div className="flex space-x-1">
              <span className="w-2 h-2 bg-neutral-600 rounded-full animate-bounce [animation-delay:-0.3s]" />
              <span className="w-2 h-2 bg-neutral-600 rounded-full animate-bounce [animation-delay:-0.15s]" />
              <span className="w-2 h-2 bg-neutral-600 rounded-full animate-bounce" />
            </div>
            <span>Waiting for agent audio track...</span>
          </div>
        )}
      </div>

      {/* Controls */}
      <div className="w-full flex items-center justify-between pt-4 border-t border-neutral-800">
        <VoiceAssistantControlBar controls={{ leave: false }} />
        <button
          onClick={handleEndCall}
          className="px-5 py-2.5 bg-red-600 hover:bg-red-500 active:bg-red-700 text-white text-sm font-medium rounded-xl transition shadow-md shadow-red-900/30"
        >
          End Call
        </button>
      </div>
    </div>
  );
}
