---
name: discover
description: Initialize evo for the current repository by identifying the target and benchmark, creating the workspace, and running the baseline experiment.
argument-hint: <context about what to optimize>
---

Set up `evo` for the current repository.

## 1. Explore the repo

Understand what this codebase does. Read READMEs, entry points, config files, and existing tests or evaluation scripts. Identify:
- The **optimization target**: which file benefits from iterative optimization?
- The **benchmark**: how is it evaluated? Look for existing eval scripts, test suites, or scoring harnesses. If none exist, you may need to create one.
- The **metric direction**: is higher better (`max`) or lower better (`min`)?
- The **gate** (optional): a static safety check that must always pass (e.g., `--gate "pytest tests/ -x"`).
- **Gates to protect**: identify critical behaviors or tests that must never break regardless of optimization. These are non-negotiable invariants -- things like "refund flow works", "model doesn't produce NaN", "core API tests pass". Gates are commands that exit 0 on success and non-zero on failure. They can be:
  - An existing test suite or subset of it
  - Specific benchmark tasks run in isolation
  - Custom validation scripts
  - Any command that verifies a critical behavior

## 2. Ensure benchmark dependencies are committed

Worktrees only contain git-tracked files. Before proceeding:
- Check `git status` for any untracked files the benchmark, gate, or target depend on.
- If any are untracked, they must be committed first or the benchmark will fail inside a worktree.

## 3. Instrument the evaluation

Prefer wrapping an existing benchmark rather than mutating it in place.

### 3a. Ask the user which instrumentation mode to use

Before writing any instrumentation code, **ask the user once** (use `AskUserQuestion`):

> "I can wire up the benchmark in one of two ways:
>
> 1. **SDK mode** -- install `evo-agent` (Python: `pip install evo-agent` / Node: `npm install @evo-hq/evo-agent`). Richer per-task logs, ~5 lines of user code.
> 2. **Inline mode** -- paste a ~30-line helper directly into your benchmark. Zero new dependencies. Same data contract."

Record the answer in `.evo/meta.json` as `"instrumentation_mode": "sdk" | "inline"` so subagents and optimize rounds stay consistent (merge it in after `evo init` with a one-liner, e.g. `python -c "import json,pathlib; p=pathlib.Path('.evo/meta.json'); d=json.loads(p.read_text()); d['instrumentation_mode']='sdk'; p.write_text(json.dumps(d,indent=2))"`). **Never install packages without this confirmation.**

Regardless of mode, the wire protocol is identical: write `task_<id>.json` into `$EVO_TRACES_DIR`, print a single `{"score": ...}` JSON object to stdout at the end, send all other output to stderr, exit non-zero only on infrastructure failure (for gates: exit 0 all-pass, 1 any-fail).

### 3b. SDK mode (evo-agent)

Python:

```python
from evo_agent import Run, Gate

with Run() as run:
    for task in tasks:
        run.log(task["id"], "starting task")
        result = evaluate(task, agent)
        run.log(task["id"], {"output": result.output})
        run.report(
            task["id"],
            score=result.score,
            summary=f"reward={result.score:.2f}",
            failure_reason=None if result.passed else "task_failed",
        )
# finish() called automatically: prints score JSON to stdout, writes traces
```

```python
with Gate() as gate:
    for task in critical_tasks:
        result = evaluate(task, agent)
        gate.check(task["id"], score=result.score)
# exits 0 all-pass / 1 any-fail
```

Node:

```js
import { Run, Gate } from '@evo-hq/evo-agent';

const run = new Run();
for (const task of tasks) {
  const result = await evaluate(task);
  run.log(task.id, { output: result.output });
  run.report(task.id, { score: result.score });
}
await run.finish();
```

The SDK auto-reads `$EVO_TRACES_DIR` and `$EVO_EXPERIMENT_ID`. Traces are flushed on each `report()`, enabling live monitoring on the dashboard.

### 3c. Inline mode (no SDK dependency)

