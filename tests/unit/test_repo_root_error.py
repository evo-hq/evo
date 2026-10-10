"""`repo_root()` must fail with a clear message outside a git repository.

Regression test for the bug noted in #58: every command that resolves the
workspace through `repo_root()` (e.g. `evo direct-status`, `evo direct`,
`evo status`) leaked the raw git failure

    ERROR: Command '['git', 'rev-parse', '--show-toplevel']' returned
    non-zero exit status 128.

when run from a directory that is not inside a git repository. That's an
internal implementation detail, not an actionable message. `repo_root()`
should instead raise a friendly, git-agnostic error.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))


class TestRepoRootOutsideGitRepo(unittest.TestCase):
    def test_raises_runtime_error_not_called_process_error(self):
        """A plain, non-git temp dir must produce a RuntimeError, never the
        raw subprocess.CalledProcessError."""
        from evo.core import repo_root
        with tempfile.TemporaryDirectory() as d:
            outside = Path(d).resolve()
            # Sanity: the temp dir really is outside any git repo.
            probe = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=outside, capture_output=True, text=True,
            )
            self.assertNotEqual(probe.returncode, 0, "temp dir unexpectedly in a git repo")

            with self.assertRaises(RuntimeError) as ctx:
                repo_root(cwd=outside)
            self.assertNotIsInstance(ctx.exception, subprocess.CalledProcessError)

    def test_message_is_actionable_and_hides_git_internals(self):
        """The message must mention the workspace/git-repo requirement and
        must not leak the raw `rev-parse` / `exit status 128` internals."""
        from evo.core import repo_root
        with tempfile.TemporaryDirectory() as d:
            outside = Path(d).resolve()
            with self.assertRaises(RuntimeError) as ctx:
                repo_root(cwd=outside)
            msg = str(ctx.exception).lower()
            self.assertIn("git repository", msg)
            self.assertNotIn("rev-parse", msg)
            self.assertNotIn("exit status", msg)


if __name__ == "__main__":
    unittest.main()
