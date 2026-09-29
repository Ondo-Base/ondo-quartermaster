// The hub between desktop agents (one websocket each, dialled out from the
// device) and browsers (server-sent events). The web UI never talks to a desktop
// directly: it asks the control plane, which records the request and relays it.

import type { WebSocket } from "ws";
import type { FastifyReply } from "fastify";
import { type DB, all, now, one, parse, run } from "../db.js";
import { audit } from "./audit.js";
import { id } from "./crypto.js";
import { type GrantKind, type OrgPolicy, normalisePolicy } from "./policy.js";

export interface AgentRow { id: string; user_id: string; hostname: string; os: string; revoked: number; last_seen: number }
export interface GrantRow { agent_id: string; kind: GrantKind; granted: number; scope_json: string; updated_at: number; updated_by: string }

interface Subscriber { userId: string; orgId: string; admin: boolean; reply: FastifyReply }

// Events that also go to the audit log (and so to the SIEM), with the action name.
const AUDITED: Record<string, string> = {
  run_started: "agent.run.started",
  run_finished: "agent.run.finished",
  run_stopped: "agent.run.stopped",
  tool_call: "agent.tool.called",
  file_access: "agent.file",
  diff_proposed: "agent.write.proposed",
  gate: "agent.gate",
  approval_requested: "agent.approval.requested",
  approval_resolved: "agent.approval.resolved",
  permission_denied: "agent.permission.denied",
  grant_changed: "agent.grant.changed",
  window_access: "agent.window",
  connector_access: "agent.connector",
};

/** What the agent says is in front of the user, among the windows they shared.
 * Held in memory only: it is presence, not a record. The window's contents never
 * come here; only its name, and only for a window the screen grant covers. */
export interface ScreenContext { window: string | null; title: string | null; watching: boolean; at: number }

export class Hub {
  agents = new Map<string, { socket: WebSocket; userId: string; orgId: string }>();
  subscribers = new Set<Subscriber>();
  screens = new Map<string, ScreenContext>();

  constructor(private db: DB) {}

  // -- browsers ---------------------------------------------------------------------

  subscribe(s: Subscriber): () => void {
    this.subscribers.add(s);
    return () => this.subscribers.delete(s);
  }

