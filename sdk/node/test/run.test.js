import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Run, Gate } from "../src/index.js";

function withTmp(fn) {
  const dir = mkdtempSync(join(tmpdir(), "evo-agent-test-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function captureStdout(fn) {
  const chunks = [];
  const orig = process.stdout.write.bind(process.stdout);
  process.stdout.write = (s) => {
    chunks.push(String(s));
    return true;
  };
  try {
    fn();
  } finally {
    process.stdout.write = orig;
  }
  return chunks.join("");
}

test("Run writes trace files and emits score JSON", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-123";
    const run = new Run();
    run.log("0", "starting");
    run.log("0", { role: "user", content: "hi" });
    run.report("0", { score: 1.0, summary: "ok" });
    run.report("1", { score: 0.0, failureReason: "bad" });

    const out = captureStdout(() => run.finish());
    const result = JSON.parse(out);
    assert.equal(result.score, 0.5);
    assert.deepEqual(result.tasks, { "0": 1.0, "1": 0.0 });

    const files = readdirSync(dir).sort();
    assert.deepEqual(files, ["task_0.json", "task_1.json"]);
    const t0 = JSON.parse(readFileSync(join(dir, "task_0.json"), "utf-8"));
    assert.equal(t0.experiment_id, "exp-123");
    assert.equal(t0.status, "passed");
    assert.equal(t0.score, 1.0);
    assert.deepEqual(t0.log, ["starting", { role: "user", content: "hi" }]);
    const t1 = JSON.parse(readFileSync(join(dir, "task_1.json"), "utf-8"));
    assert.equal(t1.status, "failed");
    assert.equal(t1.failure_reason, "bad");

    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
  });
});

test("Run writes result file when EVO_RESULT_PATH is set, stdout is silent", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-result-path";
    process.env.EVO_RESULT_PATH = join(dir, "result.json");
    const run = new Run();
    run.report("0", { score: 1.0 });
    run.report("1", { score: 0.0 });

    const out = captureStdout(() => run.finish());
    assert.equal(out, "", `expected empty stdout, got: ${out}`);

    const written = JSON.parse(readFileSync(process.env.EVO_RESULT_PATH, "utf-8"));
    assert.equal(written.score, 0.5);
    assert.deepEqual(written.tasks, { "0": 1.0, "1": 0.0 });

    const leftovers = readdirSync(dir).filter((n) => n.endsWith(".tmp"));
    assert.deepEqual(leftovers, [], `leftover tmp files: ${leftovers}`);

    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
    delete process.env.EVO_RESULT_PATH;
  });
});

test("Run raises when result file already exists", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-duplicate";
    const resultPath = join(dir, "result.json");
    process.env.EVO_RESULT_PATH = resultPath;

    // withTmp's cleanup runs synchronously after fn returns the promise, so
    // recreate the dir before the test, then pre-create the result file as
    // if an earlier writer published.
    mkdirSync(dir, { recursive: true });
    writeFileSync(resultPath, '{"score": 0.0}', "utf-8");

    const run = new Run();
    run.report("0", { score: 0.5555 });

    await assert.rejects(
      () => run.finish(),
      /already exists/,
      "Expected error on duplicate write to result.json"
    );

    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
    delete process.env.EVO_RESULT_PATH;
  });
});

test("Run falls back to stdout when EVO_RESULT_PATH is unset", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-stdout-fallback";
    delete process.env.EVO_RESULT_PATH;
    const run = new Run();
    run.report("0", { score: 0.7 });

    const out = captureStdout(() => run.finish());
    const result = JSON.parse(out);
    assert.equal(result.score, 0.7);

    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
  });
});

test("Run direction propagates to tasks_meta and traces", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-dir";
    const run = new Run();
    run.report("accuracy", { score: 0.9, direction: "max" });
    run.report("latency_ms", { score: 140, direction: "min" });
    run.report("throughput", { score: 12.5 });

    const out = captureStdout(() => run.finish({ score: 0.5 }));
    const result = JSON.parse(out);
    assert.deepEqual(result.tasks_meta, {
      accuracy: { direction: "max" },
      latency_ms: { direction: "min" },
    });

    const lat = JSON.parse(readFileSync(join(dir, "task_latency_ms.json"), "utf-8"));
    assert.equal(lat.direction, "min");
    const thr = JSON.parse(readFileSync(join(dir, "task_throughput.json"), "utf-8"));
    assert.equal(thr.direction, undefined);

    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
  });
});

test("Run direction rejects invalid value", () => {
  const run = new Run();
  assert.throws(() => run.report("t", { score: 1.0, direction: "bogus" }), /direction must be/);
});

test("Run omits tasks_meta when no directions given", async () => {
  await withTmp(async (dir) => {
    process.env.EVO_TRACES_DIR = dir;
    process.env.EVO_EXPERIMENT_ID = "exp-none";
    const run = new Run();
    run.report("a", { score: 0.5 });
    const out = captureStdout(() => run.finish());
    const result = JSON.parse(out);
    assert.equal(result.tasks_meta, undefined);
    delete process.env.EVO_TRACES_DIR;
    delete process.env.EVO_EXPERIMENT_ID;
  });
});

test("Gate.check accepts score or explicit passed", () => {
  const g = new Gate();
  g.check("a", { score: 0.8 });
  g.check("b", { score: 0.3 });
  g.check("c", { passed: true, detail: "manual" });
  assert.equal(g._checks.length, 3);
  assert.equal(g._checks[0].passed, true);
  assert.equal(g._checks[1].passed, false);
  assert.equal(g._checks[2].passed, true);
  assert.equal(g._checks[2].detail, "manual");
});
