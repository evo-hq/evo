"""Evaluation harness for the evo optimize skill.

Cross-platform: works on macOS, Linux, WSL2, and Windows (with a weaker isolation
layer on Windows because the Claude Code sandbox requires Seatbelt/bubblewrap).

The harness isolates each trial in a temp dir, copies the target skill file in,
runs `claude -p` against a synthetic fixture, and scores the output (strict
keyword + Jaccard, plus LLM judge). Intended for validating skill changes
against a planted-pattern fixture before merging.

Subcommands:
    check     preflight -- claude CLI present, OS capabilities, python version
    setup     generate fixtures (small, medium, large) with LLM narratives
    trial     run one isolated trial on a fixture with a given skill file
    matrix    run N seeds on one size; write results CSV
    score     (re)score an existing trial's parsed_output.json
    clean     remove generated fixtures and /tmp/rlm_trial_* dirs

Examples (portable -- same command works on all OSes):
    python rlm_eval.py check
    python rlm_eval.py setup
    python rlm_eval.py trial --fixture fixtures/large --out trials/t001
    python rlm_eval.py matrix --size large --seeds 5 --out trials/

A future A/B experiment can use `--skill-path <other>` to compare a candidate
file against the current SKILL.md; by default the current skill is used.
"""
from __future__ import annotations
import argparse, csv, json, os, platform, re, shutil, subprocess, sys, tempfile, textwrap, time, uuid
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent
PLUGIN_SKILL_DIR = REPO_ROOT / "plugins" / "evo" / "skills" / "optimize"

# V/R comparison: V is the vanilla baseline snapshot kept in the eval dir;
# R is the current live skill. This lets the harness do V-vs-R paired
# comparisons forever, independent of what's shipped in SKILL.md.
SKILL_V = THIS_DIR / "baseline_SKILL.md"
SKILL_R = PLUGIN_SKILL_DIR / "SKILL.md"
DEFAULT_SKILL = SKILL_R

# Paths the agent must NOT read (ground truth, generator source, scorer, prior outputs)
def leakage_deny_paths() -> list[str]:
    """Absolute paths we want to prevent the trial agent from reading.

    Covers the repo root (source of generator / scorer / ground truth), the
    system temp dir (modern trial outputs), and POSIX `/tmp/` (legacy outputs
    from earlier non-isolated runs on macOS where gettempdir != /tmp).
    """
    paths = {
        str(REPO_ROOT),
        str(Path(tempfile.gettempdir()) / "rlm_dryrun"),
        str(Path(tempfile.gettempdir()) / "rlm_test_outputs"),
    }
    if platform.system() in {"Darwin", "Linux"}:
        paths.add("/tmp/rlm_dryrun")
        paths.add("/tmp/rlm_test_outputs")
    return sorted(paths)

# ------------------------------- preflight -------------------------------

def cmd_check(_args: argparse.Namespace) -> int:
    os_name = platform.system()
    print(f"platform: {os_name} {platform.release()} ({platform.machine()})")
    print(f"python: {sys.version.split()[0]}")
    claude = shutil.which("claude")
    if not claude:
        print("claude CLI: NOT FOUND on PATH", file=sys.stderr)
        print("  install via https://code.claude.com/docs/en/quickstart", file=sys.stderr)
        return 1
    ver = subprocess.run([claude, "--version"], capture_output=True, text=True)
    print(f"claude CLI: {ver.stdout.strip()} ({claude})")

    sandbox_supported = os_name in {"Darwin", "Linux"}
    if os_name == "Windows":
        # WSL2 reports as Linux inside the distro; this path is hit for native Windows only
        print("sandbox: NOT SUPPORTED natively on Windows. Isolation will use permission "
              "deny rules only (blocks Claude's Read/Edit tools, not Bash subprocesses). "
              "For full OS-level isolation, run inside WSL2.")
    elif sandbox_supported:
        print(f"sandbox: supported ({'Seatbelt' if os_name == 'Darwin' else 'bubblewrap'})")
    if not SKILL_R.exists():
        print(f"skill R: MISSING at {SKILL_R}", file=sys.stderr)
        return 2
    if not SKILL_V.exists():
        print(f"skill V (baseline): MISSING at {SKILL_V}", file=sys.stderr)
        return 2
    print(f"skill V (baseline): {SKILL_V}")
    print(f"skill R (current):  {SKILL_R}")
    return 0

