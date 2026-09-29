import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, greeting, hhmm, isActive, useData, useScreen, type Approval, type Run, type RunEvent } from "../api";
import { AgentCard, Crumbs, RailFoot, RailHead, Shortcuts, TaskList, useShell } from "../components/Shell";
import { Icon } from "../icons";
import { useSession } from "../session";

export function Workspace() {
  const { me } = useSession();
  const { openPrompt, toast } = useShell();
  const [scope, setScope] = useState<"mine" | "team" | "all">("mine");
  const { data: runs } = useData<Run[]>(`/api/runs?scope=${scope}`, (e) => e === "run" || e === "approval" || e === "run_event");
  const { data: approvals, reload: reloadApprovals } = useData<Approval[]>("/api/approvals?status=pending", (e) => e === "approval");
  const { data: week } = useData<{ finished: number; stopped_for_decision: number; corrected: number }>("/api/summary", (e) => e === "run");
  if (!me) return null;

  const running = (runs ?? []).find((r) => ["running", "queued", "paused", "waiting"].includes(r.status));
  const pending = approvals ?? [];
  const runningCount = (runs ?? []).filter((r) => r.status === "running" || r.status === "queued").length;
  const leadParts = [
    pending.length ? `${pending.length === 1 ? "One thing needs" : `${numberWord(pending.length)} things need`} your decision.` : "Nothing is waiting on you.",
    runningCount ? `${runningCount === 1 ? "One task is" : `${numberWord(runningCount)} tasks are`} running on your machine.` : "",
  ];

  async function approve(a: Approval) {
    try {
      await api(`/api/approvals/${a.id}`, { body: { approved: true } });
      toast(a.kind === "consent" ? "Allowed. Ondo is carrying on." : "Approved. Ondo is carrying on.");
      void reloadApprovals();
    } catch (e) {
      toast((e as ApiError).message);
    }
  }

  return (
    <div className="shell">
      <nav className="rail" aria-label="Tasks">
        <RailHead newTask />
        <div className="rail-body" style={{ paddingTop: 16 }}>
          <TaskList />
          <Shortcuts />
          <AgentCard />
        </div>
        <RailFoot />
      </nav>

      <main className="main">
        <header className="topbar">
          <Crumbs items={[{ label: me.org.name }, { label: "Home" }]} />
          <span className="grow" />
          {me.user.role === "admin" && (
            <div className="pill-tabs" role="group" aria-label="Whose runs">
              {(["mine", "team", "all"] as const).map((s) => (
                <button key={s} aria-pressed={scope === s} onClick={() => setScope(s)}>{s === "mine" ? "Mine" : s === "team" ? "Team" : "All runs"}</button>
              ))}
            </div>
          )}
          <button className="btn btn-primary" onClick={() => openPrompt()}>New task</button>
        </header>

        <div className="canvas">
          <div className="col" style={{ gap: 6 }}>
            <h1 className="display">{greeting(me.user.name)}</h1>
            <p className="lead">{leadParts.filter(Boolean).join(" ")}</p>
          </div>

          <section className="card" aria-labelledby="waiting">
            <div className="card-head">
              <span id="waiting" className="eyebrow-sm">Waiting on you</span>
              <span className="grow" />
              <span className="caption tabular">{pending.length} approval{pending.length === 1 ? "" : "s"}</span>
            </div>
            {pending.length === 0 && <p className="ui secondary" style={{ padding: "16px 20px" }}>No approvals waiting. Anything that sends, submits or overwrites stops here first.</p>}
            {pending.map((a, i) => (
              <div key={a.id}>
                {i > 0 && <div className="divider" />}
                <div className="row" style={{ padding: "16px 20px", gap: 16 }}>
                  <span className="icon-square"><Icon name={a.kind === "consent" ? "plug" : a.effects.includes("sends_externally") ? "mail" : a.effects.includes("file_write") ? "file" : "card"} size={17} /></span>
                  <span className="grow col" style={{ gap: 2 }}>
                    <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>{a.title}</span>
                    <span className="caption">{a.run_title} · {changedLine(a)}</span>
                  </span>
                  <Link to={`/app/runs/${a.run_id}`} className="btn btn-sm">Review</Link>
                  <button className="btn btn-sm btn-primary" onClick={() => approve(a)}>{a.kind === "consent" ? "Allow" : "Approve"}</button>
                </div>
              </div>
            ))}
          </section>

          <section className="row" style={{ gap: 20, alignItems: "stretch" }}>
            <div className="card card-pad grow">
              {running ? (
                <>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="dot dot-accent qm-pulse" />
                    <span className="eyebrow-sm">{running.status === "paused" ? "Paused" : running.status === "waiting" ? "Waiting on you" : "Running now"}</span>
                  </div>
                  <h2 className="card-title">{running.title}</h2>
                  <p className="ui secondary">{running.status === "waiting" ? "Stopped at an approval gate. Nothing has been saved or sent yet." : `${running.steps.current ? `${running.steps.current}.` : "Starting."} It will stop before anything is submitted, sent or saved.`}</p>
                  <div className="progress" aria-hidden="true"><div style={{ width: `${Math.min(95, 12 + running.steps.done * 12)}%` }} /></div>
                  <div className="row">
                    <span className="caption tabular">Step {Math.max(1, running.steps.total)} · started {hhmm(running.created_at)}</span>
                    <span className="grow" />
                    <Link to={`/app/runs/${running.id}`} style={{ fontSize: 15, fontWeight: 600 }}>Watch it work</Link>
                  </div>
                </>
              ) : (
                <>
                  <span className="eyebrow-sm">Nothing running</span>
                  <h2 className="card-title">Say what you need</h2>
                  <p className="ui secondary">“Build the Q3 renewal pack for Northwind from the contracts in the client folder, and flag anything that uplifts above five per cent.”</p>
                  <div><button className="btn btn-primary btn-sm" onClick={() => openPrompt()}>New task</button></div>
                </>
              )}
            </div>
            <div className="card card-pad" style={{ width: 320, flexShrink: 0 }}>
              <span className="eyebrow-sm">This week</span>
              <Stat n={week?.finished} label="tasks finished" />
              <div className="divider" />
              <Stat n={week?.stopped_for_decision} label="stopped for a decision" />
              <div className="divider" />
              <Stat n={week?.corrected} label="refused by you" />
              {me.user.role === "admin" && <Link to="/admin/audit" style={{ fontSize: 15 }}>Open the audit log</Link>}
            </div>
          </section>
        </div>
      </main>

      <AssistantPanel />
    </div>
  );
}

