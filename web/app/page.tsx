import Link from "next/link";

export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center p-8 bg-zinc-950 text-zinc-100">
      <div className="max-w-md text-center space-y-6">
        <h1 className="text-4xl font-bold tracking-tight text-white">Voxora</h1>
        <p className="text-zinc-400 text-sm">
          AI-Powered Voice Receptionist for BrightSmile Dental Clinic.
        </p>

        <div className="inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1 text-xs text-zinc-300">
          <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
          AI voice receptionist
        </div>

        <div>
          <Link
            href="/voice"
            className="inline-flex items-center justify-center px-6 py-3 rounded-xl bg-blue-600 hover:bg-blue-500 active:bg-blue-700 text-white font-medium text-sm transition shadow-lg shadow-blue-900/30"
          >
            Speak to the receptionist &rarr;
          </Link>
        </div>
        <p><Link href="/dashboard" className="text-blue-300 underline">Owner dashboard</Link></p>
      </div>
    </main>
  );
}