# ------------------------------- setup -------------------------------

def cmd_setup(args: argparse.Namespace) -> int:
    from generate_fixture import generate, SIZES
    for size in SIZES:
        out = THIS_DIR / "fixtures" / size
        if out.exists():
            shutil.rmtree(out)
        generate(size, out, args.seed, with_traces=args.with_traces)
    print(f"fixtures under {THIS_DIR / 'fixtures'}")
    return 0

# ------------------------------- isolated trial -------------------------------

def write_settings(trial_dir: Path) -> None:
    """Write the project .claude/settings.json that locks the trial down.

    Permission deny rules cover Claude's built-in Read tool and bash reads on
    macOS/Linux (via sandbox). Writes are denied entirely -- the trial shouldn't
    need them.
    """
    os_name = platform.system()
    deny_paths = leakage_deny_paths()
    # Permission rules use `//absolute` prefix in Claude Code
    perm_deny = [f"Read(//{p.lstrip('/')}/**)" for p in deny_paths] + ["Edit", "Write"]
    settings: dict = {
        "permissions": {
            "allow": ["Bash", "Read", "Grep", "Glob"],
            "deny": perm_deny,
        }
    }
    if os_name in {"Darwin", "Linux"}:
        settings["sandbox"] = {
            "enabled": True,
            "filesystem": {
                "denyRead": [f"//{p.lstrip('/')}" for p in deny_paths],
            },
        }
    (trial_dir / ".claude").mkdir(parents=True, exist_ok=True)
    (trial_dir / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))

def build_prompt() -> str:
    return textwrap.dedent("""
        You are the evo optimization orchestrator performing the cross-round cross-cutting scan.

        Skill context (read this file first): ./skill.md

        The skill defines the procedure. Follow its steps, in order. Your deliverable is a list of cross-cutting patterns across the experiments under ./.evo/run_0001/experiments/. "Patterns" includes failure modes, wall-of-regression hypotheses, compound failures (intersections), AND successful improvers worth extending -- anything the next round's brief-writing could act on.

        Output ONLY a JSON object, no prose, no markdown fences, with this shape:
        {"patterns": [{"signature": "<short description>", "experiment_ids": ["exp_NNNN", ...]}, ...]}

        An experiment belongs to a pattern only if its evidence actually exhibits it. Do not guess; verify.
    """).strip()

def run_trial(fixture_dir: Path, skill_path: Path, out_dir: Path,
              keep_trial_dir: bool = True) -> dict:
    """Run one isolated trial. Returns meta dict with duration, cost, turns, leaks."""
    out_dir.mkdir(parents=True, exist_ok=True)
    trial_id = uuid.uuid4().hex[:8]
    trial_dir = Path(tempfile.gettempdir()) / f"rlm_trial_{trial_id}"
    trial_dir.mkdir(parents=True, exist_ok=False)

    # Stage only the fixture's .evo subtree and the chosen skill (renamed to neutral)
    shutil.copytree(fixture_dir / ".evo", trial_dir / ".evo")
    shutil.copy2(skill_path, trial_dir / "skill.md")
    write_settings(trial_dir)

    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not on PATH")

    prompt = build_prompt()
    stream_path = out_dir / "stream.jsonl"
    stderr_path = out_dir / "stderr.log"

    t0 = time.time()
    with stream_path.open("w") as s, stderr_path.open("w") as e:
        proc = subprocess.run(
            [claude, "-p",
             "--output-format", "stream-json",
             "--verbose",
             "--permission-mode", "default",
             prompt],
            cwd=str(trial_dir),
            stdout=s, stderr=e,
            timeout=3600,
        )
    wall = time.time() - t0
    rc = proc.returncode

    meta = _extract_meta(stream_path, out_dir)
    meta["wall_seconds"] = round(wall, 2)
    meta["returncode"] = rc
    meta["trial_dir"] = str(trial_dir)

    leaks = _scan_leaks(stream_path)
    meta["leaks"] = leaks

    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    if not keep_trial_dir:
        shutil.rmtree(trial_dir, ignore_errors=True)
    return meta

