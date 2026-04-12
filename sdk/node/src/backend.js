import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

export class LocalBackend {
  setup({ tracesDir, experimentId } = {}) {
    this._tracesDir = tracesDir || null;
    this._experimentId = experimentId || "unknown";
    if (this._tracesDir) {
      mkdirSync(this._tracesDir, { recursive: true });
    }
  }

  writeTrace(trace) {
    if (!this._tracesDir) return;
    const path = join(this._tracesDir, `task_${trace.task_id}.json`);
    writeFileSync(path, JSON.stringify(trace, null, 2), "utf-8");
  }

  emitResult(result) {
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  }

  emitGateSummary({ passed, lines }) {
    for (const line of lines) {
      process.stderr.write(line + "\n");
    }
  }
}

export function defaultBackend() {
  if (process.env.EVO_SERVER) {
    throw new Error(
      `HTTP backend not yet available (EVO_SERVER=${process.env.EVO_SERVER}). ` +
        "Use local mode by unsetting EVO_SERVER."
    );
  }
  return new LocalBackend();
}
