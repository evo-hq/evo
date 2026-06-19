---
name: finetuning
description: This skill should be used when picking or diagnosing a training move (SFT, LoRA, DPO/KTO/ORPO, RFT, GRPO/PPO/RLOO, RLHF), or when the user mentions fine-tuning, post-training, training recipe, reward design, or weight updates. Decision tree by reward shape, smoke-run gate, three failure diagnostics, five false-progress patterns. Provider recipes and I/O contract in references/.
evo_version: 0.6.2
---

# Finetuning

Priors, not rules. Only firm guardrails: held-out eval you never train on, no leakage, trust evo's recorded numbers over the run's self-report. Override anything else against the gate.

## Pick the technique by reward shape

Decide on the reward first, technique second. Choosing the comfortable technique over the matching one is the most common failure.

| Reward shape | Technique |
|---|---|
| Verifiable (exact match, unit tests, parser-decidable) | **RL** (GRPO / RLOO / PPO) — reward includes format, so the model learns to emit verifier-acceptable shape |
| Preference pairs (chosen vs rejected) | **DPO / KTO / ORPO** — cheaper than full RL, no rollouts |
| Demonstrations only (curated traces, chat data) | **SFT** — install format/tone/capability the base lacks |
| Have a scorer + want SFT stability | **RFT** — sample, filter by reward, SFT on survivors |

"SFT-then-RL" is not a law. For a competent base model on a verifiable benchmark, RL-from-base often beats SFT-then-RL end-to-end.

## Research the literature before the first commit

The decision tree above is the structural prior. The empirical answer for *this* model on *this* benchmark usually has a recent paper, blog, or HF Space recipe behind it -- and what beats baseline on a 4B base model in 2026 is not what the agent's pre-training data captures. Before picking the technique for `exp_0001` (the first experiment after baseline), invoke `evo:ideator` with a `literature` brief:

```
Task(
    subagent_type="evo:ideator",
    prompt="brief=literature\n"
           "model_family=<e.g. Qwen3-4B-Base, Llama-3.1-8B-Base>\n"
           "benchmark=<name + URL/paper if known>\n"
           "objective=<one line: what beats baseline looks like>\n"
           "constraints=<budget, data sources allowed, gated models forbidden, etc>"
)
```

The ideator returns ranked proposals with references (arXiv, HF Hub, GitHub, blogs). Read them before picking from the reward-shape table. A paper showing GRPO-from-base works on `<model_family>` for a similar verifiable benchmark beats applying the table cold.

Run this **once before `exp_0001`**, and again whenever the optimize loop hits a plateau (the "stuck across distinct techniques" diagnostic below). Not every subsequent experiment needs a literature pass -- the table + diagnostics carry the rest.

## Before committing the budget: smoke-run

Run the full pipeline on ~10 examples for ~1 minute. Must produce: a checkpoint the benchmark can load AND a non-zero eval on a held-out item. If not, the recipe is broken — fix it, don't scale it. dtype mismatch, tokenizer/template drift, OOM at this batch size, empty artifacts dir despite falling loss — all surface on 10 examples. Running longer doesn't surface them differently, just more expensively.

## Long training: checkpoint, mid-eval, early-stop in-script

Training for an hour and getting one number at the end is the wrong granularity for evo's tree search. By the time you know the recipe failed, you've spent the budget. Build the verification *into* the training script, not around it.

Pattern for any training run expected to exceed ~30 min wall-clock:

