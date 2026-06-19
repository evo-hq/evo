---
name: ship
description: Land the winning experiment from an evo run as a clean, mergeable change -- open a PR when the repo has a remote, otherwise merge into the working branch. Distills the best-scoring experiment down to the minimal diff that reproduces its behaviour, shaped for the qualities a maintainer merges on (scope discipline, test integrity, style adherence), then attaches an advisory mergeability report. Use when the user invokes /evo:ship, asks to land/merge/ship the best result, or wants to turn a finished optimization into a pull request.
evo_version: 0.6.2
---

# Ship

Turn a finished evo run into a change a maintainer would merge.

The optimize loop leaves a tree of committed experiments. The winning worktree
diff is not mergeable as-is: it carries debug prints, search-process churn,
over-broad edits, and sometimes a test that was relaxed to clear a gate. Shipping
is the step that re-derives the *minimal clean change* reproducing the winning
behaviour, lands it the way the repo expects (PR or merge), and reports how
mergeable it is.

Correctness is the floor, not the goal. The score says the behaviour works; this
skill decides whether the *diff* is fit to merge.

## Invocation

```bash
/evo:ship            # ship the auto-selected winner
/evo:ship exp_0042   # ship a specific experiment instead
```

## Stage 1 -- Select the winner

Pick the experiment to ship, then confirm it with the user before touching their
tree.

```bash
evo status    # current best valid score + counts
evo report    # top valid experiments table + score chart
```

- The default winner is the highest-scoring valid result in the graph history,
  not the frontier. `evo frontier` is for choosing where to branch next; it can
  exclude an exhausted branch whose score is still the right thing to ship. An
  explicit `exp_id` argument overrides auto-selection.
- A shippable winner must be valid: `committed`, or `pruned` with
  `prune_kind=exhausted`, with a commit and score, no `gate_result === false`,
  and no invalid-pruned ancestor. Never select `discarded`, `failed`, `active`,
  `evaluated`, legacy-pruned nodes with no `prune_kind`, `prune_kind=invalid`, or
  descendants of invalid-pruned nodes. If no valid candidate exists, stop and
  report why nothing is safe to ship.
- Resolve the run's root (baseline) node, then show the cumulative change:
  ```bash
  evo diff <root_id> <winner_id>   # target-scoped cumulative diff, baseline -> winner
  ```
  For changes outside the benchmark target, diff the commits directly
  (`git diff <baseline_commit> <winner_commit>`); each node carries `.commit`.
- Present a one-screen summary: winner id, score baseline -> winner (delta),
  the winning hypothesis, and a diffstat. Get a go before proceeding.

## Stage 2 -- Distill to a mergeable change

Work on a fresh branch off the user's current HEAD, not in the experiment
worktree. Re-derive the change so it stands on its own:

- **Scope restraint.** Keep only the files and lines the behaviour needs. Drop
  experiment scaffolding, debug logging, commented-out attempts, and churn the
  search introduced and then abandoned. Smaller, local diffs merge; sprawl does
  not.
- **Test integrity.** If the search weakened, skipped, or deleted a test to clear
  a gate, restore it. New behaviour that changes outputs needs a test that
  covers it. Never ship a green benchmark that rode on a loosened test -- call it
  out instead.
- **Mechanical cleanliness.** Match the repo's formatter and linter. No stray
  whitespace, no reordered imports unless the repo does that.
- **Codebase adherence.** Match surrounding naming, error handling, and structure.
  The diff should read like the file it lands in.

Then confirm the behaviour survived the distillation:

```bash
evo run <winner_id> --check    # or the project's benchmark / test command
```

If the distilled change no longer reproduces the winning score, do not paper over
it -- report the gap (which part of the experiment diff was load-bearing) and let
the user decide. Best-effort means honest about what could not be cleaned up, not
silently shipping the raw worktree.

## Stage 3 -- Land

Detect how the repo expects changes to arrive:

```bash
git remote -v
```

- **Remote present** -> open a pull request. Commit the distilled change on its
  branch, push, and `gh pr create` with the mergeability report (Stage 4) as the
  body. Do not push or open the PR without the user's go.
- **No remote** -> merge the distilled change into the user's working branch as a
  single clean commit. Do not force, do not rewrite existing history.

The landed commit message carries provenance: the winning experiment id, the
score delta, and the one-line hypothesis. State what changed and why it is safe;
do not narrate the search process.

## Stage 4 -- Mergeability report (advisory)

Always produce the report. It never blocks the merge -- it tells the user, and a
future reviewer, how mergeable the change is across the axes a maintainer judges
on:

- **Technique** -- what the change actually does to move the score, named
  concretely (the algorithm, data structure, or mechanism), not the search
  story. Distilled from the winning hypothesis: "replaced the O(n^2) dedup with a
  hash set", not "exp_0042 improved throughput". This is what a reviewer reads
  first.
- **Behavioural correctness** -- score baseline -> shipped (delta); benchmark
  status after distillation.
- **Regression safety** -- full test suite result on the distilled change.
- **Scope** -- files touched, diff size, whether the change stays local.
- **Test correctness** -- explicit yes/no on whether any test was modified,
  weakened, or removed, with detail; whether new behaviour is covered.
- **Mechanical cleanliness** -- formatter / linter status.
- **Codebase adherence** -- a note on style/convention fit.

Lead with a plain-language summary: what changed and why it is safe to merge. On
a remote repo this is the PR body. With no remote, print it and save it alongside
the run so the user can paste it into a review later.

## Guardrails (firm)

Everything above is method you can adapt to the repo. These are not:

- Never weaken, skip, or delete a test to make the change land. If the experiment
  did, restore it and report it.
- Never ship invalid-pruned, legacy-pruned, discarded, failed, active,
  evaluated, gate-failed, or invalid-lineage nodes. Only exhausted pruned nodes
  remain normal ship candidates.
- Never push or open a PR without the user's explicit go.
- Never rewrite or force-overwrite existing history on the user's branch.
- Never ship the raw experiment worktree diff as-is when distillation failed --
  report the gap instead.
