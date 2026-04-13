"""Python SDK usage examples. Install: `pip install evo-hq-agent`.

The SDK auto-reads $EVO_TRACES_DIR and $EVO_EXPERIMENT_ID. Traces flush on
each report() so the dashboard can stream progress live.
"""

from evo_agent import Run, Gate


# ---- Benchmark run ----

with Run() as run:
    for task in tasks:
        run.log(task["id"], "starting task")
        result = evaluate(task, agent)
        run.log(task["id"], {"output": result.output})
        run.report(
            task["id"],
            score=result.score,
            summary=f"reward={result.score:.2f}",
            failure_reason=None if result.passed else "task_failed",
        )
# finish() runs on context exit: prints the score JSON to stdout and
# writes one task_<id>.json per task under $EVO_TRACES_DIR.


# ---- Gate (exits 0 all-pass / 1 any-fail) ----

with Gate() as gate:
    for task in critical_tasks:
        result = evaluate(task, agent)
        gate.check(task["id"], score=result.score)
