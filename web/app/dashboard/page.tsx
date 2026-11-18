"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

type Value = string | number | boolean | null;
type Row = Record<string, Value>;
type Section = "overview" | "appointments" | "profile" | "hours" | "closures" | "services" | "faqs" | "policies" | "calls";
const sections: Section[] = ["overview", "appointments", "profile", "hours", "closures", "services", "faqs", "policies", "calls"];
const names: Record<Section, string> = { overview: "Overview", appointments: "Appointments", profile: "Business profile", hours: "Hours", closures: "Closures", services: "Services", faqs: "FAQs", policies: "Policies", calls: "Calls" };
const defaults: Record<string, Row> = {
  closures: { date: "", reason: "" },
  services: { name: "", description: "", duration_minutes: 30, buffer_minutes: 0, price_inr: null, min_lead_time_hours: 0, cancellation_window_hours: 24, is_active: true, sort_order: 0 },
  faqs: { topic: "", question: "", answer: "", is_active: true, sort_order: 0 },
  policies: { policy_type: "", title: "", content: "", sort_order: 0 },
};
const profileFields = ["name", "description", "address_line1", "address_line2", "city", "state", "postal_code", "country", "phone", "email", "website"];
const labels: Record<string, string> = { address_line1: "Address line 1", address_line2: "Address line 2", price_inr: "Price (INR)", min_lead_time_hours: "Minimum lead time (hours)", cancellation_window_hours: "Cancellation window (hours)" };
const numeric = new Set(["duration_minutes", "buffer_minutes", "price_inr", "min_lead_time_hours", "cancellation_window_hours", "sort_order"]);
let refreshPromise: Promise<boolean> | null = null;
async function api(path: string, options: RequestInit = {}, retry = true): Promise<Response> {
  const response = await fetch(path, { ...options, cache: "no-store", headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } });
  if (response.status !== 401 || !retry) return response;
  refreshPromise ??= fetch("/api/auth/refresh", { method: "POST", cache: "no-store" }).then(r => r.ok).catch(() => false).finally(() => { refreshPromise = null; });
  if (await refreshPromise) return api(path, options, false);
  return response;
}
const label = (key: string) => labels[key] ?? key.replaceAll("_", " ").replace(/^./, c => c.toUpperCase());

function GoogleCalendar() {
  const [status, setStatus] = useState<{ connected: boolean; needs_attention: boolean } | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const result = new URLSearchParams(window.location.search).get("calendar");
    if (result) window.history.replaceState(null, "", window.location.pathname);
    api("/api/owner/google-calendar").then(async r => {
      if (!r.ok) throw new Error();
      const data = await r.json();
      if (active) {
        setStatus(data);
        if (result) setMessage(result === "connected" ? "Google Calendar connected. Upcoming appointments will sync shortly." : result === "denied" ? "Google Calendar connection was cancelled." : "Google Calendar could not be connected. Please try again.");
      }
    }).catch(() => { if (active) setMessage("Unable to load Google Calendar status."); });
    return () => { active = false; };
  }, []);
  async function change(action: "connect" | "disconnect") {
    setBusy(true); setMessage("");
    try {
      const response = await api(`/api/owner/google-calendar/${action}`, { method: "POST" });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (action === "connect") { window.location.assign(data.url); return; }
      setStatus({ connected: false, needs_attention: false });
      setMessage("Google Calendar disconnected. Existing calendar events remain in Google Calendar.");
    } catch { setMessage("Unable to update Google Calendar. Check the integration setup and try again."); }
    finally { setBusy(false); }
  }
  return <section className="mt-6 space-y-3 rounded border border-zinc-700 p-4">
    <h3 className="text-lg font-semibold">Google Calendar</h3>
    <p>{status === null ? "Checking connection…" : status.connected ? "Connected to your primary calendar." : "Connect your calendar to sync upcoming appointments."}</p>
    {status?.needs_attention && <p className="text-amber-300">Sync needs attention. Check your Google connection or reconnect.</p>}
    {status && <button disabled={busy} className="rounded border px-4 py-2" onClick={() => void change(status.connected ? "disconnect" : "connect")}>{status.connected ? "Disconnect Google Calendar" : "Connect Google Calendar"}</button>}
    {message && <p role="status" className="text-amber-300">{message}</p>}
  </section>;
}