def _extract_patterns_json(raw: str) -> dict | None:
    """Extract {"patterns": [...]} from LLM output, with repair for common
    truncation (missing trailing `]}` or `}`). Returns None if unrecoverable."""
    s = raw.strip()
    # Strip markdown fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        return None
    candidate = s[start:]
    # Try as-is first
    for attempt in (candidate, candidate + "}", candidate + "]}", candidate + "]}}"):
        try:
            parsed = json.loads(attempt, strict=False)
            if isinstance(parsed, dict) and "patterns" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    # Last resort: bracket-counting repair on the candidate
    open_c = candidate.count("{")
    close_c = candidate.count("}")
    open_b = candidate.count("[")
    close_b = candidate.count("]")
    repaired = candidate + ("]" * (open_b - close_b)) + ("}" * (open_c - close_c))
    try:
        parsed = json.loads(repaired, strict=False)
        if isinstance(parsed, dict) and "patterns" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass
    return None


def _extract_meta(stream_path: Path, out_dir: Path) -> dict:
    events = []
    with stream_path.open() as f:
        for line in f:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    result_evt = next((e for e in reversed(events) if e.get("type") == "result"), None)
    meta = {}
    if result_evt:
        meta.update({
            "duration_ms": result_evt.get("duration_ms"),
            "num_turns": result_evt.get("num_turns"),
            "total_cost_usd": result_evt.get("total_cost_usd"),
            "stop_reason": result_evt.get("stop_reason"),
        })
        raw = result_evt.get("result", "") or ""
        parsed = _extract_patterns_json(raw)
        if parsed is not None:
            (out_dir / "parsed_output.json").write_text(json.dumps(parsed, indent=2))
            meta["patterns_reported"] = len(parsed.get("patterns", []))
        else:
            (out_dir / "parsed_output.json").write_text("{}")
            meta["parse_error"] = True
    # Classify tool invocations.
    # sub_model_calls counts any mechanism that spawns a fresh model session:
    # native Agent tool calls (Claude Code's primitive) AND Bash-spawned
    # `claude -p` subprocesses.
    sub_model_calls = 0
    agent_tool_calls = 0
    bash_claude_spawns = 0
    structured_queries = 0
    for e in events:
        if e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content", []):
            if b.get("type") != "tool_use":
                continue
            name = b.get("name")
            if name == "Agent":
                agent_tool_calls += 1
                sub_model_calls += 1
            elif name == "Bash":
                cmd = (b.get("input") or {}).get("command", "")
                if re.search(r"\bclaude\b.*(-p\b|\bprint\b)", cmd):
                    bash_claude_spawns += 1
                    sub_model_calls += 1
                elif re.search(r"\b(python|python3|jq|awk|grep|rg)\b", cmd) and re.search(r"outcome\.json|task_.*\.json|\.evo/", cmd):
                    structured_queries += 1
    meta["sub_model_calls"] = sub_model_calls
    meta["agent_tool_calls"] = agent_tool_calls
    meta["bash_claude_spawns"] = bash_claude_spawns
    meta["structured_queries"] = structured_queries
    return meta

def _scan_leaks(stream_path: Path) -> list[dict]:
    """Grep the stream for any tool_use referencing denied paths or ground-truth names."""
    bad = [
        re.escape(str(REPO_ROOT)),
        r"rlm_dryrun",
        r"rlm_test_outputs",
        r"ground_truth",
        r"generate_fixture",
        r"rlm_eval[\\/]score",
    ]
    hits: list[dict] = []
    with stream_path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("type") != "assistant":
                continue
            for block in m.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                inp_str = json.dumps(block.get("input", {}))
                for pat in bad:
                    if re.search(pat, inp_str):
                        hits.append({
                            "tool": block.get("name"),
                            "pattern": pat,
                            "input": block.get("input"),
                        })
    return hits

