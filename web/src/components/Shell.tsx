import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api, isActive, runStatusLine, useData, type Run } from "../api";
import { Icon } from "../icons";
import { useSession } from "../session";
import { Prompting } from "./Prompting";

// -- shell-wide: the prompt overlay, a toast, and Escape twice -------------------------------------

/** What a prompt is about, when it is about the screen: the window in front. */
export interface PromptContext { window: string; title?: string }
interface ShellCtx { openPrompt: (seed?: string, context?: PromptContext) => void; toast: (msg: string) => void }
const Ctx = createContext<ShellCtx>({ openPrompt: () => {}, toast: () => {} });
export const useShell = () => useContext(Ctx);

export function ShellProvider({ children }: { children: ReactNode }) {
  const [prompt, setPrompt] = useState<{ open: boolean; seed: string; context?: PromptContext }>({ open: false, seed: "" });
  const [toastMsg, setToast] = useState("");
  const lastEsc = useRef(0);
  const toast = useCallback((m: string) => { setToast(m); window.setTimeout(() => setToast(""), 4000); }, []);
  const openPrompt = useCallback((seed = "", context?: PromptContext) => setPrompt({ open: true, seed, context }), []);

  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPrompt(); return; }
      if (e.key !== "Escape" || prompt.open) return;
      const now = Date.now();
      if (now - lastEsc.current < 600) {
        // Escape twice stops every running task and stops watching the screen,
        // as it does on the device. It goes straight to the control plane and the
        // agent's kill switch; no model is involved.
        lastEsc.current = 0;
        const runs = await api<Run[]>("/api/runs").catch(() => [] as Run[]);
        const active = runs.filter(isActive);
        await Promise.all(active.map((r) => api(`/api/runs/${r.id}/stop`, { body: { reason: "Escape pressed twice" } }).catch(() => null)));
        const agents = (await api<{ agents: { id: string; connected: boolean }[] }>("/api/me").catch(() => ({ agents: [] }))).agents;
        await Promise.all(agents.filter((a) => a.connected).map((a) => api(`/api/agents/${a.id}/watch`, { body: { on: false } }).catch(() => null)));
        toast(active.length ? `Stopped ${active.length} running task${active.length === 1 ? "" : "s"} and stopped watching.` : "Nothing is running. Stopped watching the screen.");
      } else lastEsc.current = now;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPrompt, prompt.open, toast]);

  return (
    <Ctx.Provider value={{ openPrompt, toast }}>
      {children}
      {prompt.open && <Prompting seed={prompt.seed} context={prompt.context} onClose={() => setPrompt({ open: false, seed: "" })} />}
      {toastMsg && <div className="toast" role="status">{toastMsg}</div>}
    </Ctx.Provider>
  );
}

// -- rail pieces ------------------------------------------------------------------------------------

export function RailHead({ newTask }: { newTask?: boolean }) {
  const { openPrompt } = useShell();
  return (
    <div className="rail-head">
      <span className="mark">Q</span>
      <span className="wordmark grow">Ondo Quartermaster</span>
      {newTask && (
        <button type="button" className="rail-icon-btn" aria-label="New task" onClick={() => openPrompt()}>
          <Icon name="plus" size={15} />
        </button>
      )}
    </div>
  );
}

export function RailFoot() {
  const { me, signOut } = useSession();
  const nav = useNavigate();
  if (!me) return null;
  return (
    <div className="rail-foot">
      <span className="avatar">{me.user.name.split(" ").map((p) => p[0]).slice(0, 2).join("")}</span>
      <span className="grow col">
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--on-rail)" }}>{me.user.name}</span>
        <span className="caption rail-muted">{me.user.title || me.user.email}</span>
      </span>
      <button type="button" aria-label="Sign out" className="rail-icon-btn" style={{ border: "none", color: "var(--on-rail-muted)" }}
        onClick={async () => { await signOut(); nav("/signin"); }}>
        <Icon name="sign-out" size={16} />
      </button>
    </div>
  );
}

export function TaskList({ activeId, limit = 6 }: { activeId?: string; limit?: number }) {
  const { data: runs } = useData<Run[]>("/api/runs", (e) => e === "run" || e === "approval");
  const today = (runs ?? []).filter((r) => r.created_at > Date.now() - 24 * 3600_000 || isActive(r)).slice(0, limit);
  return (
    <div className="col" style={{ gap: 8 }}>
      <span className="rail-label">Today</span>
      <div className="rail-list">
        {today.length === 0 && <span className="caption rail-muted" style={{ padding: "0 12px" }}>No tasks yet today.</span>}
        {today.map((r) => (
          <Link key={r.id} to={`/app/runs/${r.id}`} className={`rail-row${r.id === activeId || (!activeId && r.status === "running") ? " active" : ""}`}>
            <span className={`dot ${r.status === "running" || r.status === "queued" ? "dot-rail" : "dot-ring"}`} />
            <span className="grow col" style={{ gap: 2, minWidth: 0 }}>
              <span className="t">{r.title}</span>
              <span className="m">{runStatusLine(r)}</span>
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function AgentCard() {
  const { me } = useSession();
  const agent = me?.agents[0];
  return (
    <div className="rail-card" style={{ marginTop: "auto", marginBottom: 16 }}>
      <div className="row" style={{ gap: 8 }}>
        <span className={`dot ${agent?.connected ? "dot-rail" : "dot-ring"}`} />
        <span className="grow caption rail-muted">Desktop agent</span>
        <span className="caption" style={{ color: "var(--on-rail)" }}>{!agent ? "Not paired" : agent.connected ? "Connected" : "Offline"}</span>
      </div>
      {agent && (
        <div className="row" style={{ gap: 8 }}>
          <span className="caption rail-muted">Device</span>
          <span className="grow caption" style={{ color: "var(--on-rail)", textAlign: "right" }}>{agent.hostname || "Unnamed"}</span>
        </div>
      )}
      <Link to="/pair" className="caption" style={{ color: "var(--rail-accent)" }}>{agent ? "Manage permissions" : "Pair the desktop agent"}</Link>
    </div>
  );
}

export function Shortcuts() {
  const { me } = useSession();
  return (
    <div className="col" style={{ gap: 8 }}>
      <span className="rail-label">Go to</span>
      <div className="rail-list">
        <NavLink to="/app/files" className="rail-row"><Icon name="folder" size={16} color="var(--on-rail-muted)" /><span className="t grow">Files and connections</span></NavLink>
        {me?.user.role === "admin" && (
          <>
            <NavLink to="/admin" className="rail-row"><Icon name="lock" size={16} color="var(--on-rail-muted)" /><span className="t grow">Admin console</span></NavLink>
            <NavLink to="/admin/audit" className="rail-row"><Icon name="workflow" size={16} color="var(--on-rail-muted)" /><span className="t grow">Audit log</span></NavLink>
          </>
        )}
      </div>
    </div>
  );
}

export function BackHome() {
  return <Link to="/app" className="back"><Icon name="arrow-left" size={16} />Back to home</Link>;
}

export function Crumbs({ items }: { items: { label: string; to?: string }[] }) {
  return (
    <>
      {items.map((c, i) => (
        <span key={i} className="row" style={{ gap: 16 }}>
          {i > 0 && <span className="crumb-sep">/</span>}
          {c.to ? <Link to={c.to} className="crumb">{c.label}</Link> : <span className={i === items.length - 1 ? "crumb-here" : "crumb"}>{c.label}</span>}
        </span>
      ))}
    </>
  );
}
