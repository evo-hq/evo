---
description: Initialize evo for the current repository by identifying the target and benchmark, creating the workspace, and running the baseline experiment.
---

Set up `evo` for the current repository.

## 1. Explore the repo

Understand what this codebase does. Read READMEs, entry points, config files, and existing tests or evaluation scripts. Identify:
- The **optimization target**: which file benefits from iterative optimization?
- The **benchmark**: how is it evaluated? Look for existing eval scripts, test suites, or scoring harnesses. If none exist, you may need to create one.
- The **metric direction**: is higher better (`max`) or lower better (`min`)?
- The **gate** (optional): any regression tests or safety checks that must always pass.

## 2. Ensure benchmark dependencies are committed

Worktrees only contain git-tracked files. Before proceeding:
- Check `git status` for any untracked files the benchmark, gate, or target depend on.
- If any are untracked, they must be committed first or the benchmark will fail inside a worktree.

## 3. Instrument the evaluation

Prefer wrapping an existing benchmark rather than mutating it in place. The wrapper (or benchmark itself) must satisfy these contracts:

- **stdout**: only structured JSON with a `"score"` field. Example: `{"score": 0.78, "tasks": {"0": 1.0, "1": 0.0}}`
- **stderr**: all logging, progress, debug output goes here -- never to stdout.
- **traces**: write per-task detail files to `$EVO_TRACES_DIR` (set automatically by `evo run`).
- **exit code**: 0 on success, non-zero on infrastructure failure.

If the underlying tool prints noisy output to stdout (progress bars, logging frameworks, rich formatting), the wrapper must redirect or suppress it.

Create a gate script if appropriate.

## 4. Cheap validation run

Before running the full baseline, validate the toolchain with the cheapest possible end-to-end execution (single task, smallest split, dry-run flag, mock mode -- whatever is fastest):

Run the benchmark command directly (outside `evo`) with `EVO_TRACES_DIR` set to a temp directory. Verify:
- All dependencies resolve and the command runs to completion.
- Stdout is **only** valid JSON with a `"score"` field.
- Traces appear in the traces directory (if applicable).
- The gate script (if any) also runs cleanly.

Fix any issues and re-validate before proceeding. This catches environment problems, import errors, missing data, and stdout pollution for near-zero cost.

## 5. Initialize the workspace

```bash
uv run evo init --target <file> --benchmark "<command with {target}>" --metric <max|min> [--gate "<gate command>"]
```

## 6. Write `.evo/project.md`

Document what the target does, what can be changed, how to interpret benchmark output, execution strategy, and any environment requirements discovered during validation.

## 7. Run the baseline

```bash
uv run evo new --parent root -m "baseline"
uv run evo run exp_0000
```

## 8. Inspect results

```bash
uv run evo get <id>            # full experiment detail with scores
uv run evo traces <id> <task>  # per-task trace
uv run evo annotate <id> <task> "analysis"  # record failure analysis
uv run evo scratchpad          # full state: tree, best path, frontier, annotations, diffs
```

Read benchmark logs, traces, and score. Annotate failing tasks if applicable.
