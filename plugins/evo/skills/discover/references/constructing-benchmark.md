# Constructing a benchmark from scratch

Used when the chosen optimization dimension doesn't have an existing eval -- you have to build the harness. This happens often in early-stage projects, or when the proposed dimension is an unexplored one nobody's measured before.

**All construction happens inside the baseline worktree (experiment 0), not in main.** Main stays free of evo-specific infrastructure. The constructed benchmark becomes part of the baseline commit; all descendant experiments inherit it automatically.

## Contents

- Design the scoring function
- Assemble test cases
- Write the runnable harness
- Goodhart check
- Held-out validation slice
- Required gate pairing
- Commit discipline

## 1. Design the scoring function

Before writing any code, decide exactly what the score means. Answer in one sentence each:

- **What single number does the benchmark output?** (e.g., "mean pass rate across N tasks")
- **Higher is better, or lower?** (`--metric max` vs `--metric min`)
- **What's the theoretical range?** (0.0 to 1.0 / unbounded / bounded by implementation)
- **What does a "meaningful" improvement look like?** (noise floor vs real signal)

If you can't answer these cleanly, the benchmark is premature -- iterate on the design before coding.

## 2. Assemble test cases

Benchmark quality is dominated by test case quality. Cheap-but-wrong tests produce noise or invite gaming.

**For programmatic benchmarks** (deterministic scoring like pass/fail):

- Aim for 10-20 cases at minimum -- fewer than 10 and random noise dominates.
- Include at least one edge case per category of input (empty, malformed, boundary, large).
- Each case should have a clear, independently-verifiable expected output.

**For LLM-as-judge or fuzzy scoring:**

- Write 15-30 cases with natural-language descriptions of what "good" looks like.
- Include a calibration set: 3-5 obviously-good and 3-5 obviously-bad responses. Verify your judge scores them correctly before trusting it on ambiguous cases.

**For performance benchmarks** (latency, throughput, memory):

- Use realistic workload shapes, not synthetic microbenchmarks -- p99 on a real trace is more useful than average on a loop.
- Warm up before measuring (first run is often JIT/cache-cold).
- Take multiple samples and report the aggregate (p50/p95/p99 or mean+stderr), not a single run.

## 3. Write the runnable harness

Output contract (same as existing evo benchmarks):

- **score channel:** a single JSON object with a `score` field and optional `tasks` breakdown, written to `$EVO_RESULT_PATH`. Example: `{"score": 0.78, "tasks": {"0": 1.0, "1": 0.5, ...}}`
- **stdout / stderr:** free for user output (logs, progress, debug)
- **exit code:** 0 on successful completion (even if score is low); non-zero only on infrastructure failure (import error, missing data, etc.)

Use the SDK or inline instrumentation depending on the user's earlier choice (recorded in `.evo/meta.json` as `instrumentation_mode`).

## 4. Goodhart check

The core risk of a constructed benchmark: *evo will optimize the metric, not the underlying thing you care about*. Every constructed benchmark needs an explicit Goodhart audit before being committed.

Ask these questions:

1. **Can the metric go up without the underlying thing improving?** Think of at least one degenerate way to game it (e.g., "special-case the exact inputs in the test set", "output a constant that averages well", "return early with a trivial answer"). If you can think of gaming strategies, evo will find them faster.

2. **Can the metric go up while breaking something obviously important?** E.g., optimizing latency by dropping correctness, or optimizing accuracy by eliminating an entire class of input.

3. **What's the held-out signal that would catch gaming?** If evo gets a suspiciously high score, what independent test would reveal the gaming?

Document the answers in `.evo/project.md` under a "Benchmark gaming risks" section, and mitigate each with either:

- A **held-out validation slice** (see next section)
- A **paired gate** (see required gate pairing section)
- A **sanity assertion** baked into the scoring function

## 5. Held-out validation slice

When constructing a benchmark from hand-written test cases, split them:

