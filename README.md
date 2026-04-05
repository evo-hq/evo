<p align="center">
  <img src="assets/banner.png" alt="evo banner" width="100%" />
</p>

# evo

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin that optimizes code through experiments. You give it a codebase. It finds what to optimize, sets up the evaluation, and starts running experiments in a loop -- trying things, keeping what improves the score, throwing away what doesn't.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) -- where an LLM runs training experiments autonomously to beat its own best score. Autoresearch is a pure hill climb: try something, keep or revert, repeat on a single branch. Evo adds structure on top of that idea:

- **Tree search over greedy hill climb.** Amends autoresearch's approach by introducing a tree of experiments. Multiple directions can fork from any committed node, so exploration doesn't collapse to one path.
- **Parallel agents.** Multiple sub-agents can each take a different frontier node and run simultaneously, each in its own git worktree.
- **Shared state.** Failure traces, annotations, and discarded hypotheses are accessible to every agent before it decides what to try next.
- **Gating.** Regression tests or safety checks can be wired up as a gate. Experiments that don't pass get discarded.
- **Observability.** A local dashboard serves the experiment tree, score history, diffs, and per-task traces.
- **Benchmark discovery.** `/discover` explores the repo, figures out what to measure, and instruments the evaluation.

## How it works

```
you: /discover
evo: finds target + benchmark, runs baseline

you: /optimize
evo: reads failures, edits code, runs experiment, keeps or discards, repeats
```

Under the hood, each experiment gets its own git worktree branching from its parent. If the score improves and the gate passes, the experiment is committed. Otherwise it's discarded and the worktree is cleaned up. The full history lives in `.evo/` as plain JSON files.

## Install

```bash
uv run --project /path/to/evo evo status
```

No pip install needed. `uv run` resolves dependencies on first use.

## Usage

Two slash commands in Claude Code:

- `/discover` -- explores the repo, instruments the benchmark, runs baseline
- `/optimize` -- one iteration: pick a frontier node, try something, evaluate


## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.12+
- git
- [uv](https://docs.astral.sh/uv/)
