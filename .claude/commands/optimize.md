---
description: Run one evo optimization iteration from an existing committed frontier node.
---

Run one `evo` optimization iteration.

Workflow:
1. Read the current state:

```bash
uv run evo status
uv run evo scratchpad
uv run evo frontier
```

2. Pick one committed frontier node.
3. Create a child experiment:

```bash
uv run evo new --parent <exp_id> -m "hypothesis"
```

4. Edit the target file in the returned worktree.
5. Run it:

```bash
uv run evo run <new_exp_id>
```

6. If useful, inspect traces/logs and annotate:

```bash
uv run evo traces <exp_id> [task]
uv run evo annotate <exp_id> [task] "analysis"
```

7. Prune dead committed branches manually with:

```bash
uv run evo prune <exp_id> --reason "dead branch"
```
