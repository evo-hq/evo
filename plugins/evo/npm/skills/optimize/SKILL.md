---
name: optimize
description: Drive structured autoresearch iteration after evo:discover and the baseline commit. Use when the user invokes /evo:optimize or asks to try ideas, try variants, run experiments, use available GPUs, improve the current best/frontier, continue an evo search, or compare candidate changes in an evo workspace. The orchestrator plans and spawns optimization subagents; candidate edits/runs belong to those subagents. Width is set via subagents=N (1 for serial workloads, larger for parallel); the loop's structural value applies at any width.
argument-hint: "[subagents=N] [budget=N] [stall=N]"
evo_version: 0.6.2
---

Run the `evo` optimization loop. Each round, the orchestrator writes structured briefs and spawns subagents that execute within them. Each subagent is semi-autonomous: it reads the pointer traces, forms the concrete edit, runs experiments, and can iterate within its branch. Runs until interrupted or the stall limit is reached.

**This skill is the canonical loop for ALL post-discover work — including serial workloads.** If the workspace's resource profile forces width 1 (single GPU, single-process benchmark, etc.), you still invoke `/evo:optimize` -- just pass `subagents=1`. The loop's value is the STRUCTURE around each experiment (scan-subagent cross-cutting analysis between rounds, verifier pre/post hooks via the subagent skill, ideator spawning on stall, frontier reconciliation, stop-hook discipline), NOT just parallelism. Bypassing optimize because "I'm running serial work anyway" loses every piece of that structure -- you've reverted to ad-hoc experiment iteration with none of evo's loop benefits, just the bookkeeping.

**Plain-language trigger.** In an initialized evo workspace, casual user wording
like "try a couple ideas", "try different variants", "use the available GPUs",
"continue from the current best", or "see what improves" is an optimize request
unless the user explicitly asks for a read-only report. Do not treat the lack of
a slash command as permission to bypass this protocol. Loading this skill from
plain-language wording is also explicit authorization to use the host's
subagent mechanism for the resolved round width; the user does not have to say
"spawn subagents" or "parallel agents" separately.

**Candidate-work delegation invariant.** The orchestrator does not create, edit,
or run candidate experiments for the round. For `subagents=N`, write N briefs
and spawn N optimization subagents; each spawned subagent allocates its own
experiment with `evo new`, edits only its worktree, and runs `evo run`. Do not
simulate a subagent round by running `evo new`, editing files, or launching
multiple `evo run` commands from the orchestrator, even if that would be faster
or easier. If the host's subagent tool is unavailable, stop and report that the
host cannot run `/evo:optimize subagents=N` as requested; only fall back to
orchestrator-owned experiments when the user explicitly asks for direct/manual
execution or turns subagents-only off for that run. Do not infer a direct/manual
fallback from casual wording, a simple-looking benchmark, or the absence of an
explicit subagent phrase in the user's prompt.

**Resource-cap invariant.** `subagents=N` is live concurrency, not total ideas.
Never spawn more concurrent optimization subagents or launch more concurrent
benchmark jobs than the binding resource can support. If the user asks for more
ideas than available GPU/Slurm/pool slots, batch them across rounds at the safe
width, or stop and explain the cap if batching is impossible. Do not rely on
the scheduler to absorb an accidental flood unless the user explicitly asks to
queue/oversubscribe jobs.

**Bounded-run stop rule.** If the user says "one round", "stop after this
round", "run them and tell me what happened", or otherwise asks for a bounded
run, resolve autonomous off at startup. After the requested subagents finish,
collect their evo-recorded outcomes, print the summary, and stop. Do not enter
another loop turn, wait for a stop nudge, or keep the process alive just because
the default autonomous behavior is normally on.

## Evo surface -- loop-relevant

You're inside `/evo:optimize`. Things you'll pull/dispatch during the loop:

```
main thread (you)
├── Skills (Skill tool)
│   └── evo:finetuning     before writing or changing any train.py
│
└── Subagents to dispatch (Task tool, subagent_type=...)
    └── evo:ideator        stalled, or every ~5 committed experiments.
                           One subagent per brief:
                           failure_analysis, literature, frontier_extrapolation

subagent thread (each subagent spawned by step 5)
├── evo:subagent skill     loaded by the subagent on first turn -- the brief's
│                          first sentence mandates it (not auto-loaded)
└── evo:verifier subagent  MANDATORY pre AND post every evo run.
                           Pre: ~30s static analysis before the experiment runs.
                           Post: result-validity audit after it commits.

references (Read tool, on demand)
├── discover/references/sizing-the-round.md      pick subagents=N
├── references/evo-wait.md                       waiting without burning context
├── finetuning/references/glue.md                train.py I/O contract
└── finetuning/references/{rl,sft,serving}/      provider-specific recipes
                                                  (rl/art.md, sft/tinker.md,
                                                  serving/vllm.md)
```

