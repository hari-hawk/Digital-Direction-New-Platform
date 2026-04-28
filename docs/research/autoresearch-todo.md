# Autoresearch loop for Digital Direction — parked TODO

**Status:** Parked — revisit after PoC ships.
**Created:** 2026-04-28
**Owner:** TBD post-PoC
**Reference:** [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
              + cross-check in this conversation
              ([gh repo clone karpathy/autoresearch])
**Why parked:** PoC stabilization (extraction completeness, customer demo,
Dhanapal redeploy) takes priority. Returning to this AFTER prod is healthy
and the customer's PoC is signed off.

---

## What this is

A nightly autonomous loop that improves our extraction quality without
human prompt tuning. Inspired by karpathy's autoresearch pattern: an LLM
agent picks an idea, edits a prompt or carrier config, runs the eval
harness, reads one number, keeps the change if it improved, reverts if
it didn't, logs to a flat-file history, and repeats — indefinitely.

The motivating shape, mapped to our project:

| autoresearch | us |
|---|---|
| `train.py` (the file the agent edits) | One prompt or one carrier YAML per experiment |
| `prepare.py::evaluate_bpb` | `evals/runner.py::run_eval` (already exists) |
| `val_bpb` (one-number metric) | `overall_accuracy` (to define — see §2) |
| `results.tsv` | `evals/results.tsv` (to create) |
| `program.md` (agent playbook) | `docs/research/autoresearch.md` (to create) |
| 5-min wall-clock budget per run | Token + dollar budget per run (to enforce) |
| `autoresearch/<tag>` branches | `autoeval/<tag>` branches |

After this is built, we point an agent at the playbook and it does
~50 prompt experiments while we sleep. Wake up to a TSV log of what
worked, what didn't, and a `main` branch that absorbed only the wins.

---

## 1. Five high-leverage patterns to adopt

In priority order:

### 1.1 One-number quality metric: `overall_accuracy`
Reduce the multi-axis eval report to a single comparable number. Proposal:
```
overall_accuracy = weighted_F1 across (
    structured_fields  × 0.4   # account #, phone, MRC, etc.
  + semi_structured    × 0.3   # service_type, carrier_name (canonical)
  + fuzzy              × 0.2   # billing_name, addresses
  + contract_specific  × 0.1   # contract_term_months, dates, MTM
)
```
Weights live in `configs/processing/eval_config.yaml`. The agent reads
one number; humans can still drill into the per-category report when needed.

### 1.2 Append-only `evals/results.tsv`
The institutional memory of what we've tried. 7 columns, tab-separated:
```
commit  timestamp  overall_acc  cost_usd  duration_s  status  description
```
- `status`: `keep` / `discard` / `crash` — exactly autoresearch's vocabulary
- Discards stay in the log so the agent doesn't try the same dead-end twice
- `crash` rows have `overall_acc=0.000` and a description like "syntax error in att/carrier.yaml"

### 1.3 `evals/autoeval.py` — the runner
Wraps `evals/runner.py` with:
- Loads a fixed test corpus (the curated golden set)
- Caps cost at `$N` per run (default `$2`); aborts on overrun
- Caps wall clock at `Tmin` (default 10 min); kills hung extractions
- Writes one TSV row at the end with the `commit` it ran on
- Returns exit code `0` on success, `2` on crash (script never silently fails)

### 1.4 Branch-per-experiment + commit-keep-or-revert convention
Agent works on `autoeval/<tag>` branches off `main`. Each accepted experiment
is one commit on the branch; rejected experiments are `git reset --hard`
back to where the branch was. After a session, a separate human-review step
cherry-picks (or merge-squashes) the keepers onto `main`.

### 1.5 `docs/research/autoresearch.md` — the agent playbook
Short, focused, action-oriented. Modeled after karpathy's `program.md`:
- "Edit ONE prompt or ONE carrier YAML per experiment"
- "Run `python -m evals.autoeval --tag <tag>`"
- "Read the new line in `evals/results.tsv`"
- "If `overall_acc` improved → keep the commit; else `git reset --hard` to baseline"
- "Crashes: log status=crash, move on, never block on a single failure"
- "**NEVER STOP** mid-loop to ask the user — the user is asleep"

---

## 2. Concrete work breakdown

When this gets unparked, scope it as 5 items with clear handoff between:

| # | Work item | Outputs | Effort |
|---|---|---|---|
| 1 | Define `overall_accuracy` formula + weights | `configs/processing/eval_config.yaml` + an entry in `evals/runner.py::EvalReport` | half day |
| 2 | Build `evals/autoeval.py` runner with budget caps | `evals/autoeval.py`; `evals/results.tsv` (header + baseline row) | 1 day |
| 3 | Curate the test corpus | `evals/golden/` (existing) + `evals/test_corpus.yaml` listing which goldens are in the autoeval set (subset for speed) | half day |
| 4 | Write `docs/research/autoresearch.md` (the playbook) | docs only | 2 hours |
| 5 | Trial run: one human-supervised session against the playbook | one filled `results.tsv`, lessons-learned addendum to the playbook | 1 day |

Total: **~3 days** to MVP. After that, it's autonomous.

---

## 3. Patterns we should explicitly NOT borrow

Honest call-outs so whoever builds this doesn't over-copy:

- **"Single file to modify"** — autoresearch's `train.py` is self-contained.
  Our quality lives in many places (prompts, carrier configs, classifier
  rules, merger priorities). Forcing one file would distort the search space.
- **5-minute wall-clock budget** — meaningful for GPU-bound training.
  We're LLM-bound; **token + dollar budget is the equivalent constraint.**
  E.g., `--cost-cap 2.00` (USD) per autoeval run.
- **NEVER-STOP autonomy on prod-touching work** — fine for offline prompt
  research; risky if the agent has access to write to `main` or trigger
  prod redeploys. The branch-per-experiment + human-review-before-merge
  rule keeps us safe.

---

## 4. Acceptance criteria for "this works"

When picking this up post-PoC, the autoresearch loop is "done" when:

1. ✅ A new `overall_accuracy` number appears in every eval run, ranges
   0.0–1.0, and trends with our intuition (deliberately bad prompts → lower).
2. ✅ `python -m evals.autoeval --tag baseline` writes a single line to
   `evals/results.tsv` and exits cleanly.
3. ✅ A test prompt change → re-run → second TSV line with a different
   `overall_acc`. Diff is grep-able.
4. ✅ A budget overrun (set cap to `$0.10`) aborts the run cleanly with
   `status=crash` written to TSV.
5. ✅ A crashing prompt (malformed YAML) writes `status=crash` and the
   loop continues — does not propagate to neighboring experiments.
6. ✅ A first overnight session (10+ keep/discard cycles) produces at
   least one accepted prompt improvement that makes it to `main`.

---

## 5. Out of scope for v1

These are tempting but should wait for v2:

- Multi-agent (agent picks idea, separate critic agent reviews) — single
  agent first, see what gaps surface
- Auto-merge to `main` on `keep` — keep human-review-before-merge for now
- Cross-carrier fairness (running on all 67 carriers per experiment) —
  scope to a curated subset that's fast enough to iterate on
- Auto-rollback after deploy if overall_acc drops on prod — interesting
  but couples eval to ops; build the eval loop first, integrate later

---

## 6. Cross-link from PENDING.md

When this is unparked:
- Move this file's status from "Parked" to "In progress"
- Add a top-level item to `PENDING.md` under priority 3 ("Nice to have, post-PoC")
- Reference this file as the design doc

---

*Reference reading when picking this up:*
- karpathy/autoresearch — `README.md`, `program.md`
- our `evals/runner.py` + `evals/judge.py` (the harness this builds on)
- our `scripts/merge_qa.py` (the closest existing one-shot eval entry point)
