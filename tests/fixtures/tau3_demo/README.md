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
   export OPENROUTER_API_KEY=sk-or-...
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

## Configuration

All defaults live in `config.json`. Environment variables override config values.

| Setting          | Config key         | Default                           |
|------------------|--------------------|-----------------------------------|
| Domain           | `domain`           | retail                            |
| Agent model      | `agent_model`      | openrouter/deepseek/deepseek-v3.2 |
| User sim model   | `user_model`       | openrouter/deepseek/deepseek-v3.2 |
| Benchmark split  | `benchmark_split`  | train                             |
| Gate split       | `gate_split`       | test                              |
| Concurrency      | `concurrency`      | 10                                |
| Gate tasks       | `gate_tasks`       | 5,9                               |
| Seed             | `seed`             | 300                               |

## Environment overrides

| Variable                  | Overrides config key | Description                       |
|---------------------------|----------------------|-----------------------------------|
| `AGENT_MODEL`             | `agent_model`        | LLM model for the agent           |
| `TAU3_USER_MODEL`         | `user_model`         | LLM model for user simulator      |
| `TAU3_DOMAIN`             | `domain`             | tau-bench domain                  |
| `TAU3_SPLIT`              | `benchmark_split`    | split for benchmark.py            |
| `TAU3_CONCURRENCY`        | `concurrency`        | parallel task evaluations         |
| `TAU3_GATE_TASKS`         | `gate_tasks`         | comma-separated task IDs for gate |
| `AGENT_REASONING_EFFORT`  | --                   | reasoning effort param for model  |