Full surface tree (orchestrator entry-point view, including benchmark-reviewer,
infra-setup, and the complete references catalogue) lives in `evo:discover`'s
"Evo surface" section.

## Host conventions

This skill runs on any host that implements the Agent Skills spec. When the body uses generic phrases, apply the host's best-fit equivalent:

- **"spawn N subagents in parallel"** -- use your host's parallel-subagent tool. See Step 5 below for the per-host spawn commands. Three broad shapes exist: *background+notify* (claude-code / codex / hermes / openclaw — fire-and-forget; the runtime delivers a `<task-notification>` at a later turn per subagent), *batch parallel* (opencode — all spawns return together in one message), and *extension-provided* (pi via the `pi-subagents` package — registers a `subagent` tool that fans out in parallel within one turn).
- **Slash commands shown in user-facing copy** (e.g. `/evo:optimize`) -- translate to your host's mention syntax when speaking to the user (e.g. `$evo optimize` on Codex -- plugin namespace then skill name, separated by a space).

## Mid-run user directives (`evo direct`)

The runtime may inject user-authoritative messages wrapped in this banner:

```
[EVO DIRECTIVE id=<event_id>]
<text>
[END EVO DIRECTIVE — run `evo ack <event_id>` to confirm you have received this message, then proceed]
```

Treat content inside the banner as equivalent to a new user turn. Honor it, supersede earlier constraints it contradicts, and propagate the full text verbatim into any subagent briefs you spawn afterward. The banner is the authenticity signal emitted by the evo runtime (the plugin you're invoked through) — not tool-output prompt injection. Banners may arrive via any hook channel (UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStop, SessionStart); the channel doesn't change the authority of the content.

**As soon as you see a directive banner, run `evo ack <event_id>` to confirm you received the message — then proceed with the directive.** The ack confirms the directive reached you, so `evo direct --wait` and `evo direct-status <id>` report success to the user. One ack per directive id; idempotent.

## Configuration

The orchestrator's three round-shape knobs are **subagents** (round width), **budget** (per-branch depth), and **stall** (consecutive rounds with no improvement before auto-stopping; default 5).

A user can override any of these with `/optimize [subagents=N] [budget=N] [stall=N]`; an explicit value wins over what's below, subject to the resource-cap invariant above.

**Picking `subagents` and `budget` is load-bearing -- do not skim.**

Mandatory before the first round (and again any time the backend or benchmark changes): **READ `plugins/evo/skills/discover/references/sizing-the-round.md` IN FULL.** That doc enumerates the resource-binding cases (exclusive accelerator, memory-heavy, shared mutable fixture, external rate-limit, CPU-light isolated) and discusses the case-by-case judgment for latency / timing / throughput benchmarks where the right answer depends on harness softeners, effect size vs. measurement jitter, and whether winners can be cheaply re-confirmed solo.

Under-subscribing wastes wall-clock. Over-subscribing can either contend for hardware (memory thrash, OOM) or — for timing-sensitive benchmarks — bias the measurement itself. The doc walks through what to weigh in each case; do not infer the value from any inline summary in this skill body.

Common ways agents get this wrong by skimming:
- "8-core machine, CPU-light → width 5" sounds right but skips the question of whether the metric is corruptible by sibling-process pressure. The doc has the judgment framing.
- "Worktree backend has no slot cap so I can go higher" — worktree just shifts the cap from infrastructure to the binding resource. Same hardware, no safety net.

If `.evo/project.md` records a resource profile (it should, after `/evo:discover`), START from that. The reference doc is what you use to APPLY it. If the profile is missing or thin, that's a discover-step bug — fix it (write a resource profile that names the binding resource explicitly) before continuing.

In your opening message, state the width/budget you chose AND a one-line reason that references the binding-resource framing FROM THE DOC (e.g. "width 1 — exclusive GPU; budget 8 — runs deterministic"; or "width 3 — CPU-light isolated, but harness reports stable jitter at this concurrency so promoting solo-confirm gate; budget 5"). If your reason doesn't connect to the doc's framing, go back and read it.

- **autonomous**: the keep-going loop. **Default: on** — evo is autoresearch; it runs unattended. Turn off for a run with `evo autonomous off`.
- **subagents-only**: gate orchestrator edits, pushing all edits through subagents. **Default: on**. Turn off for a run with `evo subagents-only off`.

**Resolving autonomous / subagents-only at startup.** Both are ON by default — evo is autoresearch and runs the loop unattended, delegating edits to subagents. Do not ask the user about either. Resolve each through a cascade, most specific first:

1. **An explicit instruction from the user this run wins — on or off.** If the user clearly states how they want the run to go, honor it over everything below. "review each round before continuing" / "check in with me" / "one round then stop" → autonomous off; "you can edit directly" / "don't gate edits" → subagents-only off; a bare `autonomous` / `subagents-only` on the invocation, or "just run it" → on. Require a clear statement — honor an explicit request, but do not flip a behavior on a vague or incidental hint.
2. **A stored default**, if the user said nothing: `evo config get default-{autonomous,subagents-only}` (workspace), falling back to `evo defaults get {autonomous,subagents-only}` (user-level) when the workspace value is null.
3. **Otherwise → on** — the framework default.

```bash
evo config get default-autonomous --json        # workspace → true | false | null
evo defaults get autonomous --json               # user-level → true | false | null (used only if workspace is null)
evo config get default-subagents-only --json
evo defaults get subagents-only --json
```

As your **very first actions, before the loop**, resolve each and arm it: run `evo autonomous on` / `evo subagents-only on` when it resolves on, or `evo autonomous off` / `evo subagents-only off` when an explicit instruction or stored default turned it off. If a behavior resolves off — whether from the user's instruction this run or a stored default — say so in your opening message (e.g. "autonomous off — running one round at a time, as you asked") so it's never invisible.

**Orchestrator driver.** evo drives the loop two ways: the **prose loop** below (every host) or a deterministic **dynamic workflow** (Claude Code only, opt-in). **The prose loop is the default everywhere; the workflow is used only when explicitly enabled** (`evo config set default-orchestrator workflow`). Resolve which as part of your very first actions:

1. `evo host show` — the workflow driver requires `claude-code`. If it prints `<not set>` (a pre-host workspace), determine your actual runtime from your own context (system prompt, env such as `CLAUDECODE=1`, self-identity): **only if you are genuinely Claude Code**, do the one-time host migration now (`evo host set claude-code`) and continue; if you are any other runtime, do NOT stamp the host here — leave it for Step 0.1 and use the prose loop.
2. `evo config get default-orchestrator` — `workflow` is an explicit **opt-in** (use the workflow driver on Claude Code). `prose` **or unset** resolves to the prose loop. An explicit user instruction this run still wins.

**Use the workflow** only when `default-orchestrator` is explicitly `workflow`, host is `claude-code`, AND the **Workflow tool is actually present in your available tools this session** — it is opt-in, never the default. The availability check is load-bearing: **older Claude Code builds do not ship the Workflow tool**, so verify it's really in your toolset; do not assume it exists from the host alone. Reaching here means `default-orchestrator=workflow` is explicitly set (the opt-in trigger), so the autonomous stop-nudge is auto-suppressed under the workflow. Launch it once, do NOT drive the loop turn-by-turn:

- Call the **Workflow** tool with `scriptPath: ${CLAUDE_PLUGIN_ROOT}/skills/optimize/workflows/evo-optimize.js` and `args: {pluginRoot: "${CLAUDE_PLUGIN_ROOT}", subagents: <N>, budget: <N>, stall: <N>}`, using the round sizing you resolved above. **Pass all four keys explicitly — never omit one.** For `stall`, use the user's `/optimize stall=N` override if given, else the default 5. (The workflow's stop condition is the stall limit, so a dropped `stall` silently reverts it to 5.)
- Report the returned `runId` and tell the user to watch progress with `/workflows`. The workflow runs the round loop itself (orient → mandatory scan + cross-history axis check → ideators on stall/periodic → briefs → fan-out + verify → collect → frontier-select → stall) plus the concurrent meta controller; you do **not** execute "The Loop" section below, and you do **not** need autonomous mode (the workflow self-drives; its stall limit is the stop).

