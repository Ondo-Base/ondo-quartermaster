// Pairing, grants, and the agent's websocket.
//
// Pairing: the signed-in, device-verified user asks for a one-time code; the
// desktop agent exchanges it for a long-lived token. The token is stored hashed.
// Grants: three, separate, each with its own scope. The control plane refuses
// anything the organisation's policy excludes, then pushes the change to the
// agent, which applies it to every running run immediately.

import type { FastifyInstance } from "fastify";
import type { Ctx } from "../app.js";
import { now, one, parse, run } from "../db.js";
import { audit } from "../lib/audit.js";
import { guard } from "../lib/auth.js";
import { id, sha256, sixDigits, token } from "../lib/crypto.js";
import { tooMany } from "../lib/limit.js";
import type { AgentRow } from "../lib/hub.js";
import { type GrantKind, checkGrant, normalisePolicy } from "../lib/policy.js";

const PAIR_MS = 10 * 60_000;
const KINDS: GrantKind[] = ["files", "screen", "input"];

export function agentRoutes(app: FastifyInstance, { db, hub }: Ctx): void {
  const verified = guard(db);

  app.post("/api/pairing", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const code = `${sixDigits().slice(0, 3)}-${sixDigits().slice(0, 3)}`;
    run(db, "INSERT INTO pairing_codes (code_hash, user_id, device_id, expires_at) VALUES (?,?,?,?)",
      sha256(code), a.user.id, a.session.device_id, now() + PAIR_MS);
    audit(db, a.user.org_id, `user:${a.user.email}`, "agent.pairing_code_issued", a.session.device_id ?? "");
    return { code, expires_at: now() + PAIR_MS };
  });

  app.post<{ Body: { code?: string; device?: { hostname?: string; os?: string } } }>("/api/agent/pair", async (req, reply) => {
    if (tooMany(`pair:${req.ip}`, 10, 10 * 60_000)) return reply.code(429).send({ error: "Too many attempts. Wait a few minutes and try again." });
    const code = String(req.body?.code ?? "").trim();
    const p = one<{ code_hash: string; user_id: string; device_id: string | null; expires_at: number; agent_id: string | null }>(db,
      "SELECT * FROM pairing_codes WHERE code_hash = ?", sha256(code));
    if (!p || p.agent_id || p.expires_at < now()) return reply.code(400).send({ error: "That pairing code is not valid or has expired." });
    const agentId = id("agt");
    const secret = token(32);
    const d = req.body?.device ?? {};
    run(db, "INSERT INTO agents (id, user_id, device_id, token_hash, hostname, os, created_at) VALUES (?,?,?,?,?,?,?)",
      agentId, p.user_id, p.device_id, sha256(secret), String(d.hostname ?? ""), String(d.os ?? ""), now());
    run(db, "UPDATE pairing_codes SET agent_id = ? WHERE code_hash = ?", agentId, p.code_hash);
    // Nothing is granted by pairing. The user grants each of the three separately.
    for (const k of KINDS) {
      run(db, "INSERT INTO grants (agent_id, kind, granted, scope_json, updated_at, updated_by) VALUES (?,?,0,'[]',?,?)", agentId, k, now(), "pairing");
    }
    const u = one<{ org_id: string; email: string }>(db, "SELECT org_id, email FROM users WHERE id = ?", p.user_id)!;
    audit(db, u.org_id, `user:${u.email}`, "agent.paired", agentId, { hostname: d.hostname, os: d.os });
    return { agent_id: agentId, token: secret };
  });

  app.get("/api/pairing/status", { preHandler: verified }, async (req) => {
    const a = req.authed!;
    const ag = one<{ id: string; hostname: string; os: string }>(db,
      "SELECT id, hostname, os FROM agents WHERE user_id = ? AND revoked = 0 ORDER BY created_at DESC LIMIT 1", a.user.id);
    return ag ? { paired: true, agent: { ...ag, connected: hub.isConnected(ag.id), grants: hub.grantsFor(ag.id), capabilities: hub.capabilitiesFor(ag.id) } } : { paired: false };
  });

  app.put<{ Params: { id: string; kind: string }; Body: { granted?: boolean; scope?: string[] } }>(
    "/api/agents/:id/grants/:kind", { preHandler: verified }, async (req, reply) => {
      const a = req.authed!;
      const kind = req.params.kind as GrantKind;
      if (!KINDS.includes(kind)) return reply.code(400).send({ error: "unknown grant" });
      const agent = one<AgentRow>(db, "SELECT * FROM agents WHERE id = ? AND revoked = 0", req.params.id);
      if (!agent) return reply.code(404).send({ error: "no such agent" });
      const owner = one<{ org_id: string }>(db, "SELECT org_id FROM users WHERE id = ?", agent.user_id)!;
      const isOwner = agent.user_id === a.user.id;
      const isAdmin = a.user.role === "admin" && owner.org_id === a.user.org_id;
      if (!isOwner && !isAdmin) return reply.code(403).send({ error: "not your agent" });
      const granted = !!req.body?.granted;
      const current = hub.grantsFor(agent.id)[kind];
      const scope = Array.isArray(req.body?.scope) ? req.body!.scope.map(String) : current.scope;
      if (granted) {
        // Administrators revoke; only the person at the device grants.
        if (!isOwner) return reply.code(403).send({ error: "Only the person using this device can grant access." });
        const policy = normalisePolicy(parse(one<{ policy_json: string }>(db, "SELECT policy_json FROM orgs WHERE id = ?", owner.org_id)!.policy_json, {}));
        const refusal = checkGrant(policy, kind, scope);
        if (refusal) {
          audit(db, owner.org_id, `user:${a.user.email}`, "grant.refused_by_policy", agent.id, { kind, scope, refusal });
          return reply.code(403).send({ error: refusal });
        }
      }
      run(db, "UPDATE grants SET granted = ?, scope_json = ?, updated_at = ?, updated_by = ? WHERE agent_id = ? AND kind = ?",
        granted ? 1 : 0, JSON.stringify(granted ? scope : []), now(), a.user.email, agent.id, kind);
      const by = isOwner ? a.user.email : `admin:${a.user.email}`;
      audit(db, owner.org_id, `user:${a.user.email}`, granted ? "grant.granted" : "grant.revoked", agent.id,
        { kind, scope: granted ? scope : [], by_admin: !isOwner });
      hub.pushGrants(agent.id, by, granted ? "" : isOwner ? "revoked by the user" : "revoked by an administrator");
      hub.publish(owner.org_id, agent.user_id, "grants", { agent_id: agent.id, kind, granted });
      return { agent_id: agent.id, grants: hub.grantsFor(agent.id), delivered: hub.isConnected(agent.id) };
    });

  // Screen watching: what is in front, and pausing or resuming it. Escape twice on
  // the device pauses it there; only the person at the device resumes it.
  function mine(agentId: string, userId: string): AgentRow | undefined {
    return one<AgentRow>(db, "SELECT * FROM agents WHERE id = ? AND user_id = ? AND revoked = 0", agentId, userId);
  }

  app.get<{ Params: { id: string } }>("/api/agents/:id/screen", { preHandler: verified }, async (req, reply) => {
    const agent = mine(req.params.id, req.authed!.user.id);
    if (!agent) return reply.code(404).send({ error: "no such agent" });
    const caps = hub.capabilitiesFor(agent.id) as { screen?: boolean; pixels?: boolean };
    return { agent_id: agent.id, available: !!caps.screen, pixels: !!caps.pixels, screen: hub.screenFor(agent.id) };
  });

  app.post<{ Params: { id: string }; Body: { on?: boolean } }>("/api/agents/:id/watch", { preHandler: verified }, async (req, reply) => {
    const a = req.authed!;
    const agent = mine(req.params.id, a.user.id);
    if (!agent) return reply.code(404).send({ error: "no such agent" });
    const on = !!req.body?.on;
    if (!hub.send(agent.id, { type: "watch", on, by: a.user.email })) {
      return reply.code(409).send({ error: "The desktop agent is not connected." });
    }
    audit(db, a.user.org_id, `user:${a.user.email}`, on ? "agent.watch.resume_requested" : "agent.watch.pause_requested", agent.id);
    return { ok: true };
  });

  // The agent dials out here. One socket per agent, token in the Authorization header.
  app.get("/agent/ws", { websocket: true }, (socket, req) => {
    const auth = String(req.headers.authorization ?? "");
    const tok = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    const agent = tok ? one<AgentRow>(db, "SELECT * FROM agents WHERE token_hash = ? AND revoked = 0", sha256(tok)) : undefined;
    const user = agent ? one<{ active: number }>(db, "SELECT active FROM users WHERE id = ?", agent.user_id) : undefined;
    if (!agent || !user?.active) {
      socket.close(4401, "unauthorised");
      return;
    }
    hub.attach(agent, socket);
  });

}
