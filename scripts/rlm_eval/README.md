# rlm_eval

Harness for measuring whether evo's `optimize` skill actually helps an
orchestrator find cross-cutting patterns in an experiment tree. Named after
the Recursive Language Model idea (Zhang/Kraska/Khattab, arXiv:2512.24601)
that seeded it -- the skill's hard rule is to fan out across experiments via
parallel sub-agents rather than read every trace inline.

It runs paired trials under `claude -p`:
- **V** (vanilla): the pre-RLM `SKILL.md`, snapshotted as `baseline_SKILL.md`.
- **R** (current): whatever is live at `plugins/evo/skills/optimize/SKILL.md`.

Each trial gets a synthetic `.evo/` tree with six planted patterns
(A/B/C/D/E/S) and is scored both strictly (planted-only) and by an LLM judge
that credits legitimate grounded observations beyond the planted set.

## Files

- `rlm_eval.py` -- CLI: `check`, `setup`, `trial`, `matrix`, `score`, `clean`.
- `generate_fixture.py` -- case-spec + one batched LLM call per case; writes
  `.evo/run_0001/experiments/<id>/...` and `ground_truth.json`.
- `score.py` -- strict scorer. Credits only planted patterns.
- `score_llm.py` -- `claude -p` judge with 0/1/2 rubric + `planted_recall`.
- `baseline_SKILL.md` -- frozen V skill. Do not edit; replace only when
  intentionally re-baselining.
- `fixtures/` -- generated, gitignored.

Trial outputs default to `trials/` at repo root, also gitignored.

## Dependencies

- `claude` CLI on PATH.
- Python 3.10+.
- macOS or Linux for sandboxed Bash isolation (Seatbelt / bubblewrap). On
  native Windows the harness falls back to permission deny rules only, which
  blocks Claude's `Read`/`Edit`/`Grep` but not arbitrary Bash subprocesses --
  use WSL2 for full isolation.

Run `python scripts/rlm_eval/rlm_eval.py check` to verify.

## Typical run

```bash
cd scripts/rlm_eval

# One-time: generate small/medium/large fixtures.
python rlm_eval.py setup --seed 1

# Single trial against R (default), large fixture.
python rlm_eval.py trial \
  --fixture fixtures/large \
  --out ../../trials/smoke

# V vs R across 5 seeds on the large fixture. Writes matrix.csv.
python rlm_eval.py matrix \
  --size large \
  --seeds 5 \
  --out ../../trials/matrix_vr
```

`matrix.csv` columns include `variant`, `strict_recall`, `llm_quality`,
`planted_found`, `missed_planted`, `agent_tool_calls`, `bash_claude_spawns`,
`num_turns`, `wall_seconds`, `cost_usd`.

## Planted patterns

The fixture plants six patterns so the scorer knows what ground truth looks
like. See `generate_fixture.py:CASES` for the exact counts and IDs.

- **A** -- shared gate failure (e.g. `refund_flow_guard` timeout across ~18 exps).
- **B** -- shared zero-score task coupling (`t7` and `t12` co-zero).
- **C** -- intersection of A and B (compound failure).
- **D** -- semantic root cause buried in trace prose (varied wording for one
  underlying cause like refund-amount parsing); D forces `t5=0.0` so the
  structural pass surfaces D experiments as a candidate cluster.
- **E** -- wall-of-regression hypothesis (same `swap_parser_v2` hypothesis
  repeatedly regresses).
- **S** -- committed improvers (candidate parent nodes for next round).

An orchestrator that only skims `outcome.json` gets A, B, E, S. Getting C, D
requires actually reading traces -- which is exactly what R's fan-out rule
forces.

## Results (5 seeds, large fixture)

| seed | V recall | R recall | V missed  | R missed | V cost | R cost |
|-----:|---------:|---------:|:----------|:---------|-------:|-------:|
| 1    | 0.67     | 0.83     | C         | -        | $0.92  | $2.65  |
| 2    | 0.50     | 1.00     | C         | -        | $0.94  | $2.62  |
| 3    | 0.67     | 1.00     | C         | -        | $0.72  | $2.88  |
| 4    | 0.50     | 1.00     | C, D      | -        | $0.73  | $2.69  |
| 5    | 0.33     | 1.00     | C         | -        | $1.13  | $3.02  |
| mean | 0.53     | 0.97     |           |          | $0.89  | $2.77  |

V misses the compound pattern C every time. R catches it on all 5 seeds,
at ~3x the cost.

## Isolation

Each trial stages only the fixture's `.evo/` subtree plus the skill under
test (renamed to `skill.md`) into a scratch dir, then runs `claude -p` there
with:
- permission `deny` covering `ground_truth.json`, `generate_fixture.py`,
  `score*.py`, and every other prior trial under `trials/`;
- `Edit`/`Write` denied entirely (trials are read-only);
- on macOS/Linux, a `sandbox` block with `denyRead` on the same paths so
  Bash subprocesses can't `cat` the ground truth either.

`_scan_leaks` walks the stream transcript after the fact and flags any tool
call that touched a denied path. `matrix.csv` surfaces a `leaks` count per
trial.

## Scoring

Two scorers run on every trial, writing `score.json` and `score_llm.json`
alongside the stream JSONL.

**Strict** (`score.py`) -- keyword match per planted pattern's signature,
plus an experiment-ID overlap check. Cheap, deterministic, but
under-credits: a perfectly valid "gate over-firing" observation the
orchestrator would actually use looks like a hallucination to it.

**LLM judge** (`score_llm.py`) -- `claude -p` with the rubric in
`score_llm.py:RUBRIC`. Scores each reported pattern 0/1/2 and records
`planted_recall: {A..S: bool}`. Missed planted patterns count as implicit 0/2
so leaving signal on the table hurts `quality_ratio`.

`quality_ratio = earned / (2 * (reported + missed_planted_count))`.

## Re-baselining

When the live `plugins/evo/skills/optimize/SKILL.md` changes in a way we
want to carry forward as the new comparison floor, replace
`baseline_SKILL.md` with the new snapshot:

```bash
cp plugins/evo/skills/optimize/SKILL.md scripts/rlm_eval/baseline_SKILL.md
```

Commit that as its own change with a note about what shifted. Until then,
`baseline_SKILL.md` is the frozen V and should not drift with live edits.

## Cost note

`cost_usd` in `matrix.csv` comes from the `total_cost_usd` field of
`claude -p --output-format json`. When the harness runs under a Claude Code
subscription (OAuth), that number is a nominal reference, not a real charge.
It's still useful for relative comparison across V and R.