Use **The Loop** below by default — it is the prose driver on every host, and the path whenever the workflow is not explicitly enabled (`default-orchestrator` unset or `prose`), the host is not `claude-code`, or the Workflow tool is unavailable (e.g. an older Claude Code build). The workflow is only an execution strategy over the same `evo` CLI; gates, frontier, dashboard, and recovery are identical either way.

**Reconcile config when you fall back to prose.** The stop-nudge that drives the prose loop is auto-suppressed whenever `default-orchestrator` is `workflow`. So if you fall back to the prose loop on Claude Code because the Workflow tool isn't available (older build) while `default-orchestrator` is still `workflow` from a prior run, you MUST set it back — `evo config set default-orchestrator prose` — and arm autonomous as usual. Otherwise the prose loop's stop-nudge stays suppressed and the run stalls after one round. Invariant to preserve: `default-orchestrator=workflow` in config iff the workflow is actually the driver this run.

**Autonomous mode.** Off lets you stop naturally at a turn boundary — finish a round, report, and stop. On arms the stop-nudge: at every turn boundary you are re-prompted to keep driving the loop until the **stall** limit is hit or the user interrupts. Without it, the loop does NOT force-continue across turn boundaries. To stop an autonomous run, the user runs `evo autonomous off` or `evo exit-optimize-mode`.

