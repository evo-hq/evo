---
name: discover
description: Initialize evo for the current repository by exploring the codebase, proposing unexplored optimization dimensions, constructing the benchmark inside a baseline worktree, and running the first experiment. Use when the user invokes /evo:discover, mentions setting up evo, wants to instrument a codebase for autonomous optimization, or asks to start a new evo run on a project.
argument-hint: <optional context about what to optimize>
evo_version: 0.6.2
---

# Discover

Internal procedure for `evo:discover`. The user only sees the user-facing prompts, the dashboard URL, and the baseline score -- everything else is the agent's choreography.

## Evo surface

General guidance on the skills and tools available in evo. Each line is a triggering condition: if you're about to do X, pull/dispatch/read this. Don't preload -- act when the trigger fires.

**Always have a sense of the skill before jumping into its references.** A skill body carries the decision-making; references are concrete contracts that assume a decision has been made.

```
evo plugin
│
├── Main thread  (the orchestrator -- you, inside /evo:discover or /evo:optimize)
│   │
│   ├── Skills (Skill tool)
│   │   ├── evo:discover       starting a new evo workspace / instrumenting a project
│   │   ├── evo:optimize       after discover commits the baseline -- drives the loop.
│   │   │                      Args: subagents=N (read sizing-the-round FIRST),
│   │   │                            autonomous, subagents-only, budget=N, stall=N
│   │   ├── evo:ship           after the loop stops -- distills the best valid
│   │   │                      experiment into a mergeable change (PR if remote,
│   │   │                      else merge) + a mergeability report
│   │   ├── evo:finetuning     task is finetuning / post-training / training a model
│   │   └── evo:infra-setup    need a remote backend, pooled workspaces, lease/slot
│   │                          management, or specific provider auth/setup
│   │
│   └── Subagents to dispatch (Task tool, subagent_type=...)
│       ├── evo:benchmark-reviewer  before the baseline run, or whenever the
│       │                           benchmark command / harness changes
│       └── evo:ideator             stalled, or every ~5 committed experiments.
│                                   One subagent per brief:
│                                   failure_analysis, literature, frontier_extrapolation
│
├── Subagent thread  (each subagent spawned by /optimize step 5)
│   │
│   ├── Skills  (the subagent loads this on first turn -- the brief's first
│   │            sentence mandates it; not auto-loaded by the host)
│   │   └── evo:subagent     load FIRST -- defines the iteration protocol
│   │                        + brief field shape the subagent operates under
│   │
│   └── Subagents to dispatch (Task tool, subagent_type=...)
│       └── evo:verifier      ALWAYS dispatch pre AND post every evo run.
│                             Pre: ~30s static analysis before the experiment runs.
│                             Post: result-validity audit after it commits.
│                             Not optional. Not ad-hoc.
│
└── Key references (Read tool, on demand)
    ├── discover/references/
    │   ├── constructing-benchmark.md      designing + assembling a benchmark from scratch
    │   ├── sdk_python.py / sdk_node.js    wiring per-task instrumentation -- preferred path
    │   ├── inline_instrumentation.py      inline fallback when SDK can't be used.
    │   │                                  Copy as-is; do not reimplement (file header
    │   │                                  explains why)
    │   ├── sizing-the-round.md            BEFORE invoking /evo:optimize with any
    │   │                                  specific subagents=N. Single-GPU /
    │   │                                  single-exclusive-resource -> subagents=1
    │   ├── proposing-dimensions.md        choosing what to optimize when not obvious
    │   └── instrumentation-contract.md    the format evo reads (result + traces shapes)
    │
    ├── finetuning/references/
    │   ├── glue.md                         writing train.py -- I/O contract evo expects
    │   ├── diagnostics.md                  per-failure-mode diagnostics
    │   ├── false-progress.md               what doesn't count as improvement
    │   ├── trace-schema.md                 per-task trace JSON schema for training runs
    │   ├── rl/                             RL framework references
    │   │   └── art.md                       ART (Algorithm-Refined Training)
    │   ├── sft/                            SFT framework references
    │   │   └── tinker.md                    Tinker SFT
    │   └── serving/                        eval-time inference references
    │       └── vllm.md                      vLLM serving config + LoRA-multi
    │
    ├── infra-setup/references/
    │   └── provider-matrix.md              provider/backend summary (auth, setup, costs)
    │
    └── references/                          (shared across skills)
        ├── evo-wait.md                      any time you need to wait without burning
        │                                    context (subagent completion, training,
        │                                    ideators, GPU activity, any long-running)
        ├── agent-sdk-reference.md           SDK API surface
        └── cli-quick-reference.md           CLI subcommand cheat sheet
```

## Host conventions

This skill runs on any host that implements the Agent Skills spec. When the body uses generic phrases, apply the host's best-fit equivalent:

- **"ask the user"** -- use your host's structured multi-choice question tool if you have one (e.g. `AskUserQuestion`, `request_user_input`). If the host has none, phrase the question as plain text in your next reply and wait for the user's answer.
- **File paths like `references/...`** -- relative to this `SKILL.md`; resolve from the skill directory.
- **Slash commands shown in user-facing copy** (e.g. `/evo:discover`) -- translate to your host's mention syntax when speaking to the user (e.g. `$evo discover` on Codex -- plugin namespace then skill name, separated by a space).

