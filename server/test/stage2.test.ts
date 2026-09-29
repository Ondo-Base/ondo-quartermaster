// Stage 2: login, policy, audit.
// Done when: an admin can revoke a grant mid-run from the console and the run
// stops. The agent side of "stops" is covered by agent/tests/test_integration.py;
// here the control plane's half: the revocation reaches the agent immediately.

import { createHmac } from "node:crypto";
import type { AddressInfo } from "node:net";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import WebSocket from "ws";
import { buildApp } from "../src/app.js";
import { loadConfig } from "../src/config.js";
import { openDb } from "../src/db.js";
import { forwardOnce } from "../src/lib/audit.js";
import { DEMO_PASSWORD } from "../src/seed.js";

type App = Awaited<ReturnType<typeof buildApp>>;
let built: App;
let base = "";

beforeEach(async () => {
  const cfg = { ...loadConfig({ ONDO_DEV: "1", ONDO_SEED: "demo", PORT: "0" }), dbPath: ":memory:" };
  built = await buildApp(cfg, openDb(":memory:"));
  await built.app.listen({ port: 0, host: "127.0.0.1" });
  base = `http://127.0.0.1:${(built.app.server.address() as AddressInfo).port}`;
  built.ctx.cfg.publicUrl = base;
});
afterEach(async () => { await built.app.close(); });

class Browser {
  cookies = new Map<string, string>();
  async req(method: string, path: string, body?: unknown) {
    const r = await fetch(base + path, {
      method, redirect: "manual",
      headers: { ...(body === undefined ? {} : { "content-type": "application/json" }), cookie: [...this.cookies].map(([k, v]) => `${k}=${v}`).join("; "), "user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/140" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    for (const c of r.headers.getSetCookie()) {
      const [kv] = c.split(";");
      const [k, ...v] = kv.split("=");
      this.cookies.set(k, v.join("="));
    }
    const text = await r.text();
    let json: any = null;
    try { json = JSON.parse(text); } catch { /* not json */ }
    return { status: r.status, json, text, headers: r.headers };
  }
}

async function lastCode(b: Browser): Promise<string> {
  const out = (await b.req("GET", "/api/dev/outbox")).json as { body: string }[];
  return /code is (\d{6})/.exec(out[0].body)![1];
}

async function signedIn(email: string, trust = true): Promise<Browser> {
  const b = new Browser();
  const r = await b.req("POST", "/api/auth/login", { email, password: DEMO_PASSWORD });
  expect(r.json.stage).toBe("needs_device");
  expect((await b.req("POST", "/api/auth/verify", { code: await lastCode(b), trust })).json.stage).toBe("verified");
  return b;
}

class FakeAgent {
  ws!: WebSocket;
  inbox: any[] = [];
  static async pair(b: Browser): Promise<FakeAgent> {
    const code = (await b.req("POST", "/api/pairing")).json.code;
    const r = await fetch(`${base}/api/agent/pair`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ code, device: { hostname: "NW-LT-4471", os: "Windows 11" } }) });
    const { agent_id, token } = await r.json();
    const a = new FakeAgent();
    a.id = agent_id;
    a.ws = new WebSocket(`${base.replace("http", "ws")}/agent/ws`, { headers: { authorization: `Bearer ${token}` } });
    a.ws.on("message", (m) => a.inbox.push(JSON.parse(String(m))));
    await new Promise((res, rej) => { a.ws.once("open", res); a.ws.once("error", rej); });
    a.send({ type: "hello", device: { hostname: "NW-LT-4471", os: "Windows 11" }, capabilities: { screen: true, input: true, desktop: true } });
    await a.next((m) => m.type === "grants");
    return a;
  }
  id = "";
  send(m: unknown) { this.ws.send(JSON.stringify(m)); }
  async next(pred: (m: any) => boolean, timeout = 3000): Promise<any> {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      const i = this.inbox.findIndex(pred);
      if (i >= 0) return this.inbox.splice(i, 1)[0];
      await new Promise((r) => setTimeout(r, 20));
    }
    throw new Error("agent did not receive the expected message; inbox: " + JSON.stringify(this.inbox));
  }
  event(run_id: string, seq: number, type: string, data: unknown) {
    this.send({ type: "event", event: { run_id, seq, type, source: "test", data, ts: Date.now() / 1000, hash: `h${seq}`, prev_hash: seq ? `h${seq - 1}` : "" } });
  }
}

