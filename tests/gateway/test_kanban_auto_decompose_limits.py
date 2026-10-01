"""Auto-decomposer brakes: recursion depth cap + trailing-24h child budget.

Field report (edgeless board, 2026-10): the auto-decomposer created 6,778 of
13,134 tasks in 60 days, 765 in one day, and re-fanned-out cards that
``block_loop_detected`` had parked in triage "for a human". Neither
``auto_decompose`` (all or nothing) nor ``auto_decompose_per_tick`` (a burst
cap, min 1 = 288 fan-outs/day at the default interval) bounds either.

Every DB this file touches lives under ``tmp_path``; the fixture asserts that
before the first write.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from gateway import kanban_watchers_common as kwc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli.kanban_db_graph import decompose_triage_task

AUTHOR = "auto-decomposer"  # the author the dispatcher tick stamps on children


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path().resolve()
    assert tmp_path.resolve() in db_path.parents, f"refusing to touch {db_path}"
    kb.init_db()
    return home


def _triage(conn, title="rough idea"):
    return kb.create_task(conn, title=title, triage=True)


def _fan_out(conn, root_id, n=2):
    children = [{"title": f"step {i}", "body": "", "assignee": None, "parents": []} for i in range(n)]
    ids = decompose_triage_task(conn, root_id, root_assignee=None, children=children, author=AUTHOR)
    assert ids is not None
    return ids


def _back_to_triage(conn, task_id):
    """What ``block_loop_detected`` does to a card: park it in triage."""
    conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (task_id,))
    conn.commit()


def test_fresh_triage_task_is_allowed(board):
    with kbc.connect_closing() as conn:
        tid = _triage(conn)
        assert decomp.auto_decompose_skip_reason(conn, tid, max_depth=1, daily_budget=100) == ""


def test_decomposer_child_is_not_fanned_out_again(board):
    with kbc.connect_closing() as conn:
        child = _fan_out(conn, _triage(conn))[0]
        _back_to_triage(conn, child)
        assert "depth 1" in decomp.auto_decompose_skip_reason(conn, child, max_depth=1, daily_budget=None)
        # A deeper cap still lets one more level through.
        assert decomp.auto_decompose_skip_reason(conn, child, max_depth=2, daily_budget=None) == ""


def test_already_decomposed_root_is_not_fanned_out_again(board):
    with kbc.connect_closing() as conn:
        root = _triage(conn)
        _fan_out(conn, root)
        _back_to_triage(conn, root)
        assert decomp.auto_decompose_skip_reason(
            conn, root, max_depth=1, daily_budget=None,
        ) == "already decomposed once"


def test_no_limits_means_no_gate(board):
    with kbc.connect_closing() as conn:
        child = _fan_out(conn, _triage(conn))[0]
        _back_to_triage(conn, child)
        assert decomp.auto_decompose_skip_reason(conn, child, max_depth=None, daily_budget=None) == ""


def test_daily_budget_counts_children_in_trailing_24h(board):
    with kbc.connect_closing() as conn:
        _fan_out(conn, _triage(conn), n=3)
        fresh = _triage(conn, "next idea")
        newest = conn.execute("SELECT MAX(created_at) FROM tasks").fetchone()[0]
        assert decomp.auto_decompose_skip_reason(
            conn, fresh, max_depth=None, daily_budget=4, now=newest,
        ) == ""
        assert "daily budget spent (3 >= " in decomp.auto_decompose_skip_reason(
            conn, fresh, max_depth=None, daily_budget=3, now=newest,
        )
        # The window slides: a day later the same children no longer count.
        assert decomp.auto_decompose_skip_reason(
            conn, fresh, max_depth=None, daily_budget=3, now=newest + 24 * 60 * 60 + 1,
        ) == ""


def test_budget_ignores_tasks_from_other_creators(board):
    with kbc.connect_closing() as conn:
        for i in range(5):
            kb.create_task(conn, title=f"human task {i}", created_by="user")
        fresh = _triage(conn)
        assert decomp.auto_decompose_skip_reason(conn, fresh, max_depth=None, daily_budget=1) == ""


def _dispatcher(monkeypatch):
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: ["default"])
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


def test_tick_leaves_held_tasks_in_triage_and_spends_no_slot_on_them(board, monkeypatch):
    with kbc.connect_closing() as conn:
        looped_child = _fan_out(conn, _triage(conn, "old root"))[0]
        _back_to_triage(conn, looped_child)
        fresh = _triage(conn, "new idea")

    called: list[str] = []

    def fake_decompose(task_id, author=None):
        called.append(task_id)
        return SimpleNamespace(ok=True, fanout=False, child_ids=None, reason=None)

    monkeypatch.setattr(decomp, "decompose_task", fake_decompose)
    monkeypatch.setattr(decomp, "list_triage_ids", lambda: [looped_child, fresh])
    dispatcher = _dispatcher(monkeypatch)

    # per_tick=1: the held child must not eat the only slot.
    assert dispatcher.auto_decompose_tick(1, 1, None) == 1
    assert called == [fresh]

    called.clear()
    # Budget already spent (2 children exist): nothing is attempted at all.
    assert dispatcher.auto_decompose_tick(5, None, 2) == 0
    assert called == []
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, fresh).status == "triage"
        assert kb.get_task(conn, looped_child).status == "triage"


def test_tick_without_limits_is_unchanged(board, monkeypatch):
    called: list[str] = []

    def fake_decompose(task_id, author=None):
        called.append(task_id)
        return SimpleNamespace(ok=True, fanout=False, child_ids=None, reason=None)

    monkeypatch.setattr(decomp, "decompose_task", fake_decompose)
    monkeypatch.setattr(decomp, "list_triage_ids", lambda: ["t1", "t2"])
    assert _dispatcher(monkeypatch).auto_decompose_tick(5) == 2
    assert called == ["t1", "t2"]


@pytest.mark.parametrize(
    "kanban_cfg, expected",
    [
        ({}, (None, None)),
        ({"auto_decompose_max_depth": 1, "auto_decompose_daily_budget": 40}, (1, 40)),
        ({"auto_decompose_max_depth": "1", "auto_decompose_daily_budget": 0}, (1, 0)),
        ({"auto_decompose_max_depth": -1, "auto_decompose_daily_budget": "lots"}, (None, None)),
        ({"auto_decompose_max_depth": True, "auto_decompose_daily_budget": None}, (None, None)),
    ],
)
def test_limits_resolve_from_live_config(kanban_cfg, expected):
    assert kwc._resolve_auto_decompose_limits(lambda: {"kanban": kanban_cfg}) == expected


def test_limits_survive_a_config_read_error():
    def boom():
        raise OSError("config unreadable")

    assert kwc._resolve_auto_decompose_limits(boom) == (None, None)
