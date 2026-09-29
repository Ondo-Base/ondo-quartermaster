# Ondo Quartermaster

An on-computer assistant for enterprise operations teams: it reads the files a
team already works in, operates web portals through their accessibility tree, and
stops for a person before anything is submitted, sent, overwritten or paid.

This repository implements **Stages 0 to 6** of
[`docs/implementation-plan.md`](docs/implementation-plan.md) against the screens in
[`design/`](design/README.md).

```
agent/    Python desktop agent: harness, model layer, decision layer, tools (files, browser, desktop, screen, connectors), permission broker
server/   TypeScript control plane: identity, device trust, pairing, grants, runs, audit, SIEM, SCIM
web/      React web UI: landing, login flow, workspace, task run, files, admin console
design/   The design canvas export (source of record for layout, colour and copy)
docs/     The implementation plan, and tool docs generated from the schema
deploy/   A sample LiteLLM gateway config
```

## How the pieces fit

```
Browser ── HTTPS + SSE ──> Control plane (server/) <── websocket, agent dials out ── Desktop agent (agent/)
                            identity, policy, audit,                                  harness + event log
                            approvals, run history                                    model layer ──> gateway ──> any provider
                                                                                      decision layer (gates, screening)
                                                                                      tools: files, browser (Playwright MCP),
                                                                                        desktop (accessibility tree),
                                                                                        screen (pixels, last resort; grounding
                                                                                        by local OCR or a self-hosted model),
                                                                                        connectors (MCP: ticketing, mail,
                                                                                        calendar, team sites)
                                                                                      permission broker (3 grants, kill switch)
```

- **Log-first harness.** Every run is one append-only, hash-chained event stream
  (`agent/src/ondo_agent/log.py`). The model's context is rebuilt from it on every
  turn, and each event names its source, so the web UI's trajectory view, the audit
  export and replay all read the same record.
- **Model-agnostic.** Tools are defined once (`tools/spec.py`) and generated into
  each wire format at the adapter. Two formats are wired and tested from day one:
  OpenAI-compatible chat (LiteLLM, OpenRouter, vLLM) and the Messages API. What
  differs between models is a capability profile (`agent/config/profiles.yaml`),
  not a branch in the loop.
- **Enforcement is configuration, not prompt text.** Grants, policy exclusions,
  origin allowlists, gate rules and budgets live in `ondo.yaml` and in org policy
  on the control plane, and are enforced by the broker and the gates whatever
  model is loaded.
- **Gates on effect.** Deterministic rules decide first; a decision model is a
  second net for what nobody enumerated; uncertainty escalates; the model can never
  lower a gate a rule raised; a run that read flagged content gates everything.
  Every file write shows a diff first.

## Running it

Requirements: Python 3.11+, Node 22+ (for `node:sqlite`), and a Chromium for the
browser rung (`npx playwright install chromium`, or set `ONDO_CHROMIUM`). For the
desktop rung on Linux: `apt install python3-pyatspi gir1.2-atspi-2.0 gir1.2-gtk-3.0`
(plus `xvfb xdotool` to run its tests), and a venv that can see them. For the screen
rung (pixels): `apt install tesseract-ocr` for local OCR and grounding, and
`python3-gi-cairo` for the sample remote-session window.

```sh
npm install                                   # server, web, and Playwright MCP
cd agent && python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e ".[dev,desktop]" && cd ..
```

Local demo, no model provider needed (the `demo` profile is a labelled, scripted
stand-in that acts only on what the tools return):

```sh
# 1. the control plane, in development mode with the sample organisation
cd server && ONDO_DEV=1 ONDO_SEED=demo npx tsx src/main.ts        # http://localhost:8787

# 2. the web UI (proxies to the control plane)
npm run dev --workspace web                                        # http://localhost:5173

# 3. the sample billing portal and client drive
cd agent && .venv/bin/ondo-agent portal &                          # http://127.0.0.1:8765
.venv/bin/ondo-agent demo-data demo
cp ondo.example.yaml ondo.yaml                                     # set ONDO_CHROMIUM if needed
#    (ONDO_MODEL_PROFILE=demo keeps this scripted; the config defaults to openrouter)
```

Sign in as `mara.okonjo@northwind-ops.com` (password `quartermaster-demo`, or use
the development identity provider). The verification code is in the development
outbox linked from the Verify screen. The Pair screen shows a one-time code:

