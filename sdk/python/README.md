# evo-hq-agent (Python SDK)

Lightweight reporting SDK for [evo](https://github.com/evo-hq/evo) experiments. Zero dependencies, Python 3.10+.

Mirrors the [`@evo-hq/evo-agent`](https://www.npmjs.com/package/@evo-hq/evo-agent) Node SDK surface: `Run` for per-task logging + scoring, `Gate` for safety checks with exit codes.

## Install

```bash
pip install evo-hq-agent
```

Install name and import name differ (same pattern as `python-dateutil` / `dateutil`):

```python
from evo_agent import Run, Gate
```

## Usage

### Benchmark (Run)

```python
from evo_agent import Run

with Run() as run:
    for task in tasks:
        run.log(task["id"], "starting task")
        result = evaluate(task)
        run.log(task["id"], {"output": result.output})
        run.report(
            task["id"],
            score=result.score,
            summary=f"reward={result.score:.2f}",
            failure_reason=None if result.passed else "task_failed",
        )
# finish() called automatically on __exit__:
#  - prints score JSON to stdout (the contract evo reads)
#  - per-task trace files were written to $EVO_TRACES_DIR as each report() ran
```

### Gate

```python
from evo_agent import Gate

with Gate() as gate:
    for task in critical_tasks:
        result = evaluate(task)
        gate.check(task["id"], score=result.score, detail=f"reward={result.score:.2f}")
# exits 0 if all passed, 1 otherwise
```

## Environment

- `EVO_TRACES_DIR`   directory where `task_<id>.json` files are written (set by `evo run`)
- `EVO_EXPERIMENT_ID`  experiment label embedded in each trace

Both are set automatically when the evo CLI spawns your benchmark. Missing vars are tolerated -- traces are just skipped.
