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
- **`evo dispatch` (optimization subagents only, host-specific).** On `claude-code`, the optimization subagents in step 5 are launched via `evo dispatch run --background` instead of the host's parallel-Task tool. This shares an explorer's KV cache across siblings of the same parent (~99% prefix reuse). On every other host (`codex`, `opencode`, `openclaw`, `hermes`, `generic`), `evo dispatch` is unsupported and exits 2 with guidance -- step 5 stays on the host's parallel-Task primitive there. The scan sub-agents in step 3 always use the host's parallel-Task tool regardless of host (they don't allocate experiments, so fork-cache doesn't apply).

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

**Step 0 (always, before anything else): ensure you have read `.evo/project.md`.** Its contents define the optimization metric and goal; every pattern you report MUST be tied to a failure mode of that goal. If you haven't read it yet in this session, read it now -- before listing experiments, opening any `outcome.json`, or spawning sub-agents. If the file is missing, stop and tell the user.

```bash
evo scratchpad          # full state: tree, best path, frontier, annotations, diffs, gates, what-not-to-try
evo frontier            # explorable nodes ranked by the configured strategy (JSON envelope: {strategy, nodes[{id,score,rank,...}], generated_at})
evo status              # one-line summary
evo annotations         # all annotations (filterable with --task/--exp)
evo path <id>           # root-to-node chain with scores
evo diff <id>           # diff vs parent
evo diff <id> <other>   # diff between any two experiments
evo gate list <id>      # effective gates for a node (inherited from ancestors)
```

### 2. Analyze state and do structural aggregation

From the scratchpad, frontier, traces, and annotations, determine:
- Which frontier nodes are most promising (`evo frontier` returns them already ranked under the configured strategy -- use its ordering rather than re-ranking; override with `evo frontier --strategy ...` only if you have a specific reason)
- What failure patterns are most common and impactful
- What strategies have been tried and their outcomes
- Which branches are plateauing or exhausted
- What gates exist on each frontier node (`evo gate list <id>`) -- subagents must satisfy these

**Read the "Awaiting Decision" section of the scratchpad.** Evaluated nodes (ran, bad outcome, not yet discarded) are a cross-agent signal: if three subagents in the last round produced evaluated nodes that all failed the same gate, surface the pattern -- maybe the gate is too tight, maybe the approach has a shared flaw. Either tell the next round to avoid it, or propose a brief that attacks it directly. Without this cross-cutting read, each subagent rediscovers the same wall independently.

**Structural pass.** For the evaluated nodes this round, load their `outcome.json` files into Python and aggregate: co-occurring `gate_failures`, shared zero-score task IDs in `benchmark.result.tasks`, recurring substrings across `error` fields.

**Emit intersections explicitly.** After computing the per-pattern sets (call them A, B, ...), MUST emit each pairwise intersection `A ∩ B` as a distinct pattern entry whenever at least 2 experiments exhibit both. Intersections carry different strategic implications from their components (compound failures warrant different briefs than single-failure clusters) and do not reconstruct from sub-agent summaries -- this is a parent-level aggregation that must happen inline.

**Improvers are a pattern too.** Enumerate the committed improvers (experiments with `outcome=committed` and `score > parent_score`) as a distinct pattern entry: they are candidate parent nodes for next-round branching and feed the brief's *Parent node* field.

Hold all these findings; step 4's brief-writing combines them with the scan sub-agents' findings from step 3.

### 3. Spawn scan sub-agents for cross-cutting free-text analysis

**Hard rule (primary delegation).** The orchestrator MUST spawn at least one scan sub-agent via your host's parallel-subagent tool in every round before emitting any pattern. This applies to all scan input -- `outcome.json`, `traces/task_*.json`, annotations, and `error` fields alike -- regardless of file size, structure, or whether the orchestrator believes a script would be faster. An inline Python aggregation over `outcome.json` does NOT substitute for delegation; it may supplement sub-agent findings (step 2's structural pass still runs), but step 3's scan sub-agents MUST still run. If you reach step 4 without a completed scan sub-agent call in step 3, you have violated this rule -- stop and spawn one.

**Narrow exception (verification).** After scan sub-agents have returned findings, the orchestrator MAY read individual trace files to: verify a specific finding before citing it in a brief, spot-check a pattern the orchestrator is unsure about, or pull a short quote for a brief's Objective or Pointer Traces field. These verification reads must be narrow (<=3 trace files per round, targeted at experiment IDs already surfaced by sub-agents). This exception does NOT let you skip the hard rule above -- it only governs what you may do after sub-agents have already run.

Partition the evaluated experiments into batches small enough that each sub-agent can read its batch's traces in one pass. Spawn one scan sub-agent per batch in a **single batch** using your host's parallel-subagent tool (see "Host conventions"). They must execute in parallel, not sequentially.