def cmd_trial(args: argparse.Namespace) -> int:
    if args.skill_path:
        skill = Path(args.skill_path).resolve()
    elif args.variant:
        skill = SKILL_V if args.variant == "V" else SKILL_R
    else:
        skill = DEFAULT_SKILL
    fixture = Path(args.fixture).resolve()
    out = Path(args.out).resolve()
    meta = run_trial(fixture, skill, out, keep_trial_dir=not args.cleanup)
    print(json.dumps(meta, indent=2))
    if meta.get("leaks"):
        print(f"LEAKAGE DETECTED ({len(meta['leaks'])} hits) -- trial is invalid", file=sys.stderr)
        return 3
    return 0

# ------------------------------- matrix -------------------------------

def cmd_matrix(args: argparse.Namespace) -> int:
    from generate_fixture import generate

    # Resolve which variants to run. Default: V (baseline) + R (current).
    if args.skill_path:
        variants = [("custom", Path(args.skill_path).resolve())]
    elif args.variants:
        lut = {"V": SKILL_V, "R": SKILL_R}
        variants = [(v, lut[v]) for v in args.variants]
    else:
        variants = [("V", SKILL_V), ("R", SKILL_R)]

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for seed in range(1, args.seeds + 1):
        fixture_dir = out_root / f"seed_{seed}" / "fixture"
        if fixture_dir.exists():
            shutil.rmtree(fixture_dir)
        generate(args.size, fixture_dir, seed)
        for variant_name, skill in variants:
            trial_out = out_root / f"seed_{seed}" / variant_name
            print(f"[seed {seed} / {variant_name}] running...")
            meta = run_trial(fixture_dir, skill, trial_out, keep_trial_dir=False)
            strict = _score_against(trial_out, fixture_dir / "ground_truth.json")
            llm = _score_llm_against(trial_out, fixture_dir / "ground_truth.json")
            row = {
                "seed": seed,
                "variant": variant_name,
                "strict_recall": strict.get("recall"),
                "strict_halluc": strict.get("hallucinated"),
                "llm_quality": llm.get("quality_ratio"),
                "planted_found": llm.get("patterns_found_of_planted"),
                "missed_planted": ",".join(llm.get("missed_planted", [])) or "-",
                "num_turns": meta.get("num_turns"),
                "agent_tool_calls": meta.get("agent_tool_calls"),
                "bash_claude_spawns": meta.get("bash_claude_spawns"),
                "structured_queries": meta.get("structured_queries"),
                "wall_seconds": meta.get("wall_seconds"),
                "cost_usd": meta.get("total_cost_usd"),
                "leaks": len(meta.get("leaks", [])),
            }
            rows.append(row)
            print(f"  -> strict_recall={row['strict_recall']} llm_quality={row['llm_quality']} "
                  f"missed={row['missed_planted']} turns={row['num_turns']} "
                  f"agent={row['agent_tool_calls']} cost=${row['cost_usd']}")
    # Write CSV
    csv_path = out_root / "matrix.csv"
    if rows:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {csv_path}")
    return 0

def _score_llm_against(trial_out: Path, ground_truth: Path) -> dict:
    """Run the LLM judge scorer and compute planted-found count."""
    import score_llm as llm_scorer
    parsed = trial_out / "parsed_output.json"
    if not parsed.exists():
        return {"quality_ratio": None, "patterns_found_of_planted": 0, "missed_planted": []}
    gt = json.loads(ground_truth.read_text())
    out = json.loads(parsed.read_text())
    try:
        result = llm_scorer.score(gt, out)
    except Exception as e:
        print(f"  [warn] LLM judge failed: {e}")
        return {"quality_ratio": None, "patterns_found_of_planted": 0, "missed_planted": []}
    (trial_out / "score_llm.json").write_text(json.dumps(result, indent=2))
    recall = result.get("planted_recall", {})
    found = sum(1 for v in recall.values() if v)
    return {
        "quality_ratio": result.get("quality_ratio"),
        "patterns_found_of_planted": found,
        "missed_planted": result.get("missed_planted", []),
    }


