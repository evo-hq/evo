# Proposing unexplored optimization dimensions

Used only when the benchmark isn't obvious — no existing eval, ambiguous user intent, or the existing eval covers a narrow slice while the interesting optimization sits elsewhere. If the right benchmark *is* obvious, use it and skip this exercise.

When this step does run, the goal is to propose a handful of dimensions for this repo that aren't already measured. Existing benchmarks cover what the authors already worried about; that's where slack is lowest.

## Where to look

1. **Already-instrumented code.** Grep for `time.`, `perf_counter`, `@profile`, `Counter(`, `metrics.`. What's tracked hints at what authors cared about; what isn't is where slack lives.
2. **Stated goals.** READMEs, module docstrings, and comments often name what the project values ("fast JSON parsing", "robust against malformed input"). If a stated goal isn't measured, that's a proposal.
3. **Author pain points.** Grep for `TODO`, `FIXME`, `XXX`, `HACK`. Check the issue tracker if accessible.
4. **Project-type defaults.** The table below, as a starting point.

## Ranking

For each candidate, answer three questions honestly in prose. No scores — a 1-5 slack rating from an LLM is a vibe, not a measurement.

- **Signal.** Does moving this metric actually correlate with "the project is better"? Or is it a proxy that could drift from what the user cares about?
- **Slack.** Has anyone hill-climbed this before in this repo? Is there plausibly room to improve, or is the current value already near a floor/ceiling?
- **Cost per run.** How long and how expensive is one benchmark run? The optimization loop runs many — expensive dimensions compound into real time and money.

Rank on a combined judgment of those three. Construction effort (the one-time cost of building the harness) is not a ranking input — flag it qualitatively when presenting, let the user weigh it.

## Project-type defaults

Start with the obvious column, then look hard at the non-obvious column.

| Project type | Obvious (often already done) | Non-obvious (usually unexplored) |
|---|---|---|
| LLM / agent | Task pass rate on a benchmark | Token efficiency per correct answer, calibration error, refusal rate on ambiguous tasks, behavior under prompt injection, latency per tool call, recovery from tool errors |
| Web API / backend | Test pass rate, integration tests | p99 latency on hot endpoints, memory per request, error rate under synthetic load, cold-start time, allocation count per request |
| ML training | Validation accuracy, loss | Sample efficiency (accuracy per 1k tokens seen), robustness to input perturbations, generalization gap, inference memory, convergence speed |
| Library / SDK | API tests passing | Import time, allocation count per call, TypeScript strict-mode coverage, docs coverage, cold-import latency, binary size |
| Compiler / DSL | Correctness on standard suite | Output code size, compile time, optimization quality on standard benchmarks, error message quality (LLM-as-judge), stack trace usefulness |
| Data pipeline | End-to-end correctness | Throughput (rows/sec), memory peak per batch, late-data handling, schema-drift resilience, idempotency under replay |
| CLI tool | Unit tests | Cold-start time, memory footprint, output stability across runs, exit-code correctness on edge inputs, help-text discoverability |
| RAG / retrieval | Recall@K | Embedding cost per indexed doc, query latency p99, answer grounding rate (% of claims traceable to source), robustness to paraphrased queries |

## Presenting to the user

For each ranked dimension include:

- **What it measures** (one sentence)
- **Why it matters for this project** (tied to what the repo actually does, not generic)
- **Construction complexity**: *None* (existing eval already produces this score) / *Minor* (wrap or instrument what exists) / *Substantial* (new test cases, scoring logic, or data)
- **Existing coverage** if any

Recommend the highest-ranked dimension whose construction is *None* or *Minor*. If every top pick is *Substantial*, say so and let the user decide whether the signal is worth the work.

## Non-picked dimensions

Save unused dimensions to `.evo/project.md` under a "Future experiment candidates" section — useful when the first dimension plateaus.
