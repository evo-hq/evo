/**
 * Inline instrumentation template for Node benchmarks.
 *
 * Use this when the user declines the `evo-agent` SDK. Import the helpers
 * into the benchmark script and call logTask() + writeResult() in place of
 * the SDK's Run class. Zero new dependencies.
 *
 * Contract (same as the SDK):
 * - Read EVO_TRACES_DIR and EVO_EXPERIMENT_ID from process.env.
 * - Write task_<id>.json files into EVO_TRACES_DIR as each task finishes.
 * - Print a single JSON object with a "score" field to stdout at the end.
 * - All other output goes to stderr.
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const TRACES_DIR = process.env.EVO_TRACES_DIR || null;
const EXPERIMENT_ID = process.env.EVO_EXPERIMENT_ID || "unknown";
const SCORES = {};
const TASK_META = {};
const STARTED_AT = new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");

if (TRACES_DIR) mkdirSync(TRACES_DIR, { recursive: true });

/**
 * Record the result for one task. `direction` is "max" (higher is better,
 * default) or "min" (lower is better, e.g. latency). Set it only when this
 * task's direction differs from the benchmark's top-level --metric.
 * Propagates to tasks_meta in the final stdout JSON.
 */
export function logTask(taskId, score, { summary, failureReason, log, direction, ...extra } = {}) {
  taskId = String(taskId);
  if (direction !== undefined && direction !== "max" && direction !== "min") {
    throw new Error(`direction must be 'max' or 'min', got ${JSON.stringify(direction)}`);
  }
  SCORES[taskId] = score;
  if (direction !== undefined) TASK_META[taskId] = { direction };
  if (!TRACES_DIR) return;
  const trace = {
    experiment_id: EXPERIMENT_ID,
    task_id: taskId,
    status: score >= 0.5 ? "passed" : "failed",
    score,
    ended_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
  };
  if (direction !== undefined) trace.direction = direction;
  if (summary !== undefined) trace.summary = summary;
  if (failureReason !== undefined) trace.failure_reason = failureReason;
  if (log !== undefined) trace.log = log;
  Object.assign(trace, extra);
  writeFileSync(join(TRACES_DIR, `task_${taskId}.json`), JSON.stringify(trace, null, 2), "utf-8");
}

export function writeResult(score) {
  const ids = Object.keys(SCORES);
  if (score === undefined) {
    score = ids.length === 0 ? 0.0 : ids.reduce((a, id) => a + SCORES[id], 0) / ids.length;
  }
  score = Math.round(score * 10000) / 10000;
  const result = {
    score,
    tasks: { ...SCORES },
    started_at: STARTED_AT,
    ended_at: new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00"),
  };
  if (Object.keys(TASK_META).length > 0) {
    result.tasks_meta = Object.fromEntries(
      Object.entries(TASK_META).map(([k, v]) => [k, { ...v }])
    );
  }
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return score;
}
