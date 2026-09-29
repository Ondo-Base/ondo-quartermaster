import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, EFFECT_LABELS, api, hhmm, isActive, useData, type Approval, type Capabilities, type Grants, type Run, type RunEvent } from "../api";
import { BackHome, Crumbs, RailFoot, RailHead, TaskList, useShell } from "../components/Shell";
import { Icon } from "../icons";

interface Detail { run: Run; events: RunEvent[]; approvals: Approval[]; grants: Grants; capabilities?: Capabilities }

interface Item {
  key: string; title: string; ts: number; status: "done" | "now" | "error" | "next";
  note?: string; chips?: string[]; typed?: { label: string; value: string }[]; diff?: string; error?: string;
}

const base = (p: string) => String(p).split("/").filter(Boolean).pop() ?? String(p);

/** Turn the event stream into the readable list of what Ondo did, one entry per model turn. */
export function buildTimeline(events: RunEvent[]): Item[] {
  const items: Item[] = [];
  let turnText = "";
  let turn: { steps: Map<string, { title: string; tool: string; status: string; ts: number; detail: any }>; args: Map<string, any>; ts: number } | null = null;
  const diffs = new Map<string, string>();

  const flush = () => {
    if (!turn || turn.steps.size === 0) { turn = null; return; }
    const steps = [...turn.steps.values()];
    const tools = new Set(steps.map((s) => s.tool));
    const failed = steps.filter((s) => s.status === "error");
    const running = steps.some((s) => s.status === "running");
    let title = steps.length === 1 ? steps[0].title : steps.map((s) => s.title).join(", ");
    if (steps.length > 1 && tools.size === 1) {
      const t = [...tools][0];
      title = t === "read_file" ? `Read ${steps.length} files` : t === "list_folder" ? `Opened ${steps.length} folders` : title;
    }
    const chips = steps.filter((s) => s.tool === "read_file" || s.tool === "edit_workbook" || s.tool === "create_workbook")
      .map((s) => base(s.detail?.path ?? s.title.replace(/^(Read|Wrote|Edited \d+ cells in) /, "")));
    const typed: { label: string; value: string }[] = [];
    for (const [id, a] of turn.args) {
      const s = turn.steps.get(id);
      if (s?.tool === "browser_fill_form") for (const f of a.fields ?? []) typed.push({ label: f.name, value: String(f.value) });
      if (s?.tool === "browser_type") typed.push({ label: a.element ?? "Field", value: String(a.text) });
      if (s?.tool === "desktop_act" && a.action === "set_text") typed.push({ label: `${a.target} in ${a.window}`, value: String(a.text) });
      if (s?.tool === "screen_act") {
        for (const x of a.actions ?? []) if (x.action === "type") typed.push({ label: `${x.target ?? "The focused field"} in ${a.window}`, value: String(x.text) });
      }
    }
    const diff = steps.map((s) => diffs.get(s.detail?.path ?? "")).find(Boolean);
    items.push({
      key: `t${turn.ts}`, title, ts: turn.ts, status: failed.length ? "error" : running ? "now" : "done",
      note: turnText || undefined, chips: chips.length > 1 || tools.has("read_file") ? chips : undefined,
      typed: typed.length ? typed : undefined, diff,
      error: failed.length ? String(failed[0].detail?.error ?? "").replace(/^Permission denied: /, "") : undefined,
    });
    turn = null;
  };

  for (const e of events) {
    if (e.type === "model_response") {
      flush();
      turnText = e.data.text ?? "";
      turn = { steps: new Map(), args: new Map(), ts: e.ts * 1000 };
      for (const c of e.data.tool_calls ?? []) turn.args.set(c.id, c.arguments);
    } else if (e.type === "step" && turn) {
      const prev = turn.steps.get(e.data.call_id);
      turn.steps.set(e.data.call_id, { title: e.data.title, tool: e.data.tool, status: e.data.status, ts: prev?.ts ?? e.ts * 1000, detail: e.data.detail ?? prev?.detail });
    } else if (e.type === "diff_proposed") {
      diffs.set(e.data.path, e.data.diff);
    }
  }
  flush();
  return items;
}

