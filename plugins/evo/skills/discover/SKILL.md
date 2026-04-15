---
name: discover
description: Initialize evo for the current repository by exploring the codebase, proposing unexplored optimization dimensions, constructing the benchmark inside a baseline worktree, and running the first experiment. Use when the user invokes /evo:discover, mentions setting up evo, wants to instrument a codebase for autonomous optimization, or asks to start a new evo run on a project.
argument-hint: <optional context about what to optimize>
---

# Discover

Internal procedure for `evo:discover`. The user only sees the user-facing prompts, the dashboard URL, and the baseline score -- everything else is the agent's choreography.

## Host conventions

This skill runs on any host that implements the Agent Skills spec. When the body uses generic phrases, apply the host's best-fit equivalent:

- **"ask the user"** -- use your host's structured multi-choice question tool if you have one (e.g. `AskUserQuestion`, `request_user_input`). If the host has none, phrase the question as plain text in your next reply and wait for the user's answer.
- **File paths like `references/...`** -- relative to this `SKILL.md`; resolve from the skill directory.
- **Slash commands shown in user-facing copy** (e.g. `/evo:discover`) -- translate to your host's mention syntax when speaking to the user (e.g. `$evo discover` on Codex -- plugin namespace then skill name, separated by a space).

## 0. Verify the evo CLI is available and in sync with the plugin

Before anything else, run:

```bash
evo-version-check
```

This wraps `evo --version` and additionally asserts the installed CLI matches the plugin manifest version (hosts refetch the plugin on version bumps, but do not reinstall the globally-installed CLI -- drift between the two breaks skills silently).

Four outcomes to handle:

1. **Exit 0, `evo-version-check: OK (plugin=X, cli=X)`** -- continue to step 1.
2. **Exit 1, "plugin manifest and installed CLI disagree"** -- stop and show the user the script's stderr verbatim; it tells them the `uv tool install --force evo-hq-cli==<version>` command to run. Then re-invoke this skill.
3. **Exit 2, "evo CLI not on PATH"** -- stop and tell the user:
   > `evo-hq-cli` isn't on your PATH. Install it once: `uv tool install evo-hq-cli` (or `pipx install evo-hq-cli`). Then re-invoke this skill.
4. **`evo-version-check: command not found`** -- the host's plugin install is incomplete (missing the `bin/` wrapper). Fall back to running `evo --version` directly and check for `evo-hq-cli` in the output; if it's a different package (commonly `evo 1.x` -- the unrelated SLAM tool), tell the user to uninstall it and install `evo-hq-cli` in its place.

Do not try to auto-install. Host sandbox + network policy may block it; leaving the install as a user action keeps failure modes clear.

## Guiding principles

- **Main stays clean.** Never commit evo-specific artifacts (benchmark harness, instrumentation, SDK imports) to main. Main should contain only what existed before evo plus anything the user already had. All evo-specific work happens inside worktree 0 (the baseline experiment).
- **Baseline is a worktree, not a main commit.** `evo init` creates `.evo/` but nothing in main changes. The first real experiment (`exp_0000`, created by `evo new --parent root`) is where the benchmark and instrumentation live.
- **Ask the user as little as possible.** Every question is a beat of friction. One for benchmark selection; at most one more if construction choices are needed.
- **Relay the dashboard URL verbatim when it prints.** This is the user's window into the run.

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
> 1. **SDK mode** -- install the SDK (Python: `pip install evo-hq-agent` / Node: `npm install @evo-hq/evo-agent`). Richer per-task logs, ~5 lines of user code.
> 2. **Inline mode** -- paste a ~30-line helper directly into the benchmark. Zero new dependencies. Same data contract."

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
evo init --target <file> --benchmark "<command using {worktree} and {target}>" --metric <max|min> \
  --instrumentation-mode <sdk|inline> [--gate "<gate command>"]