Pass this brief verbatim as the sub-agent's prompt:

> You are a read-only evo scan sub-agent. Do not run experiments or edit code.
>
> Start by reading `.evo/project.md` to understand the optimization goal and metric. All your findings should be relevant to this goal.
>
> Your batch: `[exp_IDs]`.
>
> For each experiment, read `outcome.json` and `traces/task_*.json`. Also consider `hypothesis` and prose `error` text.
>
> Find patterns that will populate the next round's subagent briefs:
> - **Shared failure causes** -- root-cause reasons recurring across 2+ experiments (the *why*, not the surface gate name). Feeds brief objectives.
> - **Wall patterns** -- approaches or gates multiple experiments consistently fail on. Feeds brief boundaries / anti-patterns.
> - **Compound-failure standouts** -- single experiments hitting multiple failure modes. Feeds brief pointer traces.
>
> Prioritize patterns tied to the goal's core failure modes or critical tasks. Deprioritize incidental observations. Skip: trace-shape statistics, fixture-structural facts, hypothesis-string-reuse, or anything the orchestrator can't act on in a brief.
>
> If your batch is still too heavy, partition further and spawn scan sub-agents recursively (same brief, smaller batch).
>
> Return JSON only: `{"findings": [{"description": "<short>", "experiment_ids": ["exp_XXXX", ...], "evidence": ["<short snippet>", ...]}]}`
>
> **Evidence must be verbatim quotes** from outcome.json fields, trace `messages`, or `error` text -- not paraphrases. Each description must be supported by the quoted evidence. **Do not speculate about causal chains** (e.g., "approach X regresses because it removes Y") unless a specific trace message or error field directly states that mechanism. If you cannot cite verbatim evidence for a finding, drop it -- err on under-reporting.
>
> Evidence: short quotes (<200 chars each), max 3 per finding.

Wait for all scan sub-agents to return. Reconcile near-duplicate findings (`timeout_error` ≈ `error_timeout`) by judgment and combine with the structural-pass findings from step 2.

**Verify every pattern before emitting it.** For each pattern in your final output, confirm that at least one reported experiment's outcome.json or trace content contains evidence that directly supports the pattern's description. If you cannot cite a specific field value or quoted message as evidence, drop the pattern. Do not emit speculative causal attributions ("approach X regresses because it removes Y") unless the trace or error text explicitly states that mechanism. This filter applies to both sub-agent findings and your own inline observations.

These unified, verified cross-cutting findings feed step 4's brief-writing.

### 4. Write subagent briefs

Write **one brief per subagent** with these four fields:

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

### 5. Spawn parallel optimization subagents

The mechanism depends on the host recorded by `evo init` (see `evo host show`). Both paths produce the same observable outcome: N parallel children, each allocating an experiment under its assigned parent and running the worker protocol up to budget. The fork-cache path is faster + cheaper when available.

#### 5a. claude-code → `evo dispatch`

On `claude-code`, dispatch one child per brief. Each call allocates a new experiment under `<parent>`, ensures an explorer session for that parent is warm (lazy on first dispatch), and forks a child via `claude -p --resume <SID> --fork-session`.

```bash
# Fan out N children in parallel
for brief in <each brief>:
  evo dispatch run \
    --parent <parent_id> \
    -m "<brief: objective + boundaries + pointer traces, formatted as you wrote it>" \
    --budget <budget> \
    [--explore-context "<focus hint, only on the first dispatch of the round>"] \
    --background

# Block until all complete
evo dispatch wait
```

The `--explore-context` flag is shared per parent (it shapes the explorer's read pass) -- pass it on the first dispatch from a given parent in a round, omit on subsequent ones. If you need a different focus mid-round, pass `--refresh-explorer` to force a rebuild.

Each child inherits the explorer's transcript (worker protocol from `subagent/SKILL.md` + the parent's code reads), and its first user message is just the brief + budget. You don't pass the protocol again per child -- that's what the explorer's KV cache is for.

#### 5b. all other hosts → host's parallel-Task primitive (existing path)

Spawn all subagents in a **single batch** using your host's parallel-subagent tool (see "Host conventions"). They must execute in parallel, not sequentially -- serial execution defeats the per-round width.

Pick a faster model for straightforward briefs and a stronger model for harder ones requiring deeper trace analysis, if your host exposes per-call model selection.

Each subagent prompt must include:
- An instruction to read `skills/subagent/SKILL.md` and follow its protocol
- The four-field brief verbatim (objective, parent, boundaries/anti-patterns, pointer traces)
- The iteration budget
- A one-paragraph scratchpad summary (current best score, frontier nodes, recent failures) for context

### 6. Collect results and update state

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

### 7. Continue or stop

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
