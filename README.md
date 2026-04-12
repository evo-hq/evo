<p align="center">
  <img src="assets/banner.png" alt="evo banner" width="100%" />
</p>

# evo

A plugin for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex](https://developers.openai.com/codex/plugins) that optimizes code through experiments. You give it a codebase. It discovers metrics to optimize, sets up the evaluation, and starts running experiments in a loop -- trying things, keeping what improves the score, throwing away what doesn't.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) -- where an LLM runs training experiments autonomously to beat its own best score. Autoresearch is a pure hill climb: try something, keep or revert, repeat on a single branch. Evo adds structure on top of that idea:

- **Tree search over greedy hill climb.** Multiple directions can fork from any committed node, so exploration doesn't collapse to one path.
- **Parallel semi-autonomous agents.** spawn multiple subagents and run them simultaneously, each in its own git worktree. Each subagent reads traces, formulates hypotheses, and can run multiple iterations within its branch.
- **Shared state.** Failure traces, annotations, and discarded hypotheses are accessible to every agent before it decides what to try next.
- **Gating.** Regression tests or safety checks can be wired up as a gate. Experiments that don't pass get discarded.
- **Observability.** A dashboard to monitor your experiments.
- **Benchmark discovery.** `evo:discover` explores the repo, figures out what to measure, and instruments the evaluation.

## Install

### Claude Code

Inside Claude Code:

```
/plugin marketplace add evo-hq/evo
/plugin install evo@evo-hq-evo
```

Then reload Claude Code. The `/evo:discover` and `/evo:optimize` slash commands become available in any repo.

### Codex (local marketplace)

Codex plugins are installed from marketplaces. For a personal install:

```bash
mkdir -p ~/.codex/plugins
cp -R /absolute/path/to/evo ~/.codex/plugins/evo

mkdir -p ~/.agents/plugins
cat > ~/.agents/plugins/marketplace.json <<'JSON'
{
  "name": "evo-local",
  "interface": {
    "displayName": "Evo Local"
  },
  "plugins": [
    {
      "name": "evo",
      "source": {
        "source": "local",
        "path": "./.codex/plugins/evo"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
JSON
```

If `~/.agents/plugins/marketplace.json` already exists, merge the `evo` plugin entry into your existing `plugins` array instead of replacing the file.

Restart Codex, open the plugin directory, pick the `Evo Local` marketplace, and install `evo`.

Requirements: Claude Code or Codex, Python 3.12+, git, [uv](https://docs.astral.sh/uv/).

## Usage

Two skill entry points:

- **`evo:discover`** -- explores the repo, instruments the benchmark, runs baseline
- **`evo:optimize`** -- runs the optimization loop with parallel subagents until interrupted

Invoke them as:

- Claude Code: `/evo:discover`, `/evo:optimize`
- Codex: `$evo:discover`, `$evo:optimize`

The same `evo:discover` and `evo:optimize` skills are shared between Claude Code and Codex; only invocation syntax differs.

`evo:optimize` accepts optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `subagents` | 5 | Number of parallel subagents per round |
| `budget` | 5 | Max iterations each subagent can run within its branch |
| `stall` | 5 | Consecutive rounds with no improvement before auto-stopping |

Example: `evo:optimize subagents=3 budget=10 stall=3`

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

## Dashboard

The dashboard starts automatically when you run `evo:discover` (or `evo init`). When it comes up, the agent surfaces the URL in the chat:

```
Dashboard live: http://127.0.0.1:8080 (pid 12345)
```

If `8080` is busy, evo auto-increments (`8081`, `8082`, ...) and prints the actual port. You can also start it manually:

```bash
uv run --project /path/to/evo evo dashboard --port 8080
```

The chosen port is persisted to `.evo/dashboard.port` so repeat runs re-use it.

## Dev install

For working on evo itself (not just using it):

```bash
git clone https://github.com/evo-hq/evo
uv run --project /path/to/evo evo status
```

`uv run` resolves dependencies on first use -- no `pip install` step.

## TODO

- [ ] Distributed evaluation via [Harbor](https://github.com/harbor-framework/harbor) -- run benchmarks in containers instead of locally, use Harbor's cloud providers to parallelize.

## License

Licensed under the [Apache License 2.0](LICENSE).