1. **Periodic checkpoint** every N steps (e.g. every 0.25 epoch, or every 200 steps — whichever is faster).
2. **Mini-eval after each checkpoint** on a small held-out subset (5–10 items, not the full held-out — that's reserved for the final committed score). Same scorer as the real eval; the model just sees fewer items.
3. **Early-stop on regression**: track best mid-eval score; stop if it hasn't improved in `patience` checkpoints (typically 2). Don't burn 60 more minutes once the trajectory has flattened or reverted.
4. **Save the BEST checkpoint, not the last.** Early-stop means the current model is probably past its peak; the checkpoint you commit should be the one that scored highest mid-training, not whatever the trainer happened to leave behind.
5. **Log every mid-eval score to your tracker** (see `## Stream training metrics live`). The user watching the live dashboard sees the trajectory build up step-by-step instead of staring at the loss curve hoping it transfers.

HuggingFace TRL: implement as a `TrainerCallback` on `on_step_end` — save checkpoint, run the mini-eval via vLLM or HF transformers, compare to `best_score`, set `control.should_training_stop = True` on stall. Pattern is one ~30-line class.

Keep vLLM warm across mid-evals when you can (one serve process, reload adapter between checkpoints) — cold-starting vLLM every 200 steps adds 5 min of overhead per checkpoint.

Use a tighter mini-eval subset than the full held-out. The mini-eval is a *signal*, not the score that gets committed. If the mini-eval scores ≥ baseline on its subset, run the full held-out as the eval-gate scoring pass at the end. If it doesn't, early-stop.

This is Pattern B from the design tradeoff with multi-node staging (Pattern A — break the training into multiple committed evo nodes, each a stage). Pattern B keeps the experiment as one evo node with the verification logic inside the script; it's simpler to write and avoids per-stage vLLM spin-up, at the cost of less tree-search introspection. Multi-stage as separate nodes is preferable when you want the orchestrator to be able to branch alternative continuations from any mid-training checkpoint.

## Cap retries at training scale

`evo run` allows up to `max_attempts=3` retries per experiment by default. That budget was designed for second-scale benchmarks where retrying after an edit-bug fix is free. At training scale (~hours per attempt), it's the wrong tradeoff — by attempt 2 you've spent more compute than just trying a fresh hypothesis would cost.

For training-heavy workspaces, set the cap to 1 once at init:

```bash
evo config set max-attempts 1
```

One attempt, one shot. Regression → `evo discard` → new branch from parent with a different hypothesis. This pairs with the in-script early-stop above: each attempt is single-shot, but its internal verification keeps it from burning the budget on a clearly-failing trajectory.

The "fix-and-rerun" retry pattern still applies for sub-minute benchmarks; leave the default `max_attempts=3` there.

## Four diagnostics

**Stuck at 0 on a verifiable benchmark after 2+ SFT runs.** Technique class is wrong, not the recipe. Pivot to RL with the verifier as reward; SFT loss can be healthy while the model emits unparseable output.

**Base scores below random before any training (knowledge-heavy benchmark).** Model lacks the knowledge, not the format. Post-training shapes existing knowledge; it does not install new knowledge. Right axis: continued pre-training on a domain corpus, distillation from a stronger model that has the knowledge, or retrieval-augmented inference.

**`delta <= 0` across several committed train moves.** Method exhausted on this target. Try a different method, change the data, or improve the harness instead of the weights.

**Stuck at the same non-zero score across 3+ experiments spanning distinct techniques.** When 3+ committed experiments — across structurally different techniques (e.g. SFT, GRPO, RFT) — all land at the same non-zero score, the bottleneck is not the training method. The most common cause is a train↔verifier objective mismatch: the model has learned to emit answers in one format, but the verifier expects a different one. Examples: training data uses `\boxed{X}` but the verifier prompt requests `ANSWER: X` (or vice versa); training uses one chat template, eval uses another; training optimizes step-by-step CoT but the verifier wants the answer alone.

Diagnostic action: spot-check 3 training examples and 3 eval-prompt examples side by side. If a perfect-score training example would NOT pass the verifier (or vice versa), the objective is mismatched. Realign the training data format to the verifier's expected output, OR change the eval prompt (if rules allow). Do NOT try a fourth training-technique variant before doing this spot-check.

## What never counts as progress

Five patterns produce a number going up without the model improving. See `references/false-progress.md` for examples + detection.

1. Training on the held-out set — direct or transitive (public instruction datasets sometimes contain eval-derived items).
2. Embedding eval items in "synthetic" data, even renamed or paraphrased.
3. Generating training data conditioned on per-eval-item failure logs.
4. Submitting a checkpoint you didn't train (off-the-shelf instruct model; parent's checkpoint unchanged).
5. Training a different objective than the verifier scores.

The verifier should catch these. List is here so the train move doesn't produce them.

