// The web UI talks to the control plane only, never to a desktop. Plain fetch
// with the session cookie, and one server-sent event stream for live updates.

import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  constructor(public status: number, message: string, public body: any) { super(message); }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const r = await fetch(path, {
    method: opts.method ?? (opts.body === undefined ? "GET" : "POST"),
    credentials: "same-origin",
    headers: opts.body === undefined ? {} : { "content-type": "application/json" },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const text = await r.text();
  let json: any = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = text; }
  if (!r.ok) throw new ApiError(r.status, json?.message ?? json?.error ?? `HTTP ${r.status}`, json);
  return json as T;
}

// -- types ------------------------------------------------------------------------------

export type GrantKind = "files" | "screen" | "input";
export interface Grant { granted: boolean; scope: string[]; updated_by?: string; updated_at?: number }
export type Grants = Record<GrantKind, Grant>;
export interface Capabilities { files?: boolean; browser?: boolean; desktop?: boolean; screen?: boolean; input?: boolean; pixels?: boolean; desktop_backend?: string | null }
/** Which shared window is in front, as the agent reports it. Only its name. */
export interface ScreenState { agent_id: string; available: boolean; pixels: boolean; screen: { window: string | null; title: string | null; watching: boolean; at: number } | null }
export interface Agent { id: string; hostname: string; os: string; connected: boolean; grants: Grants; capabilities?: Capabilities; last_seen?: number }
export interface Me {
  user: { id: string; email: string; name: string; title: string; role: "member" | "admin" };
  org: { id: string; name: string; policy: Policy };
  session: { stage: "needs_device" | "verified"; method: string };
  device: { id: string; name: string; os: string; managed: number; trusted_until: number } | null;
  agents: Agent[];
}
export interface Policy { excluded_paths: string[]; excluded_windows: string[]; disabled_grants: string[]; writes_require_approval: boolean; allowed_origins?: string[] }
export interface Run {
  id: string; agent_id: string; user_id: string; request: string; title: string; status: string; answer: string; reason: string;
  model: string; created_at: number; updated_at: number; steps: { total: number; done: number; current: string };
  pending_approvals: number; user?: { name: string; email: string }; agent_connected: boolean;
}
export interface RunEvent { seq: number; type: string; source: string; ts: number; hash?: string; data: any }
export interface ApprovalValue { label: string; after: string; before: string | null; flagged?: boolean }
export interface Approval {
  id: string; run_id: string; title: string; summary: string; effects: string[]; values: ApprovalValue[]; diff: string | null;
  tool: string; status: string; resolved_by: string | null; resolved_at: number | null; note: string; created_at: number; run_title?: string;
}

// -- live updates -------------------------------------------------------------------------

type Listener = (event: string, data: any) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;

function ensureStream() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource("/api/stream");
  for (const ev of ["run", "run_event", "approval", "agent", "grants", "screen"]) {
    source.addEventListener(ev, (e) => {
      let data: any = null;
      try { data = JSON.parse((e as MessageEvent).data); } catch { /* ignore */ }
      for (const l of listeners) l(ev, data);
    });
  }
  source.onerror = () => {
    // The browser reconnects on its own; drop and recreate if the session ended.
    if (source?.readyState === EventSource.CLOSED) { source = null; setTimeout(ensureStream, 3000); }
  };
}

export function closeStream() { source?.close(); source = null; }

export function useLive(fn: Listener, deps: unknown[] = []) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    ensureStream();
    const l: Listener = (e, d) => ref.current(e, d);
    listeners.add(l);
    return () => { listeners.delete(l); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Fetch, and refetch when a live event says something relevant changed. */
export function useData<T>(path: string | null, relevant: (event: string, data: any) => boolean = () => true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const timer = useRef<number | null>(null);
  const load = useCallback(async () => {
    if (!path) return;
    try { setData(await api<T>(path)); setError(null); } catch (e) { setError(e as ApiError); }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  useLive((e, d) => {
    if (!relevant(e, d)) return;
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void load(), 120);
  }, [load]);
  return { data, error, reload: load, setData };
}

/** The window in front on this agent, live. */
export function useScreen(agentId: string | undefined) {
  return useData<ScreenState>(agentId ? `/api/agents/${agentId}/screen` : null,
    (e, d) => ((e === "screen" || e === "agent") && d?.agent_id === agentId) || e === "grants");
}

// -- formatting -------------------------------------------------------------------------------

export const hhmm = (ms: number) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

export function whenLabel(ms: number): string {
  const d = new Date(ms);
  const today = new Date();
  const y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return `Today, ${hhmm(ms)}`;
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]!.toUpperCase()).join("");
}

export function greeting(name: string): string {
  const h = new Date().getHours();
  const first = name.split(" ")[0];
  return `${h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"}, ${first}`;
}

export const EFFECT_LABELS: Record<string, string> = {
  submits_to_system_of_record: "submissions to a system of record",
  sends_externally: "anything sent outside",
  overwrites_shared_file: "overwriting a shared file",
  moves_money: "anything that moves money",
  file_write: "every file write",
};

export function runStatusLine(r: Run): string {
  if (r.status === "waiting") return `Waiting on you · ${r.pending_approvals} approval${r.pending_approvals === 1 ? "" : "s"}`;
  if (r.status === "running" || r.status === "queued") return r.steps.total ? `Running · step ${r.steps.total}` : "Starting";
  if (r.status === "paused") return "Paused";
  if (r.status === "finished") return `Done · ${hhmm(r.updated_at)}`;
  if (r.status === "stopped") return `Stopped · ${hhmm(r.updated_at)}`;
  return r.status;
}

export const isActive = (r: Run) => ["queued", "running", "waiting", "paused"].includes(r.status);