describe("sign-in and device trust", () => {
  it("needs a device code, rejects a wrong one, and trusts the device for 30 days", async () => {
    const b = new Browser();
    expect((await b.req("POST", "/api/auth/login", { email: "mara.okonjo@northwind-ops.com", password: "wrong" })).status).toBe(401);
    expect((await b.req("POST", "/api/auth/login", { email: "mara.okonjo@northwind-ops.com", password: DEMO_PASSWORD })).json.stage).toBe("needs_device");
    // Signed in but unverified: identity only, no access to runs or pairing.
    expect((await b.req("GET", "/api/runs")).status).toBe(403);
    expect((await b.req("POST", "/api/pairing")).status).toBe(403);
    const wrong = await b.req("POST", "/api/auth/verify", { code: "000000", trust: true });
    expect([400, 410]).toContain(wrong.status);
    expect((await b.req("POST", "/api/auth/verify", { code: await lastCode(b), trust: true })).json.stage).toBe("verified");
    expect(b.cookies.has("ondo_device")).toBe(true);
    const me = (await b.req("GET", "/api/me")).json;
    expect(me.user.name).toBe("Mara Okonjo");
    expect(me.device.name).toBe("Chrome on Windows");

    // Same browser, new session: the trusted device skips the code.
    await b.req("POST", "/api/auth/logout");
    expect((await b.req("POST", "/api/auth/login", { email: "mara.okonjo@northwind-ops.com", password: DEMO_PASSWORD })).json.stage).toBe("verified");
  });

  it("reports an expired code in place", async () => {
    const b = new Browser();
    await b.req("POST", "/api/auth/login", { email: "mara.okonjo@northwind-ops.com", password: DEMO_PASSWORD });
    built.ctx.db.exec("UPDATE verification_codes SET expires_at = 0");
    const r = await b.req("POST", "/api/auth/verify", { code: await lastCode(b) });
    expect(r.status).toBe(410);
    expect(r.json.message).toBe("That code has expired. Send a new one and enter it within ten minutes.");
  });

  it("walks the development identity provider and refuses cross-site posts", async () => {
    const b = new Browser();
    expect((await b.req("GET", "/api/auth/options")).json.sso).toBe("dev");
    const start = await b.req("GET", "/auth/sso/start");
    expect(start.headers.get("location")).toBe("/auth/dev-idp");
    const r = await fetch(`${base}/api/auth/login`, { method: "POST", headers: { "content-type": "application/json", origin: "https://evil.example" }, body: "{}" });
    expect(r.status).toBe(403);
  });
});

describe("SCIM provisioning", () => {
  it("creates, finds, promotes and deprovisions people", async () => {
    const admin = await signedIn("it.admin@northwind-ops.com");
    const tok = (await admin.req("POST", "/api/admin/scim-tokens", { label: "Entra ID" })).json.token;
    const scim = (method: string, path: string, body?: unknown) => fetch(`${base}/scim/v2${path}`, {
      method, headers: { authorization: `Bearer ${tok}`, "content-type": "application/scim+json" }, body: body ? JSON.stringify(body) : undefined,
    });
    expect((await fetch(`${base}/scim/v2/Users`, { headers: { authorization: "Bearer nope" } })).status).toBe(401);
    const created = await (await scim("POST", "/Users", {
      schemas: ["urn:ietf:params:scim:schemas:core:2.0:User"], userName: "tom.reyes@northwind-ops.com",
      name: { givenName: "Tom", familyName: "Reyes" }, externalId: "e-42", active: true,
    })).json();
    expect(created.userName).toBe("tom.reyes@northwind-ops.com");
    const found = await (await scim("GET", `/Users?filter=${encodeURIComponent('userName eq "tom.reyes@northwind-ops.com"')}`)).json();
    expect(found.totalResults).toBe(1);
    const patched = await (await scim("PATCH", `/Users/${created.id}`, {
      schemas: ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
      Operations: [{ op: "add", path: "groups", value: [{ display: "ondo-admins" }] }, { op: "replace", path: "active", value: false }],
    })).json();
    expect(patched.active).toBe(false);
    const row = built.ctx.db.prepare("SELECT role, active FROM users WHERE id = ?").get(created.id) as { role: string; active: number };
    expect(row).toEqual({ role: "admin", active: 0 });
  });
});