## Surviving session compaction

Write the dataset URL, method choice, user-imposed constraints, and hyperparameters you converged on to `methodlog.md` in the experiment worktree. One line each. Re-read after any context reset, before the next train move. Prevents silent dataset swaps between experiments and re-running ablations.

## Numbers that matter (in order)

1. A reward you trust — verifiable beats a learned reward (which gets hacked).
2. A held-out eval you never train on.
3. On-policy freshness for RL — train on current policy's samples, not stale ones.
4. LoRA LR ~10x full-FT; rank 32 is a fine default. LoRA ~ full-FT for RL and small-data SFT; lags on large SFT.

Method/provider-specific numbers (LR, KL, group size) live in the recipe under `references/`.

## Stream training metrics live

A long training run is observability-blind until the experiment commits — without a live tracker, nobody can tell if loss is converging, if the GPU is idle, or if the recipe is silently broken. They get one number at the end. Wire a tracker into the training script by default.

Detection prior — apply when the corresponding env var is set, skip otherwise. Don't install a tracker the user didn't opt into:

| Env var | Tracker | TRL one-liner |
|---|---|---|
| `WANDB_API_KEY` | wandb | `SFTConfig(report_to="wandb")` |
| `TRACKIO_SPACE_ID` | trackio (wandb-compatible OSS, logs to a public HF Space) | `SFTConfig(report_to="trackio")` |
| `MLFLOW_TRACKING_URI` | mlflow | `SFTConfig(report_to="mlflow")` |
| (none set) | none | train without a tracker; don't invent one |

For custom training loops, use `tracker.init(project=..., name=f"exp_{exp_id}") + tracker.log({"loss": ..., "step": ...})` — concrete patterns in `references/observability.md`.

Use `EVO_EXPERIMENT_ID` as the run name so each experiment shows up as its own line in the tracker dashboard. The same env detection applies to HuggingFace datasets / Hub uploads: if `HF_TOKEN` is set, treat gated datasets and private Hub pushes as available.

## Warm-start from a parent / prior checkpoint

When the orchestrator branches an experiment from a committed or preserved checkpoint with `evo new --from-artifact <exp[:label]>`, evo exposes that artifact's path to your recipe as `EVO_SEED_ARTIFACT` (and, for back-compat, the same value as `EVO_PARENT_POLICY`). Warm-start from it rather than re-training from base — re-training from base every time burns the budget on duplicated work and stops the tree from accumulating capability across generations. To *make* a run reusable this way you must DECLARE your checkpoint as an artifact: write it to `EVO_CHECKPOINT_DIR` and name it in the benchmark result's `artifacts` field (full contract in `references/glue.md`). Only declared artifacts are preserved on discard and seedable via `--from-artifact`.

Concrete pattern:

```python
seed = os.environ.get("EVO_SEED_ARTIFACT") or os.environ.get("EVO_PARENT_POLICY")
if seed and os.path.exists(seed):
    print(f"warm-starting from {seed}")
    model = AutoModelForCausalLM.from_pretrained(seed, ...)
else:
    print("no seed; loading base")
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, ...)
```

Override only when the brief explicitly asks for a fresh-from-base ablation. The full I/O contract is in `references/glue.md`.

