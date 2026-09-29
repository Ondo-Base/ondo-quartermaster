// A saved workflow: its request, what running it has done, and running it again.
// Running starts an ordinary task, with the same grants, gates and approvals.

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, hhmm, runStatusLine, useData, whenLabel, workflowMeta, type Run, type Workflow } from "../api";
import { BackHome, Crumbs, RailFoot, RailHead, SavedWorkflows, TaskList, useShell } from "../components/Shell";
import { Icon } from "../icons";

export function WorkflowPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { toast } = useShell();
  const { data, error } = useData<{ workflow: Workflow; runs: Run[] }>(`/api/workflows/${id}`,
    (e, d) => (e === "workflow" && d?.workflow_id === id) || e === "run");
  const [name, setName] = useState("");
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (data) { setName(data.workflow.name); setRequest(data.workflow.request); }
    // Only when the saved workflow itself changes, not on every live refresh.
  }, [data?.workflow.id, data?.workflow.updated_at]);

  if (error) {
    return (
      <div className="shell"><main className="main"><div className="canvas" style={{ padding: 32 }}>
        <h1 className="section-heading">This workflow no longer exists</h1>
        <p className="ui secondary"><Link to="/app">Back to home</Link></p>
      </div></main></div>
    );
  }
  if (!data) return null;
  const w = data.workflow;
  const edited = name.trim() !== w.name || request.trim() !== w.request;

  async function runIt() {
    setBusy(true);
    try {
      const r = await api<{ run_id: string }>("/api/runs", { body: { workflow_id: w.id, request } });
      nav(`/app/runs/${r.run_id}`);
    } catch (e) {
      toast((e as ApiError).message);
      setBusy(false);
    }
  }
  async function save() {
    try {
      await api(`/api/workflows/${w.id}`, { method: "PUT", body: { name, request } });
      toast("Saved. The next run uses this request.");
    } catch (e) { toast((e as ApiError).message); }
  }
  async function remove() {
    if (!confirm(`Delete “${w.name}”? Its past tasks stay in your history.`)) return;
    try {
      await api(`/api/workflows/${w.id}`, { method: "DELETE" });
      toast(`Deleted “${w.name}”.`);
      nav("/app");
    } catch (e) { toast((e as ApiError).message); }
  }

  return (
    <div className="shell">
      <nav className="rail" aria-label="Workflows">
        <RailHead />
        <div className="rail-body" style={{ paddingTop: 16 }}>
          <BackHome />
          <SavedWorkflows activeId={w.id} />
          <TaskList limit={4} />
        </div>
        <RailFoot />
      </nav>
      <main className="main">
        <header className="topbar">
          <Crumbs items={[{ label: "Home", to: "/app" }, { label: "Saved workflows" }, { label: w.name }]} />
          <span className="grow" />
          <button className="btn" onClick={() => void remove()}>Delete</button>
          <button className="btn btn-primary" disabled={busy || !request.trim()} onClick={() => void runIt()}>{busy ? "Starting…" : edited ? "Run with these changes" : "Run now"}</button>
        </header>
        <div className="canvas" style={{ padding: 32, gap: 24, maxWidth: 960 }}>
          <div className="col" style={{ gap: 8 }}>
            <span className="eyebrow">Saved workflow · {workflowMeta(w)}</span>
            <h1 className="chapter-title">{w.name}</h1>
            <p className="lead">Runs like any task you type: it asks before anything is sent, submitted or saved.</p>
          </div>
          <section className="card card-pad" style={{ gap: 16 }}>
            <div className="field">
              <label className="label" htmlFor="wf-name">Name</label>
              <input id="wf-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="wf-request">What Ondo is asked to do</label>
              <textarea id="wf-request" className="input" rows={4} value={request} onChange={(e) => setRequest(e.target.value)} />
              <span className="caption">Change the month or the folder here before a run. “Run with these changes” runs your edit once; “Save” keeps it.</span>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn" disabled={!edited} onClick={() => void save()}>Save</button>
              {edited && <button className="btn" onClick={() => { setName(w.name); setRequest(w.request); }}>Discard changes</button>}
            </div>
          </section>
          <section className="card">
            <div className="card-head"><span className="eyebrow-sm">Runs</span><span className="grow" /><span className="caption tabular">{w.runs}</span></div>
            {data.runs.length === 0 && <p className="ui secondary" style={{ padding: "16px 20px" }}>Not run yet{w.created_from ? "" : "."}{w.created_from && <> since it was saved. <Link to={`/app/runs/${w.created_from}`}>See the task it was saved from</Link>.</>}</p>}
            {data.runs.map((r, i) => (
              <div key={r.id}>
                {i > 0 && <div className="divider" />}
                <Link to={`/app/runs/${r.id}`} className="row" style={{ padding: "14px 20px", gap: 12, color: "inherit", textDecoration: "none" }}>
                  <Icon name={r.status === "finished" ? "check" : r.status === "stopped" || r.status === "error" ? "close" : "workflow"} size={16} color="var(--ink-muted)" />
                  <span className="grow">{whenLabel(r.created_at)}{whenLabel(r.created_at).startsWith("Today") ? "" : `, ${hhmm(r.created_at)}`}</span>
                  <span className="caption">{runStatusLine(r)}</span>
                </Link>
              </div>
            ))}
          </section>
        </div>
      </main>
    </div>
  );
}