def _score_against(trial_out: Path, ground_truth: Path) -> dict:
    import score as scorer
    parsed = trial_out / "parsed_output.json"
    if not parsed.exists():
        return {"recall": None, "hallucinated": None, "patterns_found": 0}
    gt = json.loads(ground_truth.read_text())
    out = json.loads(parsed.read_text())
    sc = scorer.score(gt, out)
    (trial_out / "score.json").write_text(json.dumps(sc, indent=2))
    return sc

def cmd_score(args: argparse.Namespace) -> int:
    sc = _score_against(Path(args.trial_dir).resolve(), Path(args.ground_truth).resolve())
    print(json.dumps(sc, indent=2))
    return 0

# ------------------------------- cleanup -------------------------------

def cmd_clean(args: argparse.Namespace) -> int:
    targets = []
    fx = THIS_DIR / "fixtures"
    if fx.exists():
        targets.append(fx)
    for tmp in {Path(tempfile.gettempdir()), Path("/tmp")}:
        if not tmp.exists():
            continue
        for p in tmp.glob("rlm_trial_*"):
            targets.append(p)
        for legacy in ("rlm_dryrun", "rlm_test_outputs", "rlm_iso", "rlm_iso_sanity"):
            p = tmp / legacy
            if p.exists():
                targets.append(p)
    for t in sorted(set(str(x) for x in targets)):
        shutil.rmtree(t, ignore_errors=True)
        print(f"removed {t}")
    if not targets:
        print("nothing to clean")
    return 0

# ------------------------------- main -------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="rlm_eval")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(func=cmd_check)

    p_setup = sub.add_parser("setup", help="generate fixtures")
    p_setup.add_argument("--with-traces", action="store_true", default=True)
    p_setup.add_argument("--no-traces", dest="with_traces", action="store_false")
    p_setup.add_argument("--trace-chars", type=int, default=1000)
    p_setup.add_argument("--seed", type=int, default=1)
    p_setup.set_defaults(func=cmd_setup)

    p_tr = sub.add_parser("trial", help="run one isolated trial")
    p_tr.add_argument("--fixture", required=True)
    p_tr.add_argument("--variant", choices=["V", "R"], help="V=baseline_SKILL.md, R=current plugins/evo/.../SKILL.md")
    p_tr.add_argument("--skill-path", help="explicit path to a SKILL.md (overrides --variant)")
    p_tr.add_argument("--out", required=True)
    p_tr.add_argument("--cleanup", action="store_true", help="delete trial dir after")
    p_tr.set_defaults(func=cmd_trial)

    p_mx = sub.add_parser("matrix", help="run N seeds; default compares V (baseline) vs R (current)")
    p_mx.add_argument("--size", choices=["small", "medium", "large"], default="large")
    p_mx.add_argument("--seeds", type=int, default=5)
    p_mx.add_argument("--variants", nargs="+", choices=["V", "R"], help="subset of variants (default: both)")
    p_mx.add_argument("--skill-path", help="run a single custom skill path instead of V/R")
    p_mx.add_argument("--out", required=True)
    p_mx.set_defaults(func=cmd_matrix)

    p_sc = sub.add_parser("score", help="(re)score an existing trial")
    p_sc.add_argument("--trial-dir", required=True)
    p_sc.add_argument("--ground-truth", required=True)
    p_sc.set_defaults(func=cmd_score)

    sub.add_parser("clean").set_defaults(func=cmd_clean)

    args = ap.parse_args()
    rc = args.func(args)
    sys.exit(rc or 0)

if __name__ == "__main__":
    main()