export function TaskRun() {
  const { id = "" } = useParams();
  const { openPrompt, toast } = useShell();
  const { data, error, reload } = useData<Detail>(`/api/runs/${id}`, (_e, d) => d?.run_id === id || _e === "approval");
  const [tab, setTab] = useState<"steps" | "trajectory">("steps");
  const timeline = useMemo(() => buildTimeline(data?.events ?? []), [data]);

  if (error) return <div className="canvas"><p className="lead">{error.status === 404 ? "There is no run with that id." : error.message}</p><Link to="/app">Back to home</Link></div>;
  if (!data) return null;
  const { run, events, approvals, grants } = data;
  const pending = approvals.find((a) => a.status === "pending");
  const flags = events.filter((e) => e.type === "screening" && e.data.flagged);
  const denials = events.filter((e) => e.type === "permission_denied");
  const active = isActive(run);

  async function act(path: string, body: unknown = {}) {
    try { await api(`/api/runs/${run.id}/${path}`, { body }); void reload(); } catch (e) { toast((e as ApiError).message); }
  }

  const statusLine = {
    queued: "Starting", running: "Working", waiting: "Paused for approval", paused: "Paused by you",
    finished: `Finished ${hhmm(run.updated_at)}`, stopped: "Stopped", error: "Stopped by an error",
  }[run.status] ?? run.status;

  return (
    <div className="shell">
      <nav className="rail" aria-label="Tasks">
        <RailHead />
        <div className="rail-body" style={{ paddingTop: 16 }}>
          <BackHome />
          <TaskList activeId={run.id} />
          <div className="rail-card" style={{ marginTop: "auto", marginBottom: 16, gap: 10 }}>
            <span className="rail-label">This run uses</span>
            {grants.files.granted && grants.files.scope.map((f) => (
              <div key={f} className="row" style={{ gap: 8 }}><Icon name="folder" size={15} color="var(--rail-accent)" /><span className="grow" style={{ fontSize: 14 }}>{base(f)}</span></div>
            ))}
            <div className="row" style={{ gap: 8 }}><Icon name="monitor" size={15} color="var(--rail-accent)" /><span className="grow" style={{ fontSize: 14 }}>{grants.screen.granted && data.capabilities?.screen ? `Screen: ${grants.screen.scope.join(", ") || "shared windows"}` : "Screen: not shared"}</span></div>
            {grants.input.granted && <div className="row" style={{ gap: 8 }}><Icon name="keyboard" size={15} color="var(--rail-accent)" /><span className="grow" style={{ fontSize: 14 }}>{data.capabilities?.desktop ? "Keyboard and pointer" : "Web portals, through the browser"}</span></div>}
            {!grants.files.granted && !grants.input.granted && <span className="caption rail-muted">No grants are active on this agent.</span>}
          </div>
        </div>
        <RailFoot />
      </nav>

      <main className="main">
        <header className="topbar">
          <Crumbs items={[{ label: "Home", to: "/app" }, { label: run.title }]} />
          <span className="grow" />
          {active && run.status !== "paused" && <button className="btn" onClick={() => act("pause")}>Pause</button>}
          {run.status === "paused" && <button className="btn" onClick={() => act("resume")}>Resume</button>}
          {active && <button className="btn" onClick={() => act("stop", { reason: "Taken over by the user" })}>Take over</button>}
          {!active && <button className="btn" onClick={() => openPrompt(run.request)}>Run again</button>}
        </header>

        <div className="canvas" style={{ padding: 32, gap: 24 }}>
          <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
            <div className="grow col" style={{ gap: 8 }}>
              <span className="eyebrow tabular">Run {run.id.slice(-4)} · started {hhmm(run.created_at)}</span>
              <h1 className="chapter-title">{run.title}</h1>
              <p className="lead">“{run.request}”</p>
            </div>
            <div className="card" style={{ width: 200, flexShrink: 0, padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
              <span className="eyebrow-sm">Progress</span>
              <span className="stat">{run.steps.total ? `Step ${run.steps.total}` : "Starting"}</span>
              <div className="progress" aria-hidden="true"><div style={{ width: `${active ? Math.min(95, 12 + run.steps.done * 12) : 100}%` }} /></div>
              <span className="caption">{statusLine}</span>
            </div>
          </div>

          <div className="row" style={{ alignItems: "flex-start", gap: 24, minHeight: 0 }}>
            <section className="card grow" style={{ minWidth: 0 }}>
              <div className="card-head">
                <div className="pill-tabs" role="group" aria-label="View">
                  <button aria-pressed={tab === "steps"} onClick={() => setTab("steps")}>What Ondo did</button>
                  <button aria-pressed={tab === "trajectory"} onClick={() => setTab("trajectory")}>Trajectory</button>
                </div>
                <span className="grow" />
                <span className="caption">{run.model ? `Model: ${run.model}` : ""}</span>
              </div>
              {tab === "steps" ? (
                <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 4 }}>
                  {timeline.length === 0 && <p className="ui secondary">{run.status === "queued" ? "Waiting for the desktop agent to start." : "No steps yet."}</p>}
                  {timeline.map((it, i) => (
                    <Step key={it.key} item={it} last={i === timeline.length - 1 && !pending && active} />
                  ))}
                  {pending && (
                    <div className="step">
                      <div className="step-rail"><span className="step-next" /></div>
                      <div className="step-body" style={{ paddingBottom: 0 }}>
                        <span style={{ fontSize: 16, color: "var(--ink-muted)" }}>Next: {pending.title.charAt(0).toLowerCase() + pending.title.slice(1)}</span>
                        <p className="ui muted">{pending.kind === "consent"
                          ? "Waiting for you to allow this connector. Nothing has been read from it."
                          : `Blocked by an approval gate on ${pending.effects.map((e) => EFFECT_LABELS[e] ?? e).join(" and ")}.`}</p>
                      </div>
                    </div>
                  )}
                  {!active && (
                    <div className="step">
                      <div className="step-rail">{run.status === "finished" ? <span className="step-done"><Icon name="check" size={12} width={2.4} /></span> : <span className="step-error"><Icon name="close" size={11} width={2.4} /></span>}</div>
                      <div className="step-body" style={{ paddingBottom: 0 }}>
                        <div className="row" style={{ alignItems: "baseline" }}>
                          <span className="step-title">{run.status === "finished" ? "Finished" : "Stopped"}</span>
                          <span className="grow" />
                          <span className="caption tabular">{hhmm(run.updated_at)}</span>
                        </div>
                        <p className="ui secondary" style={{ whiteSpace: "pre-wrap" }}>{run.answer || run.reason}</p>
                      </div>
                    </div>
                  )}
                </div>
              ) : <Trajectory events={events} />}
            </section>

            <aside style={{ width: 380, flexShrink: 0, display: "flex", flexDirection: "column", gap: 16 }}>
              {pending && <ApprovalCard approval={pending} onDone={reload} />}
              {flags.map((f) => (
                <div key={f.seq} className="card" style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 8 }}>
                  <div className="row" style={{ gap: 8 }}><Icon name="close" size={16} color="var(--danger)" /><span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>One flag to read</span></div>
                  <p className="ui">{base(String(f.data.origin).replace(/^(file|web):/, ""))} contains text aimed at an AI agent. Ondo treated it as data, not instructions, and everything beyond reading now stops for you.</p>
                </div>
              ))}
              {denials.length > 0 && (
                <div className="card" style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 8 }}>
                  <div className="row" style={{ gap: 8 }}><Icon name="lock" size={16} color="var(--ink-secondary)" /><span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Kept out</span></div>
                  {denials.slice(0, 4).map((d) => (
                    <p key={d.seq} className="ui secondary">{base(d.data.target || d.data.tool)} — {String(d.data.reason).replace(/_/g, " ")}</p>
                  ))}
                </div>
              )}
              {approvals.filter((a) => a.status !== "pending").length > 0 && (
                <div className="card" style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 8 }}>
                  <span className="eyebrow-sm">Decisions</span>
                  {approvals.filter((a) => a.status !== "pending").map((a) => (
                    <div key={a.id} className="row" style={{ alignItems: "baseline" }}>
                      <span className="grow ui">{a.title}</span>
                      <span className={`tag ${a.status === "approved" ? "tag-accent" : "tag-outline"}`}>{a.status === "approved" ? "Approved" : a.status === "refused" ? "Refused" : "Expired"}</span>
                    </div>
                  ))}
                </div>
              )}
            </aside>
          </div>
        </div>
      </main>
    </div>
  );
}