describe("pairing, grants and runs", () => {
  it("pairs an agent that dials out, enforces policy on grants, relays runs and approvals", async () => {
    const mara = await signedIn("mara.okonjo@northwind-ops.com");
    const agent = await FakeAgent.pair(mara);
    expect(agent.inbox.length).toBeGreaterThanOrEqual(0);

    // Three separate grants. Nothing is granted by pairing.
    const status = (await mara.req("GET", "/api/pairing/status")).json;
    expect(status.agent.connected).toBe(true);
    expect(status.agent.capabilities).toMatchObject({ screen: true, desktop: true });
    expect(Object.values(status.agent.grants).every((g: any) => !g.granted)).toBe(true);

    // Policy exclusions are not grantable, whoever asks.
    const refused = await mara.req("PUT", `/api/agents/${agent.id}/grants/files`, { granted: true, scope: ["/Users/mara/HR"] });
    expect(refused.status).toBe(403);
    const ok = await mara.req("PUT", `/api/agents/${agent.id}/grants/files`, { granted: true, scope: ["/Users/mara/Northwind client drive"] });
    expect(ok.status).toBe(200);
    const pushed = await agent.next((m) => m.type === "grants" && m.grants.files.granted);
    expect(pushed.grants.files.scope).toEqual(["/Users/mara/Northwind client drive"]);
    await mara.req("PUT", `/api/agents/${agent.id}/grants/input`, { granted: true });
    await agent.next((m) => m.type === "grants" && m.grants.input.granted);

    // Start a run: the control plane records it and relays it to the agent.
    const { run_id } = (await mara.req("POST", "/api/runs", { request: "Build the Q3 renewal pack for Northwind. Flag anything above five per cent." })).json;
    const start = await agent.next((m) => m.type === "start_run");
    expect(start.run_id).toBe(run_id);
    agent.event(run_id, 0, "run_started", { request: start.request, profile: "gateway", model: "orchestrator" });
    agent.event(run_id, 1, "file_access", { path: "/Users/mara/Northwind client drive/Q3_Renewals.xlsx", op: "read", kind: "Workbook" });
    agent.event(run_id, 2, "window_access", { window: "Legacy billing", op: "acted", action: "set_text", element: 'text "Annual value"' });
    agent.event(run_id, 3, "approval_requested", { id: "apr_1", title: "Submit six lines", summary: "Nothing has been saved there yet.",
      effects: ["submits_to_system_of_record"], values: [{ label: "Annual value — Halleck Logistics", before: "184500", after: "193725" }] });
    await new Promise((r) => setTimeout(r, 100));

    const runs = (await mara.req("GET", "/api/runs")).json;
    expect(runs[0].title).toBe("Build the Q3 renewal pack for Northwind");
    expect(runs[0].steps.total).toBe(0);
    expect(runs[0].status).toBe("waiting");
    const pending = (await mara.req("GET", "/api/approvals?status=pending")).json;
    expect(pending[0].values[0].after).toBe("193725");
    const files = (await mara.req("GET", "/api/files")).json;
    expect(files.folders[0].files[0]).toMatchObject({ name: "Q3_Renewals.xlsx", status: "Read" });

    // The approval goes to the agent with who approved it.
    expect((await mara.req("POST", "/api/approvals/apr_1", { approved: true })).json.status).toBe("approved");
    const ap = await agent.next((m) => m.type === "approval");
    expect(ap).toMatchObject({ approval_id: "apr_1", approved: true, by: "mara.okonjo@northwind-ops.com" });

    // An admin revokes a grant mid-run from the console: it reaches the agent at once.
    const admin = await signedIn("it.admin@northwind-ops.com");
    const overview = (await admin.req("GET", "/api/admin/overview")).json;
    expect(overview.active_runs.map((r: any) => r.id)).toContain(run_id);
    expect((await admin.req("PUT", `/api/agents/${agent.id}/grants/input`, { granted: false })).status).toBe(200);
    const revoked = await agent.next((m) => m.type === "grants" && !m.grants.input.granted);
    expect(revoked.by).toBe("admin:it.admin@northwind-ops.com");
    expect(revoked.reason).toBe("revoked by an administrator");
    // Admins revoke; they cannot grant on someone else's device.
    expect((await admin.req("PUT", `/api/agents/${agent.id}/grants/input`, { granted: true })).status).toBe(403);

    agent.event(run_id, 4, "run_stopped", { reason: "The input grant was revoked by an administrator.", status: "stopped" });
    await new Promise((r) => setTimeout(r, 100));
    const detail = (await mara.req("GET", `/api/runs/${run_id}`)).json;
    expect(detail.run.status).toBe("stopped");
    expect(detail.events.map((e: any) => e.type)).toEqual(["run_started", "file_access", "window_access", "approval_requested", "run_stopped"]);

    // Everything above is in the audit log, hash-chained, with who did it.
    const audit = (await admin.req("GET", "/api/admin/audit?limit=500")).json as any[];
    const actions = audit.map((r) => `${r.actor} ${r.action}`);
    expect(actions).toContain("user:mara.okonjo@northwind-ops.com grant.refused_by_policy");
    expect(actions).toContain("user:mara.okonjo@northwind-ops.com approval.approved");
    expect(actions).toContain("user:it.admin@northwind-ops.com grant.revoked");
    expect(actions.some((a) => a.endsWith("agent.file.read"))).toBe(true);
    expect(actions.some((a) => a.endsWith("agent.window.acted"))).toBe(true);
    expect((await admin.req("GET", "/api/admin/audit/verify")).json.ok).toBe(true);
    agent.ws.close();
  });
});

