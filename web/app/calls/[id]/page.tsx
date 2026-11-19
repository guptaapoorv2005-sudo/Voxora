import { notFound } from "next/navigation";
import { headers } from "next/headers";
import { Suspense } from "react";

type TraceEvent = { sequence: number; occurred_at: string; event: string; details: Record<string, string | number | boolean> };
type Call = { id: string; outcome: string; started_at: string; ended_at: string | null; events: TraceEvent[] };

export default function CallPage({ params }: { params: Promise<{ id: string }> }) {
  return <Suspense fallback={<main className="dashboard-surface p-8">Loading call details…</main>}><CallDetail params={params} /></Suspense>;
}

async function CallDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^sess-[a-f0-9]{12}$/.test(id)) notFound();
  const api = process.env.VOXORA_API_URL ?? "http://127.0.0.1:8000";
  const url = new URL(api);
  let response: Response;
  const cookie = ((await headers()).get("cookie") ?? "").split(";").map(part => part.trim()).find(part => part.startsWith("voxora_access=")) ?? "";
  try { response = await fetch(`${url.origin}/api/owner/calls/${id}`, { cache: "no-store", headers: { cookie } }); }
  catch { return <main className="dashboard-surface p-8">Call details are unavailable. Check that the local API is running.</main>; }
  if (response.status === 404 || response.status === 401) notFound();
  if (!response.ok) return <main className="dashboard-surface p-8">Unable to load call details.</main>;
  const call: Call = await response.json();
  const tools = call.events.filter(e => e.event === "tool_completed");
  const timings = call.events.flatMap(e => Object.entries(e.details).filter(([key, value]) => key.endsWith("_ms") && typeof value === "number").map(([key, value]) => ({ key, value: value as number, sequence: e.sequence })));
  const maximum = Math.max(1, ...timings.map(t => t.value));
  const start = new Date(call.started_at).getTime();
  const spans = call.events.filter(e => e.event === "tool_completed" || e.event === "stage_finished").flatMap(e => {
    const duration = e.details.duration_ms;
    if (typeof duration !== "number") return [];
    const end = new Date(e.occurred_at).getTime() - start;
    return [{ sequence: e.sequence, name: String(e.details.tool ?? e.details.stage), duration, offset: Math.max(0, end - duration), end }];
  });
  let speechStart: TraceEvent | undefined;
  let turnEnd: TraceEvent | undefined;
  for (const event of call.events) {
    if (event.event === "user_turn_started") speechStart = event;
    if (event.event === "user_turn_ended") turnEnd = event;
    if (event.event === "user_transcript_received" && event.details.is_final && speechStart) {
      const begin = new Date(speechStart.occurred_at).getTime() - start;
      const end = new Date(event.occurred_at).getTime() - start;
      if (end >= begin) spans.push({ sequence: event.sequence, name: "caller speech to final transcript", duration: end - begin, offset: Math.max(0, begin), end });
      speechStart = undefined;
    }
    if (event.event === "agent_state_changed" && event.details.new_state === "speaking" && turnEnd) {
      const begin = new Date(turnEnd.occurred_at).getTime() - start;
      const end = new Date(event.occurred_at).getTime() - start;
      if (end >= begin) spans.push({ sequence: event.sequence, name: "turn end to agent speaking", duration: end - begin, offset: Math.max(0, begin), end });
      turnEnd = undefined;
    }
  }
  spans.sort((a, b) => a.sequence - b.sequence);
  const spanEnd = Math.max(1, ...spans.map(s => s.end));
  const booking = [...tools].reverse().find(e => ["confirm_booking", "confirm_reschedule", "confirm_cancellation"].includes(String(e.details.tool)));
  return <main className="dashboard-surface mx-auto max-w-5xl space-y-8 p-6 text-neutral-100">
    <header><p className="text-sm text-neutral-400">Call trace · {call.id}</p><h1 className="text-3xl font-semibold">Call details</h1><p>Outcome: {call.outcome} · Started {new Date(call.started_at).toLocaleString()} · {call.ended_at ? `Ended ${new Date(call.ended_at).toLocaleString()}` : "In progress"}</p></header>
    <section><h2 className="text-xl font-semibold">Booking result</h2><p>{booking ? `${booking.details.tool}: ${booking.details.result}` : "No booking confirmation recorded."}</p></section>
    <section><h2 className="text-xl font-semibold">Turn timeline</h2>{call.events.length ? <ol className="space-y-2">{call.events.map(e => <li key={e.sequence} className="rounded border border-neutral-700 p-3"><span className="text-neutral-400">#{e.sequence} · {new Date(e.occurred_at).toLocaleTimeString()}</span> {e.event} {e.details.stage && `· ${e.details.stage}`}</li>)}</ol> : <p>No events recorded.</p>}</section>
    <section><h2 className="text-xl font-semibold">Tool calls and errors</h2>{tools.length ? <ul>{tools.map(e => <li key={e.sequence}>#{e.sequence} · {e.details.tool} · {e.details.result}{e.details.reason && ` · ${e.details.reason}`} · {e.details.duration_ms} ms</li>)}</ul> : <p>No tool calls recorded.</p>}{call.events.filter(e => e.event === "provider_error").map(e => <p key={e.sequence}>Provider error at #{e.sequence}: {e.details.stage ?? "unknown stage"}</p>)}</section>
    <section><h2 className="text-xl font-semibold">Pipeline latency waterfall</h2><p className="text-sm text-neutral-400">Position uses the completion timestamp and measured duration; overlapping stages can run concurrently.</p>{spans.length ? spans.map(s => <div key={s.sequence} className="my-2"><div>#{s.sequence} · {s.name}: {s.duration} ms</div><div className="relative h-3 bg-neutral-800"><div className="absolute h-3 bg-emerald-400" style={{ left: `${100 * s.offset / spanEnd}%`, width: `${Math.max(0.2, 100 * s.duration / spanEnd)}%` }} /></div></div>) : <p>No pipeline timings recorded.</p>}</section>
    <section><h2 className="text-xl font-semibold">Voice stage measurements</h2><p className="text-sm text-neutral-400">Measured stage latencies; bars compare durations and do not imply a shared clock.</p>{timings.length ? timings.map(t => <div key={`${t.sequence}-${t.key}`} className="my-2"><div>#{t.sequence} · {t.key}: {t.value} ms</div><div className="h-2 bg-sky-400" style={{ width: `${Math.max(1, 100 * t.value / maximum)}%` }} /></div>) : <p>No timing measurements recorded.</p>}</section>
  </main>;
}