function Step({ item, last }: { item: Item; last: boolean }) {
  const status = last && item.status === "done" ? "done" : item.status;
  return (
    <div className="step qm-rise">
      <div className="step-rail">
        {status === "done" ? <span className="step-done"><Icon name="check" size={12} width={2.4} /></span>
          : status === "error" ? <span className="step-error"><Icon name="close" size={11} width={2.4} /></span>
          : <span className="step-now" />}
        <span className="line" />
      </div>
      <div className="step-body">
        <div className="row" style={{ alignItems: "baseline", gap: 10 }}>
          <span className="step-title">{item.title}</span>
          <span className="grow" />
          <span className="caption tabular">{hhmm(item.ts)}</span>
        </div>
        {item.note && <p className="ui secondary">{item.note}</p>}
        {item.error && <p className="ui" style={{ color: "var(--danger)" }}>{item.error}</p>}
        {item.chips && item.chips.length > 0 && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {item.chips.slice(0, 3).map((c) => <Link key={c} to="/app/files" className="chip">{c}</Link>)}
            {item.chips.length > 3 && <span className="chip muted">+ {item.chips.length - 3} more</span>}
          </div>
        )}
        {item.typed && (
          <div className="card" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="eyebrow-sm">Typed</span>
            {item.typed.slice(0, 3).map((t) => <span key={t.label} className="ui tabular">{t.label} · {Number.isFinite(Number(t.value)) ? Number(t.value).toLocaleString("en-GB") : t.value}</span>)}
            {item.typed.length > 3 && <span className="caption">and {item.typed.length - 3} more line{item.typed.length - 3 === 1 ? "" : "s"}</span>}
          </div>
        )}
        {item.diff && <pre className="diff">{item.diff}</pre>}
      </div>
    </div>
  );
}