**Subagents-only mode.** Off, the orchestrator may edit files directly — the optimization protocol still pushes edits through subagents (you write briefs; they edit in their worktrees), but a one-off orchestrator edit is not blocked. On arms the deny-gate: orchestrator file-mutation tools (Edit/Write, mutating Bash) are denied on an alternating cadence — 1st violation blocked, 2nd allowed, 3rd blocked, and so on — each block nudging you to delegate the edit to a subagent. It is a nudge, not a hard block: an edit can still land on an even-numbered attempt. Subagent edits (sessions with an `exp_id`) are never gated. To lift it, the user runs `evo subagents-only off` or `evo exit-optimize-mode`.

**Pool mode (if active).** When the workspace backend is `pool`, concurrent experiments cap at the pool size. Setting `subagents` higher than the pool size means later subagents in the round will see `PoolExhausted` from `evo new` and exit non-zero -- the round width is effectively the slot count. Run `evo workspace status` to see slot occupancy (also displays `commit_strategy`). Reduce `subagents` to the pool size if exhaustion is recurring. Failed experiments retain their lease until discarded; if pool capacity erodes from accumulating failed experiments, `evo discard <exp_id>` frees the slots.

Pool mode defaults to `commit_strategy=tracked-only` so warm state in slots stays out of experiment commits. Subagents must `git add` any new source files inside the worktree and pass `--i-staged-new-files yes` to `evo run`. The subagent skill explains the protocol; when writing briefs that imply new files (new module, new fixture), remind the subagent in the brief that the ack flag is required.

**Remote-backend mode.** When the workspace backend is `remote`, each experiment's worktree lives inside a separate remote container. Subagents use `evo bash / read / write / edit / glob / grep --exp-id <id>` instead of native `Bash`/`Read`/`Write`/`Edit` tools. **Every brief you write to a subagent in remote mode MUST start by stating the exp_id explicitly:** `"Your experiment id is exp_NNNN. Pass --exp-id exp_NNNN on every evo command."` This is the only thing that prevents one subagent from accidentally operating on another's container. evo CLI hard-errors if `--exp-id` is missing, but it can't catch a subagent that confidently passes the wrong id; the brief is the discipline.

Remote `evo run <exp_id>` is also the recovery command. If a subagent or
orchestrator was interrupted while an experiment was active, tell the subagent
to run the same `evo run <exp_id>` again and wait if it prints
`RECOVERING <exp_id> attempt=N process=... state=...`. That means evo is
reattaching to the existing remote process and finalizing the original attempt;
starting a new experiment or discarding the active one is only appropriate after
evo reports the attempt is unrecoverable.

For expensive benchmarks, design recovery around `EVO_CHECKPOINT_DIR`, not
process checkpoint/restore. evo mirrors checkpoint files into
`attempts/NNN/checkpoints/` during remote runs and writes `attempt_state.json`
for phase-level recovery. If the remote container itself dies, arbitrary process
memory is gone; the benchmark must know how to continue from its checkpoint
files or the attempt should be treated as `remote_infra_failure`.

**Infra setup is not user-invocable.** If a remote provider is missing SDKs, auth, or setup details, read `plugins/evo/skills/infra-setup/references/provider-matrix.md`. It summarizes what each provider actually needs and replaces the old per-provider prompt files.

**Runtime recipe/env.** Benchmark runtime is evo configuration, not something subagents should rediscover or copy into worktrees. Use `evo config runtime show` for prepare/before-run/prefix and `evo env show` for redacted env sources. If a run fails because expected runtime setup or env is missing, report it as setup failure or configure it from the orchestrator; do not patch benchmark code to bake in secrets or local paths. Use `evo run <exp_id> --check` for non-committing wiring validation; do not invent ad-hoc validation wrappers.

**Replicated/noisy benchmarks.** If the user or `.evo/project.md` says an idea
must pass `n=3`, `n=10`, median, mean, held-out, or cross-dataset evaluation,
configure the benchmark so each `evo run` records the grouped aggregate as that
experiment's score. Do not represent replicates as independent evo experiments
and do not judge, report, or promote an idea by its best replicate. The frontier
and best path must see the same aggregate statistic the user uses for decisions.

**CLI reference.** If you are unsure which command to use, read `plugins/evo/skills/references/cli-quick-reference.md`. It is the canonical command map; this skill only repeats the high-frequency commands.

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

**Session lineage.** Dispatching a child of a committed experiment forks the parent experiment's own session, not a separately-warmed explorer. The child inherits the parent's full transcript -- reads, edits, benchmark output -- so the prefix cache carries through generations. Explorer warming runs only for children of root, where the parent has no session.

**Trace instrumentation style**: `.evo/meta.json`'s `instrumentation_mode` records `sdk` vs `inline`. Subagents must stay consistent with it (see the **evo subagent skill** for details).

## The Loop

Repeat until interrupted or stall limit reached:

### 1. Read current state

