This fixture mimics the `auto-harness` reference at a tiny scale.

- Target file: `agent/agent.py`
- Benchmark: `benchmark.py --agent {target}`
- Gate: `gate.py --agent {target}`

The benchmark scores a few policy-following tasks and writes task traces when
`MR_TRACES_DIR` is set. The gate protects two baseline-safe tasks from
regressing.