```

**Placeholder semantics.** Benchmark and gate commands support two placeholders, resolved lazily at run time by `evo run` / gate evaluation:

- `{worktree}` resolves to the absolute path of the experiment's worktree directory (e.g. `/path/to/repo/.evo/run_0000/worktrees/exp_0000`). Use this to reference files that live on the experiment branch, not on main.
- `{target}` resolves to the absolute path of the target file *inside that worktree* (e.g. `{worktree}/agent/solve.py`). Use this when your benchmark needs to load or exec the target dynamically.

**Critical rule:** `evo run` executes from the main repo root. When the benchmark script is constructed inside the worktree (the default in this flow), the command **must** reference it via `{worktree}` or the path won't resolve.

Example for a benchmark written at `{worktree}/benchmark.py` that will be committed to exp_0000:

```bash
evo init \
  --target agent/solve.py \
  --benchmark "python3 {worktree}/benchmark.py --target {target}" \
  --metric max
```

If the project uses a specific interpreter (poetry, pipenv, a venv), qualify it: `"poetry run python {worktree}/benchmark.py ..."`, `".venv/bin/python {worktree}/benchmark.py ..."`, etc.

`evo init` creates `.evo/`, the synthetic `root` node, and auto-starts the dashboard. It prints a line like:

```
Dashboard live: http://127.0.0.1:8080 (pid 12345)
```

**Relay that line back to the user verbatim.** If port 8080 is busy, evo auto-increments -- show whatever port prints. The URL is how the user watches the run.

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
- Write the runnable harness (stdout = single JSON with `score`, stderr = everything else)
- Goodhart check (document gaming strategies, mitigate each with a gate or held-out slice)
- Held-out validation slice (60/70 training, 30/40 held-out) if the benchmark is hand-written

Do not run separate determinism checks during setup. Note the benchmark's determinism property in `project.md` (step 12) and move on. Variance surfaces during optimization itself, where it can be handled with real evidence rather than guessed at during setup.

### 10b. Apply instrumentation

Based on the instrumentation mode passed to `evo init`:

Paths below are relative to this `SKILL.md` file (resolve them against the skill directory).

- **SDK mode**: add `from evo_agent import Run` (Python) or `import { Run } from '@evo-hq/evo-agent'` (Node) to the benchmark script. Wrap the eval loop per `references/sdk_python.py` or `references/sdk_node.js`.
- **Inline mode**: copy the helper from `references/inline_instrumentation.py` (or `.js`) into the benchmark. Use `log_task` / `logTask` per task and `write_result` / `writeResult` once at the end.

The wire protocol is the same either way: `task_<id>.json` written to `$EVO_TRACES_DIR`, single `{"score": ...}` JSON on stdout at the end, everything else on stderr.

If the underlying tool prints noisy stdout (progress bars, rich formatting, logging frameworks), redirect it to stderr.

### 10c. Cheap validation run

Before the full baseline, validate the toolchain with the cheapest possible end-to-end run (single task, smallest split, dry-run flag -- whatever is fastest).

**Important: run this from the main repo root, not from inside the worktree.** The validation writes traces to `.evo/validate/`, which must resolve to the workspace's `.evo/` at the main repo root. If you run from the worktree, the relative path creates `<worktree>/.evo/validate/` and those artifacts get staged into the experiment commit when you run `git add` later.

**Resolve `{worktree}`, `{target}`, and the validator script path yourself before running.** Evo substitutes `{worktree}` / `{target}` only inside `evo run`, not in a plain shell. The validation here is a plain shell call, so build the command with concrete absolute paths:

- `WORKTREE` = the worktree path returned by `evo new`
- `TARGET` = `$WORKTREE/<relative target path, e.g. agent/solve.py>`
- `VALIDATOR` = `<absolute path to this skill dir>/scripts/validate_stdout.py` -- resolve by taking the absolute path of this `SKILL.md`'s directory and appending `scripts/validate_stdout.py`

```bash
# from main repo root
WORKTREE="<...>"
TARGET="$WORKTREE/<...>"
VALIDATOR="<...>/scripts/validate_stdout.py"

mkdir -p .evo/validate
EVO_TRACES_DIR=.evo/validate \
  python3 "$WORKTREE/benchmark.py" --target "$TARGET" \
  2>.evo/validate/stderr.log \
  | python3 "$VALIDATOR"
