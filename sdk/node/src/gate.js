import { defaultBackend } from "./backend.js";

/**
 * Gate -- safety check reporting context.
 *
 * Usage:
 *   import { Gate } from 'evo-agent';
 *   const gate = new Gate();
 *   gate.check('5', { score: 1.0 });
 *   gate.check('9', { score: 0.3 });
 *   gate.finish();  // exits 0 if all passed, 1 otherwise
 */
export class Gate {
  constructor({ threshold = 0.5, backend } = {}) {
    this._threshold = threshold;
    this._backend = backend || defaultBackend();
    this._backend.setup({
      tracesDir: process.env.EVO_TRACES_DIR,
      experimentId: process.env.EVO_EXPERIMENT_ID,
    });
    this._checks = [];
    this._finished = false;
  }

  check(taskId, { score, passed, detail = "" } = {}) {
    if (passed === undefined) {
      if (score === undefined) {
        throw new Error("provide either score or passed");
      }
      passed = score >= this._threshold;
    }
    this._checks.push({
      task_id: String(taskId),
      passed,
      score: score ?? null,
      detail,
    });
  }

  finish() {
    if (this._finished) return;
    this._finished = true;

    const lines = [];
    let nPassed = 0;
    for (const c of this._checks) {
      const tag = c.passed ? "PASS" : "FAIL";
      const scoreStr = c.score !== null ? ` ${c.score.toFixed(2)}` : "";
      const detailStr = c.detail ? `  ${c.detail}` : "";
      lines.push(`  ${tag}  task ${c.task_id}:${scoreStr}${detailStr}`);
      if (c.passed) nPassed++;
    }
    const total = this._checks.length;
    const allPassed = nPassed === total;
    lines.push(`\n[gate] ${nPassed}/${total} passed`);

    this._backend.emitGateSummary({ passed: allPassed, lines });
    process.exit(allPassed ? 0 : 1);
  }
}
