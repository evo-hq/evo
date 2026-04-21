---
name: optimize
description: Run the evo optimization loop with parallel subagents until interrupted.
argument-hint: "[subagents=N] [budget=N] [stall=N]"
---

Run the `evo` optimization loop. Each round, the orchestrator writes structured briefs and spawns parallel subagents that execute within them. Each subagent is semi-autonomous: it reads the pointer traces, forms the concrete edit, runs experiments, and can iterate within its branch. Runs until interrupted or the stall limit is reached.

## Host conventions

This skill runs on any host that implements the Agent Skills spec. When the body uses generic phrases, apply the host's best-fit equivalent:

- **"spawn N subagents in parallel"** -- use your host's parallel-subagent or background-task tool if you have one (e.g. `Agent` with `run_in_background`, `spawn_agent` + `wait_agent`, `spawn_agents_on_csv` for batch). Respect the host's concurrency cap -- if N exceeds it, run in batches. If the host has no parallel-subagent tool, run them serially and note the reduced round width in the final summary.
- **Slash commands shown in user-facing copy** (e.g. `/evo:optimize`) -- translate to your host's mention syntax when speaking to the user (e.g. `$evo optimize` on Codex -- plugin namespace then skill name, separated by a space).

## Configuration

These defaults can be overridden via arguments: `/optimize [subagents=N] [budget=N] [stall=N]`

- **subagents**: number of parallel subagents per round (default: 5)
- **budget**: max iterations each subagent can run within its branch (default: 5)
- **stall**: consecutive rounds with no improvement before auto-stopping (default: 5)

## Prerequisites

- Workspace must be initialized (`evo status` should succeed)
- A baseline experiment must be committed (run `/discover` first)
- All benchmark dependencies must be available in the environment

## Architecture

```
Orchestrator (this agent):
  - Reads state, identifies failure patterns cross-cutting the tree
  - Writes one brief per subagent: objective + parent + boundaries + pointer traces
  - Verifies briefs are diverse (no two attacking the same surface)
  - Collects results, prunes dead branches, adjusts strategy

  Subagent A (brief, budget: N iterations):
    - Reads its pointer traces, forms the concrete edit
    - Creates experiment, edits target, runs benchmark, analyzes
    - If budget remains and sees a promising follow-up, continues
    - Can run up to N serial experiments on its own branch
    - Returns: what it tried, what worked, what it learned

  Subagent B (different brief, budget: N iterations):
    - Same protocol, non-overlapping objective
    ...
```

Both layers read traces; the depth differs. The orchestrator scans for cross-cutting patterns (which failures are common, which branches plateau) -- enough to pick N non-overlapping briefs. Subagents read their pointer traces in depth, enough to commit to a concrete edit. Structured briefs are what prevent parallel subagents from duplicating each other's work.

**Trace instrumentation style**: `.evo/meta.json`'s `instrumentation_mode` records `sdk` vs `inline`. Subagents must stay consistent with it (see `skills/subagent/SKILL.md` for details).

## The Loop

Repeat until interrupted or stall limit reached:

### 1. Read current state

```bash
evo scratchpad          # full state: tree, best path, frontier, annotations, diffs, gates, what-not-to-try
evo frontier            # explorable nodes (JSON)
evo status              # one-line summary
evo annotations         # all annotations (filterable with --task/--exp)
evo path <id>           # root-to-node chain with scores
evo diff <id>           # diff vs parent
evo diff <id> <other>   # diff between any two experiments
evo gate list <id>      # effective gates for a node (inherited from ancestors)
```

On the first iteration, also read `.evo/project.md` to understand the optimization surface.

### 2. Analyze state and write subagent briefs

From the scratchpad, frontier, traces, and annotations, determine:
- Which frontier nodes are most promising
- What failure patterns are most common and impactful
- What strategies have been tried and their outcomes
- Which branches are plateauing or exhausted
- What gates exist on each frontier node (`evo gate list <id>`) -- subagents must satisfy these

**Read the "Awaiting Decision" section of the scratchpad.** Evaluated nodes (ran, bad outcome, not yet discarded) are a cross-agent signal: if three subagents in the last round produced evaluated nodes that all failed the same gate, surface the pattern -- maybe the gate is too tight, maybe the approach has a shared flaw. Either tell the next round to avoid it, or propose a brief that attacks it directly. Without this cross-cutting read, each subagent rediscovers the same wall independently.

Then write **one brief per subagent** with these four fields:

1. **Objective** -- one sentence describing the bottleneck to attack and the evidence for it. Should name *where in the system's behavior* the gain is hiding (e.g., "tool-use error recovery fails after the first bad call across tasks 2, 5, 7") but **must not name specific files, functions, or concrete edits** -- that's the subagent's job after it reads the code.
2. **Parent node** -- which experiment to branch from.
3. **Boundaries / anti-patterns** -- what this subagent should NOT try, explicitly called out with reasons. Include approaches already tried and discarded (from "What Not To Try"), gates it must not regress, and anything adjacent subagents in this round are doing (so it doesn't duplicate).
4. **Pointer traces** -- task IDs the subagent should study first, with a one-line reason each.

Be specific and bounded. Vague briefs like "improve accuracy" cause subagents to duplicate each other's work; structured briefs prevent it.

**Diversity check (before spawning).** Re-read the N briefs side by side. If two briefs:
- point at the same objective phrased differently, OR
- cite overlapping pointer traces without meaningfully different framings, OR
- attack the same area of the system,

merge or re-scope one of them. The frontier/pruning logic handles tree-level exploration vs exploitation algorithmically -- the orchestrator's job is just to make sure the round's N briefs don't collapse onto each other.

### 3. Spawn parallel subagents

Spawn all subagents in a **single batch** using your host's parallel-subagent tool (see "Host conventions" for examples). They must execute in parallel, not sequentially -- serial execution defeats the per-round width.

Pick a faster model for straightforward briefs and a stronger model for harder ones requiring deeper trace analysis, if your host exposes per-call model selection.

Each subagent prompt must include:
- An instruction to read `skills/subagent/SKILL.md` and follow its protocol
- The four-field brief verbatim (objective, parent, boundaries/anti-patterns, pointer traces)
- The iteration budget
- A one-paragraph scratchpad summary (current best score, frontier nodes, recent failures) for context

### 4. Collect results and update state

After all subagents complete:

- Review each subagent's summary
- Record the round's best score and compare to the previous best
- If no subagent improved the score, increment the stall counter
- If any improved, reset the stall counter
- Check if subagents added new gates -- note these in your state tracking
- If multiple experiments failed the same gate, consider whether the gate is too restrictive or the briefs were aimed at the wrong surface

**Cross-cut the round's evaluated nodes.** Before moving on, read `experiments/<id>/attempts/NNN/outcome.json` for each evaluated node from this round. The structured `gates[]` entries and `benchmark.result` let you spot shared failure modes the subagent summaries may have glossed over (e.g., three different subagents produced evaluated nodes whose gate_failures all included `refund_flow` -- that's a structural constraint the next round must confront, not three independent bad hypotheses).

Prune dead branches where 3+ children all regressed:
  ```bash
  evo prune <exp_id> --reason "exhausted: N children all regressed"
  ```
Update notes with cross-cutting learnings:
  ```bash
  evo set <exp_id> --note "key insight from round N"
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
- The winning diff: `evo diff <best_exp_id>`
- Suggested next steps if the score hasn't converged

Go back to step 1.