Copy the helper from the bundled reference file directly into the user's benchmark script:

- Python: `${CLAUDE_SKILL_DIR}/references/inline_instrumentation.py`
- Node: `${CLAUDE_SKILL_DIR}/references/inline_instrumentation.js`

Both expose two functions:

- `log_task(task_id, score, **fields)` / `logTask(taskId, score, {...})` -- write one task's trace
- `write_result()` / `writeResult()` -- emit the final score JSON to stdout (call once)

Wrap the user's eval loop around those calls. Zero new dependencies; same data contract as the SDK.

### 3d. Notes for both modes

If the underlying tool prints noisy output to stdout (progress bars, logging frameworks, rich formatting), the wrapper must redirect that to stderr.

Create a gate script if appropriate.

## 4. Cheap validation run

Before running the full baseline, validate the toolchain with the cheapest possible end-to-end execution (single task, smallest split, dry-run flag, mock mode -- whatever is fastest):

Run the benchmark command directly (outside `evo`) with `EVO_TRACES_DIR` set to a temp directory. **Pipe stdout through the validation script** to enforce the JSON contract:

```bash
EVO_TRACES_DIR=/tmp/evo_validate <benchmark_command> 2>/tmp/evo_validate_stderr.log | python ${CLAUDE_SKILL_DIR}/scripts/validate_stdout.py
```

The validator checks:
- stdout is **only** valid JSON (no progress bars, tables, or logging mixed in)
- JSON contains a `"score"` field with a numeric value

If validation fails, the script prints a diagnostic explaining what polluted stdout. Fix the benchmark wrapper and re-validate before proceeding.

Also verify:
- All dependencies resolve and the command runs to completion.
- Traces appear in the traces directory (if applicable).
- The gate script (if any) also runs cleanly.

Fix any issues and re-validate before proceeding. This catches environment problems, import errors, missing data, and stdout pollution for near-zero cost.

## 5. Initialize the workspace

```bash
uv run evo init --target <file> --benchmark "<command with {target}>" --metric <max|min> [--gate "<gate command>"]
```

`evo init` auto-starts the dashboard in the background and prints a line like `Dashboard live: http://127.0.0.1:8080 (pid 12345)`. **Relay that URL back to the user verbatim** -- it's the fastest way for them to watch experiments live. If port 8080 is busy, evo auto-increments; show whatever port is printed.

## 6. Set up gates

Gates are named commands that must always exit 0. They protect critical behaviors from regressing during optimization. Gates inherit down the experiment tree -- children automatically inherit all ancestor gates.

After init, add gates to the root node for behaviors identified in step 1:

```bash
# Protect an existing test suite
uv run evo gate add root --name "core_tests" --command "pytest tests/core/ -x"

# Protect specific benchmark tasks
uv run evo gate add root --name "refund_flow" --command "python benchmark.py --agent {target} --task-ids 5"

# Custom validation
uv run evo gate add root --name "no_crash" --command "python smoke_test.py --agent {target}"
```

Gate commands support `{target}` and `{worktree}` placeholders, same as benchmark commands.

Verify each gate passes on the unmodified target before proceeding:

```bash
uv run evo gate list root    # review what's registered
```

If no gates are identified during discovery, that's fine -- subagents can add gates later as they discover critical behaviors during optimization.

## 7. Write `.evo/project.md`

Document what the target does, what can be changed, how to interpret benchmark output, execution strategy, any environment requirements discovered during validation, and what gates protect.

## 8. Run the baseline

```bash
uv run evo new --parent root -m "baseline"
uv run evo run exp_0000
```

## 9. Inspect results

```bash
uv run evo get <id>            # full experiment detail with scores
uv run evo traces <id> <task>  # per-task trace
uv run evo annotate <id> <task> "analysis"  # record failure analysis
uv run evo scratchpad          # full state: tree, best path, frontier, annotations, diffs, gates
```

Read benchmark logs, traces, and score. Annotate failing tasks if applicable.
