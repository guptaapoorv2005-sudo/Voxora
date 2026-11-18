import { VoiceRoom } from "@/components/VoiceRoom";

export const metadata = {
  title: "Voice Test | Voxora AI",
  description: "Phase 1 WebRTC Voice Transport Spike testing interface.",
};

export default function VoicePage() {
  return (
    <main className="min-h-screen bg-neutral-950 flex flex-col items-center justify-center p-4 sm:p-8">
      <VoiceRoom />
    </main>
  );
}