```sh
cd agent
.venv/bin/ondo-agent pair --server http://localhost:8787 --code 123-456
.venv/bin/ondo-agent connect
```

Grant the folder `agent/demo/Northwind client drive` (absolute path) and "Type
and click for you", then try:

- *Build the Q3 renewal pack for Northwind from the contracts in the client folder, and flag anything that uplifts above five per cent.*
- *Why did row 14 not match the contract?* (in the assistant panel)
- *Key the Q3 renewal changes from the signed contracts into the billing portal. Stop before submitting.*
- *Update Halleck Logistics' annual value in the legacy billing app from the signed contract.*
  Needs `desktop.enabled: true`, `ondo-agent legacy-app` running on the same
  desktop, and the screen grant for the window "Legacy billing".
- *Update Halleck Logistics' annual value in the remote billing session from the signed contract.*
  The same change in a window that is only pixels (`ondo-agent remote-app`, a
  stand-in for Citrix). Needs `screen.enabled: true` as well, and the screen
  grant for "Remote billing". With that window in front, the assistant panel
  offers **Ask about this screen**.
- *Reply to Halleck's renewal ticket with the new annual value from the signed contract, and mark it as waiting on the customer.*
  Uses the ticketing connector (`connectors.ticketing` in `ondo.example.yaml`,
  the sample service desk in `demo/ticketing_server.py`). The first use asks
  you to allow Ticketing. The public reply and the status change each stop for
  approval with the exact text and before and after. **Files and connections**
  shows who allowed it, with **Revoke**.
- *Answer Priya's email with the renewal value from the signed contract, and offer her a 30-minute call next week.*
  Uses the mail and calendar connectors (the samples in
  `demo/workplace_servers.py`). Drafting is free; sending the reply and
  inviting Priya each stop for approval, and her address is flagged as outside
  the organisation. The team-sites connector works the same way: changing a
  shared document shows a diff and who changed it last.
- *Reconcile receivables for September.* Uses the sample ledger connector: the
  reconciliation runs on the ledger's side and the step shows its progress. Kill
  the agent while it runs and start it again: the task carries on and waits for
  the same reconciliation.
- **Testing on Windows and macOS.** `docs/testing-on-windows-and-mac.md` walks through the automated tests, `ondo-agent platform-check` against a native app, every task above end to end, and deployment. Start there before relying on either platform.
- **Deploying with Intune.** See `docs/deploy-windows.md`: an enrollment token
  from the admin console, the ADMX policy template, and the Win32 app built by
  `deploy/windows/build.ps1`.
- **Saved workflows.** On a finished task, **Save as workflow** keeps its request
  under a name. It then appears under **Saved workflows** in the rail and as a
  suggestion in the prompt overlay. Running one starts an ordinary task: same
  grants, gates and approvals. Its page lets you edit the request for one run or
  for good, and lists its past runs.

Sign in as `it.admin@northwind-ops.com` for the admin console: revoke a grant
while a task waits at an approval and watch the run stop.

With a real model: the default profile is `openrouter`, so a single
`OPENROUTER_API_KEY` is enough (see `.env.example`; the slugs are
`ONDO_ORCHESTRATOR_MODEL` and `ONDO_EXTRACTOR_MODEL`). To move off it later, run a
LiteLLM gateway (`deploy/litellm.yaml`) with `ONDO_MODEL_PROFILE=gateway` and
`ONDO_GATEWAY_URL`/`ONDO_GATEWAY_KEY`, or use the `messages` profile. The terminal
works without the control plane:

```sh
.venv/bin/ondo-agent run "Why did row 14 not match the contract?"   # approvals asked on stdin
.venv/bin/ondo-agent trajectory .ondo/runs/<run>.jsonl               # every event and its source
.venv/bin/ondo-agent replay .ondo/runs/<run>.jsonl                   # re-run from recorded responses
.venv/bin/ondo-agent fork .ondo/runs/<run>.jsonl --at 12 --model messages
```

## Tests

```sh
cd agent && .venv/bin/pytest -q      # stages 0 to 6, and the control-plane integration
npm test                             # control plane (vitest) and web
npm run typecheck
```