```

Adapt the benchmark invocation (interpreter, args) to whatever you stored with `evo init`. The non-negotiable part is that the resulting bash command contains no literal `{worktree}`, `{target}`, or relative-script paths -- expand all of them to absolute paths before the shell runs the line.

Notes:
- Traces go to `.evo/validate/` at the main repo root (already locally ignored via step 6a). Avoids /tmp collisions and cross-process conflicts, keeps artifacts scoped to the project.

The validator checks:
- stdout is **only** valid JSON (no progress bars, tables, or logging mixed in)
- JSON contains a `score` field with a numeric value

If validation fails, the script prints a diagnostic. Fix the benchmark wrapper and re-validate before proceeding. Also verify:

- All dependencies resolve and the command completes.
- Traces appear in `$EVO_TRACES_DIR` (if applicable).
- Each gate script runs cleanly on the unmodified target.

Fix any issues and re-validate before proceeding. This catches environment problems, import errors, missing data, and stdout pollution cheaply.

### 10d. Commit inside the worktree

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

Otherwise, running the benchmark once before committing will drag bytecode caches, `.pytest_cache/`, or stray `.evo/` writes into the experiment's tree and pollute every descendant branch. Belt-and-suspenders with step 10c's "run from main repo root" rule: even if cwd slips, the ignore catches it.

## 11. Run the baseline

**First, cd back to main repo root.** If the previous step left the shell inside the worktree, `evo run` will fail with "workspace not initialized" because `.evo/` only lives at the main repo root.

```bash
cd <main-repo-root>
evo run exp_0000
```

`evo run` executes the benchmark, captures the score, runs all inherited gates, and marks the experiment `committed` in a single step. Its output line ends with something like `COMMITTED exp_0000 0.4286`.

**Do NOT call `evo done` afterward.** In the current CLI, `evo run` is terminal: the experiment is already committed when it returns successfully, and calling `evo done exp_0000 --score <n>` errors with `"exp_0000 has status 'committed' -- cannot record again"`. The `evo done` command exists for cases where a human recorded a score outside of `evo run`, which is not the discover flow.

If gates failed, `evo run` exits non-zero and leaves the experiment in a failed state. Fix the benchmark or target inside the worktree, commit, then `evo run exp_0000` again.

**If `evo run` fails with a path error** (typically: `benchmark.py` not found), the stored benchmark command in `.evo/run_0000/config.json` is missing the `{worktree}` placeholder. Fix by re-initializing: `evo discard exp_0000 --reason "benchmark command missing {worktree}"` then re-run step 7 with the correct `--benchmark` string. Hand-editing `config.json` works but is technical debt.

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
- What each gate protects
- Benchmark gaming risks identified during the Goodhart check
- Future experiment candidates (the non-picked dimensions from step 3)

## 13. Report to the user

End the skill by reporting in chat:

- The dashboard URL (if not already mentioned)
- The baseline experiment ID and score
- The chosen optimization dimension and why
- A one-liner on next steps: "Run `/evo:optimize` to start the optimization loop."
- **Resume after crash:** if the host, the shell, or the machine restarts mid-flow, re-invoke `evo:optimize`. Evo reads `.evo/` and resumes from the last committed experiment -- no special restore procedure.
- **State is local to this machine:** experiment commits on branches like `evo/run_0000/exp_*` survive `git push --all`, but orchestration state (graph, annotations, project notes) lives only in `.evo/`. If that history matters to you, back up `.evo/` separately (e.g., `tar -czf evo-state-$(date +%F).tar.gz .evo/`).

## Inspection commands (for debugging, reference only)

```bash
evo get <id>                        # full experiment detail with scores
evo traces <id> <task>              # per-task trace
evo annotate <id> <task> "analysis" # record failure analysis
evo scratchpad                      # full state: tree, best path, frontier, annotations, diffs, gates
evo gate list <id>                  # effective gates at a node (inherited)
```

## Rules

- Do NOT modify main after `evo init` unless the user explicitly asks. All new artifacts live in worktree 0.
- Do NOT install packages without the user's confirmation from step 5.
- Do NOT skip the held-out gate pairing when the benchmark was constructed from scratch. The gate is the safety net against Goodhart gaming, regardless of whether the benchmark is deterministic.
- Do NOT skip the Goodhart check when the benchmark was constructed from scratch. Gate pairing is mandatory, not optional.
