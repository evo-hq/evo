// Node SDK usage example. Install: `npm install @evo-hq/evo-agent`.
//
// The SDK auto-reads $EVO_TRACES_DIR, $EVO_EXPERIMENT_ID, and
// $EVO_RESULT_PATH. Traces flush on each report() so the dashboard can
// stream progress live.

import { Run, Gate } from '@evo-hq/evo-agent';

// ---- Benchmark run ----

const run = new Run();
for (const task of tasks) {
  const result = await evaluate(task);
  run.log(task.id, { output: result.output });
  run.report(task.id, { score: result.score });
}
await run.finish();
// finish(): writes score JSON to $EVO_RESULT_PATH (or stdout if unset)
// and one task_<id>.json per task under $EVO_TRACES_DIR.

// ---- Gate (exits 0 all-pass / 1 any-fail) ----

const gate = new Gate();
for (const task of criticalTasks) {
  const result = await evaluate(task);
  gate.check(task.id, { score: result.score });
}
await gate.finish();
