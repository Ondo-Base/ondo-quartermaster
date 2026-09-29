import type { FastifyInstance } from "fastify";
import type { Ctx } from "../app.js";
import { all, now, one, parse, run } from "../db.js";
import { audit } from "../lib/audit.js";
import { guard } from "../lib/auth.js";
import { id } from "../lib/crypto.js";

interface RunRow {
  id: string; agent_id: string; user_id: string; request: string; title: string; status: string;
  answer: string; reason: string; model: string; created_at: number; updated_at: number; workflow_id: string | null;
}
interface WorkflowRow { id: string; user_id: string; name: string; request: string; created_from: string | null; created_at: number; updated_at: number }
interface ApprovalRow {
  id: string; run_id: string; title: string; summary: string; effects_json: string; values_json: string;
  diff: string | null; tool: string; status: string; resolved_by: string | null; resolved_at: number | null; note: string; created_at: number;
}

const WEEK = 7 * 24 * 3600_000;

export function approvalView(a: ApprovalRow) {
  return { ...a, effects: parse<string[]>(a.effects_json, []), values: parse<unknown[]>(a.values_json, []), effects_json: undefined, values_json: undefined };
}

export function runRoutes(app: FastifyInstance, { db, hub }: Ctx): void {
  const verified = guard(db);

  // A step is one model turn that acted (the timeline's unit), not one tool call.
  function stepCounts(runId: string) {
    const turns = all<{ data_json: string }>(db, "SELECT data_json FROM run_events WHERE run_id = ? AND type = 'model_response' ORDER BY seq", runId)
      .filter((e) => parse<{ tool_calls?: unknown[] }>(e.data_json, {}).tool_calls?.length);
    const steps = all<{ data_json: string }>(db, "SELECT data_json FROM run_events WHERE run_id = ? AND type = 'step' ORDER BY seq", runId)
      .map((s) => parse<{ status: string; title: string }>(s.data_json, { status: "", title: "" }));
    const last = steps.at(-1);
    const running = steps.filter((s) => s.status === "running").length > steps.filter((s) => s.status !== "running").length;
    return { total: turns.length, done: running ? Math.max(0, turns.length - 1) : turns.length, current: last?.title ?? "" };
  }

  function view(r: RunRow) {
    const pending = one<{ n: number }>(db, "SELECT COUNT(*) AS n FROM approvals WHERE run_id = ? AND status = 'pending'", r.id)!.n;
    const who = one<{ name: string; email: string }>(db, "SELECT name, email FROM users WHERE id = ?", r.user_id);
    return { ...r, steps: stepCounts(r.id), pending_approvals: pending, user: who, agent_connected: hub.isConnected(r.agent_id) };
  }

  function canSee(a: { user: { id: string; role: string; org_id: string } }, r: RunRow): boolean {
    if (r.user_id === a.user.id) return true;
    const owner = one<{ org_id: string }>(db, "SELECT org_id FROM users WHERE id = ?", r.user_id);
    return a.user.role === "admin" && owner?.org_id === a.user.org_id;
  }

  app.get<{ Querystring: { scope?: string } }>("/api/runs", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const scope = req.query.scope ?? "mine";
    const rows = scope === "mine" || a.user.role !== "admin"
      ? all<RunRow>(db, "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT 100", a.user.id)
      : all<RunRow>(db, "SELECT r.* FROM runs r JOIN users u ON u.id = r.user_id WHERE u.org_id = ? ORDER BY r.created_at DESC LIMIT 200", a.user.org_id);
    return rows.map(view);
  });

  app.post<{ Body: { request?: string; agent_id?: string; context?: { window?: string }; workflow_id?: string } }>("/api/runs", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    // A saved workflow runs its saved request, unless the person edited it first.
    const wf = req.body?.workflow_id
      ? one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ? AND user_id = ?", req.body.workflow_id, a.user.id)
      : undefined;
    if (req.body?.workflow_id && !wf) return reply.code(404).send({ error: "That workflow no longer exists." });
    const request = String(req.body?.request ?? wf?.request ?? "").trim();
    if (!request) return reply.code(400).send({ error: "Say what you need." });
    const agent = one<{ id: string }>(db,
      "SELECT id FROM agents WHERE user_id = ? AND revoked = 0 AND (? IS NULL OR id = ?) ORDER BY last_seen DESC LIMIT 1",
      a.user.id, req.body?.agent_id ?? null, req.body?.agent_id ?? null);
    if (!agent) return reply.code(409).send({ error: "Pair the desktop agent first." });
    // "Ask about this screen": only the window the agent says is in front now.
    // The agent checks the screen grant again before it reads anything.
    let context: { window: string } | undefined;
    if (req.body?.context?.window) {
      const screen = hub.screenFor(agent.id);
      if (!screen?.watching || screen.window !== req.body.context.window) {
        return reply.code(409).send({ error: "That window is no longer in front. Bring it back, or ask without it." });
      }
      context = { window: screen.window };
    }
    const runId = hub.startRun(agent.id, a.user.id, a.user.email, request, context, wf ? { id: wf.id, name: wf.name } : undefined);
    if (!runId) return reply.code(409).send({ error: "The desktop agent is not connected. Open it on your computer and try again." });
    audit(db, a.user.org_id, `user:${a.user.email}`, "run.requested", runId,
      { request, agent_id: agent.id, ...(context ? { context } : {}), ...(wf ? { workflow_id: wf.id, workflow: wf.name } : {}) });
    hub.publish(a.user.org_id, a.user.id, "run", { run_id: runId, status: "queued" });
    return { run_id: runId };
  });

  // -- saved workflows ----------------------------------------------------------------------

  function workflowView(w: WorkflowRow) {
    const last = one<{ created_at: number; status: string; id: string }>(db,
      "SELECT id, created_at, status FROM runs WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 1", w.id);
    const count = one<{ n: number }>(db, "SELECT COUNT(*) AS n FROM runs WHERE workflow_id = ?", w.id)!.n;
    return { ...w, last_run: last ?? null, runs: count };
  }

  app.get("/api/workflows", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    return all<WorkflowRow>(db, "SELECT * FROM workflows WHERE user_id = ? ORDER BY name COLLATE NOCASE", a.user.id).map(workflowView);
  });

  app.get<{ Params: { id: string } }>("/api/workflows/:id", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const w = one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ? AND user_id = ?", req.params.id, a.user.id);
    if (!w) return reply.code(404).send({ error: "no such workflow" });
    const runs = all<RunRow>(db, "SELECT * FROM runs WHERE workflow_id = ? ORDER BY created_at DESC LIMIT 20", w.id).map(view);
    return { workflow: workflowView(w), runs };
  });

  const clean = (v: unknown, max: number) => String(v ?? "").replace(/\s+/g, " ").trim().slice(0, max);

  app.post<{ Body: { name?: string; request?: string; from_run?: string } }>("/api/workflows", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const from = req.body?.from_run ? one<RunRow>(db, "SELECT * FROM runs WHERE id = ? AND user_id = ?", req.body.from_run, a.user.id) : undefined;
    if (req.body?.from_run && !from) return reply.code(404).send({ error: "no such run" });
    const request = String(req.body?.request ?? from?.request ?? "").trim().slice(0, 4000);
    const name = clean(req.body?.name ?? from?.title, 80);
    if (!request) return reply.code(400).send({ error: "A workflow needs the request it runs." });
    if (!name) return reply.code(400).send({ error: "Give the workflow a name." });
    if (one(db, "SELECT id FROM workflows WHERE user_id = ? AND name = ? COLLATE NOCASE", a.user.id, name)) {
      return reply.code(409).send({ error: `You already have a workflow called “${name}”.` });
    }
    const wid = id("wf");
    const t = now();
    run(db, "INSERT INTO workflows (id, user_id, name, request, created_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
      wid, a.user.id, name, request, from?.id ?? null, t, t);
    audit(db, a.user.org_id, `user:${a.user.email}`, "workflow.saved", wid, { name, request, from_run: from?.id ?? null });
    hub.publish(a.user.org_id, a.user.id, "workflow", { workflow_id: wid });
    return workflowView(one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ?", wid)!);
  });

  app.put<{ Params: { id: string }; Body: { name?: string; request?: string } }>("/api/workflows/:id", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const w = one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ? AND user_id = ?", req.params.id, a.user.id);
    if (!w) return reply.code(404).send({ error: "no such workflow" });
    const name = req.body?.name !== undefined ? clean(req.body.name, 80) : w.name;
    const request = req.body?.request !== undefined ? String(req.body.request).trim().slice(0, 4000) : w.request;
    if (!name || !request) return reply.code(400).send({ error: "A workflow needs a name and a request." });
    if (name.toLowerCase() !== w.name.toLowerCase()
      && one(db, "SELECT id FROM workflows WHERE user_id = ? AND name = ? COLLATE NOCASE", a.user.id, name)) {
      return reply.code(409).send({ error: `You already have a workflow called “${name}”.` });
    }
    run(db, "UPDATE workflows SET name = ?, request = ?, updated_at = ? WHERE id = ?", name, request, now(), w.id);
    audit(db, a.user.org_id, `user:${a.user.email}`, "workflow.changed", w.id, { name, request });
    hub.publish(a.user.org_id, a.user.id, "workflow", { workflow_id: w.id });
    return workflowView(one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ?", w.id)!);
  });

  app.delete<{ Params: { id: string } }>("/api/workflows/:id", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const w = one<WorkflowRow>(db, "SELECT * FROM workflows WHERE id = ? AND user_id = ?", req.params.id, a.user.id);
    if (!w) return reply.code(404).send({ error: "no such workflow" });
    run(db, "DELETE FROM workflows WHERE id = ?", w.id);
    audit(db, a.user.org_id, `user:${a.user.email}`, "workflow.deleted", w.id, { name: w.name });
    hub.publish(a.user.org_id, a.user.id, "workflow", { workflow_id: w.id, deleted: true });
    return { deleted: w.id };
  });

  app.get<{ Params: { id: string }; Querystring: { after?: string } }>("/api/runs/:id", { preHandler: verified }, async (req, reply) => {
    const r = one<RunRow>(db, "SELECT * FROM runs WHERE id = ?", req.params.id);
    if (!r || !canSee(req.authed!, r)) return reply.code(404).send({ error: "no such run" });
    const after = Number(req.query.after ?? -1);
    const events = all<{ seq: number; type: string; source: string; data_json: string; ts: number; hash: string }>(db,
      "SELECT seq, type, source, data_json, ts, hash FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq", r.id, after)
      .map((e) => ({ seq: e.seq, type: e.type, source: e.source, ts: e.ts, hash: e.hash, data: parse(e.data_json, {}) }));
    const approvals = all<ApprovalRow>(db, "SELECT * FROM approvals WHERE run_id = ? ORDER BY created_at", r.id).map(approvalView);
    return { run: view(r), events, approvals, grants: hub.grantsFor(r.agent_id), capabilities: hub.capabilitiesFor(r.agent_id) };
  });

  app.post<{ Params: { id: string }; Body: { reason?: string } }>("/api/runs/:id/stop", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const r = one<RunRow>(db, "SELECT * FROM runs WHERE id = ?", req.params.id);
    if (!r || !canSee(a, r)) return reply.code(404).send({ error: "no such run" });
    const by = r.user_id === a.user.id ? a.user.email : `admin:${a.user.email}`;
    const delivered = hub.send(r.agent_id, { type: "stop_run", run_id: r.id, by, reason: req.body?.reason ?? "Stopped from the web" });
    audit(db, a.user.org_id, `user:${a.user.email}`, "run.stop_requested", r.id, { delivered });
    return { delivered };
  });

  for (const action of ["pause", "resume"] as const) {
    app.post<{ Params: { id: string } }>(`/api/runs/:id/${action}`, { preHandler: verified }, async (req, reply) => {
      const a = req.authed!;
      const r = one<RunRow>(db, "SELECT * FROM runs WHERE id = ?", req.params.id);
      if (!r || r.user_id !== a.user.id) return reply.code(404).send({ error: "no such run" });
      const delivered = hub.send(r.agent_id, { type: `${action}_run`, run_id: r.id, by: a.user.email });
      if (delivered) run(db, "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?", action === "pause" ? "paused" : "running", now(), r.id);
      audit(db, a.user.org_id, `user:${a.user.email}`, `run.${action}d`, r.id, { delivered });
      hub.publish(a.user.org_id, a.user.id, "run", { run_id: r.id, status: action === "pause" ? "paused" : "running" });
      return { delivered };
    });
  }

  app.get<{ Querystring: { status?: string } }>("/api/approvals", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const rows = all<ApprovalRow & { run_title: string }>(db,
      `SELECT ap.*, r.title AS run_title FROM approvals ap JOIN runs r ON r.id = ap.run_id
       WHERE r.user_id = ? AND (? IS NULL OR ap.status = ?) ORDER BY ap.created_at DESC LIMIT 100`,
      a.user.id, req.query.status ?? null, req.query.status ?? null);
    return rows.map((r) => ({ ...approvalView(r), run_title: r.run_title }));
  });

  app.post<{ Params: { id: string }; Body: { approved?: boolean; note?: string } }>("/api/approvals/:id", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const ap = one<ApprovalRow>(db, "SELECT * FROM approvals WHERE id = ?", req.params.id);
    const r = ap ? one<RunRow>(db, "SELECT * FROM runs WHERE id = ?", ap.run_id) : undefined;
    if (!ap || !r || r.user_id !== a.user.id) return reply.code(404).send({ error: "no such approval" });
    if (ap.status !== "pending") return reply.code(409).send({ error: `This was already ${ap.status}.` });
    const approved = !!req.body?.approved;
    const note = String(req.body?.note ?? "").slice(0, 500);
    if (!hub.send(r.agent_id, { type: "approval", approval_id: ap.id, approved, by: a.user.email, note })) {
      return reply.code(409).send({ error: "The desktop agent is not connected, so nothing was sent. The run is still waiting." });
    }
    run(db, "UPDATE approvals SET status = ?, resolved_by = ?, resolved_at = ?, note = ? WHERE id = ?",
      approved ? "approved" : "refused", a.user.email, now(), note, ap.id);
    audit(db, a.user.org_id, `user:${a.user.email}`, approved ? "approval.approved" : "approval.refused", r.id,
      { approval_id: ap.id, title: ap.title, effects: parse(ap.effects_json, []), note });
    return { status: approved ? "approved" : "refused" };
  });

  app.get("/api/files", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const agent = one<{ id: string }>(db, "SELECT id FROM agents WHERE user_id = ? AND revoked = 0 ORDER BY last_seen DESC LIMIT 1", a.user.id);
    const grants = agent ? hub.grantsFor(agent.id) : null;
    const events = all<{ data_json: string; ts: number; run_id: string }>(db,
      `SELECT e.data_json, e.ts, e.run_id FROM run_events e JOIN runs r ON r.id = e.run_id
       WHERE r.user_id = ? AND e.type = 'file_access' ORDER BY e.ts`, a.user.id);
    const files = new Map<string, { path: string; name: string; kind: string; status: string; last_touched: number; last_read: number }>();
    for (const e of events) {
      const d = parse<{ path: string; op: string; kind?: string }>(e.data_json, { path: "", op: "" });
      if (!d.path || d.op === "listed") continue;
      const name = d.path.split("/").pop() ?? d.path;
      const f = files.get(d.path) ?? { path: d.path, name, kind: d.kind ?? kindOf(name), status: "Read", last_touched: 0, last_read: 0 };
      const ts = e.ts * 1000;
      f.last_touched = Math.max(f.last_touched, ts);
      if (d.op === "read") f.last_read = Math.max(f.last_read, ts);
      if (d.op === "edited") f.status = "Edited";
      else if (d.op === "excluded") f.status = f.status === "Edited" ? "Edited" : "Excluded";
      if (d.kind) f.kind = d.kind;
      files.set(d.path, f);
    }
    const folders = (grants?.files.granted ? grants.files.scope : []).map((folder) => {
      const inside = [...files.values()].filter((f) => f.path === folder || f.path.startsWith(folder.replace(/\/$/, "") + "/"));
      return {
        path: folder, name: folder.split("/").filter(Boolean).pop() ?? folder,
        files: inside.sort((x, y) => y.last_touched - x.last_touched),
        read_this_week: inside.filter((f) => f.last_read > now() - WEEK).length,
      };
    });
    const org = one<{ policy_json: string }>(db, "SELECT policy_json FROM orgs WHERE id = ?", a.user.org_id)!;
    return { folders, grants, policy: parse(org.policy_json, {}), read_this_week: [...files.values()].filter((f) => f.last_read > now() - WEEK).length };
  });

  app.get("/api/summary", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const since = now() - WEEK;
    const n = (sql: string) => one<{ n: number }>(db, sql, a.user.id, since)!.n;
    return {
      finished: n("SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND status = 'finished' AND updated_at > ?"),
      stopped_for_decision: n("SELECT COUNT(DISTINCT ap.run_id) AS n FROM approvals ap JOIN runs r ON r.id = ap.run_id WHERE r.user_id = ? AND ap.created_at > ?"),
      corrected: n("SELECT COUNT(*) AS n FROM approvals ap JOIN runs r ON r.id = ap.run_id WHERE r.user_id = ? AND ap.status = 'refused' AND ap.created_at > ?"),
    };
  });

  app.get("/api/stream", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    reply.hijack();
    reply.raw.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive", "x-accel-buffering": "no" });
    reply.raw.write(`event: hello\ndata: {}\n\n`);
    const off = hub.subscribe({ userId: a.user.id, orgId: a.user.org_id, admin: a.user.role === "admin", reply });
    const ping = setInterval(() => { try { reply.raw.write(`: ping\n\n`); } catch { /* closed */ } }, 25_000);
    req.raw.on("close", () => { clearInterval(ping); off(); });
  });
}

function kindOf(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return ({ xlsx: "Workbook", docx: "Document", pptx: "Deck", pdf: "PDF", csv: "Data" } as Record<string, string>)[ext] ?? "File";
}
