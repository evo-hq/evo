"""Frontier selection strategies.

The `frontier` command returns ranked frontier nodes (committed leaves that the
orchestrator can branch from). Which nodes and in what order depends on the
active strategy, stored in `.evo/config.json` under `frontier_strategy`.

Each strategy is a `{kind, params}` pair. The registry below is the single
source of truth: the CLI validator, the dashboard picker, and every picker
function read it. Adding a strategy means one registry entry + one picker.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Callable

from .core import (
    atomic_write_json,
    infra_path,
    load_json,
    lock_file_for,
    utc_now,
)
from .locking import advisory_lock


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #

FRONTIER_STRATEGIES: dict[str, dict[str, Any]] = {
    "argmax": {
        "label": "Argmax (current best)",
        "description": "Return the single highest-scoring frontier node. Pure exploit.",
        "detail": (
            "Always picks the single node with the highest score. No randomness, "
            "no exploration.\n\n"
            "Good when you're confident the current best branch is the right one "
            "to keep pushing. Risk: plateaus fast if that branch hits a local "
            "optimum, since nothing else gets a turn."
        ),
        "params": [],
    },
    "top_k": {
        "label": "Top-K",
        "description": "Top K frontier nodes by score. Ties broken lexicographically by id.",
        "detail": (
            "Returns the K best-scoring nodes, in descending order. Lets a round's "
            "N subagents branch from K distinct parents instead of piling onto one.\n\n"
            "Still purely aggregate-score driven -- older high-scoring branches "
            "dominate newer ones that haven't had a chance to mature."
        ),
        "params": [
            {"name": "k", "type": "int", "min": 1, "max": 50, "default": 5, "label": "K",
             "detail": "Number of top scorers to return. K=1 is equivalent to argmax. "
                       "Usually set to match your round's subagent count."},
        ],
    },
    "epsilon_greedy": {
        "label": "ε-greedy",
        "description": "Argmax with probability 1-ε; a uniform-random frontier node with probability ε. Logs the rng seed.",
        "detail": (
            "Classic bandit exploration. With probability 1-ε, return the argmax; "
            "with probability ε, return a uniformly-random frontier node.\n\n"
            "ε = 0 collapses to argmax. ε = 1 is pure random. Typical tuning: "
            "0.05–0.2. Higher values help escape local optima, lower values "
            "exploit the current best more aggressively.\n\n"
            "Stochastic — the rng seed is recorded in infra_log.json for replay."
        ),
        "params": [
            {"name": "epsilon", "type": "float", "min": 0.0, "max": 1.0, "default": 0.1, "label": "ε",
             "detail": "Exploration rate in [0, 1]. Probability of picking a random "
                       "frontier node instead of the current best."},
        ],
    },
    "softmax": {
        "label": "Softmax (temperature)",
        "description": "Sample without replacement with probability ∝ exp(score / T). Low T = exploit, high T = explore. Logs the rng seed.",
        "detail": (
            "Continuous middle-ground between argmax and uniform random. Draws K "
            "nodes without replacement, each picked with probability proportional "
            "to exp(score / T).\n\n"
            "Low T (e.g. 0.05) concentrates probability on the top scorer -- "
            "near-argmax behavior. High T (e.g. 2.0) flattens the distribution "
            "toward uniform. T = 0.5 is a reasonable default for moderate "
            "exploration.\n\n"
            "Stochastic — seed is logged."
        ),
        "params": [
            {"name": "temperature", "type": "float", "min": 0.01, "max": 10.0, "default": 0.5, "label": "Temperature",
             "detail": "Sharpness of the sampling distribution. As T → 0 behavior "
                       "approaches argmax; as T → ∞ it approaches uniform."},
            {"name": "k", "type": "int", "min": 1, "max": 50, "default": 5, "label": "Samples",
             "detail": "Number of draws without replacement. Usually match your "
                       "round's subagent count."},
        ],
    },
    "pareto_per_task": {
        "label": "Pareto per-task (GEPA-inspired)",
        "description": "Preserve nodes that are best on at least one task; drop dominated ones; sample with probability proportional to how many tasks each is best at. Honors per-task `direction` (max/min) when the benchmark emits `tasks_meta`. Logs the rng seed.",
        "detail": (
            "Preserves specialists the aggregate score would hide. Algorithm:\n\n"
            "1. For each task, find the top score across all frontier candidates.\n"
            "2. Collect candidates that tie for best on at least one task.\n"
            "3. Drop any candidate weakly dominated by another on every task it wins.\n"
            "4. Sample K from the survivors with probability proportional to how "
            "many tasks each candidate is the best on.\n\n"
            "Honors `tasks_meta[task_id].direction` when the benchmark emits it "
            "(so `latency_ms` with direction=\"min\" ranks low-is-better). Falls "
            "back to the workspace-level `--metric` when a task has no direction.\n\n"
            "Intersects task keys across outcomes — a task only counts if every "
            "considered candidate reported it. Handles benchmark drift cleanly.\n\n"
            "Inspired by GEPA (Agrawal et al., arXiv:2507.19457), specifically its "
            "selection step. The genetic mutation + reflective LLM parts of GEPA "
            "live elsewhere in evo's tree-search loop."
        ),
        "params": [
            {"name": "k", "type": "int", "min": 1, "max": 50, "default": 5, "label": "Samples",
             "detail": "Number of candidates to sample from the Pareto front. If "
                       "the front is smaller than K, returns everyone on it."},
            {"name": "task_floor", "type": "float", "min": 0.0, "max": 1.0, "default": 0.0,
             "label": "Ignore max-direction tasks where top score ≤",
             "detail": "For direction=max tasks, skip a task if no candidate scored "
                       "above this threshold. Stops 'everyone failed task T' from "
                       "polluting the front with every candidate tied at 0. "
                       "Min-direction tasks always count — \"nobody was fast\" is "
                       "itself Pareto signal."},
        ],
    },
}

DEFAULT_FRONTIER_STRATEGY: dict[str, Any] = {"kind": "argmax", "params": {}}


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #

def validate_frontier_strategy(obj: Any) -> dict[str, Any]:
    """Normalize + validate a strategy dict. Returns {kind, params} with every
    param filled from defaults. Raises ValueError on unknown kind or out-of-range params."""
    if not isinstance(obj, dict):
        raise ValueError(f"frontier_strategy must be a dict, got {type(obj).__name__}")
    kind = obj.get("kind")
    spec = FRONTIER_STRATEGIES.get(kind)
    if spec is None:
        known = ", ".join(FRONTIER_STRATEGIES.keys())
        raise ValueError(f"unknown frontier_strategy.kind: {kind!r} (known: {known})")
    params_in = obj.get("params") or {}
    if not isinstance(params_in, dict):
        raise ValueError("frontier_strategy.params must be a dict")
    params_out: dict[str, Any] = {}
    for p in spec["params"]:
        name = p["name"]
        raw = params_in.get(name, p["default"])
        caster = int if p["type"] == "int" else float
        try:
            val = caster(raw)
        except (TypeError, ValueError):
            raise ValueError(f"frontier_strategy.params.{name} is not a {p['type']}")
        lo, hi = p["min"], p["max"]
        if val < lo or val > hi:
            raise ValueError(f"frontier_strategy.params.{name}={val} out of [{lo}, {hi}]")
        params_out[name] = val
    return {"kind": kind, "params": params_out}


def resolve_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Read frontier_strategy from config with default fallback. Always validated."""
    raw = config.get("frontier_strategy") or DEFAULT_FRONTIER_STRATEGY
    return validate_frontier_strategy(raw)


