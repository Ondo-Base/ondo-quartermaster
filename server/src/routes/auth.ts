import type { FastifyInstance } from "fastify";
import type { Ctx } from "../app.js";
import { all, now, one, parse, run } from "../db.js";
import { audit } from "../lib/audit.js";
import { type UserRow, getAuthed, guard, sendCode, signIn, signOut, verifyCode } from "../lib/auth.js";
import { verifyPassword } from "../lib/crypto.js";
import { tooMany } from "../lib/limit.js";
import { normalisePolicy } from "../lib/policy.js";
import { oidcFinish, oidcStart, samlFinish, samlStart, ssoLabel, ssoMode } from "../lib/sso.js";

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export function authRoutes(app: FastifyInstance, { db, cfg, hub }: Ctx): void {
  const findUser = (email: string) => one<UserRow>(db, "SELECT * FROM users WHERE email = ? AND active = 1", email.trim());

  app.post<{ Body: { email?: string } }>("/api/pilot", async (req, reply) => {
    const email = String(req.body?.email ?? "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return reply.code(400).send({ error: "Enter a work email address." });
    run(db, "INSERT INTO pilot_requests (email, created_at) VALUES (?, ?)", email, now());
    return { received: true };
  });

  app.get("/api/auth/options", async () => ({ sso: ssoMode(cfg), sso_label: ssoLabel(cfg), password: true }));

  app.post<{ Body: { email?: string; password?: string } }>("/api/auth/login", async (req, reply) => {
    const { email = "", password = "" } = req.body ?? {};
    if (tooMany(`login:${req.ip}`, 20, 10 * 60_000)) return reply.code(429).send({ error: "Too many attempts. Wait a few minutes and try again." });
    const user = findUser(email);
    const ok = verifyPassword(password, user?.password_hash);
    if (!user || !ok) {
      const org = one<{ id: string }>(db, "SELECT id FROM orgs LIMIT 1");
      if (org) audit(db, org.id, `anonymous`, "auth.sign_in_failed", "", { email });
      return reply.code(401).send({ error: "That email and password do not match an account." });
    }
    const s = signIn(db, cfg, req, reply, user, "password");
    return { stage: s.stage };
  });

  // -- single sign-on ------------------------------------------------------------------

  app.get("/auth/sso/start", async (req, reply) => {
    const mode = ssoMode(cfg);
    if (mode === "oidc") {
      const { url, verifier, state } = await oidcStart(cfg);
      reply.setCookie("ondo_oidc", JSON.stringify({ verifier, state }), { path: "/auth", httpOnly: true, sameSite: "lax", secure: cfg.secureCookies, signed: true, maxAge: 600 });
      return reply.redirect(url);
    }
    if (mode === "saml") return reply.redirect(await samlStart(cfg, "/verify"));
    if (mode === "dev") return reply.redirect("/auth/dev-idp");
    return reply.code(404).send({ error: "single sign-on is not configured" });
  });

  app.get("/auth/oidc/callback", async (req, reply) => {
    const raw = req.cookies["ondo_oidc"];
    const un = raw ? req.unsignCookie(raw) : null;
    if (!un?.valid || !un.value) return reply.redirect("/signin?error=sso_expired");
    const { verifier, state } = JSON.parse(un.value);
    try {
      const email = await oidcFinish(cfg, new URL(req.url, cfg.publicUrl), verifier, state);
      return finishSso(email, "oidc", req, reply);
    } catch (e) {
      return reply.redirect(`/signin?error=${encodeURIComponent(String((e as Error).message))}`);
    }
  });

  app.post<{ Body: Record<string, string> }>("/auth/saml/acs", async (req, reply) => {
    try {
      const email = await samlFinish(cfg, req.body ?? {});
      return finishSso(email, "saml", req, reply);
    } catch (e) {
      return reply.redirect(`/signin?error=${encodeURIComponent(String((e as Error).message))}`);
    }
  });

  function finishSso(email: string, method: string, req: any, reply: any) {
    const user = findUser(email);
    if (!user) return reply.redirect(`/signin?error=${encodeURIComponent("Your administrator has not added this account yet.")}`);
    const s = signIn(db, cfg, req, reply, user, method);
    return reply.redirect(s.stage === "verified" ? "/signing-in" : "/verify");
  }

  // A stand-in identity provider for local development only.
  if (cfg.dev) {
    app.get("/auth/dev-idp", async (_req, reply) => {
      const users = all<{ email: string; name: string }>(db, "SELECT email, name FROM users WHERE active = 1 ORDER BY role DESC, email");
      const options = users.map((u) => `<option value="${esc(u.email)}">${esc(u.name)} — ${esc(u.email)}</option>`).join("");
      reply.type("text/html").send(`<!doctype html><meta charset="utf-8"><title>Development identity provider</title>
<body style="font-family: system-ui, sans-serif; max-width: 420px; margin: 80px auto; color: #1c2127">
<p style="font-size:12px;letter-spacing:.04em;color:#5f6b7c">DEVELOPMENT ONLY — NOT A REAL IDENTITY PROVIDER</p>
<h1 style="font-weight:400">Sign in as</h1>
<form method="post" action="/auth/dev-idp"><select name="email" style="width:100%;height:40px">${options}</select>
<p><button style="height:40px;padding:0 16px;background:#2d72d2;color:#fff;border:0;border-radius:3px">Continue</button></p></form></body>`);
    });
    app.post<{ Body: { email?: string } }>("/auth/dev-idp", async (req, reply) => finishSso(req.body?.email ?? "", "dev-sso", req, reply));

    app.get("/api/dev/outbox", async () => all(db, "SELECT * FROM outbox ORDER BY id DESC LIMIT 20"));
  }

  // -- device verification ----------------------------------------------------------------

  app.post<{ Body: { code?: string; trust?: boolean } }>("/api/auth/verify", { preHandler: guard(db, { stage: "any" }) }, async (req, reply) => {
    const a = req.authed!;
    if (a.session.stage === "verified") return { stage: "verified" };
    const r = verifyCode(db, cfg, reply, a, String(req.body?.code ?? ""), req.body?.trust !== false);
    if (r === "ok") return { stage: "verified" };
    const message = {
      wrong: "That code is not right. Check the email and try again.",
      expired: "That code has expired. Send a new one and enter it within ten minutes.",
      locked: "Too many attempts. Send a new code.",
    }[r];
    return reply.code(r === "wrong" ? 400 : 410).send({ error: r, message });
  });

  app.post("/api/auth/resend", { preHandler: guard(db, { stage: "any" }) }, async (req) => {
    const a = req.authed!;
    if (a.session.stage !== "verified") sendCode(db, a.user, a.session);
    return { sent: true };
  });

  app.post("/api/auth/logout", async (req, reply) => {
    signOut(db, cfg, req, reply);
    return { signed_out: true };
  });

  // -- who am I -------------------------------------------------------------------------

  app.get("/api/me", async (req, reply) => {
    const a = getAuthed(db, req);
    if (!a) return reply.code(401).send({ error: "signed_out" });
    const org = one<{ id: string; name: string; policy_json: string }>(db, "SELECT * FROM orgs WHERE id = ?", a.user.org_id)!;
    const device = a.session.device_id ? one(db, "SELECT id, name, os, managed, trusted_until FROM devices WHERE id = ?", a.session.device_id) : null;
    const agents = all<{ id: string; hostname: string; os: string; created_at: number; last_seen: number }>(db,
      "SELECT id, hostname, os, created_at, last_seen FROM agents WHERE user_id = ? AND revoked = 0 ORDER BY created_at DESC", a.user.id)
      .map((ag) => ({ ...ag, connected: hub.isConnected(ag.id), grants: hub.grantsFor(ag.id), capabilities: hub.capabilitiesFor(ag.id),
        connectors: hub.connectorsFor(ag.id, normalisePolicy(parse(org.policy_json, {}))) }));
    return {
      user: { id: a.user.id, email: a.user.email, name: a.user.name, title: a.user.title, role: a.user.role },
      org: { id: org.id, name: org.name, policy: JSON.parse(org.policy_json || "{}") },
      session: { stage: a.session.stage, method: a.session.method },
      device, agents,
    };
  });
}