- **Training slice** (what evo sees): 60-70% of cases. This is the score evo optimizes.
- **Held-out slice** (what evo can't see): 30-40% of cases. Evaluated separately after each committed experiment.

If the training score improves but the held-out score doesn't (or regresses), evo is overfitting to the specific training cases. The held-out score becomes either a gate or an annotation that the orchestrator watches.

For the initial v0.1.0 flow, implement the held-out slice as a **gate** (next section). For a later iteration, add first-class support in the evo CLI for "hidden eval" scoring that's orthogonal to the optimization score.

## 6. Required gate pairing

Any benchmark constructed from scratch MUST be paired with at least one gate. The gate catches gaming that the optimizer-visible metric can't.

Common pairings:

| Benchmark style | Minimum paired gate |
|---|---|
| Hand-written task pass rate | Held-out slice (other tasks, not visible during optimization) |
| Latency / performance | Correctness test (the optimized code must still produce the same outputs) |
| LLM-as-judge rating | Structural validity check (output parses / is well-formed) |
| Quality-of-output score | Sanity assertion that catches degenerate outputs (empty, constant, out-of-range) |

Add the gate via `evo gate add root --name <name> --command <command>` during the discover flow. The gate runs alongside every experiment and auto-discards any experiment that breaks it, even if the benchmark score improves.

**The gate command must exit non-zero on regression.** `evo run` checks exit code, not stdout. A bare `python3 benchmark.py --task-ids 5,6,9` always exits 0 because the benchmark script's contract is "exit 0 unless infrastructure broke" -- it prints a low score but never fails. To make a benchmark-derived gate actually catch regressions, the benchmark needs a `--min-score <threshold>` flag (or equivalent) that:

1. Computes the score on the requested slice as usual.
2. Compares against the threshold.
3. Calls `sys.exit(1)` (or `process.exit(1)`) when the score is below the threshold.

The inline helpers (`references/inline_instrumentation.py` / `.js`) make this easy: `write_result()` returns the final score, so you can do something like:

```python
score = write_result()
if args.min_score is not None and score < args.min_score:
    print(f"GATE FAIL: score {score:.4f} below minimum {args.min_score}", file=sys.stderr)
    sys.exit(1)
```

When you pick the threshold for the held-out gate, set it to the baseline score on that slice (or slightly below if the slice is small enough that one-task variance matters). That gives you "any regression on the held-out tasks fails the gate" semantics.

## 7. Commit discipline

All construction artifacts are committed to experiment 0's branch, not main:

- `benchmark.py` (or equivalent) -- the harness script
- Test fixtures, golden outputs, calibration data
- Any new gate scripts (if gates reference new files rather than existing tests)
- The instrumented form of the target (SDK or inline)

**Add a `.gitignore` first, before the first commit.** Running the benchmark will produce build artifacts (`__pycache__/`, `.pytest_cache/`, `node_modules/`, etc.) that will be captured by `git add -A` and pollute the experiment branch. Also include `.evo/` as a guard -- validation runs should target the main repo root's workspace, but if anything lands in the worktree's `.evo/` by accident, don't commit it. Minimum contents:

```
.evo/
__pycache__/
*.pyc
.pytest_cache/
node_modules/
dist/
build/
```

Commit in logical chunks where possible:

1. "add: .gitignore for build artifacts"
2. "add: benchmark harness + test cases"
3. "add: instrumentation" (only in SDK mode -- inline mode keeps harness and instrumentation in one file)

Running `evo run <exp_id>` captures the baseline score and marks the experiment committed in a single step. No separate `evo done` is needed.

## Rollback

If the Goodhart check flags an unmitigable gaming risk, or the benchmark doesn't run cleanly, discard experiment 0 with `evo discard <exp_id>` and restart from root with a simpler benchmark.

## A note on non-determinism

Some benchmarks have inherent noise (LLM calls, random sampling, concurrent execution, network latency). We deliberately skip a front-loaded multi-run determinism check during setup -- it costs real money for LLM-based benchmarks and delays the first optimization iteration for limited safety value at this stage of the project.

Honest accounting of what this means today:

- v0.1.0 of evo compares experiment scores directly (single sample per experiment). It does **not** yet retry experiments, average across runs, or use confidence intervals. A noisy benchmark can therefore commit a "lucky" experiment as a real improvement.
- The held-out gate remains the only safety net against benchmark gaming and overfitting. It is mandatory whenever the benchmark was constructed from scratch (see "Required gate pairing" above).
- Multi-run / variance-aware optimization is on the roadmap but not implemented yet. Until it lands, **noisy benchmarks should be expected to produce noisier optimization trees** -- some committed experiments will not reproduce.

Record what you know about the benchmark's determinism in `.evo/project.md`, one line under "Benchmark determinism":

- `deterministic by construction` -- pure code, no randomness, no network. Safest case.
- `uses LLMs with temp=0` -- expected to be deterministic in practice; flag in project.md if observed runs disagree.
- `sampling-based, variance expected` -- inherent noise. Optimization will be noisier; rely on the held-out gate as the truthful signal.
