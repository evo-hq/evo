"""Gate for tau3 demo.

Runs a small set of tasks on the test split to ensure the agent hasn't
regressed on critical behavior. Exits 0 if all gate tasks pass, 1 otherwise.

Uses evo-sdk's Gate class for pass/fail reporting and exit code handling.

Configuration is loaded from config.json (co-located with this script),
with environment variables as overrides.

Environment overrides:
    TAU3_DOMAIN       tau-bench domain
    AGENT_MODEL       LLM model for the agent
    TAU3_USER_MODEL   LLM model for the user simulator (defaults to AGENT_MODEL)
    TAU3_GATE_TASKS   comma-separated task IDs to gate on -- must be from test split
    TAU3_CONCURRENCY  max concurrent tasks
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from evo_sdk import Gate

_HERE = Path(__file__).resolve().parent
_CONFIG = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))


def load_agent_class(agent_path: str):
    spec = importlib.util.spec_from_file_location("tau3_agent_gate", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.EvoAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate check for tau3 agent")
    parser.add_argument("--agent", required=True, help="Path to agent.py")
    args = parser.parse_args()

    from tau2 import registry
    from tau2.data_model.simulation import TextRunConfig
    from tau2.run import run_domain

    AgentClass = load_agent_class(args.agent)

    domain = os.environ.get("TAU3_DOMAIN", _CONFIG["domain"])
    model = os.environ.get("AGENT_MODEL", _CONFIG["agent_model"])
    user_model = os.environ.get("TAU3_USER_MODEL", _CONFIG.get("user_model", model))
    gate_tasks = os.environ.get("TAU3_GATE_TASKS", _CONFIG["gate_tasks"]).split(",")
    concurrency = int(os.environ.get("TAU3_CONCURRENCY", _CONFIG["concurrency"]))
    seed = _CONFIG.get("seed", 300)

    def create_agent(tools, domain_policy, **kwargs):
        return AgentClass(
            tools=tools,
            domain_policy=domain_policy,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    if registry.get_agent_factory("tau3_gate_agent") is None:
        registry.register_agent_factory(create_agent, "tau3_gate_agent")

    config = TextRunConfig(
        domain=domain,
        agent="tau3_gate_agent",
        llm_agent=model,
        llm_user=user_model,
        task_split_name=_CONFIG.get("gate_split", "test"),
        task_ids=gate_tasks,
        max_concurrency=concurrency,
        seed=seed,
    )

    # tau-bench prints rich tables to stdout; redirect to stderr.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        results = run_domain(config)
    finally:
        sys.stdout = real_stdout

    with Gate() as gate:
        for sim in results.simulations:
            reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
            gate.check(str(sim.task_id), score=reward)


if __name__ == "__main__":
    main()
