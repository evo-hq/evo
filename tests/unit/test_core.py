"""Unit tests for pure functions in evo.core.

Fast (millisecond) tests for logic that does not touch git, subprocess, or
the filesystem. Complements the slower tests/e2e.py flow tests.

Run: `python3 tests/unit/test_core.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "evo" / "src"))

from evo.core import collect_gates_from_path, path_to_node  # noqa: E402


def _graph(*nodes: dict) -> dict:
    return {"nodes": {n["id"]: n for n in nodes}}


def _node(id_: str, parent: str | None, gates: list[dict] | None = None, **extra) -> dict:
    return {"id": id_, "parent": parent, "gates": gates or [], **extra}


def _gate(name: str, command: str = "cmd") -> dict:
    return {"name": name, "command": command, "added_at": "2026-04-15T00:00:00Z"}


def test_path_to_node_returns_root_to_leaf_chain() -> None:
    graph = _graph(
        _node("root", None),
        _node("exp_0000", "root"),
        _node("exp_0001", "exp_0000"),
    )
    chain = [n["id"] for n in path_to_node(graph, "exp_0001")]
    assert chain == ["root", "exp_0000", "exp_0001"], chain


def test_collect_gates_empty_when_no_gates_anywhere() -> None:
    graph = _graph(_node("root", None), _node("exp_0000", "root"))
    assert collect_gates_from_path(graph, "exp_0000") == []


def test_collect_gates_inherits_root_gate() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("core_tests", "pytest -x")]),
        _node("exp_0000", "root"),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert [g["name"] for g in gates] == ["core_tests"]
    assert gates[0]["command"] == "pytest -x"


def test_collect_gates_unions_root_and_own_gates_in_root_to_leaf_order() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("root_gate")]),
        _node("exp_0000", "root", gates=[_gate("own_gate")]),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert [g["name"] for g in gates] == ["root_gate", "own_gate"]


def test_collect_gates_dedupes_by_name_keeping_ancestor_wins() -> None:
    # Same gate name declared on an ancestor and a descendant: the ancestor
    # one is kept (ancestors are walked first), the descendant redeclaration
    # is ignored. Verifies we do not surface the gate twice.
    graph = _graph(
        _node("root", None, gates=[_gate("flaky", "pytest ancestor")]),
        _node("exp_0000", "root", gates=[_gate("flaky", "pytest descendant")]),
    )
    gates = collect_gates_from_path(graph, "exp_0000")
    assert len(gates) == 1
    assert gates[0]["command"] == "pytest ancestor"


def test_collect_gates_scoped_to_ancestry_not_siblings() -> None:
    graph = _graph(
        _node("root", None, gates=[_gate("root_gate")]),
        _node("exp_0000", "root", gates=[_gate("sibling_gate")]),
        _node("exp_0001", "root"),
    )
    gates = collect_gates_from_path(graph, "exp_0001")
    assert [g["name"] for g in gates] == ["root_gate"]


def test_collect_gates_on_root_returns_own_only() -> None:
    graph = _graph(_node("root", None, gates=[_gate("core_tests")]))
    gates = collect_gates_from_path(graph, "root")
    assert [g["name"] for g in gates] == ["core_tests"]


TESTS = [fn for name, fn in globals().items() if name.startswith("test_") and callable(fn)]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