describe("audit export and SIEM", () => {
  it("exports JSONL and CEF and forwards signed batches without skipping on failure", async () => {
    const admin = await signedIn("it.admin@northwind-ops.com");
    const jsonl = await admin.req("GET", "/api/admin/audit/export?format=jsonl");
    expect(jsonl.text.trim().split("\n").map((l) => JSON.parse(l).action)).toContain("auth.device_verified");
    const cef = await admin.req("GET", "/api/admin/audit/export?format=cef");
    expect(cef.text.split("\n")[0]).toMatch(/^CEF:0\|Ondo\|Quartermaster\|0\.1\|auth\.signed_in\|/);

    await admin.req("POST", "/api/admin/siem", { url: "https://siem.example/ingest", format: "jsonl", secret: "s3cret" });
    let fail = true;
    const got: { body: string; sig: string }[] = [];
    const fake = (async (_url: string, init: RequestInit) => {
      if (fail) return new Response("down", { status: 503 });
      got.push({ body: String(init.body), sig: (init.headers as Record<string, string>)["x-ondo-signature"] });
      return new Response("ok");
    }) as unknown as typeof fetch;
    expect(await forwardOnce(built.ctx.db, fake)).toBe(0);
    fail = false;
    const n = await forwardOnce(built.ctx.db, fake);
    expect(n).toBeGreaterThan(3);
    expect(got[0].sig).toBe("sha256=" + createHmac("sha256", "s3cret").update(got[0].body).digest("hex"));
    expect(await forwardOnce(built.ctx.db, fake)).toBe(0); // nothing new
  });
});