**Step 0 (always, before anything else): ensure you have read `.evo/project.md`.** Its contents define the optimization metric and goal; every pattern you report MUST be tied to a failure mode of that goal. If you haven't read it yet in this session, read it now -- before listing experiments, opening any `outcome.json`, or spawning sub-agents. If the file is missing, stop and tell the user.

**Step 0.1 (one-time auto-migration): ensure the workspace knows what runtime you are.** Run `evo host show`. If it prints `<not set>`, the workspace pre-dates the host signature field — declare your runtime now: `evo host set <one of: claude-code, codex, opencode, openclaw, hermes, pi, generic>`. You know what runtime you're operating in (your system prompt, env vars like `CLAUDECODE=1`, your own self-identity); pick the matching value. If genuinely unsure, use `generic`. Do not ask the user — this is a one-time silent migration. Subsequent rounds skip this step (`evo host show` returns the recorded value).

```bash
evo scratchpad          # bounded state summary (tree, frontier, awaiting decision, gates, annotations, what-not-to-try, notes)
evo status              # one-line summary
evo frontier            # explorable nodes ranked by the configured strategy (JSON envelope: {strategy, nodes[{id,score,rank,...}], generated_at})
evo show <id>           # full state of one node (attempts, diffs, annotations, notes, effective gates) -- the cleanest one-node getter
evo awaiting            # evaluated nodes awaiting commit/discard decision
evo discards [--like <text>]  # discarded nodes; useful for "have we tried this before"
evo notes               # all notes (per-node + workspace), recent first
evo annotations         # all annotations (filterable with --task/--exp)
evo path <id>           # root-to-node chain with scores
evo diff <id> [<other>] # diff vs parent (or between two experiments)
evo gate list <id>      # effective gates for a node (inherited from ancestors)
evo gate check <id>     # run effective gates without benchmark or state mutation
evo infra log           # recorded infra/strategy events (epoch bumps, harness changes)

# Settings (read)
evo config show               # everything; use the next three for narrower views
evo config get <field>        # one field
evo config backend show       # current execution backend + provider config
evo config runtime show       # runtime prepare/before-run/prefix recipe
evo env show                  # redacted runtime env metadata
```

### 2. Analyze state and do structural aggregation

From the scratchpad, frontier, traces, and annotations, determine:
- Which frontier nodes are most promising (`evo frontier` returns them already ranked under the configured strategy -- use its ordering rather than re-ranking; override with `evo frontier --strategy ...` only if you have a specific reason)
- What failure patterns are most common and impactful
- What strategies have been tried and their outcomes
- Which branches are plateauing or exhausted
- What gates exist on each frontier node (`evo gate list <id>`) -- subagents must satisfy these

**Read the "Awaiting Decision" section of the scratchpad.** Evaluated nodes (ran, bad outcome, not yet discarded) are a cross-agent signal: if three subagents in the last round produced evaluated nodes that all failed the same gate, surface the pattern -- maybe the gate is too tight, maybe the approach has a shared flaw. Either tell the next round to avoid it, or propose a brief that attacks it directly. Without this cross-cutting read, each subagent rediscovers the same wall independently.

**Structural pass.** For the evaluated nodes this round, load their `outcome.json` files into Python and aggregate: co-occurring `gate_failures`, shared zero-score task IDs in `benchmark.result.tasks`, recurring substrings across `error` fields. (Bulk-reading attempt artifacts under `.evo/run_*/experiments/<exp>/attempts/<NNN>/` is the right tool for this — `evo show <id>` is for one-node introspection, not batch aggregation.)

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

Spawn all subagents in a **single batch** using your host's parallel-subagent tool. They must execute in parallel, not sequentially -- serial execution defeats the per-round width.

Per host, the spawn shape matters because evo's loop depends on *completion notifications* arriving turn-by-turn (so the orchestrator can review each subagent's outcome and decide round 2):

