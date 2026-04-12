# evo-agent (Node SDK)

Lightweight reporting SDK for [evo](https://github.com/evo-hq/evo) experiments. Zero dependencies, ESM-only, Node 18+.

Mirrors the `evo-agent` Python SDK surface: `Run` for per-task logging + scoring, `Gate` for safety checks with exit codes.

## Install

```bash
npm install @evo-hq/evo-agent
```

## Usage

### Benchmark (Run)

```js
import { Run } from '@evo-hq/evo-agent';

const run = new Run();
for (const task of tasks) {
  run.log(task.id, 'starting task');
  const result = await evaluate(task);
  run.log(task.id, { output: result.output });
  run.report(task.id, {
    score: result.score,
    summary: `reward=${result.score.toFixed(2)}`,
    failureReason: result.passed ? undefined : 'task_failed',
  });
}
await run.finish();
```

On `finish()`:
- Prints the score JSON to stdout (the contract evo reads).
- Per-task trace files are written to `$EVO_TRACES_DIR` as each `report()` is called.

### Gate

```js
import { Gate } from '@evo-hq/evo-agent';

const gate = new Gate();
for (const task of criticalTasks) {
  const result = await evaluate(task);
  gate.check(task.id, { score: result.score, detail: `reward=${result.score.toFixed(2)}` });
}
gate.finish();  // exits 0 if all passed, 1 otherwise
```

## Environment

- `EVO_TRACES_DIR`   directory where `task_<id>.json` files are written (set by `evo run`)
- `EVO_EXPERIMENT_ID`  experiment label embedded in each trace

Both are set automatically when the evo CLI spawns your benchmark. Missing vars are tolerated -- traces are just skipped.
