import {
  writeFileSync,
  mkdirSync,
  openSync,
  closeSync,
  renameSync,
} from "node:fs";
import { dirname, join } from "node:path";

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
    const payload = JSON.stringify(result, null, 2);
    const resultPath = process.env.EVO_RESULT_PATH;
    if (!resultPath) {
      process.stdout.write(payload + "\n");
      return;
    }
    mkdirSync(dirname(resultPath), { recursive: true });
    // Claim destination + tmp+rename: duplicate writers fail-fast on the
    // 'wx' (O_EXCL) claim; a crash mid-publish leaves an empty file at
    // resultPath (caught by load_result) instead of a partial write.
    try {
      closeSync(openSync(resultPath, "wx"));
    } catch (e) {
      if (e.code === "EEXIST") {
        throw new Error(
          `${resultPath} already exists; only one Run.finish() / writeResult() per attempt`
        );
      }
      throw e;
    }
    const tmp = resultPath + ".tmp";
    writeFileSync(tmp, payload, "utf-8");
    renameSync(tmp, resultPath);
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
