import { defaultBackend } from "./backend.js";

function utcNow() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/**
 * Run -- benchmark reporting context.
 *
 * Two separate concerns:
 * - log(taskId, data): append observability entries (string, object, number).
 *   Called many times per task.
 * - report(taskId, { score, ... }): record the final eval result for a task.
 *   Called once per task. Flushes accumulated logs into the trace file.
 *
 * Usage:
 *   import { Run } from 'evo-agent';
 *   const run = new Run();
 *   run.log('0', 'starting task');
 *   run.report('0', { score: 1.0, summary: 'completed' });
 *   await run.finish();
 */
export class Run {
  constructor({ experimentId, backend } = {}) {
    this._experimentId =
      experimentId || process.env.EVO_EXPERIMENT_ID || "unknown";
    this._backend = backend || defaultBackend();
    this._backend.setup({
      tracesDir: process.env.EVO_TRACES_DIR,
      experimentId: this._experimentId,
    });
    this._tasks = {};
    this._taskStarted = {};
    this._logs = {};
    this._startedAt = utcNow();
    this._finished = false;
  }

  log(taskId, data) {
    taskId = String(taskId);
    const now = utcNow();
    if (!(taskId in this._taskStarted)) this._taskStarted[taskId] = now;
    if (!(taskId in this._logs)) this._logs[taskId] = [];
    this._logs[taskId].push(data);
  }

  report(taskId, opts = {}) {
    taskId = String(taskId);
    const now = utcNow();
    const {
      score,
      status,
      passThreshold = 0.5,
      summary,
      failureReason,
      cost,
      startedAt,
      endedAt,
      artifacts,
      ...extra
    } = opts;

    const trace = {
      experiment_id: this._experimentId,
      task_id: taskId,
      status: status ?? (score >= passThreshold ? "passed" : "failed"),
      score,
    };
    if (summary !== undefined) trace.summary = summary;
    if (failureReason !== undefined) trace.failure_reason = failureReason;
    if (cost !== undefined) trace.cost = cost;
    trace.started_at = startedAt || this._taskStarted[taskId] || this._startedAt;
    trace.ended_at = endedAt || now;
    if (artifacts !== undefined) trace.artifacts = artifacts;
    Object.assign(trace, extra);

    this._tasks[taskId] = score;
    if (this._logs[taskId]?.length) trace.log = [...this._logs[taskId]];

    this._backend.writeTrace(trace);
  }

  async finish({ score } = {}) {
    if (this._finished) return {};
    this._finished = true;

    const taskIds = Object.keys(this._tasks);
    if (score === undefined) {
      score = taskIds.length === 0
        ? 0.0
        : taskIds.reduce((a, id) => a + this._tasks[id], 0) / taskIds.length;
    }

    const result = {
      score: Math.round(score * 10000) / 10000,
      tasks: { ...this._tasks },
      started_at: this._startedAt,
      ended_at: utcNow(),
    };
    this._backend.emitResult(result);
    return result;
  }
}
