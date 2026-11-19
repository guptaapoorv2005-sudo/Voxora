import Link from "next/link";
import { ArrowLeft, AudioLines } from "lucide-react";
import { VoiceRoom } from "@/components/VoiceRoom";
export const metadata = { title: "Voice Receptionist | Voxora", description: "Speak with the Voxora AI receptionist." };
export default function VoicePage() {
  return <main className="voice-page"><header className="site-header wrap"><Link href="/" className="brand"><span className="brand-mark"><AudioLines size={19} /></span> voxora<span className="brand-dot">.</span></Link><Link href="/" className="text-link"><ArrowLeft size={15} /> Back to home</Link></header><div className="voice-main"><VoiceRoom /></div></main>;
}
