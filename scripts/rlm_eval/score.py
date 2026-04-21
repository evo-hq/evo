"""Score a model's pattern output against a fixture's ground truth.

Model output expected shape (JSON):
    {"patterns": [{"signature": str, "experiment_ids": [str, ...]}, ...]}

Scoring logic:
  A reported pattern matches a planted pattern when:
    (a) its signature's keywords overlap the planted signature (fuzzy), AND
    (b) Jaccard(reported_ids, planted_ids) >= id_threshold (default 0.6).

Per planted pattern, we take the best-matching reported pattern (if any) and
record whether it was found. Unmatched reported patterns are hallucinations.

We also compute ID-level precision on the matched ones.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

# Planted signature -> keywords that any matching report must overlap
SIGNATURE_KEYWORDS = {
    "A": {"refund_flow", "gate", "timeout"},
    "B": {"t7", "t12"},
    "C": {"refund", "t7", "t12"},  # must hit both A and B terms
    # D: t5 tanks (structural tell) + refund-parse semantic theme (varied wording)
    "D": {"t5", "refund", "parse", "parsing", "normalize", "deserialize", "decode", "interpret", "malformed"},
    # E: wall-pattern on a specific hypothesis that always regresses
    "E": {"swap_parser", "wall", "repeated", "consistently", "regress", "hypothesis"},
    # S: committed improvers (positive pattern)
    "S": {"committed", "improver", "commit", "improvement", "frontier", "worth extending"},
}
# Minimum keyword hits for a match per pattern
MIN_KEYWORD_HITS = {"A": 1, "B": 2, "C": 2, "D": 2, "E": 2, "S": 1}
ID_JACCARD_THRESHOLD = 0.6

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def keyword_hits(text: str, keywords: set[str]) -> int:
    text_low = text.lower()
    return sum(1 for kw in keywords if kw.lower() in text_low)

def score(ground_truth: dict, model_output: dict) -> dict:
    planted = {p["id"]: p for p in ground_truth["patterns"]}
    reported = model_output.get("patterns", [])

    # For each planted pattern, find best matching reported pattern
    used_report_idx: set[int] = set()
    per_pattern = []
    for pid, planted_p in planted.items():
        planted_ids = set(planted_p["experiment_ids"])
        kw = SIGNATURE_KEYWORDS[pid]
        min_hits = MIN_KEYWORD_HITS[pid]

        best = None
        best_j = 0.0
        for idx, rep in enumerate(reported):
            if idx in used_report_idx:
                continue
            if keyword_hits(rep.get("signature", ""), kw) < min_hits:
                continue
            rep_ids = set(rep.get("experiment_ids", []))
            j = jaccard(rep_ids, planted_ids)
            if j >= ID_JACCARD_THRESHOLD and j > best_j:
                best = (idx, rep, j)
                best_j = j

        if best is not None:
            idx, rep, j = best
            used_report_idx.add(idx)
            rep_ids = set(rep.get("experiment_ids", []))
            id_precision = len(rep_ids & planted_ids) / len(rep_ids) if rep_ids else 0.0
            id_recall = len(rep_ids & planted_ids) / len(planted_ids) if planted_ids else 0.0
            per_pattern.append({
                "id": pid,
                "found": True,
                "jaccard": round(j, 3),
                "id_precision": round(id_precision, 3),
                "id_recall": round(id_recall, 3),
            })
        else:
            per_pattern.append({"id": pid, "found": False, "jaccard": 0.0,
                                "id_precision": 0.0, "id_recall": 0.0})

    hallucinated = len(reported) - len(used_report_idx)
    recall = sum(1 for p in per_pattern if p["found"]) / len(per_pattern)
    return {
        "recall": round(recall, 3),
        "patterns_found": sum(1 for p in per_pattern if p["found"]),
        "patterns_total": len(per_pattern),
        "hallucinated": hallucinated,
        "per_pattern": per_pattern,
    }

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ground-truth", type=Path, required=True)
    ap.add_argument("--model-output", type=Path, required=True)
    args = ap.parse_args()

    gt = json.loads(args.ground_truth.read_text())
    out = json.loads(args.model_output.read_text())
    result = score(gt, out)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