describe("screen watching (Stage 5)", () => {
  it("knows which shared window is in front, starts runs about it, and pauses on Escape", async () => {
    const mara = await signedIn("mara.okonjo@northwind-ops.com");
    const agent = await FakeAgent.pair(mara);
    await mara.req("PUT", `/api/agents/${agent.id}/grants/screen`, { granted: true, scope: ["Remote billing"] });
    await agent.next((m) => m.type === "grants" && m.grants.screen.granted);
    expect((await mara.req("GET", `/api/agents/${agent.id}/screen`)).json.screen).toBeNull();

    const win = "wfica — Remote billing — Citrix Workspace";
    agent.send({ type: "screen_context", window: win, title: "Remote billing — Citrix Workspace", watching: true });
    await new Promise((r) => setTimeout(r, 100));
    expect((await mara.req("GET", `/api/agents/${agent.id}/screen`)).json.screen).toMatchObject({ window: win, title: "Remote billing — Citrix Workspace", watching: true });
    // Only the owner sees what is in front of them.
    const admin = await signedIn("it.admin@northwind-ops.com");
    expect((await admin.req("GET", `/api/agents/${agent.id}/screen`)).status).toBe(404);

    // "Ask about this screen" names the window in front, and only that one.
    const stale = await mara.req("POST", "/api/runs", { request: "What is this?", context: { window: "Personal mail" } });
    expect(stale.status).toBe(409);
    const ok = await mara.req("POST", "/api/runs", { request: "What does this say?", context: { window: win } });
    expect(ok.status).toBe(200);
    const start = await agent.next((m) => m.type === "start_run");
    expect(start.context).toEqual({ window: win });

    // Escape twice on the device pauses watching; the web can ask to resume it.
    agent.send({ type: "screen_context", window: null, watching: false, reason: "escape_twice" });
    await new Promise((r) => setTimeout(r, 100));
    expect((await mara.req("POST", "/api/runs", { request: "And now?", context: { window: win } })).status).toBe(409);
    expect((await mara.req("POST", `/api/agents/${agent.id}/watch`, { on: true })).status).toBe(200);
    expect(await agent.next((m) => m.type === "watch")).toMatchObject({ on: true });
    const actions = (await admin.req("GET", "/api/admin/audit?limit=200")).json.map((e: any) => e.action);
    expect(actions).toContain("agent.watch.paused");
    expect(actions).toContain("agent.watch.resume_requested");

    agent.ws.close();
    await new Promise((r) => setTimeout(r, 100));
    expect((await mara.req("GET", `/api/agents/${agent.id}/screen`)).json.screen).toBeNull();
  });
});

