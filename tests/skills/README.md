# Skill regression testing

How we catch behavioral drift when someone edits a `SKILL.md` or reference file.

## The problem

Skill files are natural-language instructions. There's no compiler, no type system, no unit test that directly executes them. A one-word change in an AskUserQuestion prompt or a missing `{worktree}` placeholder in a command example can silently change agent behavior — sometimes catastrophically — without any CI signal.

Naive end-to-end testing is expensive (each `claude -p` run costs ~$1-3 and takes ~10 minutes) and non-deterministic (the same skill + same fixture + same model can produce different tool-call sequences). We need a layered strategy.

## Four levels, stacked

### Level 0 — static checks (free, deterministic, every push)

Mechanical checks on the skill files themselves. No model invocation.

- YAML frontmatter parses; `name` + `description` present and within length limits
- `name` matches `^[a-z0-9-]+$` and isn't a reserved word (`anthropic`, `claude`)
- SKILL.md body is under 500 lines (the Anthropic-recommended ceiling)
- Every file referenced with a relative path (`references/x.md`, `scripts/y.py`) actually exists
- References are one level deep — references don't reference other references
- No Windows-style paths
- No bare `python` invocations (should be `python3` or env-qualified)
- No bare `pytest`, `npm`, `cargo` invocations in command examples (should be `uv run pytest`, etc.)

Implementation: `tests/skills/static/test_skill_structure.py`, ~50 lines, runs in seconds. Added to regular pytest suite.

### Level 1 — smoke test (~$1-2 per skill, PR only)

Run the skill end-to-end against a frozen pristine fixture. Assert high-level invariants on the result, not specific agent choices.

**Per-skill assertions:**
- Exit code is 0
- No stream events with `type: "error"` or `subtype: "tool_error"`
- `.evo/` directory was created
- `.evo/run_0000/graph.json` exists and contains at least one non-root node

**Discover-specific invariants:**
- Main branch HEAD is unchanged (pre-run SHA == post-run SHA) — the core "main stays pristine" property
- At least one experiment branch exists (`evo/run_0000/exp_*`)
- The experiment branch has at least 2 commits (benchmark + instrumentation minimum)
- The experiment's status in `graph.json` is `committed`, `running`, or `finished` (not `discarded` or `failed`)
- Baseline score is a number, not null

**Optimize-specific invariants:**
- Experiment tree grew during the run (more nodes than at start)
- At least one child of the baseline exists
- No main-branch commits added (optimize is purely worktree-based)

Level-1 tests use the real `claude` CLI via `--print --output-format stream-json`, with `--bare --plugin-dir <repo>` for reproducibility. Budget-capped with `--max-budget-usd 3.00` and `--max-turns 80`.

### Level 2 — behavioral checks (~$2-4 per skill, PR only when level 1 passes)

Parse the stream-json transcript and assert specific tool-call patterns the skill *must* produce. More discriminating than Level 1 but still coarse enough to survive LLM variability.

Examples for `/evo:discover`:
- Agent runs `evo init` before `evo new` (temporal ordering)
- Agent calls `evo gate add root` at least once
- Agent runs the benchmark at least 3 times before the first baseline commit (determinism check)
- Agent writes files inside `worktrees/exp_*` but not in the repo root after `evo init`
- Agent calls either `evo done <id> --score <num>` or explicitly justifies skipping

Each assertion is a function that takes the parsed event list and returns pass/fail + a diagnostic. Failures emit the relevant event window so the PR reviewer can see what happened.

### Level 3 — rubric evaluator (~$2-5 per skill, optional)

Spawn a *second* Claude instance as a judge. Feed it:
1. The original skill file
2. The stream-json transcript
3. A hand-authored rubric (10-15 criteria)

Judge returns a score (0-10) per rubric item with a one-line justification. Useful for semantic judgments that are hard to mechanize — *"did the agent genuinely propose unexplored dimensions, or did it just pick the obvious existing eval?"*

Run on major skill restructures, not every PR.

### Level 4 — manual review

For sweeping rewrites (like this one), nothing replaces eyeballing the transcript. Keep the stream-json artifact from every CI run uploaded as a workflow artifact so a reviewer can spot-check.

## Determinism strategy

LLM outputs aren't bit-identical run-to-run, even at `temperature: 0`. We accept this and design assertions that are invariant to ordering noise and phrasing choices:

- Assert on the *set* of commands executed, not the exact sequence
- Use regex matching (`r"evo init\b"`) not string equality
- Set high minimum thresholds, not exact counts (`commits >= 2`, not `commits == 3`)
- Allow the agent to discover correct behavior through trial-and-error within `--max-turns`
- Use the same `--model` for every run (pin to a stable Sonnet minor version if possible)

If a Level-2 assertion flakes >10% of the time, it's too tight — weaken the regex, raise the turn budget, or move the check to Level 3.

## Cost and scheduling

