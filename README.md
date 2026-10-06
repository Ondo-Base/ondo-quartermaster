# Ondo Quartermaster

An on-computer assistant for enterprise operations teams: it reads the files a
team already works in, operates web portals through their accessibility tree, and
stops for a person before anything is submitted, sent, overwritten or paid.

It is built from the design in [`docs/design.md`](docs/design.md)
and the screens in [`design/`](design/README.md). What is built, and how far
each part is verified, is [below](#what-is-built-and-how-far-it-is-verified).

```
agent/    Python desktop agent: harness, model layer, decision layer, tools (files, browser, desktop, screen, connectors), permission broker
server/   TypeScript control plane: identity, device trust, pairing, grants, runs, audit, SIEM, SCIM
web/      React web UI: landing, login flow, workspace, task run, files, admin console
design/   The design canvas export (source of record for layout, colour and copy)
docs/     The design rationale, guides for deployment and testing, and tool docs generated from the schema
deploy/   A sample LiteLLM gateway config, and Windows packaging (ADMX policy, Intune scripts, build)
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
cd agent && .venv/bin/pytest -q      # every capability, and the control-plane integration
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

## What is built, and how far it is verified

Organised by what the product does. For each capability: where the code and
tests are, what has been verified, and what has not. "Verified" means run
against the real thing in this repository's tests or CI, which run on Linux.
Nothing here has called a real model provider (see the first row).

| Capability | Code | Tests | Status |
| --- | --- | --- | --- |
| [Harness, event log, models](#harness-event-log-and-model-layer) | `harness/`, `log.py`, `models/` | `test_harness.py` | Verified with fake endpoints; **no real provider called** |
| [Files](#files) | `tools/files.py`, `tools/formats.py` | `test_files.py` | Verified |
| [Gates and screening](#gates-and-screening) | `gates.py`, `screening.py`, `decision/` | `test_decision.py` | Verified with the rules baseline; **Jev not called** |
| [Local decision model](#local-decision-model-laya) | `decision/laya.py`, `decision/systemone.py` | `test_local_decision_model.py` | Adapter verified; **no trained checkpoint, no bar** |
| [Sign-in, grants, policy, audit](#sign-in-devices-grants-policy-and-audit) | `server/` | `server/test/controlplane.test.ts`, `test_integration.py` | Verified; **SSO only against the development IdP** |
| [Browser](#browser) | `browser/` | `test_browser.py` | Verified |
| [Desktop (accessibility tree)](#desktop-accessibility-tree) | `desktop/` | `test_desktop.py` | Linux verified; **Windows and macOS never run** |
| [Screen (pixels) and screen watching](#screen-pixels-and-screen-watching) | `screen/`, `watch.py` | `test_screen.py`, `test_integration.py` | Linux X11 verified; **Windows and macOS never run** |
| [Connectors](#connectors) | `connectors/`, `demo/*_server.py` | `test_connectors.py`, `test_connectors_workplace.py` | Verified against sample servers only |
| [Saved workflows](#saved-workflows) | `server/src/routes/runs.ts`, `web/src/pages/Workflow.tsx` | `server/test/controlplane.test.ts` | Verified |
| [Long-running tasks and restarts](#long-running-tasks-and-restarts) | `harness/loop.py` (`resume`), `connectors/service.py` | `test_long_running.py`, `test_integration.py` | Verified |
| [Deployment](#deployment-managed-policy-enrollment-windows-packaging) | `managed.py`, `deploy/windows/` | `test_deployment.py`, `test_integration.py` | Logic verified; **never run on Windows or Intune**; ODR not built |
| [Platform check](#platform-check) | `platform_check.py` | `test_screen.py` | Linux verified |

Paths under `agent/` are relative to `agent/src/ondo_agent/`, and test files to
`agent/tests/`, unless they start with `server/` or `web/`.

### Harness, event log and model layer

One append-only, hash-chained event log per run is the only state. The model's
context is rebuilt from it every turn, and runs can be replayed, forked onto
another model, or resumed from it.

- **Verified:**
  - the same run through both wire formats (OpenAI-compatible chat and the
    Messages API), against recorded-shape fake endpoints;
  - replay from the log, and a fork onto another model;
  - the trajectory naming the source of every context injection.
- **Not verified:** **no real model provider has been called.** No API key was
  available where this was built. The first real run should be the eval set
  (`docs/design.md` §9), across models and both wire formats, recording cost, latency and
  success per model. The end-to-end tests use a scripted model (the `demo`
  profile) that acts only on what the tools return.

### Files

Reads and writes Office documents, PDFs and text in the folders a person
grants. Every write shows a diff and stops for approval first.

- **Verified:** 12 contracts and a workbook read in one turn; the order diff →
  approval → write; a refused write changes nothing; exclusions and symlink
  escapes are blocked; revoking files mid-run stops the file tools.
- **Not built:** `openpyxl` keeps formulas (and macros in `.xlsm`) but drops
  charts and images on save. The small COM path in `docs/design.md`, for what the libraries
  cannot do, is not built.

### Gates and screening

The order of authority:

1. Deterministic rules decide first.
2. Then a tool's declared effects. A declaration that names every effect a
   call can have settles the rest as absent.
3. Then the decision model, as a second net for what nobody enumerated.
4. A run that read flagged content gates every effect.

Every untrusted read (files, pages, windows, connector results) is fenced and
screened.

- **Verified:**
  - a rule raises a gate and the model cannot lower it;
  - uncertainty and outages escalate to a person;
  - injected content is fenced, flagged and taints the run;
  - thresholds come from `python -m ondo_agent.decision.calibrate` over 50
    hand-labelled actions (`agent/config/thresholds.json`), and CI fails if
    they drift.
- **Not verified:** **Jev has not been called for real.** It goes through
  OpenRouter's System One API (`https://openrouter.ai/api/v1/systemone`, model
  `typesafe/jev-1.13`, `OPENROUTER_API_KEY`). Its wire (`decision/systemone.py`,
  shared with Laya) has run against Laya's server and a fake endpoint, not
  against OpenRouter.
- **Caution:** the thresholds were measured on the rules baseline, which was
  tuned on the same 50 fixtures. Re-measure on a held-out set, and against
  Jev, before trusting them.

### Local decision model (Laya)

The same questions as Jev, answered inside the customer's network: a
`laya-serve` endpoint, or local weights. Logged decisions can be labelled and
exported for training. See [`docs/decision-local.md`](docs/decision-local.md).

- **Verified:**
  - Jev and Laya send identical requests on one wire;
  - public endpoints and Hub ids are refused, and environment proxies ignored;
  - decisions from a real run are labelled and exported, split by run;
  - calibration fails below the bar.
  - With Laya installed (the **Laya adapter** CI job), every fixture question
    is answered by Laya's own inference code and its own HTTP server, offline.
- **Not verified or not done:**
  - Only a tiny random checkpoint has run, because Hugging Face was not
    reachable; the manual **decision-measure** workflow can run real weights.
  - No checkpoint has been fine-tuned: logged decisions have no labels until a
    person adds them (`ondo-agent decisions label`).
  - Jev has not been measured, so there is no bar to meet yet.
  - The ONNX Runtime path has not run.
  - The element-choice half of the bar rests on 4 fixtures.

### Sign-in, devices, grants, policy and audit

The control plane provides:

- sign-in with SSO (OIDC with PKCE, SAML), and trusted devices;
- SCIM;
- pairing, and the three grants (files, screen, input);
- organisation policy, set in the admin console;
- a hash-chained audit log with SIEM export.

Agents dial out to it over a websocket.

- **Verified:** an admin revokes a grant mid-run and the run stops (a real
  server and a real agent in `test_integration.py`); policy refuses grants it
  excludes; the audit chain verifies.
- **Not verified:** SSO only through the development identity provider, since
  no real IdP was reachable. MDM-backed device posture is not integrated (the
  "managed" device flag is never set).
- **Placeholders** from the design (`[YOUR IDENTITY PROVIDER]`, `[YOUR MDM]`,
  `[YOUR REGION]`, the testimonial and the footer) are left as they are.

### Browser

Web portals through Playwright MCP and the accessibility tree, with no
screenshots, restricted to allowed origins. Submitting stops for approval.

- **Verified:**
  - contracts from a granted folder keyed into the sample portal by a
    text-only model;
  - one approval, with exact before and after values;
  - no images anywhere in the run;
  - origins enforced before and after navigation;
  - a refused submission leaves the portal untouched;
  - revoking input mid-run stops the browser.

### Desktop (accessibility tree)

Native apps are driven by element name, not coordinates, behind the screen and
input grants. Escape pressed twice takes the keyboard back, and the agent never
sends Escape itself.

- **Verified on Linux (AT-SPI):**
  - a GTK billing app at 1× and 2× scale, moved and resized mid-task;
  - targets named in words and picked by the decision layer;
  - the submit gated with the exact value;
  - stale elements re-queried once, and vague targets refused rather than
    guessed;
  - Escape twice, as real key presses, stopping the run.
- **Not verified:** **the Windows (`desktop/uia.py`) and macOS (`desktop/ax.py`)
  backends have never run.** Use
  [`docs/testing-on-windows-and-mac.md`](docs/testing-on-windows-and-mac.md).
  macOS's Accessibility permission cannot be granted for the user; the backend
  refuses to start and says where to allow it.
- **Limits:** element picking uses the rules baseline (word overlap) unless a
  decision model is configured. Real enterprise apps will need per-app
  profiles (`desktop.profiles`) for unnamed and duplicate controls.

### Screen (pixels) and screen watching

The last resort, for windows with no usable accessibility tree (Citrix, RDP):

- screenshots, a pointer and a keyboard;
- grounding by local OCR or a self-hosted vision model;
- used only after the tree was inspected and found empty.

Screen watching reports which shared window is in front, for **Ask about this
screen**.

- **Verified on Linux (X11):**
  - a one-canvas window, standing in for Citrix, operated at 1× and 2×, moved
    and resized mid-task;
  - by a text-only model naming targets, and by a vision model giving pixel
    positions;
  - `screen_act` refuses until `desktop_inspect` found nothing to act on;
  - the submit is gated, and Escape is never sent;
  - grounding switched to a local UI-TARS-style endpoint by configuration
    alone;
  - screen watching through the control plane.
- **Not verified:**
  - **The Windows (`screen/win32.py`) and macOS (`screen/quartz.py`) backends
    have never run.**
  - No real Citrix or RDP client has been driven: real sessions add
    compression artefacts, latency and their own keyboard handling.
  - No real grounding model has run. The vision grounder parses the UI-TARS,
    OS-Atlas and Qwen-VL formats, but was tested against a stand-in endpoint.
  - The OCR grounder is real but narrow: it refuses icons and unlabelled
    controls rather than guessing.
- **Limits:**
  - Wayland is not supported: it does not let one app capture or drive
    another's window.
  - The prompt overlay is the web UI's, not a native overlay over other apps.

### Connectors

Mail, calendar, team sites, ticketing and a ledger, reached over MCP. How they
are governed:

- Each connector is allowed by the person on its first use, and only if the
  organisation's policy (and device management) allows it. Revoking it stops
  the runs using it.
- Every tool is declared in `connectors/catalog.py`: its effect, and the exact
  values a person approves. Undeclared tools are not offered, and the server's
  own descriptions are never shown to the model.
- Results are fenced and screened.

- **Verified**, against real MCP servers over stdio:
  - consent once per connector, remembered until revoked, and "no" final for
    the run;
  - a connector policy does not allow is never started;
  - every change gated with its exact values: recipients outside the
    organisation flagged, before and after, diffs;
  - a ticket or email carrying instructions taints the run;
  - consent and revocation from the web, through the control plane.
- **Not verified:**
  - Only the sample servers (`demo/ticketing_server.py`,
    `demo/workplace_servers.py`) have been used, not a real service desk,
    Exchange, Google Workspace, SharePoint or finance system.
  - The streamable-HTTP transport (`url:`) has not run.

### Saved workflows

A finished task's request can be saved by name. Saved workflows appear in the
rail and as prompt-overlay suggestions, and can be run as is or edited for one
run. Each run is an ordinary task.

- **Verified:** saving from a run, running by name or with an edited request,
  the list of past runs, privacy to the owner, and the audit trail.

### Long-running tasks and restarts

A run survives the agent process ending:

- On reconnect, the control plane has the agent resume active runs from their
  logs, and expires their old pending approvals.
- `Harness.resume()` says honestly what became of each interrupted call.
- A connector's long operation (`OperationDecl`) is waited for by its handle,
  with progress shown, and after a restart the same operation is waited for
  rather than started again.

- **Verified:**
  - a reconciliation waited for, with progress;
  - the harness killed mid-operation, then a new process finishing on the same
    handle;
  - an unapproved change reported as not having happened;
  - through the whole stack, two runs surviving an agent restart.
- **Not built:** the MCP Tasks extension itself. The MCP SDK here (2.2) defines
  its types but implements neither side, so long operations use declared start
  and status tools. Native Tasks would be a new transport for the same
  `OperationDecl`.

### Deployment: managed policy, enrollment, Windows packaging

- **Managed policy:** the agent reads what Intune or Group Policy writes (the
  registry), or a macOS configuration profile. It only ever narrows: allowed
  and disabled connectors, grants, exclusions, screen watching and pixels.
- **Enrollment:** IT puts an enrollment token in device management. The agent
  sets itself up for the signed-in person, and does nothing until that person
  confirms the computer is theirs.
- **Windows packaging:** `deploy/windows` holds the ADMX template, Intune
  scripts and `build.ps1`. See [`docs/deploy-windows.md`](docs/deploy-windows.md).

- **Verified:**
  - managed settings only narrow, in the agent's config and on the
    organisation's policy;
  - the ADMX template writes exactly the value names the agent reads;
  - the deployed configuration loads and grants nothing;
  - enrollment through the control plane: wrong token, unknown person,
    confirm, "Not mine", and a revoked token.
- **Not verified:**
  - **None of it has run on Windows or on a real Intune tenant:** the registry
    reader, `whoami /upn`, the PowerShell scripts, the PyInstaller build and
    the ADMX import. Run the manual **windows** workflow, and
    [`docs/testing-on-windows-and-mac.md`](docs/testing-on-windows-and-mac.md).
  - The macOS profile reader has not run.
- **Not built:**
  - registering with the Windows On-device Agent Registry (ODR), and using its
    built-in File Explorer and Settings connectors. The ODR is prerelease, and
    its docs were unreachable; see `docs/deploy-windows.md`;
  - a macOS installer.

### Platform check

`ondo-agent platform-check --window <name>` runs the platform-specific pieces
against one open window, and prints PASS, FAIL or SKIP for each:

- the desktop and screen backends;
- OCR;
- the Escape-twice listener;
- the managed-policy reader.

- **Verified on Linux** against the sample app, where every check passes.
  It is the first step on Windows and macOS.

### Code that is not used

`agent/src/ondo_agent/log/` and `agent/src/ondo_agent/model/` are never
imported: `log.py` and `models/` shadow them. They are waiting on the owner's
OK to delete.
