---
description: Internal protocol for evo optimization subagents. Not user-invocable -- read by subagents spawned from /optimize.
---

# Evo Subagent Protocol

You are an evo optimization subagent. You have been given a **direction**, **specific ideas**, a **parent experiment ID**, and an **iteration budget** by the orchestrator.

Your job: improve the target by running experiments within your assigned direction.

## Important: Working Directory

All `uv run evo ...` commands run from the **main repo root** (not inside the worktree).
Only file reads/edits use the **worktree path** returned by `evo new`. The worktree is just
an isolated copy of the codebase where you make your changes.

## First Steps

1. Read `.evo/project.md` to understand the target, what can be changed, and how to interpret results.
2. Read the scratchpad for current state: `uv run evo scratchpad`
3. Study the traces the orchestrator pointed you to:
   ```bash
   uv run evo traces <exp_id> <task_id>
   ```
   Understand the failure patterns relevant to your direction.

## Iteration Loop

Repeat up to **budget** times:

### 1. Formulate hypothesis

Use the orchestrator's ideas as starting points but apply your own judgment based on what you see in the traces. Be specific -- "add a confirmation step before database writes" not "improve accuracy."

### 2. Create experiment

```bash
uv run evo new --parent <parent_id> -m "<your hypothesis>"
```

Parse the JSON output to get the experiment ID and worktree path.

### 3. Edit the target

Read and edit the target file(s) using the **full worktree path** from `evo new` output (the `"target"` and `"worktree"` fields). For example, if `evo new` returns `"target": "/path/to/.evo/worktrees/exp_0005/src/agent.py"`, read and edit that exact path.

You may edit anything within the target scope. Do NOT modify benchmark, gate, or framework code.

### 4. Run the experiment

```bash
uv run evo run <exp_id>
```

This runs benchmark + gate and prints the result. Use timeout of 600000ms (10 minutes).

### 5. Analyze the result

- **Committed** (score improved + gate passed): Read failing task traces to find the next weakness. Use this experiment as the parent for your next iteration.
- **Discarded** (score regressed or gate failed): Understand why. Try a different approach in the next iteration, branching from the original parent (not the discarded one).
- **Failed** (infrastructure error, non-zero exit): Report the error and **stop**. Do not retry.

### 6. Annotate

```bash
uv run evo annotate <exp_id> "<what you changed, what happened, and why>"
```

Always annotate so other agents can learn from your experiments.

### 7. Decide: continue or stop

Continue if:
- You have budget remaining
- You see a promising follow-up hypothesis
- Your last experiment was committed (keep pushing this branch)
- OR your last experiment was discarded but you have a meaningfully different idea

Stop if:
- Budget exhausted
- Infrastructure failure
- You've tried multiple variations with no improvement and have no new ideas

When continuing after a committed experiment, update your parent to the newly committed experiment ID.

## Rules

- Do NOT run `evo init` or `evo reset`
- You MAY run `evo discard <your_exp_id> --reason "..."` if you realize mid-edit that an approach is wrong before running
- Always annotate your experiments
- If `evo run` fails with non-zero exit (infrastructure error), stop and report
- Stay within your assigned direction -- don't drift into unrelated changes

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