**Configure for training, not inference.** Put the whole training computation on the accelerator you're training on, and don't enable *inference*-oriented conveniences for a training run. Auto device-mapping / model-sharding / CPU-offload exist to fit oversized models for *inference* by spreading or offloading layers; inside a training step they either break the backward pass or silently fall back to slower memory — so training still "runs" but crawls, with no error to surface the problem (the most dangerous case: it looks like it's working). Shard only when the model genuinely doesn't fit one device, and then use the framework's *training* parallelism path, not an inference placement shortcut. Same logic for other inference-mode defaults that leak into training (eval-mode quantization, kv-cache, dropout off). *(Concrete instance — HuggingFace: load with `device_map={"": 0}` / `.to("cuda")`, never `device_map="auto"`, which errors with a meta-device gradient mismatch or offloads to CPU at a large slowdown; for real multi-GPU use accelerate/FSDP/DDP.)*

## Cache expensive intermediates

LoRA adapters, filtered/curated datasets, tokenized datasets, computed embeddings, generated rollouts -- expensive to produce, large, and gitignored. They don't ride the experiment branch. They also don't have to be rebuilt per experiment.

Write expensive artifacts to a stable, workspace-level path; check for them first, compute only on miss. Subsequent experiments (siblings, descendants, or re-runs of the same experiment after a worktree clean) read the same path.

Convention: under `.evo/cache/`, sibling to `run_<NNNN>/`. Already gitignored (via `.evo/` in the workspace's git excludes). Survives across runs -- it's not nested inside any `run_<id>/`, so `evo new`/`evo run`/`evo reset` don't touch it.

Pattern:

```python
import os
from pathlib import Path
# walk up from cwd to find the workspace root (the dir that has .evo/)
def _workspace_root() -> Path:
    p = Path.cwd().resolve()
    for d in [p, *p.parents]:
        if (d / ".evo").is_dir():
            return d
    raise RuntimeError("not inside an evo workspace")

cache = _workspace_root() / ".evo" / "cache" / "datasets"
cache.mkdir(parents=True, exist_ok=True)
# Cache key embeds every input that changes the artifact: dataset name,
# filter recipe version, tokenizer, max length, etc. Different recipe ->
# different key, so a sibling experiment with a different filter keeps
# its own cache without trampling yours.
key = cache / "numina-cot-r1-filter-v2-qwen3-tok-3072.arrow"
if key.exists():
    ds = datasets.Dataset.load_from_disk(str(key))
else:
    ds = build_and_filter_dataset()
    ds.save_to_disk(str(key))
```

High-value caches (not exhaustive): curated/tokenized training corpora (tokenization is the slow part on millions of rows); LoRA adapters produced by prior experiments that a sibling might warm-start from (the parent path is already handled by `EVO_PARENT_POLICY` above; this is for sibling-reachable named adapters); computed embeddings, retrieval indexes, precomputed eval-time generations.

Don't duplicate the HuggingFace Hub cache (`~/.cache/huggingface/`). That handles `from_pretrained` downloads automatically and is user-level, already shared across all experiments.

Anti-pattern: writing the artifact inside the experiment's worktree (`<worktree>/some_cache/`). Worktrees are gitignored for these files, the artifact doesn't propagate to descendants via the git tree, and a worktree clean / gc removes it. Use the workspace-level `.evo/cache/` instead.

A first-class named registry (`evo asset put/get/list/use`) for these is tracked in issue #55. The path convention above is the lightweight version anyone can adopt today.

## References

Pull via Read tool when the trigger applies. Tree organized by category --
core contracts first, then provider-specific recipes under `rl/`, `sft/`, `serving/`.

```
finetuning/references/
│
├── glue.md             writing train.py -- I/O contract evo expects.
│                       Read FIRST when starting any training code.
├── trace-schema.md     TrainingTrace JSON shape (per-step train trace fields)
├── diagnostics.md      held_out_score / delta / reward_saturation /
│                       generalization_gap -- read when interpreting a result
├── false-progress.md   the five patterns + how to detect them.
│                       Read when a score improves implausibly fast or
│                       breaks the smoke gate.
├── observability.md    wandb / trackio / mlflow wiring -- env-driven detection,
│                       TRL report_to options, custom-loop patterns.
│                       Read when writing a training script.
│
├── rl/                 RL framework recipes (rollouts + reward + policy update)
│   └── art.md          ART (Algorithm-Refined Training)
│
├── sft/                SFT framework recipes
│   └── tinker.md       Tinker SFT runner
│
└── serving/            Eval-time inference framework references
    └── vllm.md         vLLM serving config + LoRA-multi (load multiple
                        adapters in one server -- saves cold-start per experiment)
```

Cross-skill references also worth pulling during finetuning work:

- `discover/references/sdk_python.py` / `sdk_node.js` -- wiring per-task instrumentation in the benchmark
- `discover/references/inline_instrumentation.py` -- inline fallback when SDK can't be used (copy as-is)
- `references/evo-wait.md` -- waiting for training / eval without burning context
