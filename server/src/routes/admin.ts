// The admin console's API: people, devices, agents and their grants, running
// work, policy, the audit log and its export, SIEM sinks and SCIM tokens.
// Revoking a grant here reaches the agent at once and stops any run using it.

import type { FastifyInstance } from "fastify";
import type { Ctx } from "../app.js";
import { all, now, one, parse, run } from "../db.js";
import { audit, forwardOnce, listAudit, toCef, toJsonl, verifyAudit } from "../lib/audit.js";
import { guard } from "../lib/auth.js";
import { id, sha256, token } from "../lib/crypto.js";
import { normalisePolicy } from "../lib/policy.js";
import { deactivateUser } from "../lib/users.js";

export function adminRoutes(app: FastifyInstance, { db, hub }: Ctx): void {
  const admin = guard(db, { admin: true });

  app.get("/api/admin/overview", { preHandler: admin }, async (req) => {
    const org = req.authed!.user.org_id;
    const users = all<{ id: string; email: string; name: string; title: string; role: string; active: number; external_id: string | null }>(db,
      "SELECT id, email, name, title, role, active, external_id FROM users WHERE org_id = ? ORDER BY name", org);
    const orgPolicy = normalisePolicy(parse(one<{ policy_json: string }>(db, "SELECT policy_json FROM orgs WHERE id = ?", org)!.policy_json, {}));
    const agents = all<{ id: string; user_id: string; hostname: string; os: string; last_seen: number; created_at: number }>(db,
      `SELECT a.id, a.user_id, a.hostname, a.os, a.last_seen, a.created_at FROM agents a JOIN users u ON u.id = a.user_id
       WHERE u.org_id = ? AND a.revoked = 0 ORDER BY a.last_seen DESC`, org)
      .map((a) => ({ ...a, connected: hub.isConnected(a.id), grants: hub.grantsFor(a.id), connectors: hub.connectorsFor(a.id, orgPolicy),
        user: users.find((u) => u.id === a.user_id) }));
    const devices = all(db, `SELECT d.id, d.user_id, d.name, d.os, d.managed, d.trusted_until, d.last_seen FROM devices d
       JOIN users u ON u.id = d.user_id WHERE u.org_id = ? AND d.trusted_until > ? ORDER BY d.last_seen DESC`, org, now());
    const active = all(db, `SELECT r.id, r.title, r.status, r.agent_id, r.user_id, r.created_at, u.name AS user_name FROM runs r
       JOIN users u ON u.id = r.user_id WHERE u.org_id = ? AND r.status IN ('queued','running','waiting') ORDER BY r.created_at DESC`, org);
    const o = one<{ name: string; policy_json: string }>(db, "SELECT name, policy_json FROM orgs WHERE id = ?", org)!;
    const sinks = all(db, "SELECT id, url, format, enabled, cursor, last_error, created_at FROM siem_sinks WHERE org_id = ?", org);
    return { org: { id: org, name: o.name }, policy: normalisePolicy(parse(o.policy_json, {})), users, agents, devices, active_runs: active, siem_sinks: sinks };
  });

  app.put<{ Body: Record<string, unknown> }>("/api/admin/policy", { preHandler: admin }, async (req) => {
    const a = req.authed!;
    const policy = normalisePolicy(req.body as never);
    run(db, "UPDATE orgs SET policy_json = ? WHERE id = ?", JSON.stringify(policy), a.user.org_id);
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.policy_changed", a.user.org_id, policy);
    // A connector the organisation no longer allows loses every consent given to it:
    // allowing it again means each person is asked again.
    const dropped = all<{ agent_id: string; connector: string }>(db,
      `SELECT c.agent_id, c.connector FROM consents c JOIN agents g ON g.id = c.agent_id JOIN users u ON u.id = g.user_id
        WHERE u.org_id = ?`, a.user.org_id).filter((c) => !policy.allowed_connectors.includes(c.connector));
    for (const c of dropped) {
      run(db, "DELETE FROM consents WHERE agent_id = ? AND connector = ?", c.agent_id, c.connector);
      audit(db, a.user.org_id, `user:${a.user.email}`, "connector.revoked", c.agent_id, { connector: c.connector, reason: "not allowed by policy" });
    }
    hub.pushPolicy(a.user.org_id, policy);
    for (const agentId of new Set(dropped.map((c) => c.agent_id))) hub.pushGrants(agentId, `admin:${a.user.email}`, "no longer allowed by policy");
    return policy;
  });

  app.put<{ Params: { id: string }; Body: { role?: string; active?: boolean } }>("/api/admin/users/:id", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const u = one<{ id: string; org_id: string; email: string }>(db, "SELECT id, org_id, email FROM users WHERE id = ?", req.params.id);
    if (!u || u.org_id !== a.user.org_id) return reply.code(404).send({ error: "no such user" });
    if (req.body.role && ["member", "admin"].includes(req.body.role)) run(db, "UPDATE users SET role = ? WHERE id = ?", req.body.role, u.id);
    if (typeof req.body.active === "boolean") {
      run(db, "UPDATE users SET active = ? WHERE id = ?", req.body.active ? 1 : 0, u.id);
      if (!req.body.active) deactivateUser(db, hub, u.id);
    }
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.user_changed", u.email, req.body);
    return { ok: true };
  });

  app.delete<{ Params: { id: string } }>("/api/admin/devices/:id/trust", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const d = one<{ id: string; user_id: string }>(db, "SELECT d.id, d.user_id FROM devices d JOIN users u ON u.id = d.user_id WHERE d.id = ? AND u.org_id = ?", req.params.id, a.user.org_id);
    if (!d) return reply.code(404).send({ error: "no such device" });
    run(db, "UPDATE devices SET trusted_until = 0 WHERE id = ?", d.id);
    run(db, "DELETE FROM sessions WHERE device_id = ?", d.id);
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.device_trust_ended", d.id);
    return { ok: true };
  });

  app.post<{ Params: { id: string } }>("/api/admin/agents/:id/revoke", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const ag = one<{ id: string }>(db, "SELECT a.id FROM agents a JOIN users u ON u.id = a.user_id WHERE a.id = ? AND u.org_id = ?", req.params.id, a.user.org_id);
    if (!ag) return reply.code(404).send({ error: "no such agent" });
    run(db, "UPDATE agents SET revoked = 1 WHERE id = ?", ag.id);
    hub.agents.get(ag.id)?.socket.close(4403, "agent revoked");
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.agent_revoked", ag.id);
    return { ok: true };
  });

  // -- audit ------------------------------------------------------------------------------

  app.get<{ Querystring: { after?: string; limit?: string; action?: string; actor?: string } }>("/api/admin/audit", { preHandler: admin }, async (req) => {
    const q = req.query;
    return listAudit(db, req.authed!.user.org_id, { after: Number(q.after ?? 0), limit: Number(q.limit ?? 200), action: q.action, actor: q.actor })
      .map((r) => ({ ...r, detail: parse(r.detail_json, {}), detail_json: undefined }));
  });

  app.get<{ Querystring: { format?: string; after?: string } }>("/api/admin/audit/export", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const rows = listAudit(db, a.user.org_id, { after: Number(req.query.after ?? 0), limit: 5000 });
    const cef = req.query.format === "cef";
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.audit_exported", "", { format: cef ? "cef" : "jsonl", rows: rows.length });
    reply.header("content-disposition", `attachment; filename="ondo-audit.${cef ? "cef" : "jsonl"}"`);
    reply.type(cef ? "text/plain" : "application/x-ndjson");
    return cef ? toCef(rows) : toJsonl(rows);
  });

  app.get("/api/admin/audit/verify", { preHandler: admin }, async (req) => verifyAudit(db, req.authed!.user.org_id));

  // -- SIEM and SCIM ----------------------------------------------------------------------

  app.post<{ Body: { url?: string; format?: string; secret?: string } }>("/api/admin/siem", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const url = String(req.body.url ?? "");
    if (!/^https?:\/\//.test(url)) return reply.code(400).send({ error: "A sink needs an http(s) URL." });
    const sid = id("siem");
    run(db, "INSERT INTO siem_sinks (id, org_id, url, format, secret, created_at) VALUES (?,?,?,?,?,?)",
      sid, a.user.org_id, url, req.body.format === "cef" ? "cef" : "jsonl", String(req.body.secret ?? ""), now());
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.siem_sink_added", sid, { url, format: req.body.format ?? "jsonl" });
    return { id: sid };
  });

  app.delete<{ Params: { id: string } }>("/api/admin/siem/:id", { preHandler: admin }, async (req) => {
    const a = req.authed!;
    run(db, "DELETE FROM siem_sinks WHERE id = ? AND org_id = ?", req.params.id, a.user.org_id);
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.siem_sink_removed", req.params.id);
    return { ok: true };
  });

  app.post("/api/admin/siem/flush", { preHandler: admin }, async () => ({ sent: await forwardOnce(db) }));

  // Enrollment tokens: what IT puts in its device management (Intune, Group Policy)
  // so agents set themselves up. Shown once; each person still confirms their computer.
  app.get("/api/admin/enrollment-tokens", { preHandler: admin }, async (req) => {
    return all(db, "SELECT id, label, created_by, created_at, revoked_at, uses FROM enrollment_tokens WHERE org_id = ? ORDER BY created_at DESC",
      req.authed!.user.org_id);
  });

  app.post<{ Body: { label?: string } }>("/api/admin/enrollment-tokens", { preHandler: admin }, async (req) => {
    const a = req.authed!;
    const t = `ondo_enr_${token(32)}`;
    const tid = id("enr");
    const label = String(req.body?.label ?? "").trim().slice(0, 80) || "Device management";
    run(db, "INSERT INTO enrollment_tokens (id, token_hash, org_id, label, created_by, created_at) VALUES (?,?,?,?,?,?)",
      tid, sha256(t), a.user.org_id, label, a.user.email, now());
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.enrollment_token_created", tid, { label });
    return { id: tid, label, token: t, note: "Shown once. Put it in the EnrollmentToken setting of your device management." };
  });

  app.delete<{ Params: { id: string } }>("/api/admin/enrollment-tokens/:id", { preHandler: admin }, async (req, reply) => {
    const a = req.authed!;
    const gone = run(db, "UPDATE enrollment_tokens SET revoked_at = ? WHERE id = ? AND org_id = ? AND revoked_at IS NULL",
      now(), req.params.id, a.user.org_id).changes;
    if (!gone) return reply.code(404).send({ error: "no such token" });
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.enrollment_token_revoked", req.params.id);
    return { revoked: req.params.id };
  });

  app.post<{ Body: { label?: string } }>("/api/admin/scim-tokens", { preHandler: admin }, async (req) => {
    const a = req.authed!;
    const t = token(32);
    run(db, "INSERT INTO scim_tokens (token_hash, org_id, label, created_at) VALUES (?,?,?,?)", sha256(t), a.user.org_id, String(req.body?.label ?? "SCIM"), now());
    audit(db, a.user.org_id, `user:${a.user.email}`, "admin.scim_token_created", String(req.body?.label ?? "SCIM"));
    return { token: t, note: "Shown once. Paste it into your identity provider's provisioning settings." };
  });
}
