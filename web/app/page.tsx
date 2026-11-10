export default function Home() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center p-8 bg-zinc-950 text-zinc-100">
      <div className="max-w-md text-center space-y-4">
        <h1 className="text-4xl font-bold tracking-tight text-white">Voxora</h1>
        <p className="text-zinc-400 text-sm">
          AI-Powered Voice Receptionist for BrightSmile Dental Clinic.
        </p>
        <div className="inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1 text-xs text-zinc-300">
          <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
          Foundation ready — Phase 0
        </div>
      </div>
    </main>
  );
}