function Stat({ n, label }: { n: number | undefined; label: string }) {
  return (
    <div className="row" style={{ alignItems: "baseline", gap: 8 }}>
      <span className="stat">{n ?? "–"}</span>
      <span className="ui secondary">{label}</span>
    </div>
  );
}

function changedLine(a: Approval): string {
  if (a.kind === "consent") return "First use of a connector: allow it?";
  const n = a.values.filter((v) => v.before != null).length;
  return n ? `${n} value${n === 1 ? "" : "s"} change${n === 1 ? "s" : ""}` : a.summary;
}

function numberWord(n: number): string {
  return ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"][n] ?? String(n);
}

// -- the assistant panel: questions become short runs on the agent ----------------------------------

const KEY = "ondo.assistant";

function AssistantPanel() {
  const { me } = useSession();
  const [asked, setAsked] = useState<{ run_id: string; q: string }[]>(() => {
    try { return JSON.parse(sessionStorage.getItem(KEY) ?? "[]"); } catch { return []; }
  });
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(true);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { try { sessionStorage.setItem(KEY, JSON.stringify(asked.slice(-8))); } catch { /* ignore */ } }, [asked]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [asked]);
  const agent = me?.agents[0];

  async function ask(e: FormEvent) {
    e.preventDefault();
    const q = text.trim();
    if (!q) return;
    setError("");
    try {
      const r = await api<{ run_id: string }>("/api/runs", { body: { request: q } });
      setAsked((a) => [...a, { run_id: r.run_id, q }]);
      setText("");
    } catch (err) {
      setError((err as ApiError).message);
    }
  }

  if (!open) {
    return (
      <button className="btn btn-ghost" style={{ position: "fixed", right: 16, top: 12 }} aria-label="Open the assistant" onClick={() => setOpen(true)}>
        <Icon name="chat" size={17} /> Quartermaster
      </button>
    );
  }

  return (
    <aside className="aside" aria-label="Quartermaster assistant">
      <header className="aside-head">
        <Icon name="chat" size={17} color="var(--ink-secondary)" />
        <span className="grow card-title">Quartermaster</span>
        <button type="button" aria-label="Close panel" className="btn-ghost" style={{ width: 28, height: 28, border: "none", background: "transparent", color: "var(--ink-muted)", cursor: "pointer" }} onClick={() => setOpen(false)}>
          <Icon name="panel-close" size={16} />
        </button>
      </header>
      <div style={{ flexGrow: 1, padding: 20, display: "flex", flexDirection: "column", gap: 12, overflowY: "auto" }}>
        <ScreenChip />
        {asked.length === 0 && (
          <div className="bubble-bot qm-rise">
            Ask about a file or a figure, or say what to do next. I read only the folders you granted, and anything that changes something stops for you first.
          </div>
        )}
        {asked.map((a) => <Exchange key={a.run_id} runId={a.run_id} q={a.q} />)}
        <div ref={end} />
      </div>
      <form onSubmit={ask} style={{ flexShrink: 0, padding: "16px 20px", borderTop: "1px solid var(--line-divider)", display: "flex", flexDirection: "column", gap: 8 }}>
        <label htmlFor="ask" className="eyebrow-sm">Ask or instruct</label>
        <div className="row" style={{ gap: 8 }}>
          <input id="ask" className="input grow" placeholder="Ask about a file, or say what to do next" value={text} onChange={(e) => setText(e.target.value)} />
          <button aria-label="Send" className="btn btn-primary" style={{ width: 40, padding: 0 }}><Icon name="arrow-up" size={16} /></button>
        </div>
        {error && <span className="error-text" role="alert">{error}</span>}
        <span className="caption">Escape twice stops any running task.</span>
      </form>
    </aside>
  );
}

