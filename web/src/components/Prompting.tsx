// The global prompt surface: the window dims, one composer takes over.
// Enter runs the request on the paired agent; Escape dismisses.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api, useData, useScreen, workflowMeta, type Workflow } from "../api";
import { Icon, type IconName } from "../icons";
import { useSession } from "../session";
import type { PromptContext } from "./Shell";

interface FilesResp { folders: { name: string; path: string; files: { name: string; kind: string }[] }[] }

export function Prompting({ seed, context: initial, onClose }: { seed: string; context?: PromptContext; onClose: () => void }) {
  const nav = useNavigate();
  const { me } = useSession();
  const [text, setText] = useState(seed);
  const [context, setContext] = useState<PromptContext | undefined>(initial);
  const { data: screen } = useScreen(me?.agents[0]?.id);
  const onScreen = screen?.screen?.watching ? screen.screen.window : null;
  const onScreenTitle = screen?.screen?.title ?? onScreen;
  const [active, setActive] = useState(-1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const { data: files } = useData<FilesResp>("/api/files", () => false);
  const { data: workflows } = useData<Workflow[]>("/api/workflows", () => false);
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => {
    // Grow with the text, one line at a time.
    const el = input.current;
    if (el) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; }
  }, [text]);

  const suggestions = useMemo(() => {
    const out: { icon: IconName; text: string; meta: string; context?: PromptContext; workflow?: Workflow }[] = [];
    // What is in front of the user comes first: it is usually what they mean.
    if (onScreen && context?.window !== onScreen) out.push({ icon: "monitor", text: "…the window open on screen", meta: onScreenTitle ?? onScreen, context: { window: onScreen, title: onScreenTitle ?? undefined } });
    // Then the saved workflow the words so far point at, or the one run most recently.
    const words = text.toLowerCase().split(/\W+/).filter((x) => x.length > 2);
    const wf = (workflows ?? []).find((w) => words.length && words.every((x) => w.name.toLowerCase().includes(x)))
      ?? (!text.trim() ? [...(workflows ?? [])].filter((w) => w.last_run).sort((a, b) => b.last_run!.created_at - a.last_run!.created_at)[0] : undefined);
    if (wf) out.push({ icon: "workflow", text: `…the saved ${wf.name.charAt(0).toLowerCase() + wf.name.slice(1)} workflow`, meta: workflowMeta(wf), workflow: wf });
    for (const f of files?.folders ?? []) {
      const wb = f.files.find((x) => x.kind === "Workbook");
      if (wb) out.push({ icon: "file", text: `…the ${wb.name} workbook in the ${f.name}`, meta: f.name });
      out.push({ icon: "folder", text: `…the contracts in the ${f.name}`, meta: f.files.length ? `${f.files.length} files touched` : "Granted folder" });
    }
    if (me?.agents[0]?.grants.input.granted) out.push({ icon: "globe", text: "…the billing portal, stopping before it submits", meta: "Browser" });
    return out.slice(0, 3);
  }, [files, me, onScreen, onScreenTitle, context, workflows, text]);

  const agent = me?.agents[0];
  const scope = agent
    ? [agent.grants.files.granted && "your granted folders", agent.grants.input.granted && "allowed web portals"].filter(Boolean).join(" and ")
    : "";

  async function submit(request: string, about: PromptContext | undefined = context, workflow?: Workflow) {
    if ((!request.trim() && !workflow) || busy) return;
    setBusy(true);
    setError("");
    try {
      const body = workflow ? { workflow_id: workflow.id } : { request, ...(about ? { context: about } : {}) };
      const r = await api<{ run_id: string }>("/api/runs", { body });
      onClose();
      nav(`/app/runs/${r.run_id}`);
    } catch (e) {
      setError((e as ApiError).message);
      setBusy(false);
    }
  }

  function key(e: KeyboardEvent) {
    if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, suggestions.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, -1)); }
    else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const s = active >= 0 ? suggestions[active] : null;
      if (s?.context) { setContext(s.context); setActive(-1); return; }
      if (s?.workflow) { void submit("", undefined, s.workflow); return; }
      void submit(s ? `${text.trim()} ${s.text.replace(/^…/, "")}`.trim() : text);
    }
  }

  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label="Ask Ondo" onKeyDown={key}>
      <div className="overlay-scrim qm-fade" onClick={onClose} />
      <div className="overlay-edge qm-edge" />
      <div className="overlay-scan qm-fade"><div className="qm-scan" /></div>
      <div className="overlay-stack" style={{ pointerEvents: "none" }}>
        <div className="composer qm-rise" style={{ pointerEvents: "auto" }}>
          <div className="row" style={{ padding: "20px 24px", alignItems: "flex-start" }}>
            <Icon name="chat" size={20} color="var(--accent)" style={{ marginTop: 5 }} />
            <label htmlFor="prompt" className="sr-only">Ask Ondo, or say what to do</label>
            <textarea id="prompt" ref={input} rows={1} value={text} onChange={(e) => setText(e.target.value)} placeholder="Say what you need" autoComplete="off" />
            <span className="caption" style={{ marginTop: 6, whiteSpace: "nowrap" }}>{busy ? "Starting…" : "Enter to run"}</span>
          </div>
          {context && (
            <div className="row" style={{ padding: "0 24px 14px", gap: 8 }}>
              <span className="context-chip" style={{ padding: "4px 6px 4px 10px" }}>
                <Icon name="monitor" size={15} color="var(--accent)" />
                <span>About {context.title ?? context.window}</span>
                <button type="button" className="chip-x" aria-label="Ask without the screen" onClick={() => setContext(undefined)}>
                  <Icon name="close" size={13} />
                </button>
              </span>
            </div>
          )}
          {error && <p className="error-text row" style={{ padding: "0 24px 12px" }} role="alert"><Icon name="close" size={16} />{error}</p>}
          {suggestions.length > 0 && <>
            <div className="divider" />
            <div className="col" style={{ padding: "12px 16px 16px", gap: 2 }}>
              <span className="eyebrow-sm" style={{ padding: "4px 8px" }}>Continue with</span>
              {suggestions.map((s, i) => (
                <button key={s.text} type="button" className={`suggestion qm-rise qm-d${i + 1}${i === active ? " active" : ""}`}
                  onClick={() => (s.context ? setContext(s.context) : s.workflow ? submit("", undefined, s.workflow) : submit(`${text.trim()} ${s.text.replace(/^…/, "")}`.trim()))}>
                  <Icon name={s.icon} size={18} color="var(--ink-muted)" />
                  <span className="grow">{s.text}</span>
                  <span className="caption">{s.meta}</span>
                </button>
              ))}
            </div>
          </>}
        </div>
        <div className="overlay-pill qm-rise qm-d4" style={{ pointerEvents: "auto" }}>
          <span className="dot dot-rail" />
          <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{!agent?.connected ? "The desktop agent is not connected"
            : onScreen ? `Ondo can see ${onScreenTitle} while this is open`
            : `Ondo works in ${scope || "nothing yet: grant a folder first"}`}</span>
          <span className="caption rail-muted" style={{ whiteSpace: "nowrap" }}>Esc to dismiss · Esc twice to stop {onScreen ? "watching" : "running tasks"}</span>
        </div>
      </div>
    </div>
  );
}