# --------------------------------------------------------------------------- #
# Pickers                                                                     #
# --------------------------------------------------------------------------- #

def _node_summary(node: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "id": node["id"],
        "score": node.get("score"),
        "epoch": node.get("eval_epoch"),
        "hypothesis": node.get("hypothesis"),
        "rank": rank,
    }


def _sign(metric: str) -> int:
    """Return +1 if higher-is-better, -1 if lower-is-better. Defaults to +1."""
    return -1 if (metric or "").lower() == "min" else 1


def _score_of(node: dict[str, Any], metric: str) -> float:
    """Directional score: always higher-is-better after this call."""
    s = node.get("score")
    if s is None:
        return float("-inf")
    return float(s) * _sign(metric)


def _pick_argmax(nodes: list[dict], params: dict, metric: str,
                 outcomes: dict, rng: random.Random) -> list[dict]:
    if not nodes:
        return []
    ranked = sorted(nodes, key=lambda n: (-_score_of(n, metric), n["id"]))
    return [_node_summary(ranked[0], 1)]


def _pick_top_k(nodes: list[dict], params: dict, metric: str,
                outcomes: dict, rng: random.Random) -> list[dict]:
    if not nodes:
        return []
    k = int(params["k"])
    ranked = sorted(nodes, key=lambda n: (-_score_of(n, metric), n["id"]))[:k]
    return [_node_summary(n, i + 1) for i, n in enumerate(ranked)]


