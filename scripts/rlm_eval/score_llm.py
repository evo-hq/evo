"""LLM-based scorer for trial outputs.

The strict scorer (score.py) credits only planted patterns, so it marks
legitimate analytical observations as "hallucinations." This scorer uses
`claude -p` as a judge with a rubric + examples to rate each reported pattern
holistically, and also records recall against the planted patterns.

Output JSON (written to <trial_dir>/score_llm.json):
  {
    "planted_recall": {"A": bool, "B": bool, "C": bool, "D": bool, "E": bool, "S": bool},
    "per_pattern_scores": [{"idx": int, "score": 0|1|2, "reason": str}, ...],
    "aggregate_score": int,
    "max_possible": int,
    "quality_ratio": float
  }

Scoring per reported pattern:
  2 = correct planted pattern OR legitimate grounded observation the
      orchestrator would actually use (gate coverage, per-experiment standout,
      round-level framing, sub-pattern within a planted one)
  1 = directionally correct but low value (trivially true, vague, or wrong IDs)
  0 = invented / hallucinated / wrong about fixture data
"""
from __future__ import annotations
import argparse, json, re, shutil, subprocess, textwrap
from pathlib import Path

RUBRIC = textwrap.dedent("""
    You are scoring the output of an evo orchestrator's cross-cutting scan. The
    orchestrator was asked to look at ~60 synthetic "evaluated experiments"
    (each with outcome.json and trace files) and report cross-cutting failure
    patterns.

    You have:
      1. GROUND TRUTH: the six patterns we deliberately planted in the fixture
         (A, B, C, D, E, S), each with the exact experiment IDs that carry it.
      2. MODEL OUTPUT: the list of patterns the model actually reported.

    The fixture plants 6 pattern types:
      A: shared gate failure (e.g., refund_flow_guard timeout)
      B: shared zero-score task coupling (e.g., t7 and t12 co-zero)
      C: intersection of A and B (compound failure)
      D: semantic root cause in trace content (e.g., varied wording around a
         single failure like refund-amount parsing)
      E: wall-pattern hypothesis (same hypothesis repeatedly regresses, e.g.
         `swap_parser_v2`)
      S: committed improvers (outcome=committed, candidate parent nodes for
         next-round branching)

    Your job is two-fold:
      (a) For each planted pattern (A/B/C/D/E/S), decide if the model found
          it (true/false). A reported pattern counts as "found" if its
          signature describes the same phenomenon and its experiment_ids
          roughly match the planted set.
      (b) For each reported pattern, give a score 0/1/2.

    ---

    Scoring rubric:

    **2 = real value.** The reported pattern is either:
      - A correct match to a planted pattern (signature describes the same
        phenomenon; experiment_ids are largely correct -- small IDs-miss okay),
      - OR a legitimate analytical observation the orchestrator would use when
        writing briefs: gate-coverage analysis (e.g., "gate fires but task
        isn't zero" = over-firing), per-experiment standouts (compound
        failures), round-level framing (e.g., "most experiments regressed"),
        sub-patterns within a planted one (e.g., "refund-validation hypotheses
        among those failing refund gate").

    **1 = directionally OK but low value.** Correctly describes fixture data
    but either:
      - trivially true (e.g., "all experiments have a hypothesis string"),
      - significantly wrong on experiment_ids (e.g., reports 3 IDs for a
        pattern that actually has 18),
      - vague enough that the orchestrator can't act on it.

    **0 = hallucinated.** The reported pattern:
      - isn't grounded in the fixture (invented correlation),
      - names experiment_ids that don't actually exhibit what's claimed,
      - or contradicts the planted data.

    ---

    Examples (use these to calibrate your scoring):

    GOOD (score 2):
      {"signature": "refund_flow_guard gate fails with identical error across 18 experiments",
       "experiment_ids": [<the 18 IDs planted for A>]}
      -> 2. Matches planted Pattern A, IDs correct.

      {"signature": "13 experiments fail refund_flow_guard gate despite t7 non-zero -- gate over-firing",
       "experiment_ids": [<A minus C, the 13 IDs>]}
      -> 2. Not a planted pattern, but legitimate gate-coverage analysis.
         Orchestrator should use this when writing the next round's briefs.

      {"signature": "exp_0030 shows compound failure: both smoke-test fails AND t7/t12 co-zero",
       "experiment_ids": ["exp_0030"]}
      -> 2. Per-experiment standout. One ID is fine when flagged as a
         standout rather than a cross-cutting pattern.

      {"signature": "59/60 experiments regressed below baseline of 0.62",
       "experiment_ids": [<59 IDs>]}
      -> 2. Round-level framing. Useful context for brief strategy.

    LOW VALUE (score 1):
      {"signature": "All experiments reuse hypothesis strings from a small pool",
       "experiment_ids": [<all 60>]}
      -> 1. True, but not specific enough to act on.

      {"signature": "refund_flow_guard gate fails",
       "experiment_ids": ["exp_0001", "exp_0002", "exp_0003"]}
      -> 1. Right pattern, but only names 3 IDs when 18 actually have it.
         Too partial to be fully useful.

    HALLUCINATED (score 0):
      {"signature": "Cache eviction causes timeouts across tasks",
       "experiment_ids": [<random IDs>]}
      -> 0. No cache-eviction signal in the fixture.

      {"signature": "token_budget overrun correlates with refund tasks",
       "experiment_ids": [<invented>]}
      -> 0. No such correlation; the correlation is fabricated.

    ---

    Output JSON ONLY, no prose, this exact shape:

    {
      "planted_recall": {"A": true|false, "B": true|false, "C": true|false, "D": true|false, "E": true|false, "S": true|false},
      "per_pattern_scores": [
        {"idx": 1, "score": 2, "reason": "<short>"},
        {"idx": 2, "score": 1, "reason": "<short>"},
        ...
      ]
    }
    """).strip()

