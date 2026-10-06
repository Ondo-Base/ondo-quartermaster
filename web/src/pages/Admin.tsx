// The admin console. Not in the design canvas; built on the same system: the
// rail, the canvas, hairline cards, one blue. Revoking a grant here reaches the
// agent at once and stops any run using it.

import { useState, type FormEvent } from "react";
import { Link, NavLink } from "react-router-dom";
import { ApiError, api, hhmm, useData, whenLabel, type AgentConnector, type Grants, type Policy } from "../api";
import { BackHome, Crumbs, RailFoot, RailHead, useShell } from "../components/Shell";
import { Icon } from "../icons";

interface Overview {
  org: { id: string; name: string };
  policy: Policy;
  users: { id: string; email: string; name: string; title: string; role: string; active: number; external_id: string | null }[];
  agents: { id: string; hostname: string; os: string; connected: boolean; grants: Grants; last_seen: number; user?: { name: string; email: string }; connectors?: AgentConnector[] }[];
  devices: { id: string; user_id: string; name: string; os: string; trusted_until: number; last_seen: number }[];
  active_runs: { id: string; title: string; status: string; user_name: string; created_at: number; agent_id: string }[];
  siem_sinks: { id: string; url: string; format: string; enabled: number; cursor: number; last_error: string }[];
}

function AdminShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="shell">
      <nav className="rail" aria-label="Admin">
        <RailHead />
        <div className="rail-body" style={{ paddingTop: 16 }}>
          <BackHome />
          <div className="col" style={{ gap: 8 }}>
            <span className="rail-label">Admin console</span>
            <div className="rail-list">
              <NavLink end to="/admin" className="rail-row"><Icon name="lock" size={16} color="var(--on-rail-muted)" /><span className="t grow">Access and policy</span></NavLink>
              <NavLink to="/admin/audit" className="rail-row"><Icon name="workflow" size={16} color="var(--on-rail-muted)" /><span className="t grow">Audit log</span></NavLink>
            </div>
          </div>
        </div>
        <RailFoot />
      </nav>
      <main className="main">
        <header className="topbar"><Crumbs items={[{ label: "Home", to: "/app" }, { label: "Admin console" }, { label: title }]} /></header>
        <div className="canvas" style={{ padding: 32, gap: 24 }}>{children}</div>
      </main>
    </div>
  );
}

const KINDS: { kind: "files" | "screen" | "input"; label: string }[] = [
  { kind: "files", label: "Files" }, { kind: "screen", label: "Screen" }, { kind: "input", label: "Keyboard and pointer" },
];

