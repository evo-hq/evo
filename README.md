<p align="center">
  <img src="assets/banner.png" alt="evo banner" width="100%" />
</p>

# evo

A plugin for your agentic framework that optimizes code through experiments. Currently supported on [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://developers.openai.com/codex), [OpenClaw](https://github.com/openclaw/openclaw), and [Hermes](https://github.com/NousResearch/hermes-agent).

You give it a codebase. It discovers metrics to optimize, sets up the evaluation, and starts running experiments in a loop -- trying things, keeping what improves the score, throwing away what doesn't.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) -- where an LLM runs training experiments autonomously to beat its own best score. Autoresearch is a pure hill climb: try something, keep or revert, repeat on a single branch. Evo adds structure on top of that idea:

- **Tree search over greedy hill climb.** Multiple directions can fork from any committed node, so exploration doesn't collapse to one path.
- **Parallel semi-autonomous agents.** Spawn multiple subagents and run them simultaneously, each in its own git worktree. Each subagent reads traces, formulates hypotheses, and can run multiple iterations within its branch.
- **Shared state.** Failure traces, annotations, and discarded hypotheses are accessible to every agent before it decides what to try next.
- **Gating.** Regression tests or safety checks can be wired up as a gate. Experiments that don't pass get discarded.
- **Observability.** A dashboard to monitor your experiments.
- **Benchmark discovery.** The `discover` skill explores the repo, figures out what to measure, and instruments the evaluation.

## Install

Common: `git`, [uv](https://docs.astral.sh/uv/), Python 3.10+.

### 1. Install the evo CLI (non-Claude Code hosts)

Claude Code bundles its own copy. Every other host calls `evo` as an external binary:

```bash
uv tool install evo-hq-cli   # or: pipx install evo-hq-cli
evo --version                # evo-hq-cli 0.2.2
```

### 2. Add the plugin

**Claude Code**

```
/plugin marketplace add evo-hq/evo
/plugin install evo@evo-hq-evo
```

Invoke: `/evo:discover`, `/evo:optimize`.

**Codex** (requires 0.121.0-alpha.2 or newer -- `npm install -g @openai/codex@alpha` if you're on 0.120.0 stable)

```bash
codex marketplace add evo-hq/evo
```

Then `/plugins` → `evo` → install. Invoke: `$evo discover`, `$evo optimize`.

**OpenClaw**

```bash
openclaw plugins install evo --marketplace https://github.com/evo-hq/evo
```

Invoke: `/discover`, `/optimize`.

**Hermes** (per-skill install, no bundle support)

```bash
hermes skills install evo-hq/evo/plugins/evo/skills/discover --force
hermes skills install evo-hq/evo/plugins/evo/skills/optimize
hermes skills install evo-hq/evo/plugins/evo/skills/subagent
```

`--force` on `discover` bypasses the SKILL.md scanner (it flags evo's own install examples). Invoke: `/discover`, `/optimize`.

## Usage

Two skills:

- **`discover`** -- explores the repo, instruments the benchmark, runs baseline
- **`optimize`** -- runs the optimization loop with parallel subagents until interrupted

Invocation syntax depends on the host -- see the Install section above.

`optimize` accepts optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `subagents` | 5 | Number of parallel subagents per round |
| `budget` | 5 | Max iterations each subagent can run within its branch |
| `stall` | 5 | Consecutive rounds with no improvement before auto-stopping |

Example (Claude Code): `/evo:optimize subagents=3 budget=10 stall=3`. Other hosts use their own invocation prefix.

Typical flow:

```
you: evo:discover
evo: explores repo, instruments benchmark, runs baseline

you: evo:optimize
evo: spawns 5 subagents in parallel, each exploring a different direction
     each subagent can run up to 5 iterations within its branch
     orchestrator collects results, prunes dead branches, adjusts strategy
     repeats until interrupted or stalled
```

Under the hood, each experiment gets its own git worktree branching from its parent. If the score improves and the gate passes, the experiment is committed. Otherwise it's discarded and the worktree is cleaned up.

### Architecture

```
Orchestrator (main agent)
  - reads state, identifies failure patterns cross-cutting the tree
  - writes a structured brief per subagent (objective, parent, boundaries, pointer traces)
  - collects results, prunes dead branches, adjusts strategy for next round

  Subagent 1 (background, budget: 5 iterations)
    - reads traces, analyzes failures in its focus area
    - formulates hypothesis, edits target, runs benchmark
    - if budget remains and sees a follow-up, iterates on its branch
    - returns: what it tried, what worked, what it learned

  Subagent 2 (background, budget: 5 iterations)
    ...up to N subagents in parallel
```

## Dashboard

The dashboard starts automatically when you run `evo:discover` (or `evo init`). When it comes up, the agent surfaces the URL in the chat:

```
Dashboard live: http://127.0.0.1:8080 (pid 12345)
```

If `8080` is busy, evo auto-increments (`8081`, `8082`, ...) and prints the actual port. You can also start it manually:

```bash
uv run --project /path/to/evo/plugins/evo evo dashboard --port 8080
```

The chosen port is persisted to `.evo/dashboard.port` so repeat runs re-use it.

## Dev install

For working on evo itself (not just using it):

```bash
git clone https://github.com/evo-hq/evo
cd evo
uv run --project plugins/evo evo --version   # evo-hq-cli 0.2.2
```

`uv run` resolves dependencies on first use -- no `pip install` step.

The SDKs live in separate packages:

- `sdk/python/` -- `evo-hq-agent`, Python 3.10+, zero deps. Tests: `cd sdk/python && uv run --with pytest pytest test/`.
- `sdk/node/` -- `@evo-hq/evo-agent`, Node 18+, zero deps. Tests: `cd sdk/node && npm test`.

## TODO

- [ ] Distributed evaluation via [Harbor](https://github.com/harbor-framework/harbor) -- run benchmarks in containers instead of locally, use Harbor's cloud providers to parallelize.

## License

Licensed under the [Apache License 2.0](LICENSE).