- **claude-code** — fire one `Bash(run_in_background=true)` call per brief. The bash invokes the subagent (the host's `Task` tool, or any equivalent that runs the brief to completion). Each backgrounded bash returns immediately and the runtime delivers a `<task-notification>` at a later turn when each subagent finishes. Do NOT wait on subagents inline; fan them out, then exit your current turn — notifications arrive in subsequent turns.
- **codex** — call `spawn_agent` once per optimization brief. If `spawn_agent`
  is deferred or not visible yet, first use Codex's tool-discovery tool
  (`tool_search`) with a query like `spawn agent subagent`, then call the
  discovered spawn tool. The optimize skill's plain-language trigger is
  sufficient authorization for this; do not stop or fall back to direct edits
  merely because the user did not explicitly write "spawn subagents." The
  candidate worker prompt is the one that starts with the mandatory "First,
  load and follow..." sentence below. A later read-only scan subagent does not
  count toward the round width.
- **hermes** — `terminal(background=true)`; notifications delivered similarly.
- **openclaw** — `sessions_spawn deliver:false`; notifications delivered similarly.
- **opencode** — *batch-parallel only* (no background notifications). Fire N `task` calls in ONE assistant message; all `tool_result`s return together when the slowest finishes. Plan all parallel work (including non-task tools) in that single message — opencode cannot interleave reasoning across turns while subagents run.
- **pi** — *batch-parallel via `subagent` tool*. Fire N calls in one assistant message; all results return together. If the tool's missing, run `evo new` → `evo run` sequentially and tell the user to `pi install npm:pi-subagents`.
- **cursor** — *batch-parallel via Cursor native Subagents*; fan all briefs out in a single batch. Fallback if native subagents are unavailable: one `cursor-agent -p "<brief>" --force` per brief (background+notify).

Respect the host's concurrency cap; batch if N exceeds it.

Pick a faster model for straightforward briefs and a stronger model for harder ones requiring deeper trace analysis, if your host exposes per-call model selection.

Each subagent prompt MUST start with the literal sentence:

> "First, load and follow the **evo subagent skill** (named `subagent` under the evo plugin in your host's skill registry — use your host's skill loader, not a filesystem path). Allocate your experiment via `evo new --parent <id>`, edit inside the returned worktree, evaluate via `evo run <exp_id>`. Do not skip these steps even if the brief looks simple. If `evo run` exits `GATE_FAILED`, fix the edit so it satisfies the inherited gate; do not weaken, bypass, delete, or argue with the gate unless the orchestrator explicitly changes the brief."

Then append:
- The four-field brief verbatim (objective, parent, boundaries/anti-patterns, pointer traces)
- The iteration budget
- A one-paragraph scratchpad summary (current best score, frontier nodes, recent failures) for context

The opening sentence is non-negotiable — without it small models often skip the evo CLI and edit files directly, which produces no committed experiments and breaks the round.

Before leaving step 5, check yourself: the number of optimization subagents you
spawned must equal the resolved `subagents=N` unless the user explicitly
requested a smaller direct/manual run. Scan/analysis subagents are separate and
do not count toward this number.

### 6. Collect results and update state

After all subagents complete:

- Review each subagent's summary
- Record the round's best score and compare to the previous best
- If no subagent improved the score, increment the stall counter
- If any improved, reset the stall counter
- Check if subagents added new gates -- note these in your state tracking
- If multiple experiments failed the same gate, consider whether the gate is too restrictive or the briefs were aimed at the wrong surface

**Cross-cut the round's evaluated nodes.** Before moving on, read `experiments/<id>/attempts/NNN/outcome.json` for each evaluated node from this round. The structured `gates[]` entries and `benchmark.result` let you spot shared failure modes the subagent summaries may have glossed over (e.g., three different subagents produced evaluated nodes whose gate_failures all included `refund_flow` -- that's a structural constraint the next round must confront, not three independent bad hypotheses).

Prune branches you have decided are exhausted:
  ```bash
  evo prune <exp_id> --exhausted --reason "exhausted: <why>"
  ```

`evo prune` accepts `committed` or `evaluated` nodes:
- `--exhausted` (default/legacy): stop branching here; the result still counts.
- `--invalid`: this result is wrong; exclude it and descendants from best/frontier.
- `--yes`: required only with `--invalid` on the current best valid spine.

Use `--invalid --yes` when you have proven a best-spine result or ancestor is
wrong. **Never `evo discard` a committed node** -- prune preserves its commit.

If a previously-pruned (or discarded-then-restored) node is worth revisiting:
  ```bash
  evo restore <exp_id>
  ```
Flips status back to committed; recreates the regular branch from the anchor
ref so future `evo new --parent <id>` works. For discarded nodes whose commit
is no longer reachable in git (rare; needs `git gc --prune=now` after the
discard), restore errors and points at `experiments/<id>/attempts/NNN/diff.patch`
for manual replay.

Update notes with cross-cutting learnings:
  ```bash
  evo set <exp_id> --note "key insight from round N"
  ```

### 6a. Pattern recognition across history (objective, not narrative)

Step 6 cross-cuts a single round. This step looks across ALL committed experiments in the run, not just this round's. The orchestrator's failure mode is tunnel vision -- iterating on the visible axis (whatever knob the recent rounds touched) while missing the orthogonal axis (the harness itself, the score definition, the environment, the input data, plumbing). The check is cheap, runs between rounds, and is the most reliable signal that you're on the wrong axis.

Four checks via `evo show` + `evo tree`:

1. **Score plateaus across structurally distinct hypotheses.** Read the `hypothesis` strings of committed experiments. If 3+ experiments with materially different hypotheses (not minor parameter sweeps of the same idea) all commit at the same score, the bottleneck is not where the hypotheses were aiming. The next move belongs on an axis none of those hypotheses touched.

2. **Repeated failure class.** Tally failure indicators across discarded + failed nodes in the run so far: `gate_failures` names, non-zero exit codes, shared error-message fragments in `benchmark_err.log`. If 2+ failures share a class, that class is structural -- fix the cause, rather than queuing more experiments that will hit the same wall.

