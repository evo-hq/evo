"""Generate a synthetic .evo fixture via case-spec + LLM narrative generation.

Per-case flow:
  1. Case specs declare count, outcome shape, hypothesis pool, narrative theme.
  2. ONE batched LLM call per case returns N narrative objects (one per
     experiment in that case).
  3. Procedural assembly builds outcome.json + 20 task traces per experiment,
     inlining the LLM-written narrative for failing tasks.

Artefacts produced (identical to real evo on disk):
  <out>/.evo/project.md                           -- objective, critical behaviors
  <out>/.evo/run_0001/experiments/<id>/attempts/001/outcome.json
  <out>/.evo/run_0001/experiments/<id>/attempts/001/traces/task_<tid>.json
  <out>/ground_truth.json                          -- planted pattern declarations

Usage:
    python generate_fixture.py --size large --out fixtures/large --seed 1
"""
from __future__ import annotations
import argparse, json, random, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ============================================================================
# Case specs
# ============================================================================

# Each case defines:
#   count            : how many experiments carry this pattern
#   outcome          : "committed" | "evaluated" | "failed"
#   score_delta_range: (min, max) score offset vs parent_score
#   failing_tasks    : list of task_ids forced to 0.0 in benchmark.result.tasks
#   gates            : list of gate specs to attach (name, error_theme if failing)
#   hypothesis_pool  : pool of hypothesis strings (picked per experiment)
#   force_hypothesis : if set, all experiments in this case use this exact string
#   narrative_theme  : prose describing what the failing tasks' traces should say;
#                      passed to the LLM for batched narrative generation
#   becomes_parent   : if True, other experiments may branch from these (sets up tree)
#   narrative_varied : if True, LLM is asked to use DIFFERENT wording per experiment

SIZES = ["small", "medium", "large"]