describe("connector consent (Stage 6)", () => {
  it("records a person's yes to a connector, bounded by policy, and takes it back", async () => {
    const mara = await signedIn("mara.okonjo@northwind-ops.com");
    const agent = await FakeAgent.pair(mara);
    const ticketing = { id: "ticketing", name: "Ticketing", description: "The service desk.", tools: [{ name: "search_tickets", effect: "read" }] };
    const billing = { id: "billing", name: "Billing", description: "Invoices.", tools: [{ name: "approve", effect: "move_money" }] };
    agent.send({ type: "hello", device: { hostname: "NW-LT-4471", os: "Windows 11" }, capabilities: { screen: true, desktop: true, connectors: [ticketing, billing] } });
    await new Promise((r) => setTimeout(r, 100));
    let me = (await mara.req("GET", "/api/me")).json;
    expect(me.agents[0].connectors.map((c: any) => [c.id, c.allowed_by_policy, c.consent])).toEqual([["ticketing", true, null], ["billing", false, null]]);

    const run_id = (await mara.req("POST", "/api/runs", { request: "Reply to Halleck's ticket." })).json.run_id;
    await agent.next((m) => m.type === "start_run");
    agent.event(run_id, 0, "run_started", { request: "Reply", profile: "p", model: "m" });
    agent.event(run_id, 1, "approval_requested", { id: "apr_c1", title: "Let Ondo use Ticketing?", summary: "", effects: [], values: [], kind: "consent", connector: "ticketing" });
    agent.event(run_id, 2, "approval_requested", { id: "apr_c2", title: "Let Ondo use Billing?", summary: "", effects: [], values: [], kind: "consent", connector: "billing" });
    agent.event(run_id, 3, "approval_requested", { id: "apr_e1", title: "Reply on NW-1042", summary: "", effects: ["sends_externally"], values: [] });
    await new Promise((r) => setTimeout(r, 100));
    expect((await mara.req("POST", "/api/approvals/apr_c1", { approved: true })).status).toBe(200);
    expect((await agent.next((m) => m.type === "approval")).approval_id).toBe("apr_c1");
    agent.event(run_id, 4, "approval_resolved", { approval_id: "apr_c1", approved: true, by: "mara.okonjo@northwind-ops.com", kind: "consent" });
    // A connector policy does not allow is not recorded even if the device says yes,
    agent.event(run_id, 5, "approval_resolved", { approval_id: "apr_c2", approved: true, by: "mara.okonjo@northwind-ops.com", kind: "consent" });
    // and an effect approval cannot be passed off as a consent.
    agent.event(run_id, 6, "approval_resolved", { approval_id: "apr_e1", approved: true, by: "mara.okonjo@northwind-ops.com", kind: "consent", connector: "billing" });
    await new Promise((r) => setTimeout(r, 150));
    me = (await mara.req("GET", "/api/me")).json;
    expect(me.agents[0].connectors[0].consent).toMatchObject({ by: "mara.okonjo@northwind-ops.com" });
    expect(me.agents[0].connectors[1].consent).toBeNull();
    // The agent is told the consent is recorded.
    expect((await agent.next((m) => m.type === "grants" && m.connectors?.ticketing)).connectors.ticketing.by).toBe("mara.okonjo@northwind-ops.com");
    const consentApproval = (await mara.req("GET", `/api/runs/${run_id}`)).json.approvals.find((x: any) => x.id === "apr_c1");
    expect(consentApproval).toMatchObject({ kind: "consent", connector: "ticketing" });

    // An administrator takes it back; the agent hears at once.
    const admin = await signedIn("it.admin@northwind-ops.com");
    expect((await admin.req("DELETE", `/api/agents/${agent.id}/connectors/ticketing`)).status).toBe(200);
    const revoked = await agent.next((m) => m.type === "grants" && m.reason === "revoked by an administrator");
    expect(revoked.connectors).toEqual({});
    expect((await admin.req("DELETE", `/api/agents/${agent.id}/connectors/ticketing`)).status).toBe(404);

    // Consent again, then policy drops the connector: every consent to it goes.
    agent.event(run_id, 7, "approval_requested", { id: "apr_c3", title: "Let Ondo use Ticketing?", summary: "", effects: [], values: [], kind: "consent", connector: "ticketing" });
    await new Promise((r) => setTimeout(r, 100));
    await mara.req("POST", "/api/approvals/apr_c3", { approved: true });
    agent.event(run_id, 8, "approval_resolved", { approval_id: "apr_c3", approved: true, by: "mara.okonjo@northwind-ops.com", kind: "consent" });
    await new Promise((r) => setTimeout(r, 100));
    const policy = (await admin.req("GET", "/api/admin/overview")).json.policy;
    expect(policy.allowed_connectors).toEqual(["ticketing"]);
    await admin.req("PUT", "/api/admin/policy", { ...policy, allowed_connectors: [] });
    expect((await agent.next((m) => m.type === "policy" && m.policy.allowed_connectors.length === 0)).policy.allowed_connectors).toEqual([]);
    expect((await agent.next((m) => m.type === "grants" && m.reason === "no longer allowed by policy")).connectors).toEqual({});
    const actions = (await admin.req("GET", "/api/admin/audit?limit=300")).json.map((e: any) => e.action);
    expect(actions.filter((x: string) => x === "connector.consented")).toHaveLength(2);
    expect(actions.filter((x: string) => x === "connector.revoked")).toHaveLength(2);
    agent.ws.close();
  });
});
