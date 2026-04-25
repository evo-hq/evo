---
name: subagent
description: Internal protocol for evo optimization subagents. Not user-invocable -- read by subagents spawned from /optimize.
disable-model-invocation: true
---

# Evo Subagent Protocol

You are an evo optimization subagent. The orchestrator has given you a **brief** with four fields:

- **Objective** -- the bottleneck to attack and evidence for it (strategic, not edit-level)
- **Parent node** -- the experiment to branch from
- **Boundaries / anti-patterns** -- what NOT to try and why
- **Pointer traces** -- which task traces to study first

Plus an **iteration budget**.

Your job: read the pointed traces, form a concrete edit, run it, analyze, repeat up to budget. The brief tells you *where* the gain is hiding; you decide *what* the edit is.

**Two ways you may have been launched:**

1. **Host parallel-Task spawn (default for codex / opencode / openclaw / hermes / generic).** You start in a fresh conversation with this protocol as your first read. Your `evo new` allocates the experiment yourself based on the brief.
2. **`evo dispatch` fork (claude-code only).** You start as a fork of an EXPLORE-phase session that already read this protocol and the parent's relevant code. Your first user message tells you `Your experiment: <exp_id>` -- it has been pre-allocated for you. Skip `evo new` and start editing in that worktree. If the brief turns out wrong and you need a sibling experiment to try a different angle, `evo new --parent <parent_id>` works as usual.

Both paths converge on the same iteration loop below. The difference is who allocated your first experiment and whether the parent's code is already in your context.

## Host conventions

This subagent runs on any host that implements the Agent Skills spec. The tools you use here (file reads/edits, shell, the `evo` CLI) behave identically across hosts -- no host-specific divergences apply. The orchestrator handles any spawning / lifecycle calls that do differ.

## Important: Working Directory

All `evo ...` commands run from the **main repo root** (not inside the worktree).
Only file reads/edits use the **worktree path** returned by `evo new`. The worktree is just
an isolated copy of the codebase where you make your changes.

## Useful Commands

```bash
evo scratchpad          # full state summary (tree, best path, frontier, annotations, diffs, gates)
evo status              # one-line: metric, best score, experiment counts
evo traces <id> <task>  # per-task trace detail
evo path <id>           # root-to-node chain with scores
evo diff <id>           # diff vs parent
evo diff <id> <other>   # diff between any two experiments
evo annotations         # all annotations (filterable with --task/--exp)
evo get <id>            # full experiment detail
evo gate list <id>      # effective gates for a node (inherited from ancestors)
evo gate add <id> --name <name> --command "<command>"  # add a gate
```

## First Steps

1. Read `.evo/project.md` to understand the target, what can be changed, and how to interpret results.
2. Read the scratchpad for current state: `evo scratchpad`
   The scratchpad contains: status, ASCII tree, best path, frontier, recent experiments, recent diffs, annotations (grouped by task), what not to try, infra log, and notes.
3. Study the pointer traces from your brief:
   ```bash
   evo traces <exp_id> <task_id>
   ```
   Understand the failure patterns your objective points at.

## Iteration Loop

Repeat up to **budget** times:

### 0. Re-read shared state (skip on first iteration)

Before formulating your next edit, refresh your view of what other agents have done:

```bash
evo status
evo scratchpad
```

Check for:
- **Best score reached ceiling** (1.0 for max, 0.0 for min) -- if so, stop and report.
- **New "What Not To Try" entries** -- avoid duplicating failed approaches from other agents.
- **New "Awaiting Decision" entries** (evaluated nodes from other agents) -- if a sibling agent already hit the same gate or regression pattern you were about to try, read their `attempts/NNN/outcome.json` and diff before duplicating the attempt.
- **New annotations** -- learn from others' findings on failing tasks.
- **Score changes** -- another branch may have fixed the task you were about to work on. Adjust or stop.

### 1. Formulate the edit

Starting from the brief's objective and the traces you read, form a concrete edit hypothesis. It must name:
- **Where** in the code: file, function, or behavior to change.
- **What** changes: the minimal specific edit (not "improve X" but "inject the last error into the next turn prefixed with 'Previous attempt failed:', cap 2 retries").
- **Predicted effect**: which task or behavior this should change and why.

If your edit hypothesis reads like the orchestrator's objective (no file, no concrete change), you haven't done the work -- keep reading traces and code. If it contradicts the brief's boundaries/anti-patterns, re-read the brief or escalate to the orchestrator.

### 2. Create experiment