export function AdminConsole() {
  const { toast } = useShell();
  const { data, reload } = useData<Overview>("/api/admin/overview", (e) => e === "agent" || e === "grants" || e === "run");
  if (!data) return null;

  async function call(fn: () => Promise<unknown>, ok: string) {
    try { await fn(); toast(ok); void reload(); } catch (e) { toast((e as ApiError).message); }
  }

  return (
    <AdminShell title="Access and policy">
      <div className="col" style={{ gap: 6 }}>
        <h1 className="display">Access and policy</h1>
        <p className="lead">{data.org.name}: {data.agents.filter((a) => a.connected).length} of {data.agents.length} agents connected, {data.active_runs.length} task{data.active_runs.length === 1 ? "" : "s"} in progress.</p>
      </div>

      <section className="card">
        <div className="card-head"><span className="eyebrow-sm">Desktop agents and their grants</span></div>
        {data.agents.length === 0 && <p className="ui secondary" style={{ padding: "16px 20px" }}>No agents are paired yet.</p>}
        {data.agents.map((a, i) => (
          <div key={a.id}>
            {i > 0 && <div className="divider" />}
            <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="row">
                <span className={`dot ${a.connected ? "dot-accent" : "dot-ring-light"}`} />
                <span className="grow col">
                  <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>{a.user?.name ?? "Unknown"} · {a.hostname || "unnamed device"}</span>
                  <span className="caption">{a.os} · {a.connected ? "connected" : `last seen ${a.last_seen ? whenLabel(a.last_seen) : "never"}`}</span>
                </span>
                <button className="btn btn-sm btn-danger" onClick={() => confirm("Revoke this agent? It disconnects now and must be paired again.") && call(() => api(`/api/admin/agents/${a.id}/revoke`, { body: {} }), "Agent revoked.")}>Revoke agent</button>
              </div>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                {KINDS.map(({ kind, label }) => {
                  const g = a.grants[kind];
                  return (
                    <span key={kind} className="row" style={{ gap: 8, padding: "6px 10px", border: "1px solid var(--line-soft)", borderRadius: 3 }}>
                      <span className={`tag ${g.granted ? "tag-accent" : "tag-outline"}`}>{g.granted ? "On" : "Off"}</span>
                      <span className="ui">{label}{kind === "files" && g.granted ? `: ${g.scope.map((s) => s.split("/").filter(Boolean).pop()).join(", ")}` : ""}</span>
                      {g.granted && <button className="btn btn-sm btn-danger" onClick={() => call(() => api(`/api/agents/${a.id}/grants/${kind}`, { method: "PUT", body: { granted: false } }), `${label} revoked. Any run using it stops.`)}>Revoke</button>}
                    </span>
                  );
                })}
                {(a.connectors ?? []).filter((c) => c.consent).map((c) => (
                  <span key={c.id} className="row" style={{ gap: 8, padding: "6px 10px", border: "1px solid var(--line-soft)", borderRadius: 3 }}>
                    <span className="tag tag-accent">Allowed</span>
                    <span className="ui">{c.name}</span>
                    <button className="btn btn-sm btn-danger" onClick={() => call(() => api(`/api/agents/${a.id}/connectors/${c.id}`, { method: "DELETE" }), `${c.name} revoked. Any run using it stops.`)}>Revoke</button>
                  </span>
                ))}
              </div>
              {data.active_runs.filter((r) => r.agent_id === a.id).map((r) => (
                <div key={r.id} className="row callout" style={{ alignItems: "center" }}>
                  <span className="dot dot-accent qm-pulse" />
                  <span className="grow"><Link to={`/app/runs/${r.id}`}>{r.title}</Link> <span className="caption">· {r.status} · started {hhmm(r.created_at)}</span></span>
                  <button className="btn btn-sm" onClick={() => call(() => api(`/api/runs/${r.id}/stop`, { body: { reason: "Stopped by an administrator" } }), "Stop sent.")}>Stop</button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>

      <PolicyCard policy={data.policy} onSaved={reload} />

      <section className="card">
        <div className="card-head"><span className="eyebrow-sm">People</span><span className="grow" /><ScimToken /></div>
        <table className="table" style={{ border: "none" }}>
          <thead><tr><th>Name</th><th>Email</th><th style={{ width: 120 }}>Role</th><th style={{ width: 120 }}>Source</th><th style={{ width: 150 }} /></tr></thead>
          <tbody>
            {data.users.map((u) => (
              <tr key={u.id}>
                <td className="name">{u.name}<div className="caption">{u.title}</div></td>
                <td>{u.email}</td>
                <td>{u.role === "admin" ? "Administrator" : "Member"}</td>
                <td>{u.external_id ? "SCIM" : "Console"}</td>
                <td>{u.active
                  ? <button className="btn btn-sm" onClick={() => confirm(`Deactivate ${u.name}? Their sessions end and every grant on their agents is revoked.`) && call(() => api(`/api/admin/users/${u.id}`, { method: "PUT", body: { active: false } }), "Deactivated.")}>Deactivate</button>
                  : <button className="btn btn-sm" onClick={() => call(() => api(`/api/admin/users/${u.id}`, { method: "PUT", body: { active: true } }), "Reactivated.")}>Reactivate</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <div className="card-head"><span className="eyebrow-sm">Trusted devices</span></div>
        {data.devices.length === 0 && <p className="ui secondary" style={{ padding: "16px 20px" }}>No devices are trusted right now.</p>}
        {data.devices.map((d, i) => (
          <div key={d.id}>
            {i > 0 && <div className="divider" />}
            <div className="row" style={{ padding: "12px 20px" }}>
              <span className="grow col"><span className="ui" style={{ color: "var(--ink)" }}>{d.name} · {data.users.find((u) => u.id === d.user_id)?.name}</span><span className="caption">Trusted until {new Date(d.trusted_until).toLocaleDateString("en-GB")}</span></span>
              <button className="btn btn-sm" onClick={() => call(() => api(`/api/admin/devices/${d.id}/trust`, { method: "DELETE" }), "Trust ended. The next sign-in needs a code.")}>End trust</button>
            </div>
          </div>
        ))}
      </section>

      <EnrollmentCard />
      <SiemCard sinks={data.siem_sinks} onChange={reload} />
    </AdminShell>
  );
}

function lines(s: string) { return s.split("\n").map((x) => x.trim()).filter(Boolean); }

function PolicyCard({ policy, onSaved }: { policy: Policy; onSaved: () => void }) {
  const { toast } = useShell();
  const [paths, setPaths] = useState(policy.excluded_paths.join("\n"));
  const [windows, setWindows] = useState(policy.excluded_windows.join("\n"));
  const [origins, setOrigins] = useState((policy.allowed_origins ?? []).join("\n"));
  const [connectors, setConnectors] = useState((policy.allowed_connectors ?? []).join("\n"));
  const [disabled, setDisabled] = useState<string[]>(policy.disabled_grants);
  const [writes, setWrites] = useState(policy.writes_require_approval);
  async function save(e: FormEvent) {
    e.preventDefault();
    try {
      await api("/api/admin/policy", { method: "PUT", body: { excluded_paths: lines(paths), excluded_windows: lines(windows), allowed_origins: lines(origins), allowed_connectors: lines(connectors), disabled_grants: disabled, writes_require_approval: writes } });
      toast("Policy saved and sent to every connected agent.");
      onSaved();
    } catch (err) { toast((err as ApiError).message); }
  }
  return (
    <form className="card" onSubmit={save}>
      <div className="card-head"><span className="eyebrow-sm">Policy</span><span className="grow" /><button className="btn btn-sm btn-primary">Save policy</button></div>
      <div style={{ padding: 20, display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 20 }}>
        <div className="field"><label className="label" htmlFor="p-paths">Excluded paths</label><textarea id="p-paths" className="input" rows={5} value={paths} onChange={(e) => setPaths(e.target.value)} /><span className="caption">One glob per line. Never readable, never grantable.</span></div>
        <div className="field"><label className="label" htmlFor="p-win">Excluded windows</label><textarea id="p-win" className="input" rows={5} value={windows} onChange={(e) => setWindows(e.target.value)} /><span className="caption">Never captured. The log says so when it bites.</span></div>
        <div className="field"><label className="label" htmlFor="p-orig">Allowed web origins</label><textarea id="p-orig" className="input" rows={5} value={origins} onChange={(e) => setOrigins(e.target.value)} /><span className="caption">The only sites the browser may open, e.g. https://billing.example.com. Empty: each agent's own configuration applies.</span></div>
        <div className="field"><label className="label" htmlFor="p-conn">Connectors people may allow</label><textarea id="p-conn" className="input" rows={5} value={connectors} onChange={(e) => setConnectors(e.target.value)} /><span className="caption">One per line, e.g. ticketing. Each person still allows each one on first use. Removing one revokes it for everyone.</span></div>
      </div>
      <div className="row" style={{ padding: "0 20px 20px", gap: 24, flexWrap: "wrap" }}>
        {KINDS.map(({ kind, label }) => (
          <label key={kind} className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={disabled.includes(kind)} onChange={(e) => setDisabled((d) => (e.target.checked ? [...d, kind] : d.filter((x) => x !== kind)))} style={{ accentColor: "var(--accent)" }} />
            <span className="ui">Disable {label.toLowerCase()} for everyone</span>
          </label>
        ))}
        <label className="row" style={{ gap: 8 }}>
          <input type="checkbox" checked={writes} onChange={(e) => setWrites(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
          <span className="ui">Every file write needs approval</span>
        </label>
      </div>
    </form>
  );
}

function ScimToken() {
  const { toast } = useShell();
  const [tok, setTok] = useState("");
  if (tok) return <span className="caption">SCIM token, shown once: <code style={{ userSelect: "all" }}>{tok}</code></span>;
  return <button type="button" className="btn btn-sm" onClick={async () => { try { setTok((await api<{ token: string }>("/api/admin/scim-tokens", { body: { label: "Identity provider" } })).token); } catch (e) { toast((e as ApiError).message); } }}>Create a SCIM token</button>;
}

interface EnrollmentToken { id: string; label: string; created_by: string; created_at: number; revoked_at: number | null; uses: number }

/** Tokens for device management: agents deployed by Intune or Group Policy enroll
 * themselves with one, and each person confirms their computer before it does anything. */
function EnrollmentCard() {
  const { toast } = useShell();
  const { data, reload } = useData<EnrollmentToken[]>("/api/admin/enrollment-tokens", () => false);
  const [label, setLabel] = useState("");
  const [shown, setShown] = useState<{ label: string; token: string } | null>(null);
  return (
    <section className="card">
      <div className="card-head"><span className="eyebrow-sm">Deployment: enrollment tokens</span></div>
      <p className="ui secondary" style={{ padding: "12px 20px 0" }}>
        Put a token in the EnrollmentToken setting of the Ondo policy template (Intune or Group Policy), with ControlPlaneUrl.
        The agent then sets itself up for whoever signs in, and does nothing until they confirm the computer is theirs.
      </p>
      {shown && (
        <div className="callout" style={{ margin: "12px 20px 0" }}>
          <span className="caption">{shown.label}, shown once: <code style={{ userSelect: "all" }}>{shown.token}</code></span>
        </div>
      )}
      {(data ?? []).map((t) => (
        <div key={t.id} className="row" style={{ padding: "12px 20px", borderTop: "1px solid var(--line-divider)" }}>
          <span className="grow col">
            <span className="ui" style={{ color: t.revoked_at ? "var(--ink-muted)" : "var(--ink)" }}>{t.label}</span>
            <span className="caption">Made by {t.created_by} {whenLabel(t.created_at).toLowerCase()} · {t.uses} computer{t.uses === 1 ? "" : "s"} enrolled{t.revoked_at ? " · revoked" : ""}</span>
          </span>
          {!t.revoked_at && <button className="btn btn-sm btn-danger" onClick={async () => {
            if (!confirm(`Revoke “${t.label}”? Computers already enrolled keep working; no new ones can enroll with it.`)) return;
            try { await api(`/api/admin/enrollment-tokens/${t.id}`, { method: "DELETE" }); void reload(); } catch (e) { toast((e as ApiError).message); }
          }}>Revoke</button>}
        </div>
      ))}
      <form className="row" style={{ padding: "12px 20px", gap: 8, borderTop: "1px solid var(--line-divider)" }}
        onSubmit={async (e) => {
          e.preventDefault();
          try {
            const r = await api<{ label: string; token: string }>("/api/admin/enrollment-tokens", { body: { label } });
            setShown(r); setLabel(""); void reload();
          } catch (err) { toast((err as ApiError).message); }
        }}>
        <label className="sr-only" htmlFor="enr-label">Label</label>
        <input id="enr-label" className="input grow" placeholder="Label, e.g. Intune: finance laptops" value={label} onChange={(e) => setLabel(e.target.value)} />
        <button className="btn">Create a token</button>
      </form>
    </section>
  );
}

function SiemCard({ sinks, onChange }: { sinks: Overview["siem_sinks"]; onChange: () => void }) {
  const { toast } = useShell();
  const [url, setUrl] = useState("");
  const [format, setFormat] = useState("jsonl");
  const [secret, setSecret] = useState("");
  return (
    <section className="card">
      <div className="card-head"><span className="eyebrow-sm">SIEM streaming</span><span className="grow" />
        <button className="btn btn-sm" onClick={async () => { const r = await api<{ sent: number }>("/api/admin/siem/flush", { body: {} }); toast(`Sent ${r.sent} records.`); onChange(); }}>Send now</button></div>
      {sinks.map((s, i) => (
        <div key={s.id}>
          {i > 0 && <div className="divider" />}
          <div className="row" style={{ padding: "12px 20px" }}>
            <span className="grow col"><span className="ui" style={{ color: "var(--ink)" }}>{s.url}</span><span className="caption">{s.format.toUpperCase()} · delivered up to record {s.cursor}{s.last_error ? ` · last error: ${s.last_error}` : ""}</span></span>
            <button className="btn btn-sm" onClick={async () => { await api(`/api/admin/siem/${s.id}`, { method: "DELETE" }); onChange(); }}>Remove</button>
          </div>
        </div>
      ))}
      <form className="row" style={{ padding: "12px 20px", gap: 8, borderTop: sinks.length ? "1px solid var(--line-divider)" : undefined }}
        onSubmit={async (e) => { e.preventDefault(); try { await api("/api/admin/siem", { body: { url, format, secret } }); setUrl(""); setSecret(""); onChange(); } catch (err) { toast((err as ApiError).message); } }}>
        <label className="sr-only" htmlFor="siem-url">Sink URL</label>
        <input id="siem-url" className="input grow" placeholder="https://siem.example.com/ingest" value={url} onChange={(e) => setUrl(e.target.value)} />
        <label className="sr-only" htmlFor="siem-format">Format</label>
        <select id="siem-format" className="input" style={{ width: 110 }} value={format} onChange={(e) => setFormat(e.target.value)}><option value="jsonl">JSONL</option><option value="cef">CEF</option></select>
        <label className="sr-only" htmlFor="siem-secret">Signing secret</label>
        <input id="siem-secret" className="input" style={{ width: 180 }} placeholder="Signing secret" value={secret} onChange={(e) => setSecret(e.target.value)} />
        <button className="btn">Add sink</button>
      </form>
    </section>
  );
}

interface AuditRow { id: number; ts: number; actor: string; action: string; target: string; detail: any; hash: string }

export function AuditLog() {
  const [action, setAction] = useState("");
  const { data } = useData<AuditRow[]>(`/api/admin/audit?limit=1000${action ? `&action=${encodeURIComponent(action)}` : ""}`, () => true);
  const { data: chain } = useData<{ ok: boolean; brokenAt?: number }>("/api/admin/audit/verify", () => false);
  const rows = [...(data ?? [])].reverse();
  return (
    <AdminShell title="Audit log">
      <div className="row" style={{ alignItems: "flex-end" }}>
        <div className="grow col" style={{ gap: 6 }}>
          <h1 className="display">Audit log</h1>
          <p className="lead">Every sign-in, grant, approval and agent action, with who asked and who approved. {chain ? (chain.ok ? "The hash chain verifies." : `The hash chain is broken at record ${chain.brokenAt}.`) : ""}</p>
        </div>
        <a className="btn" href="/api/admin/audit/export?format=jsonl">Export JSONL</a>
        <a className="btn" href="/api/admin/audit/export?format=cef">Export CEF</a>
      </div>
      <div className="pill-tabs" role="group" aria-label="Filter" style={{ alignSelf: "flex-start" }}>
        {[["", "Everything"], ["auth", "Sign-in"], ["grant", "Grants"], ["approval", "Approvals"], ["agent.file", "Files"], ["agent.gate", "Gates"], ["agent.permission", "Denials"], ["admin", "Admin"]].map(([k, l]) => (
          <button key={k} aria-pressed={action === k} onClick={() => setAction(k)}>{l}</button>
        ))}
      </div>
      <table className="table">
        <thead><tr><th style={{ width: 150 }}>When</th><th style={{ width: 260 }}>Who</th><th style={{ width: 220 }}>What</th><th>Detail</th></tr></thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={4}>Nothing recorded yet.</td></tr>}
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="tabular">{whenLabel(r.ts)}{new Date(r.ts).toDateString() === new Date().toDateString() ? "" : ` ${hhmm(r.ts)}`}</td>
              <td style={{ fontSize: 14, overflowWrap: "anywhere" }}>{r.actor}</td>
              <td><span className={`tag ${/denied|revoked|refused|excluded|stopped/.test(r.action) ? "tag-danger" : ""}`}>{r.action}</span></td>
              <td style={{ fontSize: 13, wordBreak: "break-word" }}>{r.target && r.target.startsWith("run-") ? <Link to={`/app/runs/${r.target}`}>{r.target}</Link> : r.target} {describe(r)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </AdminShell>
  );
}

function describe(r: AuditRow): string {
  const d = r.detail ?? {};
  if (d.path) return `${d.op ?? ""} ${String(d.path).split("/").pop()}${d.reason ? ` (${String(d.reason).replace(/_/g, " ")})` : ""}`;
  if (r.action === "agent.gate") return `${d.tool}: ${d.required ? `gated ${d.effects.join(", ")}` : "no gate"}`;
  if (d.kind) return `${d.kind}${d.scope?.length ? `: ${d.scope.join(", ")}` : ""}${d.refusal ? ` — ${d.refusal}` : ""}`;
  if (d.title) return d.title;
  if (d.request) return `“${d.request}”`;
  if (d.method) return `${d.method}${d.device ? ` on ${d.device}` : ""}`;
  if (d.name) return d.name;
  if (d.reason) return d.reason;
  return "";
}