The browser tests drive a real Chromium through Playwright MCP; the desktop tests
start a throwaway X display, D-Bus session and AT-SPI registry and drive a real
GTK app; the screen tests drive a canvas-only GTK window by screenshots, OCR and
synthesised input on that display; the integration tests start the real control plane. Each skips if its
dependencies are missing. CI (`.github/workflows/ci.yml`) runs everything, including both
model wire formats, and fails if the committed gate thresholds no longer match
their measurement.

## CI/CD

Every pull request runs lint and typecheck, the full test suite, a container
build, a secret scan, dependency review and audit, and CodeQL. Pushing a `v*`
tag re-runs all of it, then publishes the control-plane image to GHCR and the
agent to a GitHub release, both with signed provenance. Pre-commit hooks run the
same checks locally. See [`docs/ci-cd.md`](docs/ci-cd.md), including the branch
rules an admin needs to switch on to enforce them.

## Stages and their "done when"

| Stage | Done when (from the plan) | Where it is shown |
| --- | --- | --- |
| 0 · Skeleton, log, model layer | Ask about a local spreadsheet, replay the run from the log, re-run it on the second provider by changing config | `agent/tests/test_stage0.py` — same run through both wire formats, replay from the log, fork onto another model |
| 1 · Files, properly | "Build the renewal pack from these twelve contracts" end to end with no GUI automation, every write showing a diff first | `test_stage1.py` — 12 contracts + workbook read in one turn, diff → approval → write ordering asserted, refusals change nothing, exclusions and symlink escapes blocked |
| 1.5 · Decision layer | Thresholds from measured precision and recall, screening on every read, decisions accumulating as labels | `test_stage15.py`; `python -m ondo_agent.decision.calibrate` over 50 hand-labelled actions writes `agent/config/thresholds.json` |
| 2 · Login, policy, audit | An admin revokes a grant mid-run from the console and the run stops | `agent/tests/test_integration.py` (real server + real agent); `server/test/stage2.test.ts` |
| 3 · The browser | A task spanning the document store and a web portal completes with no screenshots, passing with a text-only model | `test_stage3.py` — contracts from the granted folder keyed into the portal through the accessibility tree, one approval with exact before/after values, no images anywhere, origins enforced before and after navigation |
| 4 · Semantic desktop control | A legacy app driven by element name, not coordinates, surviving a moved window and a rescaled display, picking its target without an orchestrator turn | `test_stage4.py` — a GTK billing app on a virtual display, run at 1× and 2× scale and moved and resized mid-task; the model names targets in words and the decision layer picks them; the submit is gated with the exact value; Escape twice (real keypresses) takes the keyboard back and stops the run. `test_integration.py` runs the same task through the control plane |
| 5 · Pixels, as the floor | A Citrix or remote-desktop window can be operated; the harness picks pixels only after trying the ladder; grounding can be switched to a locally hosted model without touching the executor | `test_stage5.py`: a window that is one canvas (the Citrix stand-in) operated at 1x and 2x, moved and resized mid-task, by a text-only model naming targets and by a vision model giving pixels in an 800-pixel screenshot. `screen_act` refuses until `desktop_inspect` found nothing to act on. The submit is gated with the typed value, and Escape is never sent. The same task passes with grounding pointed at a local UI-TARS-style endpoint by configuration alone. `test_integration.py` covers screen watching and "Ask about this screen" through the control plane |
| 6 · Connectors and scale | IT can deploy through Intune and control MCP access from Settings without talking to us. **Built:** per-connector consent; ticketing, mail, calendar, team-sites and ledger connectors; saved workflows; long-running tasks; managed policy, enrollment and Intune packaging. **Not built:** the ODR | `test_stage6.py`, `test_stage6_workplace.py`, against real MCP servers over stdio: consent once per connector, remembered until revoked; connectors policy does not allow are never started; every change gated with its exact values (recipients outside the organisation flagged, before and after, diffs); a ticket or email carrying instructions taints the run. `test_stage6_long.py`: a reconciliation waited for with progress; after the agent is killed, a new process waits for the same operation rather than starting another, and says honestly what became of each interrupted call. `test_stage6_managed.py`: device-management settings only narrow, and the ADMX template writes exactly what the agent reads. `test_integration.py`: consent and revocation from the web; a run surviving an agent restart through the control plane; an enrolled agent that does nothing until its person confirms it. `server/test/stage2.test.ts`: saved workflows |

