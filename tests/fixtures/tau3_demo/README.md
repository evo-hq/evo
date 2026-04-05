# tau3 demo

Real tau-bench fixture for testing evo on the same use case as auto-harness:
optimizing an LLM agent on customer-service tasks (retail domain).

## Setup

1. Install tau3 dependencies:
   ```bash
   uv sync --extra tau3
   ```

2. Set environment variables:
   ```bash
   export OPENAI_API_KEY=sk-...
   export TAU2_DATA_DIR=~/.cache/tau2   # where tau-bench task data lives
   ```

3. Fetch tau-bench data (first time only):
   ```bash
   git clone --depth 1 https://github.com/sierra-research/tau2-bench.git /tmp/tau2-bench
   mkdir -p $TAU2_DATA_DIR
   cp -r /tmp/tau2-bench/data/tau2 $TAU2_DATA_DIR/
   rm -rf /tmp/tau2-bench
   ```

4. Reset existing workspace and init for tau3:
   ```bash
   uv run evo reset --yes
   uv run evo init \
     --target tests/fixtures/tau3_demo/agent/agent.py \
     --benchmark "python tests/fixtures/tau3_demo/benchmark.py --agent {target}" \
     --gate "python tests/fixtures/tau3_demo/gate.py --agent {target}" \
     --metric max
   ```

5. Run baseline:
   ```bash
   uv run evo new --parent root -m "baseline"
   uv run evo run exp_0000
   ```

## Defaults (matching auto-harness)

| Setting          | Value     |
|------------------|-----------|
| Domain           | retail    |
| Agent model      | gpt-5.4   |
| Benchmark split  | train     |
| Gate split       | test      |
| Concurrency      | 3         |
| Gate tasks       | 0,1       |
| Seed             | 300       |

## Environment overrides

| Variable             | Default  | Description                        |
|----------------------|----------|------------------------------------|
| `AGENT_MODEL`        | gpt-5.4  | LLM model for the agent            |
| `TAU3_DOMAIN`        | retail   | tau-bench domain                   |
| `TAU3_SPLIT`         | train    | split for benchmark.py             |
| `TAU3_CONCURRENCY`   | 3        | parallel task evaluations           |
| `TAU3_GATE_TASKS`    | 0,1      | comma-separated task IDs for gate  |
| `AGENT_REASONING_EFFORT` | (none) | reasoning effort param for model |