def build_prompt(ground_truth: dict, model_output: dict) -> str:
    return (
        RUBRIC
        + "\n\n---\n\nGROUND TRUTH:\n"
        + json.dumps(ground_truth, indent=2)
        + "\n\n---\n\nMODEL OUTPUT:\n"
        + json.dumps(model_output, indent=2)
    )

def score(ground_truth: dict, model_output: dict) -> dict:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not on PATH")
    prompt = build_prompt(ground_truth, model_output)
    proc = subprocess.run(
        [claude, "-p", "--output-format", "json", prompt],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"judge call failed (rc={proc.returncode}): {proc.stderr[:500]}")
    envelope = json.loads(proc.stdout)
    raw = envelope.get("result", "") or ""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON in judge result: {raw[:500]}")
    parsed = json.loads(m.group(0))

    # Compute aggregates. Each missed planted pattern counts as an implicit 0/2
    # so that leaving signal on the table hurts the ratio.
    per = parsed.get("per_pattern_scores", [])
    recall = parsed.get("planted_recall", {})
    missed_planted = [k for k, v in recall.items() if not v]
    reported = len(per)
    earned = sum(int(p.get("score", 0)) for p in per)
    denom = 2 * (reported + len(missed_planted))

    parsed["aggregate_score"] = earned
    parsed["missed_planted"] = missed_planted
    parsed["max_possible"] = denom
    parsed["quality_ratio"] = round(earned / denom, 3) if denom else 0.0
    parsed["_judge_cost_usd"] = envelope.get("total_cost_usd")
    parsed["_judge_duration_ms"] = envelope.get("duration_ms")
    return parsed

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ground-truth", type=Path, required=True)
    ap.add_argument("--model-output", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="Write score JSON here")
    args = ap.parse_args()
    gt = json.loads(args.ground_truth.read_text())
    out = json.loads(args.model_output.read_text())
    result = score(gt, out)
    s = json.dumps(result, indent=2)
    print(s)
    if args.out:
        args.out.write_text(s)

if __name__ == "__main__":
    main()
