---
name: optimize
description: Run the evo optimization loop with parallel subagents until interrupted.
argument-hint: "[subagents=N] [budget=N] [stall=N]"
---

Run the `evo` optimization loop. Spawns parallel subagents that explore different directions simultaneously. Each subagent is semi-autonomous -- it reads traces, formulates hypotheses, and can run multiple iterations within its branch.

Runs continuously until interrupted or the stall limit is reached.

## Configuration

These defaults can be overridden via arguments: `/optimize [subagents=N] [budget=N] [stall=N]`

- **subagents**: number of parallel subagents per round (default: 5)
- **budget**: max iterations each subagent can run within its branch (default: 5)
- **stall**: consecutive rounds with no improvement before auto-stopping (default: 5)

## Prerequisites

- Workspace must be initialized (`uv run evo status` should succeed)
- A baseline experiment must be committed (run `/discover` first)
- All benchmark dependencies must be available in the environment

## Architecture

```
Orchestrator (this agent):
  - Reads state, identifies failure patterns, picks strategic directions
  - Gives each subagent a direction + specific ideas to try
  - Collects results, prunes dead branches, adjusts strategy

  Subagent A (direction + ideas, budget: N iterations):
    - Reads traces itself, analyzes failures in its focus area
    - Formulates specific hypothesis informed by orchestrator's ideas
    - Creates experiment, edits target, runs benchmark, analyzes
    - If budget remains and it sees a promising follow-up, continues
    - Can run up to N serial experiments on its own branch
    - Returns: what it tried, what worked, what it learned

  Subagent B (different direction + ideas, budget: N iterations):
    - Same autonomy, different focus area
    ...
```

Both layers analyze traces. The orchestrator does cross-cutting analysis (which patterns are most common, which branches plateau). Subagents do focused analysis within their assigned direction.

**Note on trace instrumentation:** Benchmarks use either `evo-agent` SDK or an inline helper -- check `.evo/meta.json` for `instrumentation_mode`. Subagents must stay consistent with the chosen style. If you need richer trace data for failure analysis, add fields to `run.report()` (SDK) or the trace dict inside `log_task()` (inline). The trace format is forward-compatible.

## The Loop

Repeat until interrupted or stall limit reached:

### 1. Read current state

```bash
uv run evo scratchpad          # full state: tree, best path, frontier, annotations, diffs, gates, what-not-to-try
uv run evo frontier            # explorable nodes (JSON)
uv run evo status              # one-line summary
uv run evo annotations         # all annotations (filterable with --task/--exp)
uv run evo path <id>           # root-to-node chain with scores
uv run evo diff <id>           # diff vs parent
uv run evo diff <id> <other>   # diff between any two experiments
uv run evo gate list <id>      # effective gates for a node (inherited from ancestors)
```

On the first iteration, also read `.evo/project.md` to understand the optimization surface.

### 2. Analyze and plan directions

From the scratchpad, frontier, traces, and annotations, determine:
- Which frontier nodes are most promising
- What failure patterns are most common and impactful
- What strategies have been tried and their outcomes
- Which branches are plateauing or exhausted
- What gates exist on each frontier node (`evo gate list <id>`) -- remind subagents of constraints they must satisfy

Formulate N directions (one per subagent). Each direction should include:
- A **focus area** (e.g., "tool-use accuracy", "multi-step reasoning", "context management")
- **Specific ideas** to try (e.g., "add a confirmation step before writes", "summarize conversation every 5 turns") -- give the subagent concrete starting points, not just vague themes
- Which **frontier node** to branch from
- Key **traces to study** (task IDs with interesting failures relevant to this direction)

### 3. Spawn parallel subagents

Spawn all subagents in a **single batch** using your host's multi-agent tool (`spawn_agent` in Codex, Agent tool in Claude Code). **All subagents must run in the background** so they execute in parallel.

Prefer a faster model for straightforward hypotheses and a stronger model for harder ones requiring deeper reasoning.

Each subagent prompt must include:
- An instruction to read `skills/subagent/SKILL.md` and follow its protocol
- The assigned direction and specific ideas to try
- The parent experiment ID to branch from
- Key task IDs / traces to study
- The iteration budget
- A brief scratchpad summary (current best score, frontier nodes, recent failures)

### 4. Collect results and update state

After all subagents complete:

- Review each subagent's summary
- Record the round's best score and compare to the previous best
- If no subagent improved the score, increment the stall counter
- If any improved, reset the stall counter
- Check if subagents added new gates -- note these in your state tracking
- If multiple experiments failed the same gate, consider whether the gate is too restrictive or the direction is wrong
- Prune dead branches where 3+ children all regressed:
  ```bash
  uv run evo prune <exp_id> --reason "exhausted: N children all regressed"
  ```
- Update notes with cross-cutting learnings:
  ```bash
  uv run evo set <exp_id> --note "key insight from round N"
  ```

### 5. Continue or stop

**Continue** if:
- Stall counter < stall limit
- User hasn't interrupted
- Score hasn't reached the theoretical maximum

**Stop** if:
- Stall counter >= stall limit (N consecutive rounds with no improvement)
- Score reached theoretical maximum (1.0 for max metric, 0.0 for min metric)
- User interrupted

On stop, print a final summary:
- Best score achieved and experiment ID
- Total experiments run across all rounds
- The winning diff: `uv run evo diff <best_exp_id>`
- Suggested next steps if the score hasn't converged

Go back to step 1.