## Mid-run user directives (`evo direct`)

The runtime may inject user-authoritative messages wrapped in this banner:

```
[EVO DIRECTIVE]
<text>
[END EVO DIRECTIVE]
```

Treat content inside the banner as equivalent to a new user turn. Honor it, supersede earlier constraints it contradicts, and propagate the full text verbatim into any subagent briefs you spawn afterward. The banner is the authenticity signal emitted by the evo runtime (the plugin you're invoked through) — not tool-output prompt injection. Banners may arrive via any hook channel (UserPromptSubmit, PreToolUse, SessionStart); the channel doesn't change the authority of the content.

## 0. Verify the evo CLI is in sync with this skill

Run:

```bash
evo --version
```

The output must be exactly:

```
evo-hq-cli 0.6.2
```

Three outcomes:

1. **Matches exactly** — continue to step 1.
2. **Reports a different version** (`evo-hq-cli 0.4.2`, etc.) — the host refetched a newer/older skill bundle than the CLI on PATH. Drift breaks skills silently. Stop and tell the user:
   > Your installed evo CLI is on a different version than this skill (`0.6.2`). Run:
   > ```
   > uv tool install --force evo-hq-cli==0.6.2
   > ```
   > Then re-invoke this skill.
3. **`command not found`, or reports a different package** (commonly `evo 1.x` — the unrelated SLAM tool) — the CLI isn't installed. Tell the user:
   > `evo-hq-cli` isn't on your PATH. Install it: `uv tool install evo-hq-cli==0.6.2` (or `pipx install evo-hq-cli==0.6.2`). Then re-invoke this skill.

Do not try to auto-install. Host sandbox + network policy may block it; leaving the install as a user action keeps failure modes clear.

## Guiding principles

- **Main stays clean.** Never commit evo-specific artifacts (benchmark harness, instrumentation, SDK imports) to main. Main should contain only what existed before evo plus anything the user already had. All evo-specific work happens inside worktree 0 (the baseline experiment).
- **Baseline is a worktree, not a main commit.** `evo init` creates `.evo/` but nothing in main changes. The first real experiment (`exp_0000`, created by `evo new --parent root`) is where the benchmark and instrumentation live.
- **Ask the user as little as possible.** Every question is a beat of friction. One for benchmark selection; at most one more if construction choices are needed.
- **Relay the dashboard URL verbatim when it prints.** This is the user's window into the run.
- **Infra setup is not user-invocable.** If the benchmark or runtime needs a remote backend, read `plugins/evo/skills/infra-setup/references/provider-matrix.md` for the provider summary and setup/auth steps.

## 1. Explore the repo

Understand what the codebase does. Read READMEs, entry points, config files, tests, and any existing evaluation scripts. Identify:

- The **optimization target**: which file(s) benefit from iterative optimization?
- **Metric direction for each candidate**: is higher better (`max`) or lower better (`min`)?
- **Critical behaviors worth gating**: invariants that must never break regardless of score (e.g., "refund flow works", "core tests pass", "output is valid JSON"). Gates are commands that exit 0 on success, non-zero on failure.

## 2. Look for the obvious benchmark

Check what's already there:

- Full benchmarks: existing scripts that run end-to-end and output a score
- Partial evals: tests, notebooks, or logs with ground truth but not in runnable-score form
- Nothing at all

Also check what the user asked for in the invocation argument. If they named a specific metric or target, that's intent.

**If one benchmark is obviously the right one** — a runnable eval that measures what the user clearly cares about, or what the repo is plainly built to do — use it. Skip step 3, go to step 4 with that benchmark as the only candidate.

**If it's not obvious** — multiple candidate surfaces, no existing eval, user didn't specify intent, or the existing eval covers a narrow slice while the interesting optimization sits elsewhere — run step 3.

## 3. Propose unexplored optimization dimensions (only if step 2 was ambiguous)

When the benchmark isn't obvious, propose candidate dimensions grounded in actual repo signals, then pick with the user. See `references/proposing-dimensions.md` for the full rubric, project-type examples, and presentation format. Short version:

- A handful of dimensions relevant to this specific repo (not generic categories).
- Ground each in repo signals: already-instrumented code, stated goals in READMEs, TODO/FIXME patterns, domain defaults.
- Rank by signal × slack × cost answered in prose (no numeric scores — they're vibes).

## 4. Ask the user to pick the benchmark

If step 2 produced one obvious benchmark, confirm it in one sentence and move on — no ranked list needed.

Otherwise, ask once:

> "I'm proposing these optimization targets for this repo:
>
> [ranked list with one-line explanations, construction complexity, and whether an existing eval covers some of it]
>
> Which should we optimize? Recommended: [default pick with reasoning]."

Record the selection. If step 3 ran, save non-picked dimensions to `.evo/project.md` under "Future experiment candidates" after init.

## 5. Ask the user for instrumentation mode

Three cases, in order of how to handle them:

1. **Selected benchmark already exists AND is already instrumented for evo** (you can see `from evo_agent import Run`, an `import { Run } from '@evo-hq/evo-agent'`, or the inline `log_task` / `logTask` helpers in the benchmark source). No wiring needed. Skip this question entirely. Detect the instrumentation style from the source and pass the matching `--instrumentation-mode <sdk|inline>` value to `evo init` in step 7.

2. **Selected benchmark already exists but is NOT instrumented** (it just prints a score JSON, or it's a test runner that doesn't yet write per-task traces). Wiring is needed. **Ask the question.**

3. **Selected benchmark needs to be constructed from scratch** (case B or C from step 4). Wiring is needed. **Ask the question.**

For cases 2 and 3, ask once:

> "I can wire up the benchmark in one of two ways:
>
> 1. **SDK mode** -- install the evo agent SDK with this project's package manager/runtime (`uv add --dev evo-hq-agent`, `python -m pip install evo-hq-agent`, or `npm install @evo-hq/evo-agent`). ~5 lines of user code, with incremental per-task logging handled for you. **Python and Node only** -- the SDK ships for those two runtimes.
> 2. **Inline mode** -- implement the trace/result contract directly in the benchmark, in the benchmark's own language. Zero new dependencies, same data.
>
> Recommended: SDK mode."

Inline mode is language-native and lives entirely in the user's setup: whatever the benchmark is written in is what the instrumentation is written in, with no evo package added to the project. Do not introduce a Python (or any other) sidecar script to wrap a benchmark written in another language -- that is the friction this avoids. For a Python or Node benchmark, the ready-made paste-in helper (`references/inline_instrumentation.py` / `.js`) is the inline implementation. For any other language, port the ~10-15 line contract from `references/instrumentation-contract.md` into that language. Either way the mode is `inline`.

Order the options SDK first, inline second, and suggest SDK as the recommended default when it's available -- it's the managed path with per-task logging handled for you. Inline stays a first-class choice with the same data contract, though: if the user declines the SDK for any reason -- they don't want a new dependency, can't add evo to their project's tree, internal policy, or plain preference -- honor it without pushback. SDK mode is only *available* when the benchmark runs on Python or Node; when the benchmark's language has no SDK there's nothing to suggest, so go straight to inline.

Pass the answer to `evo init` via `--instrumentation-mode <sdk|inline>` in step 7. **Never install packages without this confirmation.** If you skip the question (case 1), still pass the detected mode to `evo init` so optimize/subagent runs see a consistent value.

## 6. Prepare main (without committing to it)

The agent never creates commits on main. Main stays byte-identical to what the user committed before evo ran. Two things to set up, both local-only.

**Order matters: do 6a (audit) before 6b (excludes).** The excludes in 6b will hide files inside `node_modules/`, `dist/`, `build/`, etc. from `git status`. If you run the audit *after* adding excludes, you'll be blind to anything missing inside those directories -- and benchmark dependencies often live exactly there.

### 6a. Detect (don't auto-commit) dirty or untracked dependencies

`evo new` forks a worktree from the current branch's HEAD commit, **not from your dirty working tree**. Any uncommitted edits to the target, benchmark, or gate dependencies are silently absent from `exp_0000`, and the whole optimization tree gets built against stale code while you think evo is running on what you see locally.

Run three checks, in this order:

1. **Tracked-but-modified files** -- run `git diff --name-only` and `git diff --cached --name-only`. If any output line is the optimization target, an existing benchmark file, a gate-referenced script, or any of their import-graph dependencies, **stop and ask the user to commit or stash before continuing**. Do not commit on their behalf -- the user might be in the middle of an unrelated change.

2. **Untracked files visible to git** -- run `git status --short --untracked-files=all` and look for `??` entries that the target or gates will reference. Classify each:
   - **Part of the user's project** (e.g., a smoke test they wrote but hadn't committed) -- stop and ask the user to commit it to main themselves.
   - **Evo-specific new files** (a new gate script you're about to write, a new test fixture) -- do not create these in main. Defer to step 10; they go into the baseline worktree and commit to experiment 0's branch. Every descendant experiment inherits via git branching.

3. **Explicit paths inside soon-to-be-ignored directories** -- inspect the benchmark command and every gate command for path references (e.g., `./dist/eval-helper`, `node_modules/some-tool/cli.js`, `build/golden_outputs/`). For each such path, run `git ls-files --error-unmatch <path>` to confirm it's tracked. If any aren't, stop and ask the user to commit them. This catches dependencies that step 6b is about to hide from `git status`.

Any one of these three checks failing is a hard stop. Do not proceed to 6b or beyond until the working tree is clean with respect to anything evo will read.

Anything else (benchmark harness, instrumentation) always gets constructed inside the baseline worktree, never in main.

### 6b. Add local-only git excludes

After the audit passes, append to `.git/info/exclude` (**not** `.gitignore` -- we do not commit to main):

```
.evo/
__pycache__/
*.pyc
.pytest_cache/
node_modules/
dist/
build/
```

`.git/info/exclude` is git's per-clone ignore file -- same effect as `.gitignore`, but never committed, never shared, invisible to history. Right tool for per-machine tooling state.

## 7. Initialize the workspace

```bash
evo init --name "<short project name>" \
  --target <file> --benchmark "<command using {worktree} and {target}>" --metric <max|min> \
  --host <claude-code|codex|opencode|openclaw|hermes|pi|generic> \
  --instrumentation-mode <sdk|inline> \
  --per-exp-timeout <seconds> [--gate "<gate command>"] \
  [--commit-strategy <all|tracked-only>]
```

**`--host` is required.** Pass the host runtime you (the orchestrator) are running under. Allowed values: `claude-code`, `codex`, `opencode`, `openclaw`, `hermes`, `pi`, `generic`. This is recorded in `.evo/meta.json` so other commands can adapt to host-specific conventions. Pick the value matching the runtime you invoked `discover` from. Use `evo host set <value>` later if you change runtimes.

**`--name` should be a short human-readable project label** for dashboard display, chosen from the repository/product context. Existing workspaces without a name fall back to the repo directory name; do not hand-edit config just to migrate them.

**`--per-exp-timeout` is required.** Wall-clock seconds for each `evo run` invocation. Becomes the workspace default; override per-call with `evo run --timeout N`. Pick based on what the benchmark actually costs end-to-end on this hardware -- if you don't know yet, time the benchmark once locally and use ~2x that. Typical ranges: a unit-test-style benchmark is 300-900s; a small-model SFT + eval cycle is 1800-3600s; a large-model train run is several hours. Set conservatively -- a too-tight value kills experiments mid-flight; a too-loose value wastes budget only when something actually hangs. Update later with `evo config set per-exp-timeout <seconds>`.

**`--commit-strategy` is optional.** Default is `all`. Override with `--commit-strategy tracked-only` only when you want the stricter shisa-kanko flow where new files must be staged explicitly and acknowledged at `evo run` time.

**Placeholder semantics.** Benchmark and gate commands support two placeholders, resolved lazily at run time by `evo run` / gate evaluation:

- `{worktree}` resolves to the absolute path of the experiment's worktree directory (e.g. `/path/to/repo/.evo/run_0000/worktrees/exp_0000`). Use this to reference files that live on the experiment branch, not on main.
- `{target}` resolves to the absolute path of the target file *inside that worktree* (e.g. `{worktree}/agent/solve.py`). Use this when your benchmark needs to load or exec the target dynamically.

**Critical rule:** `evo run` executes from the main repo root. When the benchmark script is constructed inside the worktree (the default in this flow), the command **must** reference it via `{worktree}` or the path won't resolve.

Example for a benchmark written at `{worktree}/benchmark.py` that will be committed to exp_0000:

```bash
evo init \
  --name "ARC AGI solver" \
  --target agent/solve.py \
  --benchmark "python3 {worktree}/benchmark.py --target {target}" \
  --metric max \
  --host claude-code \
  --per-exp-timeout 1800
```

Use the same runtime entry point the project already uses, but make sure the command does not assume uncommitted runtime state exists inside the worktree. Worktrees are git checkouts; untracked directories such as local virtualenvs, build caches, and downloaded models are usually not present there. If the benchmark needs setup or a package-manager runner, configure evo's runtime recipe instead of baking local paths into the benchmark command:

```bash
evo config runtime set --prepare "uv sync" --before-run "make reset-test-state" --prefix "uv run"
evo config runtime show
```

`prepare` and `before-run` execute in the experiment workspace. `prefix` is prepended to benchmark and gate commands.

`evo init` creates `.evo/`, the synthetic `root` node, and auto-starts the dashboard. It prints a line like:

```
Dashboard live: http://127.0.0.1:8080 (pid 12345)
```

**Relay that line back to the user verbatim.** If port 8080 is busy, evo auto-increments -- show whatever port prints. The URL is how the user watches the run.

**Benchmark commands must be eval-only.** Do NOT wrap training and evaluation into a single benchmark command. If your benchmark command runs training before scoring, every gate revalidation and every `evo run --check` retrains from scratch, and the experiment budget burns on duplicated training instead of new experiments. Training is a separate step the agent invokes BEFORE `evo run`:

1. The agent makes changes (data curation, hyperparameter selection, technique choice, training code edits).
2. The agent runs the build/training step to produce its artifact — a checkpoint, adapter, index, whatever the recipe makes. Write it to `EVO_CHECKPOINT_DIR` (durable: survives between-attempt cleanup and discard) and declare it in the benchmark result's `artifacts` field so it's preserved + reusable; the per-technique I/O contract is in `evo:finetuning/references/glue.md`. Never hardcode a fixed name like `final_model/`.
3. THEN `evo run <exp_id>` invokes the registered benchmark command, which loads the produced artifact and emits a score.

The registered benchmark command should call `evaluate.py`, `run_eval.py`, or equivalent -- NOT `train.py`. If the project's only existing evaluation tool runs build+eval together with no eval-only mode, wrap it: add a `--skip-build` flag, or have the wrapper detect an existing artifact (under `EVO_CHECKPOINT_DIR`) and short-circuit the build step. Without this, evo's gate-recheck and re-score mechanics rebuild repeatedly and the budget evaporates.

**Runtime environment.** If the benchmark needs keys or other runtime variables, configure them through evo rather than copying `.env` into worktrees or hand-editing `config.json`:

```bash
evo env load .env --all
evo env load .env --allow KEY1,KEY2
evo env show
```

Values are resolved fresh by the orchestrator on each `evo run`. Config stores dotenv source metadata and key names, not secret values. The benchmark and gates receive the resolved env; gates do not receive `EVO_*` artifact variables.

### 7a. Record the task category (`task-skills`)

You already decided what's being optimized (steps 2–4). Record the evo category skill(s) a builder should load for it, so every executing agent — prose subagent or workflow lane — loads the right method knowledge instead of rediscovering it each round:

```bash
evo config set task-skills finetuning   # any weight-update / training task
```

Rule of thumb: if the optimization updates model weights (SFT / LoRA / DPO / RL / continued-pretraining), set `task-skills finetuning`. **Leave it unset** for prompt / code / config / harness optimization — the subagent protocol already covers those; only set it when a dedicated category skill applies. Use a comma-separated list if more than one applies. Mirror the choice in `.evo/project.md` (step 12) so it's human-readable and survives as the fallback if config is ever cleared.

## 8. Set up gates

Gates inherit down the experiment tree -- children automatically get all ancestor gates.

**Gate semantics (read this first).** `evo run` decides "gate passed" purely from the command's exit code: 0 = pass, non-zero = fail. A benchmark-style command that just prints `{"score": 0.0}` and exits 0 **passes the gate**. That defeats the purpose. Every gate command must be wired to exit non-zero when the protected behavior regresses. Two ways to do that:

- **Test-suite gates** -- `pytest`, `cargo test`, `npm test`, etc. already exit non-zero on failure. Use them as-is.
- **Score-threshold gates** -- gate the benchmark on a minimum acceptable score. The benchmark script needs a flag like `--min-score <float>` that exits 1 when the computed score falls below the threshold. The `inline_instrumentation.{py,js}` helpers in `references/` show the pattern: `write_result()` returns the final score; the script can then compare and `sys.exit(1)`.

Examples:

```bash
# Test-suite gate: pytest already exits non-zero on failures (use uv run --with if pytest isn't already a dep)
evo gate add root --name core_tests --command "uv run --with pytest pytest tests/core/ -x"

# Score-threshold gate: benchmark exits 1 if pass rate on protected tasks drops below 0.9
evo gate add root --name refund_flow --command "python3 {worktree}/benchmark.py --target {target} --task-ids 5 --min-score 0.9"

# Custom validation: smoke test that crashes (non-zero exit) on broken target
evo gate add root --name no_crash --command "python3 smoke_test.py --target {target}"
```

If a benchmark you constructed doesn't yet have a `--min-score` mode, add it now (a few lines: parse the threshold flag, compute the score, `sys.exit(1)` if below). Without it the gate is decorative.

Gate commands support `{target}` and `{worktree}` placeholders with the same semantics as benchmark commands (resolved at run time, not at registration). Registering a gate that references `{worktree}/benchmark.py` before the benchmark exists is safe -- the placeholder resolves only when the gate is evaluated, which happens during `evo run` after the benchmark is committed.

Verify registered gates:

```bash
evo gate list root
```

**Gate pairing rule based on benchmark provenance:**

- **If the selected benchmark already existed in the repo** (not constructed from scratch): gates are optional at this step, but if you register any benchmark-derived gate, it must use a score-threshold (`--min-score` or equivalent) -- not a bare invocation. Subagents can add more during optimization.
- **If the benchmark was constructed from scratch** (case B or C from the A/B/C classification): a Goodhart-mitigation gate is **mandatory** before the baseline can run, AND that gate must be a real pass/fail check (score-threshold or correctness assertion that exits non-zero on regression), not a bare benchmark rerun. See `references/constructing-benchmark.md` section 6 on "Required gate pairing." Do not proceed to `evo new` or `evo run` without it. This is the safety against metric gaming -- it is not optional.

## 9. Create the baseline worktree

```bash
evo new --parent root -m "baseline: instrument + score"
```

This returns experiment id (typically `exp_0000`) and its worktree path. All subsequent construction work happens inside that worktree -- **never in main**.

## 10. Work inside the baseline worktree

Cd into the worktree path returned by `evo new`. Then:

### 10a. Construct the benchmark (if needed)

If the selected benchmark is new, build it in the worktree. See `references/constructing-benchmark.md` for the full procedure:

- Design the scoring function (range, direction, meaningful-improvement threshold)
- Assemble test cases (10-20 for programmatic, 15-30 for fuzzy, realistic workload for perf)
- Write the runnable harness (helper/SDK writes the score JSON to `$EVO_RESULT_PATH`; stdout and stderr are free for user output)
- Goodhart check (document concrete gaming strategies and mitigation). Include validation/gold-answer leakage explicitly: assume subagents can see benchmark traces and gold answers, so detection is the defense, not concealment. Prefer a crisp deterministic cheat-check gate, such as a workspace-specific script that greps the target/worktree for exact validation strings and exits non-zero on a match; register it with `evo gate add ... --phase pre` only after the user explicitly opts in. Mention expected cost for any LLM-judge variant and reserve it for paraphrase cases because it is flakier than exact-string checks.
- Held-out validation slice (60/70 training, 30/40 held-out) if the benchmark is hand-written

Do not run separate determinism checks during setup. Note the benchmark's determinism property in `project.md` (step 12) and move on. Variance surfaces during optimization itself, where it can be handled with real evidence rather than guessed at during setup.

### 10b. Audit the harness for amortizable wins

Apply any change that preserves what we measure -- descendants inherit it. Changes that could move the score (including for a different target) belong in `/evo:optimize`, not here.

Patterns to scan for:

- Serial loop over independent tasks -> thread/process pool
- Constant prefix across tasks -> prompt cache
- Per-task setup that could be one-time -> hoist out of the loop
- Transport errors (429/5xx) counted as task failures -> retry

### 10c. Apply instrumentation

Both modes satisfy the same file-and-env contract: per-task `task_<id>.json` written to `$EVO_TRACES_DIR`, a result JSON with a numeric `score` written to `$EVO_RESULT_PATH` (stdout if unset). `evo run` reads those files; it never inspects the language. The full spec is `references/instrumentation-contract.md`.

Paths below are relative to this `SKILL.md` file (resolve them against the skill directory).

- **SDK mode** (Python/Node only): add `from evo_agent import Run` (Python) or `import { Run } from '@evo-hq/evo-agent'` (Node) to the benchmark script. Wrap the eval loop per `references/sdk_python.py` or `references/sdk_node.js`.
- **Inline mode**: implement the contract in the benchmark's own language.
  - Benchmark is Python or Node: paste `references/inline_instrumentation.py` (or `.js`) and call `log_task` / `logTask` per task, `write_result` / `writeResult` once at the end.
  - Benchmark is any other language: port the contract from `references/instrumentation-contract.md` directly into that language (~10-15 lines: read the env vars, write each task trace, atomically publish the result). Do not add a Python/JS wrapper around it.

**Per-task emission is load-bearing.** If your benchmark evaluates N independent items (per-question math, per-test-case unit tests, per-document QA, per-sample reasoning trace), emit ONE `log_task` / `report` / trace file PER ITEM -- not one aggregate. Include the item's input, expected output, model output, and any per-item metadata as extras; that detail is what the dashboard's per-task panel + the verifier's reproducibility spot-check + the ideator's failure-clustering all rely on. Wrappers that compute the average score themselves and emit a single aggregate task entry look like they work but lose every diagnostic capability evo provides. The reference files have explicit USAGE EXAMPLES showing the per-item loop AND an ANTI-PATTERN block showing what NOT to do (see `references/inline_instrumentation.{py,js}` and `references/sdk_{python,node}.{py,js}`). Follow them. Single-aggregate emission is only valid when the benchmark really is one indivisible measurement (one e2e workflow, one perf number) -- and even then, attach every observable as extras.

**Runner-library wrappers are the common failure mode.** When the selected benchmark wraps a runner library (e.g. `inspect_evals`, `lm-eval-harness`, `evals`), the per-item loop is hidden inside the runner. The wrapper script's natural shape is to call the runner, read its aggregate output, and write a single `{"score": X}` to `result.json`. This is the anti-pattern. **Even when the runner library handles the per-sample loop internally, the wrapper script MUST parse the runner's per-sample output JSON and emit one `log_task(item_id, score=..., extras={...})` per item.** The runner typically already writes per-sample data to disk (most of these libraries do — `inspect_evals` writes per-sample logs, `lm-eval-harness` writes per-doc records); the wrapper just hasn't been forwarding it to evo. Without per-item forwarding, the dashboard's Tasks tab is empty, the verifier can't spot-check, and the agent has no way to diagnose WHICH items the model fails on — which is required for any RL-on-failures or curriculum strategy.

**Write traces for the future reader.** The only consumers of a committed experiment's traces look BACK at them -- a future orchestrator picking the next move, a verifier auditing for false progress, an ideator clustering failure modes. A trace that records `{score: 0}` and nothing else is unrecoverable: the future reader cannot tell whether the model produced wrong reasoning, unparseable format, or no output at all. Two rules of thumb:

- **`log_task` extras carry context** -- the item's input, expected output, the model's actual output (or first ~500 chars of it), parse outcome, any error. Cost: a few KB per task. Payoff: diagnosis is possible later without re-running the eval.
- **`evo annotate <exp_id>` is the human-readable summary** -- one or two factual lines after a run commits: which data sources were used, what the score actually represented, the failure mode if any. Annotations get loaded into future orchestrator decisions and ideator briefs, so write them as facts ("model emitted `\boxed{}`, eval prompt requested `ANSWER:`, format mismatch suspected"), not as plans or feelings.

### 10d. Audit with benchmark-reviewer (mandatory before baseline)

Before `evo run` is invoked for the first time, audit the harness via the bundled subagent:

```
Task(subagent_type="evo:benchmark-reviewer",
     prompt="workspace=<absolute path to dir containing .evo/>\nbenchmark_command=<the literal --benchmark string from evo init>\nunit=<one-line description of an item, e.g. 'AIME problem', 'HumanEval task', 'BFCL turn'>")
```

The subagent returns a JSON report with `passed`, `findings[]`. Each finding has `severity: block|warn|note`.

**Gate the baseline on this.** If `passed: false`, address every `block` finding before re-invoking the reviewer. Do **not** proceed to `evo run` until the report comes back clean. Typical `block` findings: aggregate-only emission (most common -- see step 10c), training source that overlaps the held-out set, no real gate registered for a constructed benchmark, harness silently writes `{"score": 0.0}` on error instead of crashing.

`warn` and `note` findings are informational -- record them in `.evo/project.md` and proceed.

### 10e. Cheap validation run

Before the full baseline, validate the toolchain with the cheapest possible end-to-end run (single task, smallest split, dry-run flag -- whatever is fastest). Run the check from the main repo root:

```bash
evo run exp_0000 --check
evo gate check exp_0000
```

`--check` runs the configured benchmark and gates and writes artifacts to a fresh check directory, but does **not** commit, evaluate, or consume retry budget. It uses evo's real placeholder substitution, runtime env resolution, remote workspace routing, and absolute `EVO_RESULT_PATH` / `EVO_TRACES_DIR` paths, so do not hand-roll a `mktemp` wrapper. Inspect the check artifacts with `evo show exp_0000` (the latest check appears under `attempts`).

Use `evo gate check <exp_id>` when only gate wiring changed or when you need to validate inherited gates without running the benchmark. It writes a `gate_check.json` artifact under the same checks directory and also does not mutate experiment state.

This is the authoritative wiring check, and it is language-agnostic -- it runs the real benchmark command and inspects the JSON artifacts, so a native inline implementation in any language is validated the same way a Python one is. The check asserts `result.json` exists, is non-empty, and is a JSON object with a numeric `score`. Also verify:

- All dependencies resolve and the command completes.
- Traces appear in `$EVO_TRACES_DIR` (if applicable).
- Each gate script runs cleanly on the unmodified target.

Fix any issues and re-validate before proceeding.

### 10f. Commit inside the worktree

Logical commits are ideal but not required. Minimal acceptable:

1. `add: benchmark harness + test cases`
2. `add: instrumentation` (only in SDK mode -- inline mode keeps the harness and instrumentation in one file, so this commit collapses into the previous one)

Use git from inside the worktree directory. These commits are on the experiment's branch, not main.

**Before the first commit in the worktree, add a `.gitignore`** for build artifacts and any stray evo workspace writes that shouldn't land on the experiment branch. At minimum:

```
.evo/
__pycache__/
*.pyc
.pytest_cache/
node_modules/
dist/
build/
```

Otherwise, running the benchmark once before committing will drag bytecode caches, `.pytest_cache/`, or stray `.evo/` writes into the experiment's tree and pollute every descendant branch. Belt-and-suspenders with step 10d's "run from main repo root" rule: even if cwd slips, the ignore catches it.

## 11. Run the baseline

**First, cd back to main repo root.** If the previous step left the shell inside the worktree, `evo run` will fail with "workspace not initialized" because `.evo/` only lives at the main repo root.

```bash
cd <main-repo-root>
evo run exp_0000
```

`evo run` executes the benchmark, captures the score, runs all inherited gates, and marks the experiment `committed` in a single step. Its output line ends with something like `COMMITTED exp_0000 0.4286`.

**Do NOT call `evo done` afterward.** In the current CLI, `evo run` is terminal: the experiment is already committed when it returns successfully, and calling `evo done exp_0000 --score <n>` errors with `"exp_0000 has status 'committed' -- cannot record again"`. The `evo done` command exists for cases where a human recorded a score outside of `evo run`, which is not the discover flow.

If gates failed, `evo run` exits non-zero and leaves the experiment in a failed state. Fix the benchmark or target inside the worktree, commit, then `evo run exp_0000` again.

**If `evo run` fails with a path error** (typically: `benchmark.py` not found), the stored benchmark command is missing the `{worktree}` placeholder. Confirm with `evo config get benchmark`, then fix it in place: `evo config set benchmark "<correct command>"`. Re-run `evo run exp_0000` if attempts remain; otherwise `evo discard exp_0000 --reason "..."` and re-allocate.

## 12. Write `.evo/project.md`

Lives at the top level of `.evo/` (run-agnostic, stable path regardless of active run). `evo init` creates an empty stub; overwrite it.

Document:
- What the target does
- What can be changed by optimization vs what must stay stable
- How to interpret benchmark output (score meaning, direction)
- **Benchmark determinism** -- one line, pick what fits:
  - `deterministic by construction` -- pure code, no randomness, no network
  - `uses LLMs with temp=0` -- expected to be deterministic in practice; flag if it isn't
  - `sampling-based, variance expected` -- inherent noise; optimize will need multi-run strategies
- Environment requirements discovered during validation
- **Resource profile (for run sizing)** -- the binding resource one benchmark run needs (exclusive GPU/accelerator, peak memory, an exclusive port, a shared DB/fixture, or an external API rate limit / $-per-run), whether concurrent benchmark runs are safe on this backend or must serialize, and rough time/cost per run. `/evo:optimize` reads this to size each round (see `plugins/evo/skills/optimize/references/sizing-the-round.md`)
- What each gate protects
- Benchmark gaming risks identified during the Goodhart check
- Future experiment candidates (the non-picked dimensions from step 3)

## 13. Report public-safe use case telemetry

Call the telemetry helper with a concrete but sanitized use-case summary. This
command is globally disabled when the user runs `evo telemetry off`; do not
branch on that yourself.

Write one short sentence that captures: workload/system shape, optimization
lever, and measured signal or failure mode. Add 3-6 lower-kebab-case tags for
the workload, lever, metric/failure mode, runtime category, or benchmark source.
Avoid filler-only tags like `optimization`, `benchmark`, or `codebase`.

```bash
evo telemetry usecase \
  --description "<public-safe, concrete use case summary>" \
  --tag <tag> --tag <tag> --tag <tag>
```

Privacy rule: do not include repo names, company names, customer names, file
paths, benchmark commands, prompt text, task examples, secrets, URLs, internal
system names, environment names, raw error logs, or exact dataset/item names.
Generalize private nouns instead of deleting useful signal: "internal billing
agent" becomes "workflow agent"; "Acme support router" becomes "support-style
routing agent".

Good example:

```bash
evo telemetry usecase \
  --description "Optimizing a tool-calling coding agent for higher task pass rate by tuning planner/retry behavior against an existing per-task eval." \
  --tag coding-agent --tag tool-calling --tag retry-policy --tag pass-rate --tag existing-eval
```

## 14. Report to the user

End the skill by reporting in chat:

- The dashboard URL (if not already mentioned)
- The baseline experiment ID and score
- The chosen optimization dimension and why
- A one-liner on next steps: "Run `/evo:optimize` to start the optimization loop."

**Do not run experiments outside `/evo:optimize`.** Even if the workspace's resource profile forces serial execution (e.g. exclusive-GPU, width 1), you still go through `/evo:optimize` with `subagents=1`. The optimize loop's value isn't just parallelism -- it's the structured loop around every experiment (scan-subagent cross-cutting analysis, brief writing, verifier pre/post hooks, ideator spawning on stall, frontier reconciliation). Bypassing optimize to "drive experiments directly" loses all of that and reverts to ad-hoc iteration. If you are tempted to skip optimize because the workload is serial, read `plugins/evo/skills/optimize/SKILL.md` for how to configure it for serial work -- the answer is `subagents=1`, NOT "don't run optimize."

- **Resume after crash:** if the host, the shell, or the machine restarts mid-flow, re-invoke `evo:optimize`. Evo reads `.evo/` and resumes from the last committed experiment -- no special restore procedure.
- **State is local to this machine:** experiment commits on branches like `evo/run_0000/exp_*` survive `git push --all`, but orchestration state (graph, annotations, project notes) lives only in `.evo/`. If that history matters to you, back up `.evo/` separately (e.g., `tar -czf evo-state-$(date +%F).tar.gz .evo/`).

## Polling discipline

When waiting on a long-running background process (training, evaluation, batch generation, any externally-spawned long task), do NOT use `while true; do sleep N; tail file; done`. That loop never exits when the underlying process crashes -- the tail keeps reading the same dead file, the agent interprets "no growth" as "still working," and the agent blocks indefinitely.

Use `evo wait`. The CLI is the bounded, structured replacement:

```bash
# wait for a training subprocess to exit, OR its log to stall, OR the GPU to go idle,
# whichever first; 60-minute ceiling; structured JSON on stdout
evo wait --for process=$TRAIN_PID \
         --for log-growth=$TRAIN_LOG \
         --for gpu-idle \
         --timeout 60m --stall-threshold 5m --json
```

Multiple `--for` flags combine; the wait returns on the first matching condition. The JSON output's `exit_reason` and `triggered_by` identify which condition fired (process-exited / log-stalled / gpu-idle / timed-out). Process / log-growth / gpu-* watches do not require an evo workspace; the workspace-anchored watches (`--for experiments`, `--for ideators`) still work for ideator-proposal and commit waits.

Full surface, exit codes, JSON shape, and examples in `references/evo-wait.md`.

If `evo wait` is not available for some reason (older CLI on PATH), fall back to a bounded poll loop that checks all three signals -- process liveness via `kill -0 $PID`, log growth via `wc -c` delta, GPU via `nvidia-smi --query-gpu=utilization.gpu` -- and exits on any one going negative. NEVER unbounded `while true`.

## Inspection commands (for debugging, reference only)

```bash
evo show <id>                       # full state of one experiment (attempts, diffs, annotations, notes)
evo config show                     # redacted workspace configuration
evo config runtime show             # runtime prepare/before-run/prefix recipe
evo env show                        # redacted runtime env metadata
evo traces <id> <task>              # per-task trace
evo annotate <id> <task> "analysis" # record failure analysis
evo scratchpad                      # bounded state summary
evo gate list <id>                  # effective gates at a node (inherited)
evo gate check <id>                 # run effective gates without benchmark or state mutation
```

## Rules

- Do NOT modify main after `evo init` unless the user explicitly asks. All new artifacts live in worktree 0.
- Do NOT install packages without the user's confirmation from step 5.
- Do NOT skip the held-out gate pairing when the benchmark was constructed from scratch. The gate is the safety net against Goodhart gaming, regardless of whether the benchmark is deterministic.
- Do NOT skip the Goodhart check when the benchmark was constructed from scratch. Gate pairing is mandatory, not optional.
