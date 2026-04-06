"""Tau-bench benchmark wrapper for evo.

Runs tau-bench tasks against the agent loaded from --agent, outputs
evo-compatible JSON to stdout, and writes per-task traces to $EVO_TRACES_DIR
as each task completes (for live monitoring).

Uses evo-sdk's Run class for trace writing and score reporting.

Configuration is loaded from config.json (co-located with this script),
with environment variables as overrides.

Environment overrides:
    TAU3_DOMAIN       tau-bench domain
    AGENT_MODEL       LLM model for the agent
    TAU3_USER_MODEL   LLM model for the user simulator (defaults to AGENT_MODEL)
    TAU3_SPLIT        task split to run
    TAU3_CONCURRENCY  max concurrent tasks
    EVO_TRACES_DIR    set by `evo run` -- directory for per-task trace files
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from evo_sdk import Run

_HERE = Path(__file__).resolve().parent
_CONFIG = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))


def load_agent_class(agent_path: str):
    """Dynamically load EvoAgent from the given file path."""
    spec = importlib.util.spec_from_file_location("tau3_agent", agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {agent_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.EvoAgent


def _extract_events(sim) -> list[dict]:
    """Extract conversation events from a simulation, tolerating varied message types."""
    events = []
    try:
        for msg in sim.messages:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", None)
            if content is None:
                content = str(msg)
            elif not isinstance(content, str):
                try:
                    content = json.dumps(content)
                except (TypeError, ValueError):
                    content = str(content)
            events.append({"role": role, "content": content[:2000]})
    except Exception:
        events.append({"note": "could not extract remaining message history"})
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Run tau-bench benchmark")
    parser.add_argument("--agent", required=True, help="Path to agent.py")
    parser.add_argument("--task-ids", nargs="*", help="Specific task IDs to run")
    args = parser.parse_args()

    from tau2 import registry
    from tau2.data_model.simulation import TextRunConfig
    from tau2.run import run_domain

    AgentClass = load_agent_class(args.agent)

    domain = os.environ.get("TAU3_DOMAIN", _CONFIG["domain"])
    model = os.environ.get("AGENT_MODEL", _CONFIG["agent_model"])
    user_model = os.environ.get("TAU3_USER_MODEL", _CONFIG.get("user_model", model))
    split = os.environ.get("TAU3_SPLIT", _CONFIG["benchmark_split"])
    concurrency = int(os.environ.get("TAU3_CONCURRENCY", _CONFIG["concurrency"]))
    seed = _CONFIG.get("seed", 300)

    def create_agent(tools, domain_policy, **kwargs):
        return AgentClass(
            tools=tools,
            domain_policy=domain_policy,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    if registry.get_agent_factory("tau3_bench_agent") is None:
        registry.register_agent_factory(create_agent, "tau3_bench_agent")

    # Run() must be created before stdout redirect so the backend captures
    # the real stdout for emitting the final JSON result.
    reported: set[str] = set()

    with Run() as run:
        # Hook into tau2's Results to report traces as tasks complete.
        from tau2.data_model.simulation import Results

        _original_list_append = list.append

        class TracingList(list):
            """A list that reports to evo-sdk each time a simulation is appended."""

            def append(self, sim):
                _original_list_append(self, sim)
                tid = str(sim.task_id)
                reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
                reported.add(tid)
                run.report(
                    tid,
                    score=reward,
                    summary=f"reward={reward:.2f} domain={domain} split={split}",
                    failure_reason=None if reward >= 0.5 else "task_failed",
                    events=_extract_events(sim),
                )
                print(f"  trace: task {tid} = {reward:.2f}", file=sys.stderr)

        _orig_results_init = Results.__init__

        def _patched_init(self, *a, **kw):
            _orig_results_init(self, *a, **kw)
            tracing = TracingList(self.simulations)
            object.__setattr__(self, "simulations", tracing)

        Results.__init__ = _patched_init

        config = TextRunConfig(
            domain=domain,
            agent="tau3_bench_agent",
            llm_agent=model,
            llm_user=user_model,
            task_split_name=split,
            task_ids=args.task_ids,
            max_concurrency=concurrency,
            seed=seed,
        )

        # tau-bench prints rich tables to stdout; redirect to stderr so only
        # our JSON lands on stdout (required by evo's score parser).
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            results = run_domain(config)
        finally:
            sys.stdout = real_stdout
            Results.__init__ = _orig_results_init

        # Report any tasks not already captured by the tracing hook.
        for sim in results.simulations:
            tid = str(sim.task_id)
            if tid not in reported:
                reward = float(sim.reward_info.reward) if sim.reward_info else 0.0
                run.report(
                    tid,
                    score=reward,
                    summary=f"reward={reward:.2f} domain={domain} split={split}",
                    failure_reason=None if reward >= 0.5 else "task_failed",
                    events=_extract_events(sim),
                )


if __name__ == "__main__":
    main()
