import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, useData, whenLabel, type Grants, type Me, type Policy } from "../api";
import { BackHome, Crumbs, RailFoot, RailHead, useShell } from "../components/Shell";
import { Icon } from "../icons";
import { useSession } from "../session";

interface FileRow { path: string; name: string; kind: string; status: string; last_touched: number; last_read: number }
interface Folder { path: string; name: string; files: FileRow[]; read_this_week: number }
interface FilesResp { folders: Folder[]; grants: Grants | null; policy: Policy; read_this_week: number }

export function Files() {
  const { me } = useSession();
  const { data } = useData<FilesResp>("/api/files", (e, d) => e === "grants" || (e === "run_event" && d?.type === "file_access"));
  const [selected, setSelected] = useState(0);
  const [q, setQ] = useState("");
  const { toast } = useShell();
  // Connectors and who allowed them, kept live: a consent given in a run shows up here.
  const { data: live, reload: reloadLive } = useData<Me>("/api/me", (e) => e === "grants" || e === "agent");
  if (!me || !data) return null;
  const agent = (live ?? me).agents[0];
  const connectors = agent?.connectors ?? [];
  async function revoke(id: string, name: string) {
    try {
      await api(`/api/agents/${agent!.id}/connectors/${id}`, { method: "DELETE" });
      toast(`${name} revoked. Any task using it stops, and the next one asks you again.`);
      void reloadLive();
    } catch (e) {
      toast((e as ApiError).message);
    }
  }
  const folder = data.folders[selected] ?? null;
  const rows = (folder?.files ?? []).filter((f) => !q || f.name.toLowerCase().includes(q.toLowerCase()));
  const excluded = data.policy.excluded_windows ?? [];
  const input = data.grants?.input.granted;

  return (
    <div className="shell">
      <nav className="rail" aria-label="Folders">
        <RailHead />
        <div className="rail-body" style={{ paddingTop: 16 }}>
          <BackHome />
          <div className="col" style={{ gap: 8 }}>
            <span className="rail-label">Granted folders</span>
            <div className="rail-list">
              {data.folders.length === 0 && <span className="caption rail-muted" style={{ padding: "0 12px" }}>No folders granted.</span>}
              {data.folders.map((f, i) => (
                <button key={f.path} onClick={() => setSelected(i)} className={`rail-row${i === selected ? " active" : ""}`} style={{ background: i === selected ? undefined : "transparent", border: "none", textAlign: "left", cursor: "pointer" }}>
                  <Icon name="folder" size={16} color={i === selected ? "var(--rail-accent)" : "var(--on-rail-muted)"} />
                  <span className="t grow">{f.name}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="rail-card" style={{ marginTop: "auto", marginBottom: 16, gap: 6 }}>
            <span className="rail-label">Excluded by policy</span>
            <span style={{ fontSize: 14, lineHeight: 1.5, color: "var(--on-rail)" }}>{excluded.join(" · ") || "Nothing excluded"}</span>
            {data.policy.excluded_paths?.length > 0 && <span className="caption rail-muted">Files matching {data.policy.excluded_paths.join(", ")} stay closed too.</span>}
            <span className="caption rail-muted">Set by your administrator. Not grantable here.</span>
          </div>
        </div>
        <RailFoot />
      </nav>

      <main className="main">
        <header className="topbar">
          <Crumbs items={[{ label: "Home", to: "/app" }, { label: "Files and connections" }]} />
          <span className="grow" />
          <label className="pill-search" style={{ width: 280 }}>
            <Icon name="search" size={15} color="var(--ink-muted)" />
            <span className="sr-only">Search files Ondo has touched</span>
            <input placeholder="Search files Ondo has touched" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <Link to="/pair" className="btn">Add a folder</Link>
        </header>

        <div className="canvas" style={{ padding: 32, flexDirection: "row", gap: 24, alignItems: "flex-start" }}>
          <section className="grow col" style={{ gap: 16 }}>
            {folder ? (
              <>
                <div className="col" style={{ gap: 4 }}>
                  <h1 className="section-heading">{folder.name}</h1>
                  <p className="ui muted">{folder.files.length} file{folder.files.length === 1 ? "" : "s"} touched · Ondo has read {folder.read_this_week} of them this week</p>
                </div>
                <table className="table">
                  <thead><tr><th>Name</th><th style={{ width: 120 }}>Type</th><th style={{ width: 180 }}>Last touched</th><th style={{ width: 150 }}>Ondo</th></tr></thead>
                  <tbody>
                    {rows.length === 0 && <tr><td colSpan={4}>Ondo has not opened anything here yet.</td></tr>}
                    {rows.map((f) => (
                      <tr key={f.path}>
                        <td className="name" title={f.path}>{f.name}</td>
                        <td>{f.kind}</td>
                        <td className="tabular">{whenLabel(f.last_touched)}</td>
                        <td><span className={`tag ${f.status === "Edited" ? "tag-accent" : f.status === "Excluded" ? "tag-outline" : ""}`}>{f.status}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="ui secondary">Ondo only opens a file inside a task you started, and every open is written to the audit log.{me.user.role === "admin" && <> <Link to="/admin/audit">Open the audit log</Link>.</>}</p>
              </>
            ) : (
              <div className="col" style={{ gap: 8 }}>
                <h1 className="section-heading">No folders granted</h1>
                <p className="ui secondary">Ondo cannot open any file until you name a folder. <Link to="/pair">Grant a folder</Link>.</p>
              </div>
            )}
          </section>

          <aside style={{ width: 340, flexShrink: 0, display: "flex", flexDirection: "column", gap: 16 }}>
            <div className="card">
              <div className="card-head" style={{ padding: "0 16px" }}><span className="eyebrow-sm">Connected apps</span></div>
              <App icon="folder" title="Document store" meta={`${data.folders.length} granted folder${data.folders.length === 1 ? "" : "s"} · read, and write with approval`} />
              <div className="divider" />
              <App icon="globe" title="Web portals" meta={input ? "Through the browser. Only origins your administrator allows; submitting needs approval." : "Off. Grant “Type and click for you” to use them."} />
              {connectors.map((c) => (
                <div key={c.id}>
                  <div className="divider" />
                  <App icon="plug" title={c.name}
                    meta={!c.allowed_by_policy ? "Not allowed by your administrator"
                      : c.consent ? `Allowed by you ${whenLabel(c.consent.at).toLowerCase()} · reads freely, every change asks you first`
                      : "Asks you the first time a task uses it"}
                    muted={!c.allowed_by_policy || !c.consent}
                    action={c.consent ? <button type="button" className="link-btn" style={{ fontSize: 13, fontWeight: 600 }} onClick={() => revoke(c.id, c.name)}>Revoke</button> : undefined} />
                </div>
              ))}
              {!connectors.some((c) => c.id === "mail") && <><div className="divider" /><App icon="mail" title="Mail and calendar" meta="Not connected" muted /></>}
            </div>
            <div className="card card-pad" style={{ padding: 16, gap: 8 }}>
              <span className="eyebrow-sm">Screen access</span>
              <p className="ui">{data.grants?.screen.granted && me.agents[0]?.capabilities?.screen
                ? `Ondo can read ${data.grants.screen.scope.length ? data.grants.screen.scope.join(", ") : "the windows you share"} through their accessibility tree, only inside a task you started${me.agents[0]?.capabilities?.pixels ? ". Where a window has no tree, it takes screenshots, which stay on this computer" : ". It takes no screenshots"}. Nothing else on your desktop is read.`
                : "Ondo is not reading any window. Nothing on your desktop is captured."}</p>
              <Link to="/pair" style={{ fontSize: 15 }}>Change what it can do</Link>
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
}

function App({ icon, title, meta, muted, action }: { icon: "folder" | "globe" | "mail" | "plug"; title: string; meta: string; muted?: boolean; action?: ReactNode }) {
  return (
    <div className="row" style={{ padding: "14px 16px", gap: 12 }}>
      <Icon name={icon} size={18} color={muted ? "var(--ink-muted)" : "var(--accent-hover)"} />
      <span className="grow col">
        <span style={{ fontSize: 15, fontWeight: 600, color: muted ? "var(--ink-secondary)" : "var(--ink)" }}>{title}</span>
        <span className="caption">{meta}</span>
      </span>
      {action}
    </div>
  );
}