const GERUND: Record<string, string> = { send: "sending", submit: "submitting", pay: "paying", save: "saving" };

const fmt = (v: string | null | undefined) => (v != null && v !== "" && Number.isFinite(Number(v)) ? Number(v).toLocaleString("en-GB") : v ?? "");

export function ApprovalCard({ approval, onDone }: { approval: Approval; onDone: () => void }) {
  if (approval.kind === "consent") return <ConsentCard approval={approval} onDone={onDone} />;
  return <EffectCard approval={approval} onDone={onDone} />;
}

/** A connector's first use: may Ondo use it at all? Yes lasts until revoked in Files and connections. */
function ConsentCard({ approval, onDone }: { approval: Approval; onDone: () => void }) {
  const { toast } = useShell();
  const [busy, setBusy] = useState(false);
  const name = approval.title.replace(/^Let Ondo use /, "").replace(/\?$/, "");
  async function decide(approved: boolean) {
    setBusy(true);
    try {
      await api(`/api/approvals/${approval.id}`, { body: { approved } });
      toast(approved ? `${name} allowed. Ondo is carrying on.` : `${name} not allowed. Ondo carries on without it.`);
      onDone();
    } catch (e) {
      toast((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="approval qm-rise" role="region" aria-label="Allow a connector">
      <div className="approval-head">
        <Icon name="plug" size={17} />
        <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>{approval.title}</span>
      </div>
      <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
        <p className="ui">{approval.summary}</p>
        <div className="kv">
          {approval.values.map((v) => (
            <div key={v.label} className="kv-row">
              <span className="grow" style={{ fontSize: 14 }}>{v.label}</span>
              <span className="v" style={{ fontVariantNumeric: "normal" }}>{v.after}</span>
            </div>
          ))}
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn btn-primary grow" disabled={busy} onClick={() => decide(true)}>Allow {name}</button>
          <button className="btn" disabled={busy} onClick={() => decide(false)}>Don't allow</button>
        </div>
      </div>
    </div>
  );
}

function EffectCard({ approval, onDone }: { approval: Approval; onDone: () => void }) {
  const { toast } = useShell();
  const [changing, setChanging] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const changed = approval.values.filter((v) => v.before != null);
  const total = changed.reduce((s, v) => s + (Number(v.after) - Number(v.before)), 0);
  const numeric = changed.length > 0 && changed.every((v) => Number.isFinite(Number(v.after)) && Number.isFinite(Number(v.before)));
  const verb = approval.effects.includes("sends_externally") ? "send" : approval.effects.includes("submits_to_system_of_record") ? "submit"
    : approval.effects.includes("moves_money") ? "pay" : "save";

  async function decide(approved: boolean) {
    setBusy(true);
    try {
      await api(`/api/approvals/${approval.id}`, { body: { approved, note } });
      toast(approved ? "Approved. Ondo is carrying on." : "Sent back. Nothing was changed.");
      onDone();
    } catch (e) {
      toast((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="approval qm-rise" role="region" aria-label="Approval required">
      <div className="approval-head">
        <Icon name="lock" size={17} />
        <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>Approval required</span>
      </div>
      <div style={{ padding: 20, display: "flex", flexDirection: "column", gap: 12 }}>
        <p className="ui">{approval.summary}</p>
        <div className="kv">
          {numeric && changed.length > 1 && (
            <div className="kv-row"><span className="grow">Total change</span><span className="v">{total >= 0 ? "+ " : "− "}{Math.abs(total).toLocaleString("en-GB")}</span></div>
          )}
          {changed.length > 0 && <div className="kv-row"><span className="grow">Values changing</span><span className="v">{changed.length}</span></div>}
          {(changed.length ? changed : approval.values).slice(0, 6).map((v) => (
            <div key={v.label} className="kv-row">
              <span className="grow" style={{ fontSize: 14 }}>{v.label}</span>
              <span className="v" style={v.flagged ? { color: "var(--danger)" } : undefined}>{v.before != null ? `${fmt(v.before)} → ${fmt(v.after)}` : fmt(v.after)}</span>
            </div>
          ))}
          {(changed.length || approval.values.length) > 6 && <div className="kv-row caption">and {(changed.length || approval.values.length) - 6} more</div>}
        </div>
        {approval.diff && !changed.length && <pre className="diff">{approval.diff}</pre>}
        {changing && (
          <div className="field">
            <label htmlFor="note" className="label">What should change?</label>
            <textarea id="note" className="input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Ondo reads this and tries again. Nothing is saved meanwhile." />
          </div>
        )}
        <div className="row" style={{ gap: 8 }}>
          {!changing ? (
            <>
              <button className="btn btn-primary grow" disabled={busy} onClick={() => decide(true)}>Approve and {verb}</button>
              <button className="btn" disabled={busy} onClick={() => setChanging(true)}>Change</button>
            </>
          ) : (
            <>
              <button className="btn btn-primary grow" disabled={busy} onClick={() => decide(false)}>Send back without {GERUND[verb]}</button>
              <button className="btn" onClick={() => setChanging(false)}>Cancel</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** Every event, with the component that produced it. ">" marks what entered the model's context. */
function Trajectory({ events }: { events: RunEvent[] }) {
  const CONTEXT = new Set(["system_prompt", "user_message", "context_injection", "model_response", "tool_result"]);
  const summary = (e: RunEvent) => {
    const d = e.data;
    switch (e.type) {
      case "gate": return `${d.description} — ${d.required ? `gated: ${d.effects.join(", ")}` : "no gate"}`;
      case "screening": return `${base(String(d.origin).replace(/^(file|web):/, ""))} p=${d.probability} ${d.flagged ? "FLAGGED" : "clear"}`;
      case "decision": return `${d.purpose}: ${(d.answers ?? []).map((a: any) => `${a.question_id}=${a.probability}`).join(" ")}`;
      case "tool_call": return `${d.name}(${JSON.stringify(d.arguments).slice(0, 90)})`;
      case "tool_result": return `${d.name}: ${String(d.content).slice(0, 90)}`;
      case "model_response": return d.text || (d.tool_calls ?? []).map((c: any) => c.name).join(", ");
      case "file_access": return `${d.op} ${base(d.path)}`;
      default: return String(d.text ?? d.title ?? d.reason ?? d.request ?? d.answer ?? "").slice(0, 110);
    }
  };
  return (
    <div style={{ padding: 12, overflowX: "auto" }}>
      <table className="table" style={{ border: "none", tableLayout: "fixed" }}>
        <thead><tr><th style={{ width: 28, padding: "10px 8px" }} aria-label="In context" /><th style={{ width: 52 }}>#</th><th style={{ width: 150 }}>Event</th><th style={{ width: 190 }}>Source</th><th>Detail</th></tr></thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.seq}>
              <td title={CONTEXT.has(e.type) ? "Entered the model's context" : ""} style={{ color: "var(--accent)", fontWeight: 600 }}>{CONTEXT.has(e.type) ? "›" : ""}</td>
              <td className="tabular caption">{e.seq}</td>
              <td style={{ fontSize: 13 }}>{e.type}</td>
              <td style={{ fontSize: 13, fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>{e.source}</td>
              <td style={{ fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={summary(e)}>{summary(e)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="caption" style={{ padding: "10px 4px" }}>› entered the model's context. Every event is hash-chained; this view, the audit export and replay read the same stream.</p>
    </div>
  );
}