3. **Internal-vs-benchmark delta.** Compare each evaluated node's *internal* indicators (progress signals the experiment's own process produces during a run -- intermediate test pass-rates, training loss, build success, agent self-report metrics, whatever the trace stream carries) to its *committed* benchmark score. Healthy internal signal + flat benchmark score = the experiment is optimizing something the benchmark does not reward. The fix is usually in the harness, output format, or score definition -- not in another hypothesis on the same axis.

4. **Annotate facts, not narratives.** Annotations via `evo annotate <exp_id>` and `evo set <exp_id> --note` should record what HAPPENED -- scores, exact error messages, surprising observations, sources used. Not what you hoped would happen, what you plan to try next, or how you feel about the result. Annotations get loaded into future decision context and into ideator briefs; narrative noise contaminates them. State facts; leave plans to TodoWrite or `evo set --note` on the round itself.

If any check surfaces a structural issue, the next round's subagent briefs should target the orthogonal axis the pattern identifies. Another iteration on a plateaued or systematically-failing axis produces another data point with the same conclusion.

### 6b. Periodically spawn ideators (in parallel)

The optimize loop's scan sub-agents (step 3) read the CURRENT round's evaluated experiments for failure patterns. They don't do deep cross-graph analysis or external literature scans -- that work belongs to the ideator skill (`evo:ideator`).

Spawn ideators in parallel when ANY of these triggers fire:

- **Periodic**: every N=5 committed experiments since the last ideator round
- **Stall**: best score unchanged for M=3 consecutive committed experiments (the stall counter from step 6)
- **Failure cluster**: M=3 consecutive discards with related root causes (use the `evo discards` output)
- **User-triggered**: a directive (`evo direct`) asks for fresh ideas

When a trigger fires, spawn three parallel **evo ideator subagents** via your host's Task tool -- one per brief:

```
Task(subagent_type="evo:ideator", prompt="workspace=<path>\nbrief=failure_analysis")
Task(subagent_type="evo:ideator", prompt="workspace=<path>\nbrief=literature")
Task(subagent_type="evo:ideator", prompt="workspace=<path>\nbrief=frontier_extrapolation")
```

| Brief | What it does |
|---|---|
| `failure_analysis` | Cross-graph clustering of discards/failures |
| `literature` | Web/arXiv scan for untried techniques in the workspace domain |
| `frontier_extrapolation` | Deeper variants of the steepest score gradient on the best path |

Each subagent runs the brief in its own context, appends proposals as JSONL lines to `.evo/run_<run_id>/ideator/proposals.jsonl` (single final write), and returns a JSON summary. See `plugins/evo/agents/ideator.md` for the full procedure each ideator follows.

Ideators take 5-10 min while the optimize loop's next round is typically 1-2 min away. If you fire and continue, proposals miss the next round's brief-writing every time. Two patterns work:

- **Block here briefly.** If the trigger was a STALL or FAILURE CLUSTER, the next round's quality depends on fresh ideas -- block until enough proposals land:
  ```bash
  evo wait --for ideators --count 3 --timeout 900   # 15 min cap, fail-open
  ```
  Exit 0 means the proposals are ready; exit 124 (timeout) means proceed with whatever's available -- proposals.jsonl may have partial results.

- **Fire and continue for periodic spawns** (every-N-commits trigger). The next round can run without proposals; the round after that will read them once they land. Use this when there's plenty of in-graph signal still to extract.

### 6c. Reconcile ideator proposals at brief-writing time

Before writing the next round's briefs (step 4 of the next iteration), check for new ideator proposals:

```bash
# Read proposals newer than the last round
test -f .evo/run_*/ideator/proposals.jsonl && \
  tail -n +1 .evo/run_*/ideator/proposals.jsonl | \
  jq -s --argjson cutoff "$LAST_ROUND_END_TS" \
    'map(select(.generated_at > $cutoff))'
```

If you fired ideators in 6b WITHOUT blocking (periodic trigger) and they haven't landed yet, you can also wait here -- but only a short timeout, since brief-writing should not stall indefinitely:

```bash
evo wait --for ideators --count 1 --timeout 120   # 2 min cap, fail-open
```

For each new proposal:

1. Check the workspace graph -- has the proposed config already been tried? Use `evo discards --like "<keyword>"` to scan. If yes, skip.
2. Score each remaining proposal by `expected_score_uplift × confidence`. Confidence ranking: `frontier_extrapolation > failure_analysis > literature`, all else equal.
3. The top 1-2 proposals become objectives in the next round's briefs (step 4). Cite the proposal's `hypothesis` and `mechanism` in the brief's *Objective* field.
4. Leave the rest in the queue -- they may surface as winners after a few more rounds when the frontier shifts.