/** The context chip: what Ondo can see right now, and a way to ask about it. */
function ScreenChip() {
  const { me } = useSession();
  const { openPrompt, toast } = useShell();
  const agent = me?.agents[0];
  const { data } = useScreen(agent?.id);
  const s = data?.screen;
  const shared = agent?.grants.screen.granted && agent.capabilities?.screen;
  const summary = !agent ? "No desktop agent paired"
    : !agent.connected ? "Desktop agent offline"
    : `${agent.grants.files.scope.length} granted folder${agent.grants.files.scope.length === 1 ? "" : "s"} · ${shared ? `${agent.grants.screen.scope.length || "all"} shared window${agent.grants.screen.scope.length === 1 ? "" : "s"}` : "no windows shared"}`;
  async function resume() {
    try { await api(`/api/agents/${agent!.id}/watch`, { body: { on: true } }); } catch (e) { toast((e as ApiError).message); }
  }
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="context-chip">
        <span className={`dot ${agent?.connected ? "dot-accent qm-pulse" : "dot-ring-light"}`} />
        <span className="grow">{summary}</span>
        <Link to="/pair" style={{ fontSize: 13, fontWeight: 600, color: "var(--accent-hover)" }}>Change</Link>
      </div>
      {agent?.connected && shared && s && (
        s.watching ? (
          s.window && (
            <div className="context-chip qm-rise">
              <Icon name="monitor" size={15} color="var(--ink-secondary)" />
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={s.window}>On screen: {s.title ?? s.window}</span>
              <button type="button" className="link-btn" onClick={() => openPrompt("", { window: s.window!, title: s.title ?? undefined })}>Ask about this screen</button>
            </div>
          )
        ) : (
          <div className="context-chip qm-rise">
            <Icon name="monitor" size={15} color="var(--ink-muted)" />
            <span className="grow">Not watching the screen</span>
            <button type="button" className="link-btn" onClick={resume}>Resume</button>
          </div>
        )
      )}
    </div>
  );
}

function Exchange({ runId, q }: { runId: string; q: string }) {
  const { data } = useData<{ run: Run; events: RunEvent[] }>(`/api/runs/${runId}`, (e, d) => d?.run_id === runId);
  const run = data?.run;
  const reads = [...new Set((data?.events ?? []).filter((e) => e.type === "file_access" && e.data.op === "read").map((e) => String(e.data.path).split("/").pop()))];
  return (
    <>
      <div className="bubble-user qm-rise">{q}</div>
      {!run || isActive(run) ? (
        run?.status === "waiting" ? (
          <div className="bubble-bot qm-rise">This needs your approval before I carry on. <Link to={`/app/runs/${runId}`}>Review it</Link>.</div>
        ) : (
          <div className="bubble-bot qm-rise" style={{ flexDirection: "row", gap: 5, padding: 14 }} aria-label="Working">
            <span className="qm-dot" style={dotStyle} /><span className="qm-dot qm-dot2" style={dotStyle} /><span className="qm-dot qm-dot3" style={dotStyle} />
          </div>
        )
      ) : (
        <div className="bubble-bot qm-rise">
          <span>{run.answer || run.reason || "Stopped."}</span>
          {reads.slice(0, 3).map((f) => (
            <Link key={f} to="/app/files" className="row" style={{ gap: 8, padding: "8px 10px", background: "var(--surface-sunken)", borderRadius: 4 }}>
              <Icon name="file" size={15} color="var(--ink-muted)" />
              <span className="grow caption" style={{ color: "var(--ink-body)" }}>{f}</span>
            </Link>
          ))}
          <Link to={`/app/runs/${runId}`} className="caption">See every step</Link>
        </div>
      )}
    </>
  );
}

const dotStyle = { width: 6, height: 6, borderRadius: "50%", background: "var(--ink-muted)", display: "block" } as const;