def _pick_epsilon_greedy(nodes: list[dict], params: dict, metric: str,
                         outcomes: dict, rng: random.Random) -> list[dict]:
    if not nodes:
        return []
    eps = float(params["epsilon"])
    if rng.random() < eps:
        chosen = rng.choice(nodes)
    else:
        chosen = min(nodes, key=lambda n: (-_score_of(n, metric), n["id"]))
    return [_node_summary(chosen, 1)]


def _pick_softmax(nodes: list[dict], params: dict, metric: str,
                  outcomes: dict, rng: random.Random) -> list[dict]:
    if not nodes:
        return []
    temperature = float(params["temperature"])
    k = min(int(params["k"]), len(nodes))
    scores = [_score_of(n, metric) for n in nodes]
    # Subtract max for numerical stability.
    m = max(scores)
    weights = [math.exp((s - m) / temperature) for s in scores]
    return _weighted_sample_without_replacement(nodes, weights, k, rng)


def _pick_pareto_per_task(nodes: list[dict], params: dict, metric: str,
                          outcomes: dict, rng: random.Random) -> list[dict]:
    if not nodes:
        return []
    k = int(params["k"])
    task_floor = float(params["task_floor"])
    top_level_sign = _sign(metric)

    # Gather per-task score vectors + per-task direction metadata. A task may
    # override the top-level metric via `outcome.benchmark.result.tasks_meta`.
    # When direction is "min" the score is negated so higher-is-better holds
    # uniformly inside this function -- the task_floor comparison uses the
    # raw score before negation so users set it in benchmark units.
    task_scores: dict[str, dict[str, float]] = {}  # node_id -> task_id -> normalized score
    task_raw: dict[str, dict[str, float]] = {}  # raw scores for floor comparison
    task_direction: dict[str, str] = {}  # task_id -> resolved direction
    for n in nodes:
        exp_id = n["id"]
        outcome = outcomes.get(exp_id) or {}
        result = (outcome.get("benchmark") or {}).get("result") or {}
        tasks = result.get("tasks") or {}
        tasks_meta = result.get("tasks_meta") or {}
        per_node: dict[str, float] = {}
        per_node_raw: dict[str, float] = {}
        for tid, s in tasks.items():
            try:
                raw = float(s)
            except (TypeError, ValueError):
                continue
            d = (tasks_meta.get(tid) or {}).get("direction")
            if d not in ("max", "min"):
                d = "max" if top_level_sign == 1 else "min"
            sign = 1 if d == "max" else -1
            per_node[tid] = raw * sign
            per_node_raw[tid] = raw
            # First writer wins; conflicts across outcomes are unlikely since
            # the benchmark owns the schema, but skip silently if they disagree.
            task_direction.setdefault(tid, d)
        task_scores[exp_id] = per_node
        task_raw[exp_id] = per_node_raw

    # Only consider tasks every frontier candidate reported (intersection).
    # Without intersection, a node missing task t would silently "win" nothing
    # for t while another ties for best on t -- misleading.
    if not task_scores:
        all_tasks: set[str] = set()
    else:
        task_sets = [set(v.keys()) for v in task_scores.values() if v]
        all_tasks = set.intersection(*task_sets) if task_sets else set()

    # Per-task winners. task_floor applies only to max-direction tasks (the
    # common case of "ignore tasks everyone failed"); min-direction tasks are
    # always considered, since "nobody was fast" is itself meaningful Pareto
    # signal.
    winners: dict[str, set[str]] = {}
    for tid in all_tasks:
        best_norm = max(task_scores[e][tid] for e in task_scores if tid in task_scores[e])
        if task_direction.get(tid) == "max":
            best_raw = max(task_raw[e][tid] for e in task_raw if tid in task_raw[e])
            if best_raw <= task_floor:
                continue
        winners[tid] = {
            exp_id for exp_id in task_scores
            if task_scores[exp_id].get(tid) == best_norm
        }

    if not winners:
        # Nothing to Pareto over (no tasks, or all below floor). Fall back to argmax.
        return _pick_argmax(nodes, {}, metric, outcomes, rng)

    # Survivor set = union of per-task winners.
    survivors: set[str] = set().union(*winners.values())

    # Dominance prune: drop A if some other B >= A on every task where A wins.
    to_drop: set[str] = set()
    for a in list(survivors):
        a_winning_tasks = [t for t, w in winners.items() if a in w]
        for b in survivors:
            if a == b or b in to_drop:
                continue
            # b dominates a iff for every task where a wins, b's score >= a's score.
            dominates = True
            for t in a_winning_tasks:
                bs = task_scores[b].get(t, float("-inf"))
                as_ = task_scores[a].get(t, float("-inf"))
                if bs < as_:
                    dominates = False
                    break
            if dominates and any(
                task_scores[b].get(t, float("-inf")) > task_scores[a].get(t, float("-inf"))
                for t in a_winning_tasks
            ):
                to_drop.add(a)
                break
    survivors -= to_drop

    # Frequency weights.
    freq: dict[str, int] = {s: 0 for s in survivors}
    for w in winners.values():
        for s in w & survivors:
            freq[s] += 1

    survivor_nodes = [n for n in nodes if n["id"] in survivors]
    weights = [freq[n["id"]] for n in survivor_nodes]
    k = min(k, len(survivor_nodes))
    return _weighted_sample_without_replacement(survivor_nodes, weights, k, rng)