CASES = {
    # 3 experiments that actually improve the benchmark. They commit and become
    # branching points for other experiments in the tree.
    "S_improver": {
        "count_per_size": {"small": 1, "medium": 2, "large": 3},
        "outcome": "committed",
        "score_delta_range": (0.05, 0.10),  # above parent
        "failing_tasks": [],
        "gates": [{"name": "_init_gate", "from": "config", "passes": True}],
        "hypothesis_pool": [
            "cache refund lookups to avoid duplicate API calls",
            "batch parser calls for adjacent refund tasks",
            "parallelize refund tool invocations",
        ],
        "narrative_theme": (
            "successful approach: the agent's edit removes a bottleneck on refund "
            "processing; benchmark score improves slightly. No gate failures."
        ),
        "becomes_parent": True,
        "narrative_varied": True,
    },

    # 18 experiments share the refund_flow_guard gate failure. Grep-findable.
    "A_gate_timeout": {
        "count_per_size": {"small": 3, "medium": 8, "large": 18},
        "outcome": "evaluated",
        "score_delta_range": (-0.10, -0.05),
        "failing_tasks": ["t7"],  # t7 tanks because the gate timed out
        "gates": [
            {"name": "_init_gate", "from": "config", "passes": True},
            {"name": "refund_flow_guard", "from": "inherited", "passes": False,
             "error": "refund_flow timeout in task t7"},
        ],
        "hypothesis_pool": [
            "retry on tool-use errors instead of aborting",
            "raise max_tokens for the planning step",
            "add few-shot examples for argument parsing",
            "reorder tools so database calls come before web",
            "shorten the system prompt header",
            "add an explicit no-op tool to let the agent wait",
        ],
        "narrative_theme": (
            "agent invokes the refund_flow tool for task t7; the tool call times "
            "out; agent retries once with extended timeout; tool times out again; "
            "agent fails the task. Keep the failure mode consistent across experiments."
        ),
        "narrative_varied": False,  # same narrative shape OK; A is structurally findable
    },

    # 12 experiments: task t7 and t12 both score 0.0. They share a dependency.
    "B_task_coupling": {
        "count_per_size": {"small": 2, "medium": 5, "large": 12},
        "outcome": "evaluated",
        "score_delta_range": (-0.18, -0.10),
        "failing_tasks": ["t7", "t12"],
        "gates": [{"name": "_init_gate", "from": "config", "passes": True}],
        "hypothesis_pool": [
            "cache intermediate SQL plans between subtasks",
            "swap the planner model from haiku to sonnet",
            "chunk long inputs before the reasoning pass",
            "validate refund amounts before submitting",
        ],
        "narrative_theme": (
            "agent handles tasks t7 and t12 which share a refund-calculation "
            "dependency. The dependency produces a bad intermediate result, and "
            "both tasks fail downstream of it. Show the shared dependency failure."
        ),
        "narrative_varied": False,
    },

    # 5 experiments: intersection of A and B. Both refund_flow gate fails AND
    # t7/t12 tank. These are the worst regressors.
    "C_intersection": {
        "count_per_size": {"small": 1, "medium": 2, "large": 5},
        "outcome": "evaluated",
        "score_delta_range": (-0.22, -0.15),
        "failing_tasks": ["t7", "t12"],
        "gates": [
            {"name": "_init_gate", "from": "config", "passes": True},
            {"name": "refund_flow_guard", "from": "inherited", "passes": False,
             "error": "refund_flow timeout in task t7"},
        ],
        "hypothesis_pool": [
            "retry on tool-use errors instead of aborting",
            "validate refund amounts before submitting",
            "swap the planner model from haiku to sonnet",
        ],
        "narrative_theme": (
            "compound failure: agent hits refund_flow timeout on task t7 AND the "
            "shared refund-calculation dependency breaks t12 too. Both the gate "
            "and the downstream task go red."
        ),
        "narrative_varied": False,
    },

    # 8 experiments: task t5 tanks with a refund-parsing failure described in
    # varied wording. This is the semantic-clustering test -- no single
    # substring grep catches all 8 narratives.
    "D_semantic_root": {
        "count_per_size": {"small": 2, "medium": 6, "large": 8},
        "outcome": "evaluated",
        "score_delta_range": (-0.15, -0.08),
        "failing_tasks": ["t5"],
        "gates": [{"name": "_init_gate", "from": "config", "passes": True}],
        "hypothesis_pool": [
            "add locale-aware number parsing",
            "strip currency symbols before decimal conversion",
            "route ambiguous refund strings through a fallback parser",
            "use a regex-based parser for refund amounts",
            "convert refund strings to Decimal instead of float",
            "add explicit format validation for refund inputs",
        ],
        "narrative_theme": (
            "agent tries to parse a refund amount in task t5 and the parse FAILS. "
            "The failure mode is always 'refund amount could not be parsed' BUT "
            "YOU MUST USE DIFFERENT WORDING FOR EACH EXPERIMENT. Vary the "
            "vocabulary: one says 'parser rejected', another 'unable to normalize', "
            "another 'locale format mismatch', another 'decode step raised', "
            "another 'off-spec stringified number', etc. No single substring "
            "should catch all of them."
        ),
        "narrative_varied": True,  # THIS is the critical case for variation
    },

    # 6 experiments: all use a specific hypothesis string that consistently
    # regresses. Wall pattern for brief-writing's "don't try this again".
    "E_wall_hypothesis": {
        "count_per_size": {"small": 1, "medium": 3, "large": 6},
        "outcome": "evaluated",
        "score_delta_range": (-0.10, -0.06),
        "failing_tasks": ["t3", "t9"],  # disjoint from t5, t7, t12
        "gates": [{"name": "_init_gate", "from": "config", "passes": True}],
        "force_hypothesis": "swap_parser_v2",
        "narrative_theme": (
            "agent attempts the 'swap_parser_v2' approach: swaps the refund "
            "parser implementation. The new parser produces wrong output shape, "
            "and multiple downstream tasks (t3, t9) fail as a result. The "
            "approach consistently backfires."
        ),
        "narrative_varied": False,
    },

    # ~18 experiments of pure noise: slightly below baseline, no distinctive
    # pattern, varied hypotheses from a broad pool.
    "noise": {
        "count_per_size": {"small": 1, "medium": 5, "large": 18},
        "outcome": "evaluated",
        "score_delta_range": (-0.06, 0.01),  # small regressions, some near baseline
        "failing_tasks": [],  # no forced failure
        "gates": [{"name": "_init_gate", "from": "config", "passes": True}],
        "hypothesis_pool": [
            "add retries with jitter",
            "restructure the tool-use prompt into sections",
            "use a larger context for the planner",
            "cache tool outputs across similar queries",
            "shrink the system prompt",
            "pre-compute static lookups",
            "reorder steps in the refund pipeline",
            "add rate limiting to outbound calls",
            "switch to streaming for long responses",
            "batch tool calls where possible",
            "add structured output for planner step",
            "increase retry budget",
        ],
        "narrative_theme": (
            "agent attempts the hypothesis; the change has no strong effect on "
            "the benchmark; score stays near baseline with mild noise. No "
            "distinctive failure mode; no specific task tanks."
        ),
        "narrative_varied": True,
    },
}


