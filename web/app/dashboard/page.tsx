"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, ArrowLeft, ArrowRight, AudioLines, CalendarDays, CheckCircle2, Clock3, FileText, Headphones, HelpCircle, LayoutDashboard, LogOut, MapPin, Plus, Search, Settings2, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";

type Value = string | number | boolean | null;
type Row = Record<string, Value>;
type Section = "overview" | "appointments" | "profile" | "hours" | "closures" | "services" | "faqs" | "policies" | "calls";
const sections: Section[] = ["overview", "appointments", "profile", "hours", "closures", "services", "faqs", "policies", "calls"];
const names: Record<Section, string> = { overview: "Overview", appointments: "Appointments", profile: "Business profile", hours: "Hours", closures: "Closures", services: "Services", faqs: "FAQs", policies: "Policies", calls: "Calls" };
const icons: Record<Section, typeof LayoutDashboard> = { overview: LayoutDashboard, appointments: CalendarDays, profile: MapPin, hours: Clock3, closures: CalendarDays, services: Sparkles, faqs: HelpCircle, policies: ShieldCheck, calls: Headphones };
const descriptions: Record<Section, string> = { overview: "A little overview of your reception, all in one place.", appointments: "Review appointments recorded by your receptionist.", profile: "Keep the details your receptionist shares up to date.", hours: "Set the hours your clinic is available for appointments.", closures: "Add dates when your clinic will be unavailable.", services: "Manage the services available to your callers.", faqs: "Give your receptionist answers to common questions.", policies: "Keep your clinic’s policies clear and current.", calls: "Review call activity and explore individual traces." };
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
  return <section className="owner-card integration-card">
    <div className="card-heading"><span className="owner-icon"><CalendarDays size={19} /></span><span className={`owner-pill ${status?.connected ? "is-active" : ""}`}>{status === null ? "Checking" : status.connected ? "Connected" : "Not connected"}</span></div>
    <h3>Google Calendar</h3><p>{status === null ? "Checking connection…" : status.connected ? "Upcoming appointments sync with your primary calendar." : "Connect your primary calendar to sync upcoming appointments."}</p>
    {status?.needs_attention && <p className="owner-warning" role="status">Sync needs attention. Check your Google connection or reconnect.</p>}
    {status && <Button variant="outline" size="sm" disabled={busy} onClick={() => void change(status.connected ? "disconnect" : "connect")}>{busy ? "Please wait…" : status.connected ? "Disconnect calendar" : "Connect calendar"}<ArrowRight size={14} /></Button>}
    {message && <p role="status" className="owner-feedback">{message}</p>}
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
      const payload = section === "profile" && profile ? Object.fromEntries(profileFields.map(k => [k, profile[k] ?? null])) : section === "hours" ? rows.map(row => row.is_closed ? { ...row, open_time: null, close_time: null, break_start: null, break_end: null } : row) : rows;
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
    if (key === "day_of_week") return <p key={key} className="owner-day">{["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][Number(value)]}</p>;
    if (typeof value === "boolean") return <label key={key} className="owner-check"><input type="checkbox" checked={value} onChange={e => change(e.target.checked)} />{title}</label>;
    const type = key === "date" ? "date" : key.endsWith("_time") || key === "break_start" || key === "break_end" ? "time" : numeric.has(key) ? "number" : "text";
    return <label key={key} className="owner-label">{title}<input className="owner-input" type={type} step={key === "price_inr" ? "0.01" : undefined} value={value === null ? "" : String(value)} onChange={e => change(e.target.value === "" && (numeric.has(key) || type === "time" || ["address_line2", "phone", "email", "website", "description"].includes(key)) ? null : numeric.has(key) ? Number(e.target.value) : e.target.value)} /></label>;
  }
  const editRow = (index: number, key: string, value: Value) => setRows(prev => prev.map((row, i) => i === index ? { ...row, [key]: value } : row));
  const readOnly = section === "appointments" || section === "calls";
  const visibleRows = readOnly ? rows.filter(row => JSON.stringify(row).toLowerCase().includes(filter.toLowerCase())) : rows;
  function navigate(s: Section) {
    if (s !== activeSection.current) { activeSection.current = s; loadVersion.current += 1; setRows([]); if (s === "overview" || s === "profile") setProfile(null); setLoading(true); setMessage(""); setSection(s); }
    setFilter("");
  }
  return <main className="dashboard-surface owner-shell">
    <header className="owner-topbar"><Link href="/" className="brand owner-brand"><span className="brand-mark"><AudioLines size={19} /></span> voxora<span className="brand-dot">.</span></Link><div className="owner-topbar-right"><span className="owner-workspace">BRIGHTSMILE / OWNER SPACE</span>{authenticated && <button className="owner-signout" disabled={loading} onClick={() => void logout()}><LogOut size={16} /><span>Sign out</span></button>}</div></header>
    {authenticated !== true ? <div className="owner-auth-wrap"><div className="owner-auth-intro"><div className="eyebrow"><span className="eyebrow-line" /> YOUR CLINIC, IN GOOD HANDS</div><h1>A calmer way to<br /><em>manage your day.</em></h1><p>Everything your receptionist needs, together in one place.</p></div><div className="owner-auth-card owner-card">{authenticated === false ? <form onSubmit={login} className="owner-form"><span className="owner-icon"><ShieldCheck size={20} /></span><div><p className="owner-kicker">OWNER ACCESS</p><h2>Welcome back.</h2><p>Sign in to manage your clinic.</p></div><label className="owner-label">Email address<input className="owner-input" type="email" autoComplete="username" placeholder="you@clinic.com" required value={email} onChange={e => setEmail(e.target.value)} /></label><label className="owner-label">Password<input className="owner-input" type="password" autoComplete="current-password" placeholder="Enter your password" required value={password} onChange={e => setPassword(e.target.value)} /></label><Button disabled={loading} className="w-full">{loading ? "Signing in…" : "Sign in"}<ArrowRight size={16} /></Button></form> : <div className="owner-form"><p role="status">{message || "Checking your session…"}</p>{message && <Button onClick={() => void load()}>Try again</Button>}</div>}{message && authenticated === false && <p role="status" className="owner-feedback">{message}</p>}</div></div> :
    <div className="owner-layout"><aside className="owner-sidebar"><p className="owner-nav-label">WORKSPACE</p><nav aria-label="Dashboard sections" className="owner-nav">{sections.map(s => { const Icon = icons[s]; return <button key={s} onClick={() => navigate(s)} aria-current={section === s ? "page" : undefined} className={section === s ? "active" : ""}><Icon size={17} /><span>{names[s]}</span></button>; })}</nav><div className="owner-sidebar-note"><Activity size={17} /><p>Made for the moments that matter.</p></div></aside>
      <div className="owner-content"><div className="owner-content-inner"><div className="owner-page-heading"><div><p className="owner-kicker">CLINIC WORKSPACE / {names[section].toUpperCase()}</p><h1>{section === "overview" ? "Your clinic, at a glance." : names[section]}</h1><p>{descriptions[section]}</p></div><Link className="owner-home-link" href="/"><ArrowLeft size={15} /> Back to site</Link></div>
      {message && <div role="status" className="owner-notice">{message}</div>}
      {loading && <div role="status" className="owner-loading"><span className="owner-spinner" /> Loading {names[section].toLowerCase()}…</div>}
      {section === "overview" && profile && <div className="owner-overview-grid"><section className="owner-card receptionist-card"><div className="card-heading"><span className="owner-icon"><AudioLines size={20} /></span><span className={`owner-pill ${profile.receptionist_enabled ? "is-active" : ""}`}><span />{profile.receptionist_enabled ? "Accepting calls" : "Paused"}</span></div><div><p className="owner-kicker">YOUR VOICE RECEPTIONIST</p><h2>{profile.receptionist_enabled ? "Ready to help." : "Reception is paused."}</h2><p>Disabling prevents new sessions. Calls already in progress continue.</p></div><Button variant={profile.receptionist_enabled ? "outline" : "default"} size="sm" disabled={loading} onClick={() => void toggle()}>{profile.receptionist_enabled ? "Pause receptionist" : "Enable receptionist"}<ArrowRight size={14} /></Button></section><GoogleCalendar /><section className="owner-card owner-quick"><span className="owner-icon"><Settings2 size={19} /></span><h3>Keep things current</h3><p>Update your clinic information, services, hours, and policies as things change.</p><button onClick={() => navigate("profile")}>Edit clinic profile <ArrowRight size={14} /></button></section></div>}
      {section === "overview" && !profile && !loading && <Button variant="outline" onClick={() => void load()}>Try loading overview again</Button>}
      {section === "profile" && profile && <section className="owner-card owner-editor"><div className="owner-section-head"><div><p className="owner-kicker">CLINIC DETAILS</p><h2>Business profile</h2><p>These details help your receptionist answer questions accurately.</p></div><span className="owner-pill">{String(profile.timezone ?? "—")}</span></div><div className="owner-fields">{profileFields.map(k => input(k, profile[k] ?? null, v => setProfile({ ...profile, [k]: v })))}</div><div className="owner-actions"><Button disabled={loading} onClick={() => void save()}>Save changes <ArrowRight size={15} /></Button></div></section>}
      {readOnly && <div className="owner-search"><Search size={17} /><label className="sr-only" htmlFor="record-filter">Filter {names[section].toLowerCase()}</label><input id="record-filter" placeholder={`Search ${names[section].toLowerCase()}…`} value={filter} onChange={e => setFilter(e.target.value)} /></div>}
      {section !== "overview" && section !== "profile" && <div className="owner-records">{!loading && !message && visibleRows.length === 0 && <div className="owner-empty"><span className="owner-icon"><FileText size={20} /></span><h2>{filter ? "No matching results" : `No ${names[section].toLowerCase()} yet`}</h2><p>{filter ? "Try a different search term." : readOnly ? "Records will appear here when available." : "Add an entry to get started."}</p></div>}{visibleRows.map((row, index) => {
        if (readOnly) return <article key={row.id == null ? `${section}-row-${index}` : `${section}-id-${String(row.id)}`} className="owner-card owner-record"><div className="owner-record-head"><span className="owner-icon">{section === "calls" ? <Headphones size={18} /> : <CalendarDays size={18} />}</span><span className="owner-kicker">{section === "calls" ? "CALL RECORD" : "APPOINTMENT"}</span></div><dl>{Object.entries(row).map(([k, v]) => <div key={k}><dt>{label(k)}</dt><dd>{String(v ?? "—")}</dd></div>)}</dl>{section === "calls" && row.id != null && <Link className="owner-inline-link" href={`/calls/${encodeURIComponent(String(row.id))}`}>View call details <ArrowRight size={14} /></Link>}</article>;
        if (section === "hours") {
          const day = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][Number(row.day_of_week)] ?? "Day";
          const closed = row.is_closed === true;
          return <fieldset key={String(row.day_of_week)} className="owner-card schedule-card"><legend className="sr-only">{day} hours</legend>
            <div className="schedule-header"><div><p className="owner-kicker">WEEKLY HOURS</p><h2>{day}</h2><p>{closed ? "The clinic is closed this day." : "Set opening hours and an optional break."}</p></div>
              <label className="schedule-toggle"><input type="checkbox" checked={closed} onChange={e => editRow(index, "is_closed", e.target.checked)} /><span>Closed</span></label></div>
            <fieldset className="schedule-times" disabled={closed}><legend className="sr-only">{day} opening and break times</legend>
              <div className="schedule-group"><p className="schedule-group-title">CLINIC HOURS</p><div className="schedule-pair">{["open_time", "close_time"].map(key => input(key, row[key] ?? null, value => editRow(index, key, value)))}</div></div>
              <div className="schedule-group"><p className="schedule-group-title">BREAK <span>OPTIONAL</span></p><div className="schedule-pair">{["break_start", "break_end"].map(key => input(key, row[key] ?? null, value => editRow(index, key, value)))}</div></div>
            </fieldset>{closed && <p className="schedule-note">Hours will be cleared when you save this closed day.</p>}
          </fieldset>;
        }
        return <fieldset key={index} className="owner-card owner-editor"><legend className="sr-only">{`Entry ${index + 1}`}</legend><div className="owner-section-head"><div><p className="owner-kicker">{names[section].toUpperCase()}</p><h2>{`Entry ${index + 1}`}</h2></div><button type="button" className="owner-remove" onClick={() => setRows(rows.filter((_, i) => i !== index))}><Trash2 size={15} /> Remove</button></div><div className="owner-fields">{Object.entries(row).map(([k, v]) => input(k, v, value => editRow(index, k, value)))}</div></fieldset>;
      })}{!readOnly && <div className="owner-actions">{defaults[section] && <Button variant="outline" disabled={loading} onClick={() => setRows([...rows, { ...defaults[section] }])}><Plus size={16} /> Add entry</Button>}<Button disabled={loading} onClick={() => void save()}><CheckCircle2 size={16} /> Save changes</Button></div>}</div>}
      </div></div></div>}
  </main>;
}

