# Ondo Quartermaster — design

Why the product in `design/` is built the way it is: an on-computer assistant
for enterprise operations teams that reads files, watches the screen, controls
the keyboard and pointer, and acts through connected apps. Written 25 September
2026, before it was built, and kept for its reasoning. What is built, and how
far each part is verified, is in the [README](../README.md#what-is-built-and-how-far-it-is-verified).

**This plan is model-agnostic.** No provider's SDK, tool schema or proprietary
feature appears in the core of the system. The model is a replaceable part
behind an interface you own. That is a product requirement, not a preference:
enterprise buyers ask which model you use, whether it can run in their tenancy,
and what happens when they want a different one. "Yours, ours, or one you host
yourself" is a better answer than any single vendor's name.

---

## 1. The one idea that shapes everything

There are three ways for software to touch a computer. They are not equal:

| Way | How it works | Reliability | Use it |
| --- | --- | --- | --- |
| **Structured** | An API, an MCP server, a file format library | Highest. No pixels, no guessing. | Always, when it exists |
| **Semantic** | The accessibility tree — the OS's own list of windows, buttons and fields, with names and roles | High. Survives theme changes, window moves, font sizes. | When there is no API |
| **Pixels** | Screenshot, find the thing, click coordinates | Lowest. Breaks on resolution, scaling, animation, scroll position. | Last resort only |

**Build the ladder, and always climb down it in order.** Most products in this
space start at pixels because a screenshot demo is easy. That is the trap: the
demo works and the fiftieth run does not. Try structured first, semantic second,
pixels only when the other two have nothing to offer — usually a Citrix window, a
remote desktop, or a custom-drawn legacy app.

The ladder is also what makes the product model-agnostic in practice. Pixel
control needs a strong vision model and is where providers differ most. Structured
and semantic control need a model that can call tools and read text, which is
table stakes everywhere. **The higher you sit on the ladder, the less your choice
of model matters** — and the cheaper, faster and more deterministic the product
gets. Every rung you climb widens the set of models that can run your workload.

---

## 2. The pieces

Four components. Keep them separate from day one; they have different release
cadences and different security reviews.

```
┌─────────────────────────────────────────────────────────┐
│  Web UI (browser)                                        │
│  React + TypeScript. The screens in design/.             │
│  Talks to the control plane only. Never to the desktop.   │
└────────────────────────┬────────────────────────────────┘
                         │ HTTPS + SSE
┌────────────────────────┴────────────────────────────────┐
│  Control plane (server)                                  │
│  Identity, policy, the audit log, run history,           │
│  approval gates, connector credentials.                   │
│  System of record. Owns nothing that runs on the desktop. │
└────────────────────────┬────────────────────────────────┘
                         │ authenticated websocket, agent dials out
┌────────────────────────┴────────────────────────────────┐
│  Desktop agent (the user's machine)                      │
│  ├── Harness: the agent loop, event log, context mgmt    │
│  ├── Model layer: adapters + gateway (swappable)          │
│  ├── Decision layer: typed gates, element pick, screening │
│  ├── Tool layer: files, accessibility, input, browser,    │
│  │   MCP client, office documents                         │
│  └── Permission broker: the three grants, kill switch     │
└─────────────────────────────────────────────────────────┘
                         │
              Model gateway → any provider,
              or a model hosted inside the customer
```

**Language:** Python for the desktop agent and the harness — every OS automation
binding worth having is Python-first (`pywinauto`, `pyobjc`, `pyautogui`,
`openpyxl`, `python-docx`). TypeScript for the web UI and the control plane. If
you would rather have the orchestrator in TypeScript, almost nothing below
changes; the tool layer stays Python either way because the bindings are there.

**Why the agent dials out:** no inbound ports on user machines, no firewall
exceptions, no VPN requirement. One authenticated websocket, held open.

---

## 3. The harness

> *"An agent is a model plus a harness — the runtime that couples an LLM to the
> world through a loop, tools, context management, safety controls,
> orchestration, and extension surfaces."*
> — [Harness Engineering: a source-code study of eleven systems](https://arxiv.org/abs/2609.00006) (2026)

That paper is the best starting reference for this project, because it is a
source-code study of eleven production agents — Claude Code, Codex CLI, Gemini
CLI, Mistral Vibe, OpenHands, Aider, Mini-SWE-Agent, Hermes, Pi, OpenCode and
OpenClaw — across every major vendor and several independents. It finds seven
canonical subsystems, 29 recurring design patterns, and two facts that should
settle arguments early:

- **None of them import a general-purpose agent framework.** All eleven
  hand-roll an async loop. Do the same. The loop is 300 lines, it is the part you
  will tune most, and a framework's abstractions will be in the way by month two.
- **None of them use vector embeddings for code retrieval.** Retrieval is
  deterministic — globs, greps, direct reads. The same applies to files here: the
  user said "the contracts in the client folder", so list that folder and read
  them. Do not build a vector index to find a file whose path the user gave you.

### The structure to copy: log-first

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (MIT,
open-sourced August 2026) is built on "everything is a plugin" — the chat
surface, the tool calls, the context injection and the agent loop itself are all
swappable. Three decisions are worth copying outright:

1. **An append-only session log as the single source of truth.** Everything the
   model saw is one event stream — system prompts, reasoning, tool calls, tool
   results, subagent scheduling, every context injection. Nothing is derived
   state; the UI, the audit export and replay all read the same stream.
2. **A trajectory view that names the source of every injection.** If a system
   prompt is in context, the view says which component put it there. For an
   enterprise product this is not a debugging luxury — it *is* the audit trail the
   security review will ask for, and the `TaskRun` screen in `design/` is already
   drawn against it.
3. **Resume, fork, search and replay from the log.** A failed run can be replayed
   against the same history with one tool changed — or **one model swapped**,
   which is how you evaluate a provider change without asking a customer to
   reproduce anything.

Point 3 is the agnosticism payoff. A log-first harness makes "would Model B have
done better here?" a question you can answer from recorded runs.

### The prompting patterns

Published system prompts are the best prompting textbook available, and several
vendors' are now documented and tracked — see
[Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts)
(MIT, updated per release) and
[asgeirtj/system_prompts_leaks](https://github.com/asgeirtj/system_prompts_leaks),
which covers Anthropic, OpenAI, Google, xAI and others side by side. Read them
for the patterns, not the text — the text is tuned to a specific model and will
not transfer, but the structure does:

- **Run-loop prompting.** The model runs until a clear stopping condition. Say
  what "done" is, explicitly.
- **Parallel tool calls, stated as a rule.** Independent reads go in one turn.
  Biggest latency win in a file-heavy task, and models do it reliably only when
  told to.
- **Boundary signaling.** Mark what is a system limit, what is a tool result, and
  what is the user's text. Here this matters twice over: **file contents and
  screen text are untrusted input** and must be fenced as data, never merged into
  instructions.
- **Layered context management.** A proactive layer watching token counts, a
  reactive layer catching context-length errors, a bounded-memory layer, and a
  collapse layer for verbose tool output. Build the proactive and collapse layers
  first — screenshots and spreadsheet dumps are exactly what the collapse layer
  is for.
- **Subagents are the same harness with a different system prompt.** Delegation
  lives in prompts, not in a framework. "Read these twelve contracts and return
  the uplift terms" is a subagent with a narrow prompt and a smaller model.
- **Write tool descriptions like documentation.** In every harness studied, tool
  descriptions are a large share of the prompt and carry the operating rules. A
  description saying *"prefer the accessibility tree; only screenshot when the
  tree is empty"* does more work than the same sentence in the system prompt.

One warning from the paper: policy is migrating **from prompt-based to
configuration-based** across these systems. Rules that must hold — what needs
approval, what is excluded — belong in enforced configuration, not in prompt
text. A prompt is a suggestion to a model. Your permission broker is not.

---

## 4. Keeping the model swappable

Three layers, and a discipline.

### One internal tool schema

Define tools once, in your own format, and generate each provider's wire format
from it at the adapter. Tool-calling shapes differ across vendors and change
between versions; that churn should touch one file. The same generator produces
your documentation and your eval fixtures.

### A gateway for transport

Run **[LiteLLM](https://github.com/BerriAI/litellm)** self-hosted as the egress
point — one interface over 100+ providers plus self-hosted models. OpenHands,
Aider and Mini-SWE-Agent all use it for exactly this. Self-hosting matters for
an enterprise product: you keep the credentials, the logs and the routing inside
the customer's boundary. **[OpenRouter](https://openrouter.ai)** is the hosted
alternative — faster to start, one more third party in the data path.

The gateway handles transport and failover. It does not handle semantics, which
is the next layer.

### A thin adapter you own

Above the gateway, a small module that normalizes what actually differs:
reasoning/thinking controls, prefix-caching hints, streaming events, refusal and
safety-stop shapes, image limits, and which tool-call quirks need patching. Keep
it to a few hundred lines and make it a **capability object**, not a pile of
`if provider ==` branches:

```
model_profile:
  supports_vision: true
  supports_prefix_cache: true        # automatic, explicit, or none
  max_images_per_request: 20
  max_image_long_edge_px: 2576
  native_computer_tool: false        # we use our own schema regardless
  reasoning_control: "effort" | "budget" | "none"
  parallel_tool_calls: true
  context_window: 1_000_000
```

The harness reads the profile and adapts. Adding a provider is a new profile and
an adapter case, not a change to the loop.

### The discipline

- **No provider-only feature in the core loop.** Where one genuinely helps —
  server-side context compaction, a vendor's hosted computer tool — put it behind
  a capability flag with a portable fallback you actually test. If the fallback
  is untested, you are not agnostic; you have a primary vendor and a story.
- **Roll your own context management.** All eleven systems in the study do.
  Compaction, pruning and the screenshot buffer are harness behaviour, and
  keeping them yours means they behave the same on every model.
- **Budget in your own units.** Track tokens and steps in the harness and stop
  there. Do not depend on a vendor's budget parameter.
- **Prefix-stable prompts anyway.** Caching works on a stable prefix everywhere
  it exists, automatic or explicit: stable content first (frozen tool list,
  frozen system prompt), volatile content last (timestamps, run ids, the current
  screen). Costs nothing if a provider has no caching, saves a lot where it does.
- **The eval set is what makes swapping safe.** Without it, changing models is a
  vibe. With it, it is a number. See §9.

### Choosing models per job

Do not name one model in the plan; name the job and the requirements.

| Job | Needs | Runs on |
| --- | --- | --- |
| **Orchestrator** | Long-horizon planning, reliable tool calls, big context | Your strongest available model. Sets the ceiling — do not prototype on a small one and conclude the approach fails. |
| **Extractors / subagents** | Read N documents, return structured rows | A small fast model. Most of your token volume lives here. |
| **GUI grounding** | "Where is the Submit button in this screenshot" | A vision model, or a specialist (below). |
| **Classification / routing** | Gate checks, element picks, screening | A decision model, not a generative one — see §5. Often not a model at all. |

---

## 5. The decision layer

A generative model is the wrong tool for most of the questions this product asks
itself. "Is this action a submission to a system of record?" "Which of these 40
accessibility elements is the Submit button?" "Does this PDF contain instructions
aimed at an agent?" Those are classifications. Sending them to the orchestrator
costs a full turn each, returns prose that must be parsed, and gives no usable
measure of confidence.

A **decision model** answers them instead: you send a state plus typed questions,
it returns typed answers with calibrated probabilities, and it generates no text.
Same abstraction discipline as §4 — define the interface, keep two
implementations, let the eval set decide.

### The interface

```
decision_model.ask(state, questions) -> answers

question types:
  choice(options[], criteria?)   -> one option + distribution
  score(criteria[])              -> fractional position + distribution
  boolean()                      -> calibrated probability 0..1

answer: { value, probability, distribution }
```

Three question types cover everything below. Keep the interface this small; it is
what makes the implementations interchangeable.

### The two implementations

| | **Jev** (TypeSafe AI) | **Laya** (Convai Innovations) |
| --- | --- | --- |
| Released | 16 Sep 2026 | 18 Sep 2026 |
| Licence | Closed, hosted only | **Apache 2.0, open weights** |
| Self-host | **No** — all inference on TypeSafe's cloud | Yes, one GPU |
| Size / backbone | Not published | ~421M, ModernBERT-large |
| Latency | 70–500ms end to end | ~33ms on one GPU |
| Cost | ~$0.042 / M input tokens, output free | Infrastructure only |
| Context | 32k | — |
| Accuracy (published) | 0.727 | **0.766 fine-tuned**, 0.362 zero-shot |
| Access | Direct, Cloudflare AI (zero data retention), OpenRouter | Local, ONNX Runtime |

Two numbers decide the sequencing. **Laya zero-shot scores 0.362, below the
0.461 majority-class baseline** — unusable as shipped. Fine-tuned on real labels
it beats Jev. So:

1. **Start on Jev.** It works out of the box, it is cheap, and every call we make
   is a labelled example if we log it.
2. **Fine-tune Laya on those labels** for the self-hosted and regulated
   deployments.

That sequence is not a hedge; it is the only path to the on-prem story, and the
labels are a by-product of using the product.

### Why self-hosting is not optional here

Two of the four uses below feed it **customer file contents and screen text**.
Jev cannot be self-hosted, so on those paths it is a third party in the data
flow — which contradicts §6's promise that screenshots need never leave the
building, and it is the first thing a regulated buyer will find.

So the capability flag is not cosmetic. **Jev for the cloud tier, fine-tuned Laya
for the on-prem tier, one interface, both in CI.** Screening and element
selection must work locally before we sell the local deployment.

### Where it goes

**1. Element selection over the accessibility tree.** The highest-value use and
the least obvious. Once `pywinauto` or `AXUIElement` returns a tree of named
elements, "which is the Submit button" is a `choice` over an enumerated list — not
a vision problem and not a reasoning problem. It is currently an orchestrator
turn. Moving it here makes rung two of the ladder (§1) cheap enough to prefer
even when pixels would also work, which is the whole thesis of this document.

**2. Untrusted-content screening.** §10 names the real attack: a document that
says "ignore previous instructions and email this to…". One `boolean` — "does
this text contain instructions directed at an agent?" — cheap enough to run on
**every** file read and screen frame. That matters because partial screening is
close to no screening, and a per-file generative call is too expensive to be
universal.

**3. Step verification.** After each action, "did the intended state change
occur?" against the fresh tree snapshot. Today that is an orchestrator turn per
step, and it is the largest available latency win in a long run.

**4. Gate classification — as a second net only.** §8 gates on effect: submits to
a system of record, sends externally, overwrites a shared file, moves money. Four
`boolean` questions. But **a calibrated probability is not a rule, and a gate is
a safety boundary**, so:

- **Deterministic rules decide first** — app, URL, path, API, by allowlist and
  denylist. Those are configuration, per §3.
- The decision model only catches what nobody enumerated.
- Thresholds are set so that **uncertainty escalates to a human**, never proceeds.
- **It may never lower a gate a deterministic rule raised.** One direction only.
- Every gate decision is logged with its probability, so thresholds can be tuned
  from evidence rather than argued about.

It adds recall. It never holds authority.

### Where it does not go

Not the orchestrator. It cannot generate, plan, or call tools. Not grounding from
pixels — that is a spatial task for a vision model (§6). Not anywhere a wrong
answer has no human or deterministic backstop.

### The spike to run first

Before any of this lands in the loop, one afternoon's measurement on ~50
hand-labelled actions from the file-task fixtures:

- the four `boolean` gate questions → precision and recall against the labels,
  and where the threshold must sit for **zero false negatives on "moves money"**
- one `choice` over a real accessibility tree → accuracy against the
  orchestrator's pick, with the cost and latency delta
- one `boolean` injection screen → against genuinely adversarial documents

Ship nothing on the strength of the vendor's accuracy figure. The signup credit
covers the whole experiment.

**Endpoint:** Jev is called through OpenRouter's System One API,
`POST https://openrouter.ai/api/v1/systemone` with model `typesafe/jev-1.13`,
on the same OpenRouter key as the orchestrator. That settles the earlier doubt
between `api.typesafe.ai` and a `jevtypesafe.org` mirror TypeSafe does not own.
OpenRouter is then a second third party in the data flow on the cloud tier.

---

## 6. Pixels, without a vendor's tool

Every major provider ships a native computer-use tool, and they all differ in
schema, action names and coordinate conventions. Adopting one puts a vendor at
the centre of your most sensitive code path.

**Define your own computer tool instead.** The action set is well understood and
effectively standard across implementations:

```
screenshot · zoom(region) · move(x,y) · click(x,y,button,modifiers)
double_click · drag(from,to) · scroll(x,y,direction,amount)
type(text) · key(combo,repeat) · hold_key(key,duration) · wait(seconds)
```

That is your schema. Adapters map it to a provider's native tool where one exists
and is better, and to plain vision-plus-function-calling where it does not. Your
executor, your coordinate handling, your safety checks — unchanged either way.

### Grounding can be a separate, self-hostable model

This is the strongest argument for the agnostic design. **GUI grounding** — turning
"click Submit" into a coordinate — is a specialist task with good open-weight
models:

- **[UI-TARS](https://github.com/bytedance/UI-TARS)** (ByteDance) — open weights,
  7B and 72B, continually trained from Qwen2-VL, with a desktop app and CLI in the
  open. UI-TARS-2 extends to GUI, tool use and code in one model.
- **[OS-Atlas](https://github.com/OS-Copilot/OS-Atlas)** — foundation action
  models at 4B and 7B for cross-platform grounding (mobile, desktop, web).

Two things follow. First, you can run grounding **entirely inside the customer's
network on one GPU**, so screenshots never leave the building — which is the
objection you will hit in every regulated deal, and closed hosted tools cannot
answer it. Second, grounding stops being a per-action API cost on your busiest
code path.

A good split: a strong general model plans and decides, a small local model
grounds. Both are replaceable, independently.

### The details that silently break pixel control

True on every vision model; the exact numbers come from the capability profile:

- **Pre-downscale screenshots before sending.** Around 1280×720 is a sane default.
  Oversized images get downscaled somewhere in the pipeline and your coordinates
  stop matching — the most common cause of "it clicks the wrong thing".
- **Scale returned coordinates back** to native resolution before clicking.
  **Retina is 2×** — halve the coordinates or downscale the screenshot.
- **Put the text instruction before the image.** The model knows what it is
  looking for while it reads the screenshot; click accuracy improves.
- **End a batch of actions with a screenshot** so the model can check its work.
- **Execute batched actions in order and stop at the first failure**, returning a
  result for every requested action, marking the skipped ones "not executed".
- **Bound the image buffer.** Keep roughly the 3 most recent screenshots and prune
  the rest in batches; ~20 images per request is a common ceiling.
- **Implement `zoom`.** Models use it for small text and dense UI, which is every
  operations app.
- **Treat screen text as untrusted.** Some providers scan tool results for
  injection; do not rely on it, because your fallback provider may not.

---

## 7. Tooling: what to use for what

All of this is vendor-neutral already.

### Desktop control

| Job | Use | Notes |
| --- | --- | --- |
| Windows, semantic | **`pywinauto`** (UI Automation / MSAA) | The mature choice for native Windows apps. Objects, not coordinates. `FlaUI` if you end up in .NET. |
| macOS, semantic | **`AXUIElement`** via `pyobjc` | Reads a structured UI tree from any process granted the Accessibility TCC permission. Expect rough edges — practitioners report caching failure modes on macOS 26. Build a re-query-on-stale path. |
| Cross-platform, semantic | `pyuiauto` wraps both behind one interface | Convenient; verify per-app before trusting it. |
| Pixels, any OS | **`pyautogui`** for input | Keep it. Just keep it *last*. It is the floor, not the plan. |
| Windows, scripted | AutoIt via `pyautoit`, or PowerShell | Good for the handful of legacy apps with known control IDs. |

### Browser

Use **[Playwright MCP](https://github.com/microsoft/playwright-mcp)** rather than
driving Playwright yourself. It gives the model the page's **accessibility tree as
a structured snapshot** instead of screenshots — no vision model needed,
deterministic element references, far cheaper per turn. Chromium, Firefox and
WebKit; persistent profiles so the user stays logged in; origin allow/block lists
for policy.

This is the ladder in one decision, and it is the clearest case of climbing it
widening your model choice: with an accessibility snapshot, **a text-only model
can drive a browser.**

### Office files — do not automate the GUI

For `.xlsx`, `.docx` and `.pptx`, read and write the file, never the application:
`openpyxl`, `python-docx`, `python-pptx`, `pypdf`. Faster, deterministic, no app
install, no visible window, no model involved in the mechanics.

COM automation (`pywin32`) on Windows only for what the libraries genuinely cannot
do — recalculating volatile formulas, running a macro, honouring a template's
print setup. Keep that path small and named.

**This is the highest-value shortcut in the product.** The demo is "Ondo types in
Excel". The reliable product is "Ondo edits the workbook and shows you the diff".

### MCP — the integration layer, and already neutral

MCP moved to the Agentic AI Foundation under the Linux Foundation in December
2025, so it is a vendor-neutral standard governed in the open. Current revision is
**[2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)**: servers
offer **tools, resources and prompts**; clients offer **elicitation**; and the
interesting work is in opt-in **extensions** — **Tasks** (async long-running
operations with durable handles and mid-flight input), **Skills over MCP**, and
**MCP Apps** (inline interactive UI).

Adopt the Tasks extension rather than inventing a job protocol: a two-hour
reconciliation is precisely "a long-running operation with a durable handle".

Per platform:

- **Windows — the [On-device Agent Registry](https://learn.microsoft.com/en-us/windows/ai/mcp/overview)
  (ODR).** A native registry for discovering and calling MCP servers, local or
  remote, with **per-agent consent managed from Windows Settings and Intune**,
  **containment** (servers isolated, limited to approved resources, to blunt
  cross-prompt injection), and **logging and auditability**. Built-in connectors
  include File Explorer and Settings, plus an `odr.exe` CLI. Prerelease and
  subject to change. Register Ondo's capabilities as ODR connectors and consume
  the platform ones — enterprise IT will trust it more than anything you build.
- **macOS — App Intents and Shortcuts.** App Intents is the structured action API
  behind Spotlight and the system's AI surfaces; macOS 26 executes intents
  directly and added real automation triggers. Where an app exposes intents, call
  them instead of clicking. No ODR equivalent — you manage servers yourself.
- **Cross-platform** — consume the public registry for third-party servers; ship
  first-party connectors for mail, calendar, document store and ticketing.

A rule for the tool descriptions: **an MCP tool description is untrusted input.**
The spec says so explicitly. Never let a server's own text become an instruction.

---

## 8. Permissions and audit — build this first, not last

The design in `design/` already commits to the model. Enforce it in code from the
first commit; retrofitting a permission boundary is a rewrite. And per §3, this
is **configuration the harness enforces**, never prompt text the model is asked to
respect — which also means it holds identically whatever model is loaded.

- **Three independent grants** — read files, see the screen, control input — each
  scoped, each revocable mid-run, each surfaced in the UI. Never one "allow".
- **A policy layer above the user.** Admin-excluded windows and folders (personal
  mail, HR, password managers) are not grantable by the user at all, and the
  exclusion is logged when it bites.
- **Approval gates on effect, not on tool.** The gate is not "uses the keyboard";
  it is "submits to a system of record", "sends mail", "overwrites a shared file",
  "moves money". Gate the effect and show exact values before they land — the
  `TaskRun` approval card.
- **Escape twice releases input**, always, in the agent process, never through the
  model.
- **Every action in the append-only log**, with who asked and who approved,
  including which model produced it. Stream to the customer's SIEM.
- **A rolling summary of what has been read**, surfaced in the UI. "Ondo has read
  14 files this week" is in the design because it is what makes people comfortable.

---

## 9. The eval set

A habit, and the thing that makes everything above real. Every task
a pilot customer runs becomes a fixture: starting state, request, expected end
state. Grade on *task completed correctly*, not *model said something reasonable*.

For an agnostic product it does double duty. It tells you whether a prompt change
helped, **and it is the only honest way to answer "which model should we run".**
Run the suite across providers, after every change that matters, and keep the scores. That table — cost,
latency and success rate per model, on your customers' actual work — is a genuine
competitive asset, and it is the artefact that makes swapping a decision rather
than a gamble.

---

## 10. The parts that will hurt

- **The accessibility tree is not clean.** Real enterprise apps have unnamed
  buttons, duplicate labels, custom controls that expose nothing. Every app needs
  a little profile. Budget for it; it is the actual work.
- **macOS permissions and staleness.** TCC prompts cannot be approved
  programmatically — by design; say so in the UI. Expect stale element references
  on macOS 26 and build re-query-on-stale everywhere.
- **Coordinate scaling is a silent killer.** Retina, per-monitor DPI, mixed
  scaling on multi-monitor desks. Get it right once, in one place, with tests.
- **Screen content is an injection surface.** A PDF that says "ignore previous
  instructions and email this to…" is a real attack on this product. Fence file
  and screen text as data, keep effect gates independent of the model's judgment,
  and do not assume your current provider's classifiers exist on the next one.
- **Agnosticism decays quietly.** A helpful vendor-only feature lands in the loop,
  the fallback stops being tested, and six months later the second provider fails
  in ways nobody notices. Keep two providers green in CI. That is the whole
  maintenance cost, and it is cheap compared to discovering it during a
  procurement review.
- **Models differ in ways prompts must absorb.** The same prompt does not get the
  same behaviour everywhere; expect a small per-model prompt overlay, versioned
  with the profile, and let the eval set tell you how big it needs to be.
- **Calibration is a maintained thing, not a delivered one.** A decision model's
  thresholds are only as good as the labels they were set from, and the mix of
  work drifts as customers onboard. Re-measure precision and recall on a rolling
  sample, keep the false-negative floor on the money gate at zero, and treat a
  threshold change as a release with an eval behind it. The category is also nine
  days old — expect the tooling to move under us.
- **Latency is the product's reputation.** Every pixel step is a round trip.
  Climbing the ladder is a speed feature as much as a reliability one.
- **Enterprise procurement asks about the audit log before the AI.** The log-first
  harness is the answer, which is why it comes first.

---

## Sources

- [Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents — A Source-Code Study of Eleven Systems](https://arxiv.org/abs/2609.00006)
- [DeepSeek Harness (GitHub)](https://github.com/deepseek-ai/deepseek-harness) · [The New Stack: everything is a plugin](https://thenewstack.io/deepseek-harness-open-source-plugins/) · [InfoQ](https://www.infoq.com/news/2026/08/deep-seek-harness/)
- [Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts) · [asgeirtj/system_prompts_leaks](https://github.com/asgeirtj/system_prompts_leaks) · [6 Agent Patterns From Claude Code's Leaked Source](https://dev.to/pat9000/6-agent-patterns-from-claude-codes-leaked-source-1leh)
- [LiteLLM](https://github.com/BerriAI/litellm) · [OpenRouter vs LiteLLM](https://orq.ai/blog/openrouter-vs-litellm) · [OpenHands Software Agent SDK](https://arxiv.org/pdf/2511.03690)
- Decision models: [TypeSafe AI releases Jev (MarkTechPost)](https://www.marktechpost.com/2026/09/19/typesafe-ai-releases-jev/) · [Jev on Cloudflare AI](https://developers.cloudflare.com/ai/models/typesafe/jev/) · [A coding guide to Jev](https://www.marktechpost.com/2026/09/23/a-coding-guide-to-typesafe-ai-jev/) · [Laya (Apache 2.0)](https://github.com/receptron/laya) · [laya-typed-decisions weights](https://huggingface.co/convaiinnovations/laya-typed-decisions) · [Laya vs Jev, benchmarked honestly](https://flowtivity.ai/blog/laya-open-source-jev-alternative/)
- [UI-TARS (ByteDance)](https://github.com/bytedance/UI-TARS) · [OS-Atlas](https://github.com/OS-Copilot/OS-Atlas) · [UI-TARS Desktop overview](https://themenonlab.blog/blog/ui-tars-desktop-open-source-gui-agent)
- [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)
- [MCP on Windows / On-device Agent Registry](https://learn.microsoft.com/en-us/windows/ai/mcp/overview) · [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) · [MCP Registry](https://registry.modelcontextprotocol.io/docs)
- [pywinauto](https://github.com/pywinauto/pywinauto) · [pyuiauto](https://pypi.org/project/pyuiauto) · [PyAutoGUI alternatives overview](https://testdriver.ai/articles/top-12-alternatives-to-pyautogui-for-windows-macos-linux-testing/)
- [App Intents are Apple's new API to your app](https://blakecrosley.com/blog/app-intents-are-apples-new-api-to-your-app) · [macOS accessibility automation failure modes](https://fazm.ai/t/macos-accessibility-automation)