export default function Dashboard() {
  const [section, setSection] = useState<Section>("overview");
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [profile, setProfile] = useState<Row | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const activeSection = useRef<Section>("overview");
  const loadVersion = useRef(0);

  const load = useCallback(async () => {
    if (activeSection.current !== section) return;
    const version = ++loadVersion.current;
    const isCurrent = () => version === loadVersion.current && activeSection.current === section;
    setLoading(true); setMessage("");
    try {
      const me = await api("/api/auth/me");
      if (!isCurrent()) return;
      if (me.status === 401) { setAuthenticated(false); return; }
      if (!me.ok) throw new Error("Unable to verify your session.");
      setAuthenticated(true);
      const target = section === "overview" ? "profile" : section;
      const response = await api(`/api/owner/${target}`);
      if (!isCurrent()) return;
      if (response.status === 401) { setAuthenticated(false); return; }
      if (!response.ok) throw new Error("Unable to load this page. Please try again.");
      const data = await response.json();
      if (!isCurrent()) return;
      if (target === "profile") setProfile(data as Row); else setRows(data as Row[]);
    } catch { if (isCurrent()) setMessage("Unable to reach the server. Please try again."); }
    finally { if (isCurrent()) setLoading(false); }
  }, [section]);
  useEffect(() => { void load(); return () => { loadVersion.current += 1; }; }, [load]);

  async function login(event: React.FormEvent) {
    event.preventDefault(); setLoading(true); setMessage("");
    try {
      const response = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }, false);
      setPassword("");
      if (!response.ok) { setMessage(response.status === 429 ? "Too many attempts. Try again later." : "Unable to sign in. Check your credentials and try again."); return; }
      await load();
    } catch { setMessage("Unable to reach the server. Please try again."); }
    finally { setLoading(false); }
  }
  async function logout() {
    loadVersion.current += 1;
    setLoading(true);
    try {
      const response = await api("/api/auth/logout", { method: "POST" }, false);
      if (!response.ok) { setMessage("Unable to sign out. Please try again."); return; }
      setAuthenticated(false); setRows([]); setProfile(null); setEmail("");
    } catch { setMessage("Unable to sign out. Please try again."); }
    finally { setLoading(false); }
  }
  async function save() {
    setLoading(true); setMessage("");
    try {
      const payload = section === "profile" && profile ? Object.fromEntries(profileFields.map(k => [k, profile[k] ?? null])) : rows;
      const response = await api(`/api/owner/${section}`, { method: "PUT", body: JSON.stringify(payload) });
      if (activeSection.current !== section) return;
      if (response.status === 401) { setAuthenticated(false); return; }
      if (!response.ok) { setMessage("Could not save. Check the values, including required fields and time ranges."); return; }
      await load();
      if (activeSection.current === section) setMessage("Changes saved.");
    } catch { if (activeSection.current === section) setMessage("Unable to save. Please try again."); }
    finally { if (activeSection.current === section) setLoading(false); }
  }
  async function toggle() {
    if (!profile) return;
    setLoading(true); setMessage("");
    try {
      const response = await api("/api/owner/receptionist", { method: "PUT", body: JSON.stringify({ enabled: !profile.receptionist_enabled }) });
      if (activeSection.current !== section) return;
      if (response.status === 401) { setAuthenticated(false); return; }
      if (!response.ok) throw new Error();
      await load();
      if (activeSection.current === section) setMessage("Receptionist setting updated.");
    } catch { if (activeSection.current === section) setMessage("Unable to update the receptionist."); }
    finally { if (activeSection.current === section) setLoading(false); }
  }
  function input(key: string, value: Value, change: (v: Value) => void) {
    if (key === "id") return null;
    const title = key === "day_of_week" ? "Day (0 = Monday, 6 = Sunday)" : label(key);
    if (key === "day_of_week") return <p key={key}>{["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][Number(value)]}</p>;
    if (typeof value === "boolean") return <label key={key} className="flex gap-2"><input type="checkbox" checked={value} onChange={e => change(e.target.checked)} />{title}</label>;
    const type = key === "date" ? "date" : key.endsWith("_time") || key === "break_start" || key === "break_end" ? "time" : numeric.has(key) ? "number" : "text";
    return <label key={key} className="block text-sm">{title}<input className="mt-1 block w-full rounded border border-zinc-600 bg-zinc-900 p-2 text-white" type={type} step={key === "price_inr" ? "0.01" : undefined} value={value === null ? "" : String(value)} onChange={e => change(e.target.value === "" && (numeric.has(key) || type === "time" || ["address_line2", "phone", "email", "website", "description"].includes(key)) ? null : numeric.has(key) ? Number(e.target.value) : e.target.value)} /></label>;
  }
  const editRow = (index: number, key: string, value: Value) => setRows(prev => prev.map((row, i) => i === index ? { ...row, [key]: value } : row));
  const readOnly = section === "appointments" || section === "calls";
  return <main className="mx-auto max-w-6xl p-6 text-zinc-100">
    <header className="mb-6 flex flex-wrap items-center justify-between gap-4"><div><Link href="/" className="text-sky-300">Voxora</Link><h1 className="text-3xl font-semibold">Owner dashboard</h1></div>{authenticated && <button className="rounded border px-4 py-2" onClick={() => void logout()}>Sign out</button>}</header>
    {authenticated === false ? <form onSubmit={login} className="max-w-md space-y-4 rounded border border-zinc-700 p-5"><h2 className="text-xl">Owner sign in</h2><label className="block">Email<input className="block w-full rounded bg-zinc-900 p-2" type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /></label><label className="block">Password<input className="block w-full rounded bg-zinc-900 p-2" type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></label><button disabled={loading} className="rounded bg-blue-600 px-4 py-2">Sign in</button></form> : authenticated === null ? <p>Checking your session…</p> : <>
      <nav aria-label="Dashboard sections" className="mb-6 flex flex-wrap gap-2">{sections.map(s => <button key={s} onClick={() => { if (s !== activeSection.current) { activeSection.current = s; loadVersion.current += 1; setRows([]); if (s === "overview" || s === "profile") setProfile(null); setLoading(true); setMessage(""); setSection(s); } setFilter(""); }} aria-current={section === s ? "page" : undefined} className={`rounded px-3 py-2 ${section === s ? "bg-blue-600" : "bg-zinc-800"}`}>{names[s]}</button>)}</nav>
      <h2 className="mb-4 text-2xl font-semibold">{names[section]}</h2>
      {loading && <p role="status">Loading…</p>}
      {section === "overview" && profile && <div className="space-y-3"><p>Receptionist is <strong>{profile.receptionist_enabled ? "enabled" : "disabled"}</strong>.</p><p className="text-zinc-400">Disabling prevents new sessions. Calls already in progress continue.</p><button disabled={loading} className="rounded bg-blue-600 px-4 py-2" onClick={() => void toggle()}>{profile.receptionist_enabled ? "Disable receptionist" : "Enable receptionist"}</button></div>}
      {section === "overview" && <GoogleCalendar />}
      {section === "profile" && profile && <div className="max-w-2xl space-y-3"><p>Timezone: {profile.timezone}</p>{profileFields.map(k => input(k, profile[k] ?? null, v => setProfile({ ...profile, [k]: v })))}<button disabled={loading} className="rounded bg-blue-600 px-4 py-2" onClick={() => void save()}>Save changes</button></div>}
      {readOnly && <label>Filter <input className="rounded bg-zinc-800 p-2" value={filter} onChange={e => setFilter(e.target.value)} /></label>}
      {section !== "overview" && section !== "profile" && <div className="mt-4 space-y-4">{!loading && !message && rows.length === 0 && <p>No {names[section].toLowerCase()} found.</p>}{rows.map((row, index) => {
        if (readOnly && !JSON.stringify(row).toLowerCase().includes(filter.toLowerCase())) return null;
        if (readOnly) return <article key={row.id == null ? `${section}-row-${index}` : `${section}-id-${String(row.id)}`} className="rounded border border-zinc-700 p-4">{Object.entries(row).map(([k, v]) => <p key={k}><span className="text-zinc-400">{label(k)}:</span> {String(v ?? "—")}</p>)}{section === "calls" && row.id != null && <Link className="text-sky-300 underline" href={`/calls/${encodeURIComponent(String(row.id))}`}>View call details</Link>}</article>;
        return <fieldset key={index} className="grid gap-3 rounded border border-zinc-700 p-4 md:grid-cols-2"><legend>{section === "hours" ? "Schedule" : `Entry ${index + 1}`}</legend>{Object.entries(row).map(([k, v]) => input(k, v, value => editRow(index, k, value)))}{section !== "hours" && <button type="button" className="text-left text-red-300" onClick={() => setRows(rows.filter((_, i) => i !== index))}>Remove entry</button>}</fieldset>;
      })}{!readOnly && <div className="flex gap-3">{defaults[section] && <button disabled={loading} className="rounded border px-4 py-2" onClick={() => setRows([...rows, { ...defaults[section] }])}>Add entry</button>}<button disabled={loading} className="rounded bg-blue-600 px-4 py-2" onClick={() => void save()}>Save changes</button></div>}</div>}
    </>}
    {message && <p role="status" className="mt-4 text-amber-300">{message}</p>}
  </main>;
}