# ============================================================================
# LLM narrative generation
# ============================================================================

def _extract_json(text: str, case_id: str) -> dict:
    """Robust JSON extraction from LLM output. Handles markdown fences,
    unescaped control characters, and trailing prose."""
    import re
    s = text.strip()
    # Strip markdown fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # Find first '{' and last '}' and slice
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < 0 or end <= start:
        debug_path = Path(f"/tmp/rlm_narr_{case_id}_raw.txt")
        debug_path.write_text(text)
        raise RuntimeError(f"no JSON object in {case_id} (raw saved to {debug_path})")
    s = s[start:end + 1]
    # Try strict first, then loose (strict=False permits control chars in strings)
    for strict in (True, False):
        try:
            return json.loads(s, strict=strict)
        except json.JSONDecodeError as e:
            last_err = e
    debug_path = Path(f"/tmp/rlm_narr_{case_id}_raw.txt")
    debug_path.write_text(text)
    raise RuntimeError(f"JSON parse failed for {case_id}: {last_err} (raw saved to {debug_path})")


def generate_narratives(case_id: str, case: dict, count: int, seed: int) -> list[dict]:
    """One batched LLM call per case. Returns N narrative objects of the form:
        {"hypothesis": str, "failing_task_traces": {tid: {"messages": [...], "tool_calls": [...]}}}

    Non-failing tasks are filled procedurally by the caller -- the LLM only
    produces content for the failing tasks (or a generic activity snippet when
    the case has no failing tasks, used to flavor a random task for noise/S).
    """
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not on PATH")

    failing = case["failing_tasks"] or ["<pick-one-task>"]
    hyp_constraint = (
        f'Use the exact hypothesis string "{case["force_hypothesis"]}" for every experiment.'
        if "force_hypothesis" in case
        else f'Pick a hypothesis per experiment from this pool (varying across experiments): {case["hypothesis_pool"]}'
    )
    varied_hint = (
        "CRITICAL: each experiment MUST use DIFFERENT wording for the failure mode. "
        "Vary vocabulary, tool-call details, and the exact error phrasing across experiments."
        if case.get("narrative_varied")
        else "Use a consistent narrative shape; minor detail variation is fine but the core failure wording can repeat."
    )

    # Retry loop: LLM occasionally drops a brace. Up to 3 tries.
    for attempt in range(3):
        try:
            return _call_narratives(claude, case_id, case, count, failing, hyp_constraint, varied_hint)
        except (RuntimeError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise
            print(f"  [retry] {case_id} attempt {attempt+1} failed ({e}); retrying...", file=sys.stderr)


def _call_narratives(claude: str, case_id: str, case: dict, count: int,
                     failing: list[str], hyp_constraint: str, varied_hint: str) -> list[dict]:
    prompt = f"""You are generating synthetic agent-run traces for an evo benchmark fixture.

Case: {case_id}
Experiments to generate: {count}
Theme: {case['narrative_theme']}
Failing tasks per experiment: {failing}
Hypotheses: {hyp_constraint}
{varied_hint}

For each experiment, produce ONE narrative object:
- "hypothesis": a string matching the constraint above
- "failing_task_traces": object mapping each failing task_id to:
    {{
      "messages": [ {{"role": "assistant"|"tool", "content": "<short>"}} , ... ],
      "tool_calls": [ {{"name": "<tool>", "args": {{...}}, "ok": false, "error": "<short>"}} , ... ]
    }}

Rules:
- 3-6 messages per failing task
- 2-4 tool_calls per failing task, most with ok=false
- Each message content under 250 chars
- Each tool error under 200 chars
- Realistic agent-transcript style, not generic filler

Return ONLY a JSON object, no prose, no markdown fences. Double-check the braces and brackets match before outputting -- mismatched braces will break parsing.

{{"narratives": [<narrative_object_1>, <narrative_object_2>, ..., <narrative_object_{count}>]}}
"""

    proc = subprocess.run(
        [claude, "-p", "--output-format", "json", prompt],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"LLM narrative call failed for {case_id} (rc={proc.returncode}): {proc.stderr[:400]}")

    envelope = json.loads(proc.stdout)
    raw = envelope.get("result", "") or ""
    parsed = _extract_json(raw, case_id)
    narratives = parsed.get("narratives", [])
    if len(narratives) < count:
        # Pad with duplicates if the LLM returned fewer than asked; still valid.
        print(f"[warn] {case_id}: LLM returned {len(narratives)}, padding to {count}", file=sys.stderr)
        while len(narratives) < count:
            narratives.append(narratives[-1] if narratives else {"hypothesis": "fallback", "failing_task_traces": {}})
    return narratives[:count]


# ============================================================================
# Procedural filler (non-failing tasks)
# ============================================================================

TOOL_NAMES = ["read_file", "grep", "edit_file", "bash", "python", "http_get", "sql_query", "vector_search"]
NORMAL_ACTIVITY_SNIPPETS = [
    "Inspecting the module before editing.",
    "Calling the catalogue API for the needed record.",
    "Batching adjacent lookups to reduce round-trips.",
    "Validating the returned payload against the schema.",
    "Routing to the standard handler for this task.",
    "Caching the intermediate result for reuse.",
    "Output format confirmed; emitting final answer.",
    "Running the benchmark harness to capture the score.",
]


def make_normal_trace(rng: random.Random, task_id: str, task_score: float) -> dict:
    """Generate a procedural 'normal activity' trace for a non-failing task."""
    n_messages = rng.randint(3, 6)
    messages = []
    tool_calls = []
    for _ in range(n_messages):
        role = rng.choice(["assistant", "tool"])
        if role == "tool":
            tool = rng.choice(TOOL_NAMES)
            messages.append({"role": "tool", "tool": tool, "content": rng.choice(NORMAL_ACTIVITY_SNIPPETS)})
            tool_calls.append({
                "name": tool,
                "args": {"input": rng.choice(NORMAL_ACTIVITY_SNIPPETS)[:60]},
                "ok": rng.random() > 0.15,
            })
        else:
            messages.append({"role": "assistant", "content": rng.choice(NORMAL_ACTIVITY_SNIPPETS)})
    return {
        "task_id": task_id,
        "score": task_score,
        "messages": messages,
        "tool_calls": tool_calls,
    }


# ============================================================================
# Experiment assembly
# ============================================================================

def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


TASK_IDS = [f"t{i}" for i in range(1, 21)]


def build_experiment(
    rng: random.Random,
    exp_id: str,
    case_id: str,
    case: dict,
    narrative: dict,
    parent_id: str,
    parent_score: float,
    committed_sha: str | None = None,
) -> tuple[dict, dict[str, dict]]:
    """Assemble outcome.json + per-task traces using the narrative for failing tasks.

    Returns (outcome_dict, traces_dict_by_task).
    """
    # 1. Per-task scores
    lo, hi = case["score_delta_range"]
    target_delta = rng.uniform(lo, hi)
    target_score = max(0.0, min(1.0, parent_score + target_delta))

    # Start with per-task scores dithered around target
    tasks = {}
    for t in TASK_IDS:
        tasks[t] = round(rng.uniform(max(0.0, target_score - 0.10), min(1.0, target_score + 0.08)), 3)

    # Force failing tasks to 0.0
    for ft in case["failing_tasks"]:
        tasks[ft] = 0.0

    # Recompute overall score as the mean (realistic)
    overall_score = round(sum(tasks.values()) / len(tasks), 3)

    # 2. Gates
    gates = []
    for gspec in case["gates"]:
        g = {
            "name": gspec["name"],
            "from": gspec["from"],
            "command": ["bash", "-lc", f"check_{gspec['name']}.sh"],
            "passed": gspec["passes"],
            "error": gspec.get("error"),
        }
        gates.append(g)

    # 3. Hypothesis
    hypothesis = narrative.get("hypothesis") or (
        case.get("force_hypothesis") or rng.choice(case.get("hypothesis_pool", ["unknown"]))
    )

    # 4. Timestamps
    started = datetime.now(timezone.utc) - timedelta(minutes=rng.randint(5, 600))
    finished = started + timedelta(seconds=rng.randint(30, 900))

    outcome = {
        "experiment_id": exp_id,
        "attempt": 1,
        "outcome": case["outcome"],
        "hypothesis": hypothesis,
        "parent_id": parent_id,
        "parent_score": round(parent_score, 3),
        "metric": "max",
        "score": overall_score,
        "started_at": iso(started),
        "finished_at": iso(finished),
        "benchmark": {
            "command": ["bash", "-lc", "python bench.py"],
            "returncode": 0,
            "result": {"score": overall_score, "tasks": tasks},
        },
        "gates": gates,
        "error": None,
        "commit": committed_sha,
    }

    # 5. Traces
    traces = {}
    failing_task_traces = narrative.get("failing_task_traces", {}) or {}
    for tid in TASK_IDS:
        if tid in case["failing_tasks"] and tid in failing_task_traces:
            ft = failing_task_traces[tid]
            traces[tid] = {
                "task_id": tid,
                "score": tasks[tid],
                "messages": ft.get("messages", []),
                "tool_calls": ft.get("tool_calls", []),
            }
        else:
            traces[tid] = make_normal_trace(rng, tid, tasks[tid])

    return outcome, traces


# ============================================================================
# Fixture generation
# ============================================================================

PROJECT_MD_TEMPLATE = """# Project: refund-processing agent

## Objective
Maximize the benchmark score for a customer-service agent that handles
refund requests. The benchmark runs 20 tasks covering different refund
scenarios; score is the mean of per-task scores. Baseline (root) score is 0.62.

## Critical behaviors
- Correctly parse refund amounts in multiple input formats. Task `t5` is the
  hardest parse case; agents frequently regress here.
- Trigger `refund_flow_guard` only when a real refund-flow error occurs;
  spurious firing or missing coverage both matter.
- Pass `task_t7` and `task_t12` -- the two hardest refund-calculation tasks
  (they share a refund-calculation dependency that breaks together).

## Current frontier (committed improvers)
{improver_list}

## Known anti-patterns (from prior rounds)
- `swap_parser_v2` has been attempted {e_count}+ times; consistently regresses
  the benchmark. Avoid.

## What's being optimized
Per-attempt edits to the agent's tool-use logic, prompt structure, or
parsing rules. Each attempt commits only if the benchmark score improves
over the parent and no gates regress. Attempts that ran but didn't commit
are marked `evaluated` and live in the frontier for subsequent rounds.
"""


def write_project_md(evo_dir: Path, improvers: list[dict], e_count: int) -> None:
    if improvers:
        lines = [f"- `{imp['experiment_id']}` (score {imp['score']}): {imp['hypothesis'][:80]}" for imp in improvers]
        improver_list = "\n".join(lines)
    else:
        improver_list = "- (none yet -- all attempts have regressed)"
    (evo_dir / "project.md").write_text(PROJECT_MD_TEMPLATE.format(
        improver_list=improver_list, e_count=e_count,
    ))


def size_count(case: dict, size: str) -> int:
    return case["count_per_size"].get(size, 0)


def generate(size: str, out: Path, seed: int, with_traces: bool = True) -> None:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    evo_dir = out / ".evo"
    root = evo_dir / "run_0001" / "experiments"
    root.mkdir(parents=True, exist_ok=True)

    # 1. Assign experiment IDs per case. Generate exp_0001.. sequentially.
    total = sum(size_count(c, size) for c in CASES.values())
    exp_ids = [f"exp_{i:04d}" for i in range(1, total + 1)]
    rng.shuffle(exp_ids)

    assignments: dict[str, list[str]] = {}  # case_id -> [exp_id]
    cursor = 0
    for case_id, case in CASES.items():
        n = size_count(case, size)
        assignments[case_id] = exp_ids[cursor:cursor + n]
        cursor += n

    # 1a. Handle the A/B/C intersection: C's exp_ids should also be treated as
    # sharing A's gate and B's task failures. We achieve this by making C a
    # distinct case (already done via its own gates + failing_tasks), but we
    # need the ground_truth to reflect that C ⊂ A and C ⊂ B for scoring.
    # Pattern sets for ground_truth:
    a_ids = set(assignments["A_gate_timeout"]) | set(assignments["C_intersection"])
    b_ids = set(assignments["B_task_coupling"]) | set(assignments["C_intersection"])
    c_ids = set(assignments["C_intersection"])
    d_ids = set(assignments["D_semantic_root"])
    e_ids = set(assignments["E_wall_hypothesis"])
    s_ids = set(assignments["S_improver"])

    # 2. Generate narratives per case via LLM (skipped when with_traces=False;
    #    the downstream build_experiment falls back to a generic hypothesis).
    narratives: dict[str, list[dict]] = {}
    if with_traces:
        for case_id, case in CASES.items():
            n = len(assignments[case_id])
            if n == 0:
                narratives[case_id] = []
                continue
            print(f"  [llm] {case_id}: generating {n} narratives...", file=sys.stderr)
            narratives[case_id] = generate_narratives(case_id, case, n, seed)
    else:
        for case_id in CASES:
            narratives[case_id] = []

    # 3. Build S (improvers) FIRST so their sha is available as parent for others.
    improver_meta: list[dict] = []
    for i, exp_id in enumerate(assignments["S_improver"]):
        case = CASES["S_improver"]
        narr = narratives["S_improver"][i] if narratives["S_improver"] else {"hypothesis": "fallback", "failing_task_traces": {}}
        sha = f"{rng.randrange(16**10):010x}"
        outcome, traces = build_experiment(
            rng, exp_id, "S_improver", case, narr,
            parent_id="root", parent_score=0.62, committed_sha=sha,
        )
        write_experiment(root, exp_id, outcome, traces)
        improver_meta.append({"experiment_id": exp_id, "score": outcome["score"], "hypothesis": outcome["hypothesis"]})

    # 4. Build the rest, letting ~40% branch from an S experiment (tree depth).
    rest_exp_ids = [e for case_id in CASES if case_id != "S_improver" for e in assignments[case_id]]
    for case_id, case_exps in assignments.items():
        if case_id == "S_improver":
            continue
        case = CASES[case_id]
        for i, exp_id in enumerate(case_exps):
            narr = narratives[case_id][i] if narratives[case_id] else {"hypothesis": "fallback", "failing_task_traces": {}}
            if improver_meta and rng.random() < 0.4:
                imp = rng.choice(improver_meta)
                parent_id = imp["experiment_id"]
                parent_score = imp["score"]
            else:
                parent_id = "root"
                parent_score = 0.62
            outcome, traces = build_experiment(
                rng, exp_id, case_id, case, narr,
                parent_id=parent_id, parent_score=parent_score,
            )
            write_experiment(root, exp_id, outcome, traces)

    # 5. project.md + ground_truth.json
    write_project_md(evo_dir, improver_meta, e_count=len(e_ids))

    ground_truth = {
        "size": size,
        "seed": seed,
        "total_experiments": total,
        "patterns": [
            {"id": "A", "signature": "refund_flow_guard gate fails with 'refund_flow timeout in task t7'",
             "experiment_ids": sorted(a_ids)},
            {"id": "B", "signature": "tasks t7 and t12 both score 0.0",
             "experiment_ids": sorted(b_ids)},
            {"id": "C", "signature": "intersection of A and B (refund_flow gate AND t7/t12 both zero)",
             "experiment_ids": sorted(c_ids)},
            {"id": "D", "signature": "task t5 tanks due to refund-amount parsing failures described in varied wording across traces",
             "experiment_ids": sorted(d_ids)},
            {"id": "E", "signature": "hypothesis 'swap_parser_v2' repeatedly attempted and consistently regresses",
             "experiment_ids": sorted(e_ids)},
            {"id": "S", "signature": "committed improvers worth extending",
             "experiment_ids": sorted(s_ids)},
        ],
    }
    (out / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))

    # 6. Summary
    print(f"wrote {total} experiments to {root}")
    for p in ground_truth["patterns"]:
        print(f"  pattern {p['id']} ({len(p['experiment_ids'])}): {p['experiment_ids'][:8]}{'...' if len(p['experiment_ids']) > 8 else ''}")
    print(f"ground truth at {out / 'ground_truth.json'}")


def write_experiment(root: Path, exp_id: str, outcome: dict, traces: dict[str, dict]) -> None:
    a_dir = root / exp_id / "attempts" / "001"
    a_dir.mkdir(parents=True, exist_ok=True)
    (a_dir / "outcome.json").write_text(json.dumps(outcome, indent=2))
    traces_dir = a_dir / "traces"
    traces_dir.mkdir(exist_ok=True)
    for tid, tr in traces.items():
        (traces_dir / f"task_{tid}.json").write_text(json.dumps(tr))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", choices=["small", "medium", "large"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    generate(args.size, args.out, args.seed)


if __name__ == "__main__":
    main()