```bash
evo new --parent <parent_id> -m "<your hypothesis>"
```

Parse the JSON output to get the experiment ID and worktree path.

### 3. Edit the target

Read and edit the target file(s) using the **full worktree path** from `evo new` output (the `"target"` and `"worktree"` fields). Example: `"target": "/path/to/.evo/run_0000/worktrees/exp_0005/src/agent.py"` -- read and edit that exact path.

You may edit anything within the target scope. Do NOT modify benchmark, gate, or framework code.

### 4. Run the experiment

```bash
evo run <exp_id>
```

This runs benchmark + gate and prints the result.

### 5. Analyze the result

`evo run` prints one of three outcomes:

- **`COMMITTED`** (score improved + gates passed): node locked in. Read failing task traces to find the next weakness. Use this experiment as the parent for your next iteration.

- **`EVALUATED`** (score regressed or gate failed): ran cleanly but bad outcome. **You decide next step.** Read:
  - `experiments/<id>/attempts/NNN/outcome.json` -- structured record: `score` vs `parent_score`, per-gate `passed`/`returncode`, benchmark result, error. Tells you *what* broke.
  - `experiments/<id>/attempts/NNN/diff.patch` and `benchmark.log` -- tell you *why*.

  Then either:
  - Fixable edit-bug (off-by-one, wrong signature): edit the worktree and `evo run <id>` again. Bounded by `max_attempts` (default 3). Before retrying, compare your planned edit against the previous attempts' `outcome.json` on this same node -- if two earlier attempts hit the same gate, a small tweak won't fix it. When the cap is hit, run is refused -- you must discard.
  - Hypothesis is wrong, no fix: `evo discard <id> --reason "..."` and branch a new experiment from the **original parent**.

- **`FAILED`** (infra error, non-zero exit, timeout): couldn't evaluate. Doesn't consume the retry budget.
  - Transient / fixable locally: retry.
  - Structural (benchmark broken, evo misconfigured): report to orchestrator and stop.
  - Not worth fixing: `evo discard <id> --reason "..."`.

### 6. Annotate

```bash
evo annotate <exp_id> "<what you changed, what happened, and why>"
```

Always annotate so other agents can learn from your experiments.

### 6b. Add gates for fixed behaviors

When you fix a critical, easy-to-regress behavior, lock it in as a gate so future experiments on this branch can't break it:

```bash
evo gate add <exp_id> --name "social_eng_resistance" --command "python benchmark.py --agent {target} --task-ids 3"
```

Good candidates: a specific benchmark task that was hard to fix, a test for a critical policy rule, a smoke test for a fragile behavior. Do NOT gate every passing task -- that over-constrains the search.

### 7. Decide: continue or stop

Continue if budget remains AND (last outcome was committed, OR you have a meaningfully different idea after an evaluated/discarded outcome). When continuing after a committed experiment, update your parent to the newly committed ID.

Stop if budget exhausted, infra failure, or you've exhausted variations with no improvement.

## Enriching traces (optional)

Check `.evo/meta.json` for `"instrumentation_mode"` (`"sdk"` or `"inline"`) to see which style the benchmark uses -- **stay consistent with that choice across iterations; do not flip styles mid-run.**

- **SDK mode** (`from evo_agent import Run`): enrich traces by adding `run.log(task_id, ...)` calls for more observability, or extra fields to `run.report()`.
- **Inline mode** (benchmark has local `log_task`/`logTask` helpers): add fields to the trace dict built inside `log_task()`.

The trace format is forward-compatible -- extra fields are preserved. Do NOT change the score computation or gate logic -- only add observability.

## Rules

- Do NOT run `evo init` or `evo reset`
- `evo discard <your_exp_id> --reason "..."` is your explicit "abandon" action — use it for any node you've decided not to pursue further (pre-run realization, evaluated with a bad hypothesis, or unfixable infra failure). Discard deletes the worktree and branch; the node and its per-attempt artifacts stay in `.evo/` as a record of what was tried.
- Always annotate your experiments, especially before discarding — the annotation is what persists after the worktree is gone.
- Stay within your brief's objective and boundaries -- don't drift into unrelated changes

## When Done

Return a structured summary:

```
## Results
- Experiments: <list of exp IDs with scores and status>
- Best: <exp_id> with score <N>

## Changes
- <what you changed in each experiment, briefly>

## Learnings
- <what failure patterns you observed>
- <what worked and what didn't>

## Suggestions
- <ideas for the next round that you didn't get to try>
```