PICKERS: dict[str, Callable[[list, dict, str, dict, random.Random], list[dict]]] = {
    "argmax": _pick_argmax,
    "top_k": _pick_top_k,
    "epsilon_greedy": _pick_epsilon_greedy,
    "softmax": _pick_softmax,
    "pareto_per_task": _pick_pareto_per_task,
}


# --------------------------------------------------------------------------- #
# Dispatch                                                                    #
# --------------------------------------------------------------------------- #

def pick(nodes: list[dict[str, Any]], strategy: dict[str, Any], metric: str,
         outcomes: dict[str, dict] | None = None,
         seed: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """Apply `strategy` to `nodes`. Returns (ranked_list, seed_used).

    `seed_used` is returned for logging. If seed is None a fresh one is drawn.
    """
    strategy = validate_frontier_strategy(strategy)
    picker = PICKERS[strategy["kind"]]
    used_seed = seed if seed is not None else random.SystemRandom().randint(0, 2**31 - 1)
    rng = random.Random(used_seed)
    ranked = picker(list(nodes), strategy["params"], metric, outcomes or {}, rng)
    # Ensure rank field reflects output order.
    for i, item in enumerate(ranked, 1):
        item["rank"] = i
    return ranked, used_seed


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _weighted_sample_without_replacement(items: list[dict], weights: list[float],
                                          k: int, rng: random.Random) -> list[dict]:
    """Efraimidis-Spirakis reservoir: assign key = rand^(1/weight), pick top k by key.

    Returns items in sampled order, ranked 1..k.
    """
    if k <= 0 or not items:
        return []
    paired = []
    for item, w in zip(items, weights):
        if w <= 0:
            continue
        u = rng.random()
        if u == 0.0:
            u = 1e-12
        key = math.log(u) / w
        paired.append((key, item))
    # Top k by key (largest key = highest priority in this formulation).
    paired.sort(key=lambda x: x[0], reverse=True)
    picked = [item for _, item in paired[:k]]
    return [_node_summary(n, i + 1) for i, n in enumerate(picked)]


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #

def append_frontier_log(root: Path, strategy: dict[str, Any],
                        returned_ids: list[str], seed: int | None = None) -> dict[str, Any]:
    """Append a frontier-selection event to .evo/infra_log.json."""
    path = infra_path(root)
    seed_str = f" seed={seed}" if seed is not None else ""
    event = {
        "kind": "frontier",
        "timestamp": utc_now(),
        "message": f"frontier({strategy.get('kind', '?')}) -> {len(returned_ids)} id(s){seed_str}",
        "strategy": strategy,
        "returned_ids": returned_ids,
    }
    if seed is not None:
        event["seed"] = seed
    with advisory_lock(lock_file_for(path)):
        data = load_json(path, {"events": []})
        data.setdefault("events", []).append(event)
        atomic_write_json(path, data)
    return event
