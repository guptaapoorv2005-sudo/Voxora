import { VoiceRoom } from "@/components/VoiceRoom";

export const metadata = {
  title: "Voice Receptionist | Voxora AI",
  description: "Speak with the Voxora AI receptionist.",
};

export default function VoicePage() {
  return (
    <main className="min-h-screen bg-neutral-950 flex flex-col items-center justify-center p-4 sm:p-8">
      <VoiceRoom />
    </main>
  );
}