## What is not done, or not verified here

Said plainly, per the plan's own rule about never claiming what is not there:

- **Real model providers were not called.** No API keys were available where this
  was built. Both wire formats are tested against recorded-shape fake endpoints,
  and the end-to-end tests use a scripted model that acts only on tool output. The
  first run against real providers should be the §9 eval set, not a demo.
- **Jev's endpoint is unverified**, as the plan flags. The adapter takes its URL
  from configuration and isolates the wire mapping in two functions; confirm both
  against TypeSafe's official docs before enabling it. The committed thresholds
  were measured on the rules baseline, which was tuned on the same 50 fixtures:
  re-measure on a held-out set, and against Jev, before trusting them.
- **SSO** (OIDC with PKCE, SAML) is implemented but was only exercised through the
  development identity provider; no IdP was reachable. MDM-backed device posture
  is not integrated (the "managed" flag is never set).
- **Only the Linux desktop backend (AT-SPI) has run.** The Windows (`pywinauto`,
  UI Automation) and macOS (`AXUIElement`) backends are written to the same
  interface but have never executed; both are marked UNTESTED in their modules.
  Run `test_stage4.py` on each platform, against a native test app, before
  relying on them. macOS's Accessibility permission cannot be granted for the
  user; the backend refuses to start and says where to allow it.
- **Element picking uses the rules baseline** (word overlap) unless a decision
  model is configured. It refuses vague targets rather than guessing, but real
  enterprise apps will need per-app profiles (`desktop.profiles`) for unnamed and
  duplicate controls.
- **The screen rung has only run on Linux (X11).** The Windows (`screen/win32.py`)
  and macOS (`screen/quartz.py`) screen backends are written to the same
  interface but have never executed; both are marked UNTESTED. Run
  `test_stage5.py` on each before relying on them. Wayland sessions are not
  supported: they do not allow one app to capture or drive another's window.
- **No real Citrix or RDP client was driven.** The remote session is a stand-in:
  a GTK window drawn on one canvas, which is how such a window looks locally, but
  real sessions add compression artefacts, latency and their own keyboard
  handling. Try one before promising it.
- **No real grounding model was run.** The vision grounder speaks the
  OpenAI-compatible API and parses the UI-TARS, OS-Atlas and Qwen-VL coordinate
  formats. It was tested against a local stand-in endpoint, not a UI-TARS or
  OS-Atlas deployment. The OCR grounder is real but narrow: it finds text and the
  input box beside a label, and refuses what it cannot see (icons, unlabelled
  controls) rather than guessing. Those need a vision grounder.
- **The prompt overlay is the web UI's**, over the Ondo window, not a native
  overlay drawn over other apps. Screen watching on Linux reads which window is
  active from AT-SPI; a window manager or toolkit that does not report it shows
  as nothing on screen.
- **Stage 6 is built, except the On-device Agent Registry (ODR).** The ODR is
  prerelease and its documentation could not be reached from here, so
  registering Ondo with it and using the built-in File Explorer and Settings
  connectors are not built. `docs/deploy-windows.md` says what is ready for
  them. The rest of "IT can deploy through Intune and control MCP access" is
  built: managed policy, enrollment and packaging. But it **has not run on
  Windows or on a real Intune tenant**. Start the manual **windows** workflow
  first.
- **The connectors were only run against sample servers** (`demo/ticketing_server.py`,
  `demo/workplace_servers.py`), not a real service desk, Exchange, Google
  Workspace, SharePoint or finance system. The streamable-HTTP transport
  (`url:`) is written but untested.
- **Long operations use declared tools, not the MCP Tasks extension.** The MCP
  SDK here (2.2) defines the Tasks types but implements neither side. So a
  connector's long operation is declared as a start tool plus a status tool,
  and the agent keeps the handle in the log. When servers speak Tasks natively,
  that is a new transport for the same `OperationDecl`.
- **Stage 6.5, the local decision model, is not started.**
- **Office round-trips**: `openpyxl` keeps formulas (and macros in `.xlsm`) but
  drops charts and images on save. The plan's small COM path for what the
  libraries cannot do is not built.
- **Placeholders** from the design (`[YOUR IDENTITY PROVIDER]`, `[YOUR MDM]`,
  `[YOUR REGION]`, testimonial and footer) are left as placeholders.