Proposals are advisory, not mandatory. If none look better than what step 3's scan sub-agents surfaced from in-graph signal, ignore them and proceed with the in-graph briefs. Ideator output complements, doesn't replace.

### 7. Continue or stop

**Continue** if:
- Stall counter < stall limit
- User hasn't interrupted
- Score hasn't reached the theoretical maximum

To continue, go back to step 1.

**Stop** if:
- Stall counter >= stall limit (N consecutive rounds with no improvement)
- Score reached theoretical maximum (1.0 for max metric, 0.0 for min metric)
- User interrupted

On stop, the loop is done -- do not go back to step 1. Print a final summary:
- Best score achieved and experiment ID
- Total experiments run across all rounds
- The winning diff: `evo diff <best_exp_id>`
- Suggested next steps if the score hasn't converged
- **How to land it:** point the user at `/evo:ship` to turn the winning
  experiment into a mergeable change (a PR when the repo has a remote, else a
  merge into the working branch). The raw winning diff is not the artifact to
  merge by hand -- `/evo:ship` distills it to the minimal clean change and
  attaches a mergeability report. Mention this whenever the run produced a
  committed experiment that beats the baseline.

### 8. Report evo-side feedback when evo itself went wrong

Use feedback for evo product/orchestration issues, not for ordinary bad
experiments. Good triggers: confusing CLI behavior, subagent handoff failures,
workflow/meta-controller mistakes, recovery that failed, remote backend handling
that was hard to diagnose, or an evo policy/gate that blocked the wrong thing.
The command is anonymous and no-ops when telemetry is off; do not branch on that.

Keep the report public-safe but actionable enough for the evo backend team to
reproduce the case. Include the phase, what you expected evo to do, what it did,
and a generic repro shape. Do not include repo names, company names, file paths,
commands, prompt text, raw logs, URLs, secrets, dataset names, or exact task
examples. If the issue is tied to a specific evo experiment, include its
anonymous experiment id with `--exp-id`.

```bash
evo telemetry feedback \
  --exp-id exp_0007 \
  --kind orchestration \
  --phase optimize \
  --summary "Subagent completed but the orchestrator did not collect its evaluated result before writing the next round briefs." \
  --expected "Round collection should wait for all dispatched subagents or clearly report the missing one." \
  --actual "The next round was planned from partial results, leaving one evaluated node out of the pattern scan." \
  --repro "Run optimize with multiple subagents on a workspace where one branch finishes after the first completion notification." \
  --tag optimize-loop --tag subagent-handoff --tag collection
```

## Polling discipline

When waiting on a long-running background process (a subagent's training subprocess, a long evaluation, a batch job), do NOT use `while true; do sleep N; tail file; done`. That loop never exits when the underlying process crashes -- the tail keeps reading the same dead file, the agent interprets "no growth" as "still working," and the agent blocks indefinitely.

Use `evo wait`. The CLI is the bounded, structured replacement:

```bash
# wait until the training subprocess exits, OR its log stalls, OR the GPU goes idle,
# whichever first; 60-minute ceiling; structured JSON on stdout
evo wait --for process=$TRAIN_PID \
         --for log-growth=$TRAIN_LOG \
         --for gpu-idle \
         --timeout 60m --stall-threshold 5m --json
```

Multiple `--for` flags combine; the wait returns on the first matching condition. The JSON output's `exit_reason` and `triggered_by` identify which condition fired. Process / log-growth / gpu-* watches do not require an evo workspace context; the workspace-anchored watches (`--for experiments`, `--for ideators`) cover the ideator + commit waits described elsewhere in this skill.

Full surface, exit codes, JSON shape, examples: `references/evo-wait.md` (under `plugins/evo/skills/references/`).

If `evo wait` is not available for some reason (older CLI on PATH, sandbox constraint), fall back to a bounded poll loop that checks all three signals -- process liveness via `kill -0 $PID`, log growth via `wc -c` delta, GPU via `nvidia-smi --query-gpu=utilization.gpu` -- and exits on any one going negative. NEVER unbounded `while true`.

## Resetting the eval epoch

`evo infra event -m "<reason>" --breaking` bumps `current_eval_epoch` and blocks
non-root `evo run` calls until a new root baseline commits. Old experiments
stay in the tree but are excluded from frontier and best-score lookups via
their epoch tag.

Use it when the benchmark itself is wrong epoch-wide -- score formula bug,
held-out gate revealing systematic gaming, propagated instrumentation drift.
Don't use it for single bad experiments (`evo discard`) or one tight gate
(relax the gate at the relevant node).

Recovery:
1. `evo infra event -m "<reason>" --breaking`
2. Fix the harness in the baseline worktree (or branch a fresh root).
3. `evo new --parent root -m "v2 baseline: <what changed>"`
4. `evo run <new_exp_id>` -- commits, flips the block off, establishes the
   new-epoch baseline. Resume the loop.
