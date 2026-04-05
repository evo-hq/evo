<p align="center">
  <img src="assets/banner.png" alt="evo banner" width="100%" />
</p>

# evo

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin that optimizes code through experiments. You give it a codebase. It finds what to optimize, sets up the evaluation, and starts running experiments in a loop -- trying things, keeping what improves the score, throwing away what doesn't.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) -- where an LLM runs training experiments autonomously to beat its own best score. Autoresearch is a pure hill climb: try something, keep or revert, repeat on a single branch. Evo adds structure on top of that idea:

- **Tree search over greedy hill climb.** Multiple directions can fork from any committed node, so exploration doesn't collapse to one path.
- **Parallel semi-autonomous agents.** Up to 5 subagents (configurable) run simultaneously, each in its own git worktree. Each subagent reads traces, formulates hypotheses, and can run multiple iterations within its branch -- they're not just dumb workers executing orders.
- **Two-layer intelligence.** The orchestrator analyzes cross-cutting failure patterns and assigns strategic directions with specific ideas. Subagents do focused analysis within their direction and decide their own tactical moves.
- **Shared state.** Failure traces, annotations, and discarded hypotheses are accessible to every agent before it decides what to try next.
- **Gating.** Regression tests or safety checks can be wired up as a gate. Experiments that don't pass get discarded.
- **Observability.** A local dashboard serves the experiment tree, score history, diffs, and per-task traces.
- **Benchmark discovery.** `/discover` explores the repo, figures out what to measure, and instruments the evaluation.

## How it works

```
you: /discover
evo: explores repo, instruments benchmark, runs baseline

you: /optimize
evo: spawns 5 subagents in parallel, each exploring a different direction
     each subagent can run up to 5 iterations within its branch
     orchestrator collects results, prunes dead branches, adjusts strategy
     repeats until interrupted or stalled
```

Under the hood, each experiment gets its own git worktree branching from its parent. If the score improves and the gate passes, the experiment is committed. Otherwise it's discarded and the worktree is cleaned up. The full history lives in `.evo/` as plain JSON files.

### Architecture

```
Orchestrator (main agent)
  - reads state, identifies failure patterns, picks strategic directions
  - gives each subagent a direction + specific ideas + iteration budget
  - collects results, prunes dead branches, adjusts strategy for next round

  Subagent 1 (background, budget: 5 iterations)
    - reads traces, analyzes failures in its focus area
    - formulates hypothesis, edits target, runs benchmark
    - if budget remains and sees a follow-up, iterates on its branch
    - returns: what it tried, what worked, what it learned

  Subagent 2 (background, budget: 5 iterations)
    ...up to N subagents in parallel
```

## Install

```bash
uv run --project /path/to/evo evo status
```

No pip install needed. `uv run` resolves dependencies on first use.

## Usage

Two slash commands in Claude Code:

- **`/discover`** -- explores the repo, instruments the benchmark, runs baseline
- **`/optimize`** -- runs the optimization loop with parallel subagents until interrupted

`/optimize` accepts optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `subagents` | 5 | Number of parallel subagents per round |
| `budget` | 5 | Max iterations each subagent can run within its branch |
| `stall` | 5 | Consecutive rounds with no improvement before auto-stopping |

Example: `/optimize subagents=3 budget=10 stall=3`

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.12+
- git
- [uv](https://docs.astral.sh/uv/)
