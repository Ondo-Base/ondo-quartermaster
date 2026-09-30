# The local decision model (Stage 6.5)

Screening ("does this file contain instructions aimed at an agent?") and element
selection ("which of these elements is Submit?") are questions for a decision
model. The cloud tier asks Jev, which is hosted by TypeSafe and called through
OpenRouter's System One API. An on-prem
deployment needs the same answers from a model that runs inside the customer's
network. That model is [Laya](https://pypi.org/project/laya/) (Apache 2.0),
behind the same interface (`agent/src/ondo_agent/decision/interface.py`).

The plan's bar: **screening and element selection meet the Jev-measured bar,
with no call leaving the customer's network, and both adapters stay green in
CI.**

## What is built, and what is not

| | State |
| --- | --- |
| One wire for Jev and Laya (`decision/systemone.py`, `POST /v1/systemone`) | Built. Run against Laya 0.3.22's own server code; Jev goes to OpenRouter's `https://openrouter.ai/api/v1/systemone` (model `typesafe/jev-1.13`), per its docs, **not yet called** |
| Laya adapter (`decision/laya.py`): a `laya-serve` inside the network, or local weights in the agent | Built and tested against Laya's own inference code and HTTP server, with a tiny random checkpoint |
| "No call leaves the network", enforced in code | Built: see below |
| Labelling logged decisions, and exporting them to fine-tune on | Built (`ondo-agent decisions`) |
| Measuring a model against a reference report (`calibrate --bar`) | Built |
| CI | **Laya adapter** job on every PR; **decision-measure** by hand, for a real checkpoint |
| ONNX Runtime path (`decision.onnx`) | Written, **not run**: Laya's export script is not in its PyPI package |
| A fine-tuned checkpoint | **Not done.** It needs labelled decisions from pilots and a GPU |
| Jev's report on the fixtures (the bar itself) | **Not measured.** It needs an `OPENROUTER_API_KEY` with credit |
| The bar met | **No.** Neither of the two rows above exists yet |

## Configuration

```yaml
decision:
  provider: laya
  # Either a laya-serve the customer hosts…
  endpoint: http://10.20.0.5:8000
  internal_hosts: [.corp.example]   # only if a name inside the network resolves to a public address
  # …or weights on this computer, run in the agent (pip install "ondo-agent[laya]"):
  # checkpoint: C:/ProgramData/Ondo/laya
  # calibration: C:/ProgramData/Ondo/laya/temperatures.json
  # device: cpu
```

Then re-run calibration against Laya, and point `gates.thresholds_file` at its
output. Thresholds belong to a model; the committed
`agent/config/thresholds.json` was measured on the rules baseline.

### How "no call leaves the network" holds

- An `endpoint` must be a loopback, private or link-local address, or resolve
  only to those, or be named in `internal_hosts`. Anything else is refused when
  the agent starts (`OffNetwork`), so a public host cannot be configured by
  mistake. A suffix entry (`.corp.example`) matches only as a suffix.
- The HTTP client ignores `HTTPS_PROXY` and similar settings from the
  environment, since a proxy would carry screen text out of the network.
- A `checkpoint` must be a directory on disk. Hub ids are refused, and the
  Hugging Face libraries are put in offline mode before they load.
- As with Jev, a failure never blocks and never permits: every question answers
  0.5, and the gate sends the action to a person.

The endpoint check runs when the agent starts. It does not check again on every
call. If DNS inside the network could later point that name outside, list the
server by IP address.

## Getting from here to the bar

1. **Label.** The agent appends every decision it asks to
   `agent.decision_labels` (default `.ondo/decisions.jsonl`), along with the
   model's answer. A person supplies the right answer:

   ```sh
   ondo-agent decisions --file .ondo/decisions.jsonl list
   ondo-agent decisions --file .ondo/decisions.jsonl label <id> inj0=false --by you@example.com
   ondo-agent decisions --file .ondo/decisions.jsonl label <id> pick=2      # a choice: its text or number
   ```

   Labels are appended to a sidecar file (`decisions.labels.jsonl`), and the
   latest label wins. The decisions file holds full screen and file text, so
   treat both files as the customer's data.

2. **Export.**
   `ondo-agent decisions --file … export --out finetune/` writes
   `train.jsonl` and `heldout.jsonl`. Each line is a `/v1/systemone` request
   plus the right answer per question: `true`/`false`, an option key such as
   `"B"`, or a level index. The split is by run, so no run is on both sides.
   The calibration fixtures are never in the decisions file, so they stay a
   clean measurement.

3. **Fine-tune** with Laya's notebook
   (`notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb` in Laya's
   repository). It takes about 4 to 5 hours on two T4s for 30k questions. The
   notebook's own dataset loader was not reachable from where this was built,
   so check its input format against `train.jsonl`. It may need a small
   mapping.

4. **Measure Jev once** on the same fixtures, through OpenRouter, with
   `OPENROUTER_API_KEY` set:
   `python -m ondo_agent.decision.calibrate --provider jev --out jev.json`.
   Commit `jev.json`: that report is the bar.

5. **Measure the checkpoint against it.** Where the checkpoint is:
   `python -m ondo_agent.decision.calibrate --provider laya --checkpoint ./ckpt --bar jev.json --out laya.json`.
   Or run the **decision-measure** workflow by hand with the checkpoint's
   repository and `bar: jev.json`. The command exits 1 when screening recall,
   screening precision or element-choice accuracy is below Jev's.

The fixtures are small (50 gate actions, 12 injection documents, 4 element choices),
and the rules baseline was tuned on them. Add held-out fixtures from pilot work
before calling the bar met. Laya's shipped checkpoints score near chance
zero-shot (Laya's own figure: 0.362 on typed decisions, below the 0.461
majority-class baseline), so expect the base checkpoint to fail the bar. That
is what step 3 is for.
