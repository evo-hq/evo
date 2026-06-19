---
name: report
description: Read-only evo run reporting. Use when the user invokes /evo:report, asks what happened overnight, asks what improved recently, asks for the best/frontier candidates, asks for a quick score chart without opening the dashboard, or wants the scatter plot in chat output. Never run benchmarks, gates, Slurm commands, evo run, or ad-hoc verification scripts for report requests.
evo_version: 0.6.2
---

# Report

Report the current evo workspace from recorded state only. A report request is
read-only, even if the user phrases it casually as "what happened?", "what got
better?", "what should I pay attention to?", or "I just woke up".

Do not spend compute while reporting:

- Do not run `evo run`, `evo gate check`, benchmark commands, or project eval
  scripts.
- Do not run `python bench.py`, `python slurm_eval.py`, `sbatch`, `srun`,
  `squeue`, `sacct`, or `scancel` to verify a result.
- Do not create launcher, monitor, parsing, or analysis scripts.
- Do not edit files.

Use stored evo state instead: `evo report`, `evo status`, `evo tree`,
`evo frontier`, `evo show <id>`, `evo diff <id>`, and immutable artifacts under
`.evo/run_*/experiments/<exp>/attempts/<NNN>/`.

For chart requests, render the dashboard's scatter plot as a colored terminal
block, one chart per run, sized to the current terminal.

## What it shows

Mirrors the web dashboard's score scatter (left rail of `evo dashboard`):

- X = experiment creation order, Y = score
- Dot color by status: green = committed valid result, red = failed, purple = active, grey = pending / evaluated / discarded / pruned
- ★ marks the current best valid committed-result experiment. `pruned` with `prune_kind=exhausted` can still be best; `prune_kind=invalid` and its descendants cannot.
- Yellow ring on dots that sit on the best-path spine (root → best)
- Yellow stair line traces cumulative-best across valid committed-result experiments
- ○ at the baseline for experiments that have no score yet (active / pending)

Every run in the workspace is rendered, stacked top-to-bottom, with a header line showing `run_id · target · metric`.

## How to invoke

Run:

```bash
evo report
```

That is it. Print the output verbatim in your reply so the user sees the chart. Do not summarize the chart in prose — the visual is the point.

Flags:

- `--color always|never|auto` — force or suppress ANSI color. Default `auto` (color when stdout is a TTY). Pass `--color always` if you are piping through a host that strips TTY but renders ANSI in chat.
- `--watch [SECONDS]` — live-refresh mode (like `nvidia-smi -l`). Re-reads the workspace every N seconds (default 2) and redraws in place. Ctrl-C to exit. Use this when you want to babysit a running optimization without manually re-invoking the report.

## When not to use

- For one-off score lookups, `evo status` or `evo show <id>` is faster.
- For navigating the tree shape, `evo tree` is the right command.
- For interactive exploration (click a dot, open a drawer), point the user at `evo dashboard` instead.

## Overnight / Improvement Reports

When the user asks what happened recently or what improved, summarize from
recorded evo state:

1. Run `evo status`, `evo frontier`, and `evo tree`.
2. Use `evo show <id>` for the best node and any recent committed/evaluated
   nodes you mention.
3. Use `evo diff <id>` only to explain what changed in a recorded experiment.
4. If you need benchmark details, read the existing `outcome.json`,
   `benchmark.log`, or declared artifacts for that experiment. Treat missing
   artifacts as "not recorded", not as permission to rerun.

Report:

- best current experiment and score;
- score delta versus baseline or parent;
- top candidates/frontier if relevant;
- failed/evaluated nodes that need attention;
- any caveats about gates, missing held-out checks, or tied candidates.

If the user wants fresh validation or reruns, ask them to explicitly start a new
optimization or evaluation command. Do not infer that from a report request.