| Trigger | Levels run | Cost per run |
|---|---|---|
| Every push | 0 | ~$0 |
| PR touching `skills/**` | 0, 1, 2 | ~$5-10 per PR (3 skills) |
| Manual dispatch on `main` | 0, 1, 2, 3 | ~$15-25 |
| Nightly on `main` | 0, 1, 2 | ~$5-10 |

`ANTHROPIC_API_KEY` lives as a repo secret. Gate level 1+ behind an `ok-to-test` label if cost control is needed.

## Directory layout

```
tests/skills/
├── README.md                         # this file
├── conftest.py                       # pytest fixtures (fresh git repos from templates)
├── harness.py                        # wraps claude -p, parses stream-json, exposes helpers
├── fixtures/
│   ├── pristine_python/              # no existing benchmark — forces construction path
│   │   ├── README.md
│   │   ├── agent/solve.py
│   │   ├── data/problems.jsonl
│   │   └── tests/test_solve.py
│   └── pristine_node/                # TODO: equivalent for Node SDK testing
├── static/
│   └── test_skill_structure.py       # Level 0
├── test_discover.py                  # Level 1 + 2 for /evo:discover
├── test_optimize.py                  # Level 1 + 2 for /evo:optimize
└── judge/
    ├── rubric_discover.md            # Level 3 rubric
    └── eval.py                       # judge runner
```

## Harness sketch

```python
# harness.py
@dataclass
class RunResult:
    exit_code: int
    num_turns: int
    events: list[dict]                 # parsed stream-json
    fixture_path: Path
    pre_run_main_sha: str
    post_run_main_sha: str

    def bash_commands(self) -> list[str]: ...
    def first_bash_matching(self, pattern: str) -> int | None: ...
    def files_written_matching(self, glob: str) -> list[Path]: ...
    def experiments(self) -> list[dict]: ...   # parsed from .evo/run_*/graph.json


def run_skill(fixture: Path, skill: str, prompt: str, **flags) -> RunResult:
    """Copy fixture to a temp git repo, run claude -p, return structured result."""
```

Tests then look like:

```python
def test_discover_keeps_main_pristine(pristine_python, runner):
    result = runner(
        fixture=pristine_python,
        skill="discover",
        prompt="Execute /evo:discover on this repository.",
        max_turns=60,
    )
    assert result.exit_code == 0
    assert result.pre_run_main_sha == result.post_run_main_sha
    assert len(result.experiments()) >= 1

def test_discover_calls_init_before_new(pristine_python, runner):
    result = runner(fixture=pristine_python, skill="discover")
    init_idx = result.first_bash_matching(r"evo init\b")
    new_idx = result.first_bash_matching(r"evo new\b")
    assert init_idx is not None and new_idx is not None
    assert init_idx < new_idx
```

## Current status

As of 2026-04-13: this README is a design document. `fixtures/pristine_python/` has been populated (copied from the first real dry-run of `feat/discover-rewrite`). The harness and actual test cases are not yet implemented — building them is a follow-up PR, separate from the discover rewrite itself.

The initial manual dry-run that motivated this design is documented in the commit history of `feat/discover-rewrite`; see commits `048f8b0` and `6c41872` for the rewrite and subsequent patches informed by the dry-run findings.

## Why not just unit-test the agent?

Tempting, but agents aren't functions. A skill is a prompt; a skill's output is whatever the model decides to do next given that prompt + its context. There's no deterministic "if input X then output Y" relationship to assert against.

What we *can* assert is: the agent produces artifacts that satisfy our invariants. That's what these tests do.

## Gotcha: string substitutions don't fire in prompt-driven dry runs

Claude Code skills support `${CLAUDE_SKILL_DIR}`, `${CLAUDE_SESSION_ID}`, `$ARGUMENTS`, and similar substitutions. These are rendered into the skill content **before** it enters the model's context — but only when the skill is invoked via `/skill-name` or the Skill tool, *not* when the raw SKILL.md content is passed to `claude -p` as a prompt.

If a test harness reads `SKILL.md` and hands its raw text to the agent (a convenient way to simulate `/evo:discover` in `-p` mode, where user-invoked slash commands aren't available), any `${CLAUDE_SKILL_DIR}` references will reach the agent unresolved. The agent will see the literal string and have to infer what it means.

Three options:

1. **Pre-substitute before handing to the agent.** The test harness computes the actual skill dir path (e.g. via plugin introspection or hard-coded knowledge of the test layout) and does a `str.replace()` on the rendered SKILL.md before embedding it in the prompt. Simple, keeps assertions honest.

2. **Assert on behavior that doesn't depend on substitutions.** E.g. main-stays-pristine, experiment-branch-has-commits, baseline-score-captured — none of which require `${CLAUDE_SKILL_DIR}` to be resolved for the agent to get them right.

3. **Invoke via real plugin install.** Spin up the plugin in an interactive session via a script that writes to the Claude Code session socket. Expensive and hard to automate; not recommended for routine CI.

We use option 1 + 2 combined. The manual dry runs that motivated this design hit the substitution gap and surfaced one false-positive finding (`CLAUDE_SKILL_DIR` "never defined") — after confirming against the docs, the finding was dismissed and the harness design updated to pre-substitute.