  publish(orgId: string, userId: string | null, event: string, data: unknown): void {
    const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const s of this.subscribers) {
      if (s.orgId !== orgId) continue;
      if (userId && !s.admin && s.userId !== userId) continue;
      try { s.reply.raw.write(payload); } catch { this.subscribers.delete(s); }
    }
  }

  // -- agents -----------------------------------------------------------------------

  isConnected(agentId: string): boolean {
    return this.agents.has(agentId);
  }

  send(agentId: string, msg: unknown): boolean {
    const a = this.agents.get(agentId);
    if (!a) return false;
    a.socket.send(JSON.stringify(msg));
    return true;
  }

  orgOf(userId: string): { org_id: string; policy_json: string } {
    return one<{ org_id: string; policy_json: string }>(this.db,
      "SELECT u.org_id, o.policy_json FROM users u JOIN orgs o ON o.id = u.org_id WHERE u.id = ?", userId)!;
  }

  grantsFor(agentId: string): Record<string, { granted: boolean; scope: string[]; updated_by: string; updated_at: number }> {
    const out: Record<string, { granted: boolean; scope: string[]; updated_by: string; updated_at: number }> = {
      files: { granted: false, scope: [], updated_by: "", updated_at: 0 },
      screen: { granted: false, scope: [], updated_by: "", updated_at: 0 },
      input: { granted: false, scope: [], updated_by: "", updated_at: 0 },
    };
    for (const g of all<GrantRow>(this.db, "SELECT * FROM grants WHERE agent_id = ?", agentId)) {
      out[g.kind] = { granted: !!g.granted, scope: parse<string[]>(g.scope_json, []), updated_by: g.updated_by, updated_at: g.updated_at };
    }
    return out;
  }

  capabilitiesFor(agentId: string): Record<string, unknown> {
    const r = one<{ capabilities_json: string }>(this.db, "SELECT capabilities_json FROM agents WHERE id = ?", agentId);
    return parse(r?.capabilities_json, {});
  }

  /** Connectors the person allowed on this agent: { id: { by, at } }. */
  consentsFor(agentId: string): Record<string, { by: string; at: number }> {
    const out: Record<string, { by: string; at: number }> = {};
    for (const c of all<{ connector: string; consented_by: string; consented_at: number }>(this.db,
      "SELECT connector, consented_by, consented_at FROM consents WHERE agent_id = ?", agentId)) {
      out[c.connector] = { by: c.consented_by, at: c.consented_at };
    }
    return out;
  }

  /** What the web shows for each connector the agent has: can it be used, and who allowed it. */
  connectorsFor(agentId: string, policy: OrgPolicy) {
    const caps = this.capabilitiesFor(agentId) as { connectors?: { id: string; name: string; description: string; tools: unknown[] }[] };
    const consents = this.consentsFor(agentId);
    return (caps.connectors ?? []).map((c) => ({
      ...c,
      allowed_by_policy: policy.allowed_connectors.includes(c.id),
      consent: policy.allowed_connectors.includes(c.id) ? consents[c.id] ?? null : null,
    }));
  }

  screenFor(agentId: string): ScreenContext | null {
    return this.isConnected(agentId) ? this.screens.get(agentId) ?? null : null;
  }

  pushGrants(agentId: string, by: string, reason = ""): void {
    const grants = this.grantsFor(agentId);
    this.send(agentId, { type: "grants", by, reason, grants, connectors: this.consentsFor(agentId) });
  }

  pushPolicy(orgId: string, policy: OrgPolicy): void {
    for (const [agentId, a] of this.agents) {
      if (a.orgId === orgId) this.send(agentId, { type: "policy", policy });
    }
  }

  attach(agent: AgentRow, socket: WebSocket): void {
    const { org_id, policy_json } = this.orgOf(agent.user_id);
    this.agents.get(agent.id)?.socket.close(4000, "replaced by a newer connection");
    this.agents.set(agent.id, { socket, userId: agent.user_id, orgId: org_id });
    run(this.db, "UPDATE agents SET last_seen = ? WHERE id = ?", now(), agent.id);
    audit(this.db, org_id, `agent:${agent.id}`, "agent.connected", agent.id, { hostname: agent.hostname });
    // The control plane is the source of truth for grants and policy once paired.
    this.send(agent.id, { type: "policy", policy: normalisePolicy(parse(policy_json, {})) });
    this.pushGrants(agent.id, "control-plane", "connected");
    this.publish(org_id, agent.user_id, "agent", { agent_id: agent.id, connected: true });

    socket.on("message", (raw) => {
      let msg: any;
      try { msg = JSON.parse(String(raw)); } catch { return; }
      try { this.handle(agent, org_id, msg); } catch (e) { console.error("agent message failed", e); }
    });
    socket.on("close", () => {
      const cur = this.agents.get(agent.id);
      if (cur?.socket === socket) {
        this.agents.delete(agent.id);
        this.screens.delete(agent.id);
        audit(this.db, org_id, `agent:${agent.id}`, "agent.disconnected", agent.id);
        this.publish(org_id, agent.user_id, "agent", { agent_id: agent.id, connected: false });
      }
    });
  }

  private handle(agent: AgentRow, orgId: string, msg: any): void {
    run(this.db, "UPDATE agents SET last_seen = ? WHERE id = ?", now(), agent.id);
    if (msg.type === "hello") {
      const d = msg.device ?? {};
      run(this.db, "UPDATE agents SET hostname = ?, os = ?, capabilities_json = ? WHERE id = ?",
        String(d.hostname ?? ""), String(d.os ?? ""), JSON.stringify(msg.capabilities ?? {}), agent.id);
      this.publish(orgId, agent.user_id, "agent", { agent_id: agent.id, connected: true });
      return;
    }
    if (msg.type === "event") return this.ingest(agent, orgId, msg.event);
    if (msg.type === "screen_context") {
      const prev = this.screens.get(agent.id);
      const next: ScreenContext = {
        window: typeof msg.window === "string" && msg.window ? msg.window.slice(0, 300) : null,
        title: typeof msg.title === "string" && msg.title && msg.window ? msg.title.slice(0, 300) : null,
        watching: msg.watching !== false,
        at: now(),
      };
      this.screens.set(agent.id, next);
      if (prev?.watching !== next.watching) {
        audit(this.db, orgId, `agent:${agent.id}`, next.watching ? "agent.watch.resumed" : "agent.watch.paused", agent.id,
          { reason: String(msg.reason ?? "") });
      }
      this.publish(orgId, agent.user_id, "screen", { agent_id: agent.id, ...next });
      return;
    }
    if (msg.type === "run_status") {
      const r = one<{ user_id: string }>(this.db, "SELECT user_id FROM runs WHERE id = ? AND agent_id = ?", msg.run_id, agent.id);
      if (!r) return;
      run(this.db, "UPDATE runs SET status = ?, answer = COALESCE(?, answer), reason = COALESCE(?, reason), updated_at = ? WHERE id = ?",
        String(msg.status), msg.answer ?? null, msg.reason ?? null, now(), msg.run_id);
      this.publish(orgId, r.user_id, "run", { run_id: msg.run_id, status: msg.status });
    }
  }

  ingest(agent: AgentRow, orgId: string, e: any): void {
    const r = one<{ user_id: string; status: string }>(this.db, "SELECT user_id, status FROM runs WHERE id = ? AND agent_id = ?", e.run_id, agent.id);
    if (!r) return; // not a run this control plane started for this agent
    const inserted = run(this.db,
      "INSERT OR IGNORE INTO run_events (run_id, seq, type, source, data_json, ts, hash, prev_hash) VALUES (?,?,?,?,?,?,?,?)",
      e.run_id, e.seq, e.type, e.source, JSON.stringify(e.data ?? {}), e.ts, e.hash, e.prev_hash).changes;
    if (!inserted) return; // a resend after reconnecting
    const d = e.data ?? {};
    const t = now();
    switch (e.type) {
      case "run_started":
        run(this.db, "UPDATE runs SET status = 'running', model = ?, updated_at = ? WHERE id = ?", `${d.profile}:${d.model}`, t, e.run_id);
        break;
      case "approval_requested":
        run(this.db, `INSERT OR IGNORE INTO approvals (id, run_id, title, summary, effects_json, values_json, diff, tool, kind, connector, created_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
          d.id, e.run_id, d.title ?? "", d.summary ?? "", JSON.stringify(d.effects ?? []), JSON.stringify(d.values ?? []),
          d.diff ?? null, d.tool ?? "", d.kind === "consent" ? "consent" : "effect", d.connector ?? null, t);
        run(this.db, "UPDATE runs SET status = 'waiting', updated_at = ? WHERE id = ?", t, e.run_id);
        this.publish(orgId, r.user_id, "approval", { approval_id: d.id, run_id: e.run_id, status: "pending" });
        break;
      case "approval_resolved":
        run(this.db, "UPDATE approvals SET status = ?, resolved_by = COALESCE(resolved_by, ?), resolved_at = COALESCE(resolved_at, ?) WHERE id = ? AND status = 'pending'",
          d.approved ? "approved" : "refused", d.by ?? "", t, d.approval_id);
        run(this.db, "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'waiting'", t, e.run_id);
        this.publish(orgId, r.user_id, "approval", { approval_id: d.approval_id, run_id: e.run_id, status: d.approved ? "approved" : "refused" });
        this.recordConsent(agent, orgId, r.user_id, d);
        break;
      case "run_finished":
        run(this.db, "UPDATE runs SET status = 'finished', answer = ?, updated_at = ? WHERE id = ?", d.answer ?? "", t, e.run_id);
        break;
      case "run_stopped":
        run(this.db, "UPDATE runs SET status = ?, reason = ?, updated_at = ? WHERE id = ?", d.status === "error" ? "error" : "stopped", d.reason ?? "", t, e.run_id);
        run(this.db, "UPDATE approvals SET status = 'expired' WHERE run_id = ? AND status = 'pending'", e.run_id);
        break;
      default:
        run(this.db, "UPDATE runs SET updated_at = ? WHERE id = ?", t, e.run_id);
    }
    const action = AUDITED[e.type];
    if (action) {
      const detail = e.type === "tool_call" ? { name: d.name, call_id: d.call_id }
        : e.type === "file_access" ? { op: d.op, path: d.path, reason: d.reason }
        : e.type === "gate" ? { tool: d.tool, required: d.required, effects: d.effects, url: d.url, path: d.path, by_effect: d.by_effect }
        : e.type === "approval_requested" ? { approval_id: d.id, title: d.title, effects: d.effects }
        : e.type === "run_started" ? { request: d.request, model: d.model, profile: d.profile }
        : d;
      const actor = e.type === "approval_resolved" ? `user:${d.by}` : `agent:${agent.id}`;
      const name = e.type === "file_access" ? `agent.file.${d.op}` : e.type === "window_access" ? `agent.window.${d.op}`
        : e.type === "connector_access" ? `agent.connector.${d.op}` : action;
      audit(this.db, orgId, actor, name, e.run_id, { seq: e.seq, source: e.source, ...detail });
    }
    this.publish(orgId, r.user_id, "run_event", { run_id: e.run_id, seq: e.seq, type: e.type, source: e.source, data: d, ts: e.ts });
  }

  /** A consent approval answered yes: that connector is allowed on this agent until
   * revoked. Only if policy allows it, and only for a consent the agent asked for. */
  private recordConsent(agent: AgentRow, orgId: string, userId: string, d: any): void {
    if (!d.approved) return;
    const ap = one<{ kind: string; connector: string | null }>(this.db, "SELECT kind, connector FROM approvals WHERE id = ?", d.approval_id);
    if (!ap || ap.kind !== "consent" || !ap.connector) return;
    const policy = normalisePolicy(parse(this.orgOf(agent.user_id).policy_json, {}));
    if (!policy.allowed_connectors.includes(ap.connector)) return;
    run(this.db, "INSERT OR REPLACE INTO consents (agent_id, connector, consented_by, consented_at) VALUES (?,?,?,?)",
      agent.id, ap.connector, String(d.by ?? ""), now());
    audit(this.db, orgId, `user:${d.by}`, "connector.consented", agent.id, { connector: ap.connector, approval_id: d.approval_id });
    // Confirm it to the agent: from now on, leaving it out of a push is a revocation.
    this.pushGrants(agent.id, String(d.by ?? ""), "");
    this.publish(orgId, userId, "grants", { agent_id: agent.id, kind: `connector:${ap.connector}`, granted: true });
  }

  // -- runs ---------------------------------------------------------------------------

  startRun(agentId: string, userId: string, userEmail: string, request: string, context?: { window: string },
    workflow?: { id: string; name: string }): string | null {
    if (!this.isConnected(agentId)) return null;
    const runId = id("run").replace("_", "-");
    const t = now();
    run(this.db, "INSERT INTO runs (id, agent_id, user_id, request, title, status, workflow_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
      runId, agentId, userId, request, workflow?.name ?? titleFor(request), "queued", workflow?.id ?? null, t, t);
    this.send(agentId, { type: "start_run", run_id: runId, request, user: userEmail, ...(context ? { context } : {}) });
    return runId;
  }
}

/** A short name for a run: the first clause of the request, cut before "from…"
 * or "into…" when it runs long. The full request is always shown beside it. */
export function titleFor(request: string): string {
  const first = request.replace(/\s+/g, " ").trim().split(/(?<=[.?!])\s/)[0] ?? request;
  let t = first.replace(/[.?!]$/, "").split(", ")[0];
  if (t.length > 48) {
    const cut = [" from ", " into ", " in the ", " using ", " for the "].map((w) => t.indexOf(w)).filter((i) => i >= 16).sort((a, b) => a - b)[0];
    if (cut !== undefined) t = t.slice(0, cut);
  }
  return t.length > 64 ? t.slice(0, 61).replace(/\s+\S*$/, "") + "…" : t;
}
