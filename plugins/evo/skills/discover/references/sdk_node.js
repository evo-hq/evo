// Node SDK usage example. Install: `npm install @evo-hq/evo-agent`.
//
// The SDK auto-reads $EVO_TRACES_DIR and $EVO_EXPERIMENT_ID. Traces flush
// on each report() so the dashboard can stream progress live.

import { Run, Gate } from '@evo-hq/evo-agent';

// ---- Benchmark run ----

const run = new Run();
for (const task of tasks) {
  const result = await evaluate(task);
  run.log(task.id, { output: result.output });
  run.report(task.id, { score: result.score });
}
await run.finish();
// finish(): prints score JSON to stdout, writes task_<id>.json per task.

// ---- Gate (exits 0 all-pass / 1 any-fail) ----

const gate = new Gate();
for (const task of criticalTasks) {
  const result = await evaluate(task);
  gate.check(task.id, { score: result.score });
}
await gate.finish();
