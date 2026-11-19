import { notFound } from "next/navigation";
import { headers } from "next/headers";
import { Suspense } from "react";
import Link from "next/link";
import { ArrowLeft, AudioLines } from "lucide-react";

type TraceEvent = { sequence: number; occurred_at: string; event: string; details: Record<string, string | number | boolean> };
type Call = { id: string; outcome: string; started_at: string; ended_at: string | null; events: TraceEvent[] };

export default function CallPage({ params }: { params: Promise<{ id: string }> }) {
  return <Suspense fallback={<main className="dashboard-surface owner-shell trace-message">Loading call details…</main>}><CallDetail params={params} /></Suspense>;
}

async function CallDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^sess-[a-f0-9]{12}$/.test(id)) notFound();
  const api = process.env.VOXORA_API_URL ?? "http://127.0.0.1:8000";
  const url = new URL(api);
  let response: Response;
  const cookie = ((await headers()).get("cookie") ?? "").split(";").map(part => part.trim()).find(part => part.startsWith("voxora_access=")) ?? "";
  try { response = await fetch(`${url.origin}/api/owner/calls/${id}`, { cache: "no-store", headers: { cookie } }); }
  catch { return <main className="dashboard-surface owner-shell trace-message">Call details are unavailable. Check that the local API is running.</main>; }
  if (response.status === 404 || response.status === 401) notFound();
  if (!response.ok) return <main className="dashboard-surface owner-shell trace-message">Unable to load call details.</main>;
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
  return <main className="dashboard-surface owner-shell"><header className="owner-topbar"><Link href="/" className="brand owner-brand"><span className="brand-mark"><AudioLines size={19} /></span> voxora<span className="brand-dot">.</span></Link><Link className="owner-home-link" href="/dashboard"><ArrowLeft size={15} /> Owner dashboard</Link></header>
    <div className="trace-content"><header className="owner-page-heading"><div><p className="owner-kicker">CALL TRACE / {call.id}</p><h1>Call details.</h1><p>Started {new Date(call.started_at).toLocaleString()} · {call.ended_at ? `Ended ${new Date(call.ended_at).toLocaleString()}` : "In progress"}</p></div><span className="owner-pill is-active">{call.outcome}</span></header>
    <div className="trace-summary"><section className="owner-card"><p className="owner-kicker">BOOKING RESULT</p><h2>Outcome</h2><p>{booking ? `${booking.details.tool}: ${booking.details.result}` : "No booking confirmation recorded."}</p></section><section className="owner-card"><p className="owner-kicker">SESSION ACTIVITY</p><h2>{call.events.length} events</h2><p>{tools.length} tool calls recorded in this session.</p></section></div>
    <section className="owner-card trace-section"><div className="trace-heading"><div><p className="owner-kicker">CONVERSATION ACTIVITY</p><h2>Turn timeline</h2></div><span className="owner-pill">{call.events.length} events</span></div>{call.events.length ? <ol className="trace-timeline">{call.events.map(e => <li key={e.sequence}><span className="trace-sequence">{String(e.sequence).padStart(2, "0")}</span><div><strong>{e.event.replaceAll("_", " ")}</strong>{e.details.stage && <span> · {e.details.stage}</span>}<p>{new Date(e.occurred_at).toLocaleTimeString()}</p></div></li>)}</ol> : <p className="trace-empty">No events recorded.</p>}</section>
    <section className="owner-card trace-section"><p className="owner-kicker">ACTIONS &amp; EXCEPTIONS</p><h2>Tool calls and errors</h2>{tools.length ? <ul className="trace-tools">{tools.map(e => <li key={e.sequence}><strong>{e.details.tool}</strong><span>#{e.sequence} · {e.details.result}{e.details.reason && ` · ${e.details.reason}`} · {e.details.duration_ms} ms</span></li>)}</ul> : <p className="trace-empty">No tool calls recorded.</p>}{call.events.filter(e => e.event === "provider_error").map(e => <p className="owner-warning" key={e.sequence}>Provider error at #{e.sequence}: {e.details.stage ?? "unknown stage"}</p>)}</section>
    <section className="owner-card trace-section"><p className="owner-kicker">TIMING</p><h2>Pipeline latency waterfall</h2><p>Position uses the completion timestamp and measured duration; overlapping stages can run concurrently.</p>{spans.length ? spans.map(s => <div key={s.sequence} className="trace-bar-row"><div><span>#{s.sequence} · {s.name}</span><strong>{s.duration} ms</strong></div><div className="trace-bar"><div style={{ left: `${100 * s.offset / spanEnd}%`, width: `${Math.max(0.2, 100 * s.duration / spanEnd)}%` }} /></div></div>) : <p className="trace-empty">No pipeline timings recorded.</p>}</section>
    <section className="owner-card trace-section"><p className="owner-kicker">TIMING</p><h2>Voice stage measurements</h2><p>Measured stage latencies; bars compare durations and do not imply a shared clock.</p>{timings.length ? timings.map(t => <div key={`${t.sequence}-${t.key}`} className="trace-bar-row"><div><span>#{t.sequence} · {t.key}</span><strong>{t.value} ms</strong></div><div className="trace-bar"><div style={{ width: `${Math.max(1, 100 * t.value / maximum)}%` }} /></div></div>) : <p className="trace-empty">No timing measurements recorded.</p>}</section>
    </div></main>;
}
