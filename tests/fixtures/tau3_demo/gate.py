"""Gate for tau3 demo.

Runs a small set of tasks on the test split to ensure the agent hasn't
regressed on critical behavior. Exits 0 if all gate tasks pass, 1 otherwise.

Environment variables:
    TAU3_DOMAIN       tau-bench domain (default: retail)
    AGENT_MODEL       LLM model for the agent (default: gpt-5.4)
    TAU3_GATE_TASKS   comma-separated task IDs to gate on (default: 0,1)
    TAU3_CONCURRENCY  max concurrent tasks (default: 3)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path


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

    domain = os.environ.get("TAU3_DOMAIN", "retail")
    model = os.environ.get("AGENT_MODEL", "gpt-5.4")
    gate_tasks = os.environ.get("TAU3_GATE_TASKS", "0,1").split(",")
    concurrency = int(os.environ.get("TAU3_CONCURRENCY", "3"))

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
        task_split_name="test",
        task_ids=gate_tasks,
        max_concurrency=concurrency,
        seed=300,
    )

    results = run_domain(config)

    passed = 0
    total = len(results.simulations)
    for sim in results.simulations:
        reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
        status = "PASS" if reward >= 0.5 else "FAIL"
        print(f"  {status}  task {sim.task_id}: {reward:.2f}")
        if reward >= 0.5:
            passed += 1

    print(f"\n[gate] {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
