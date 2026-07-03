"""Tests for Kanban routines (recurring tasks)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_init_creates_routines_table(kanban_home):
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "routines" in names


def test_routines_index_exists(kanban_home):
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_routines_board_status" in names


# ---------------------------------------------------------------------------
# create_routine validation
# ---------------------------------------------------------------------------

def test_create_routine_requires_title(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="title is required"):
            kb.create_routine(conn, title="", cron_expr="0 9 * * *")


def test_create_routine_validates_cron(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="invalid cron expression"):
            kb.create_routine(conn, title="bad cron", cron_expr="not a cron")


def test_create_routine_validates_status(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="status must be one of"):
            kb.create_routine(conn, title="x", cron_expr="0 9 * * *", status="enabled")


def test_create_routine_validates_concurrency(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="concurrency must be one of"):
            kb.create_routine(conn, title="x", cron_expr="0 9 * * *", concurrency="queue")


def test_create_routine_validates_catch_up(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="catch_up must be one of"):
            kb.create_routine(conn, title="x", cron_expr="0 9 * * *", catch_up="replay")


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def test_materialize_routine_creates_task(kanban_home):
    with kb.connect() as conn:
        rid = kb.create_routine(
            conn,
            title="Daily sweep",
            cron_expr="0 9 * * *",
            assignee="sweeper",
            body="Run the daily repo sweep",
        )
        now = int(time.time())
        results = kb.materialize_routines(conn, now=now)
        task_id = results[0][1]
        assert task_id is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
    assert task.title == "Daily sweep"
    assert task.assignee == "sweeper"
    assert task.body == "Run the daily repo sweep"
    assert task.idempotency_key == f"routine-{rid}-{now}"


def test_materialize_routine_is_idempotent(kanban_home):
    with kb.connect() as conn:
        kb.create_routine(conn, title="Daily sweep", cron_expr="0 9 * * *")
        now = int(time.time())
        first = kb.materialize_routines(conn, now=now)
        second = kb.materialize_routines(conn, now=now)
    assert first[0][1] == second[0][1]
    assert len(first) == len(second) == 1


def test_materialize_routine_skips_if_active(kanban_home):
    with kb.connect() as conn:
        rid = kb.create_routine(
            conn,
            title="Weekly report",
            cron_expr="0 9 * * 1",
            assignee="scribe",
            concurrency="skip_if_active",
        )
        now = int(time.time())
        first_id = kb.materialize_routines(conn, now=now)[0][1]
        assert first_id is not None
        # Simulate the task still in-flight.
        assert kb.get_task(conn, first_id).status == "ready"
        second_id = kb.materialize_routines(conn, now=now + 1)[0][1]
        assert second_id is None


def test_materialize_routine_skips_missed_windows(kanban_home):
    """catch_up=skip_missed (default) must never replay multiple missed windows."""
    with kb.connect() as conn:
        kb.create_routine(
            conn,
            title="Hourly tick",
            cron_expr="0 * * * *",
            concurrency="allow",
        )
        # Window at 12:00.
        t_1200 = 1704067200  # 2024-01-01 12:00 UTC
        kb.materialize_routines(conn, now=t_1200)
        # Gateway down until 15:30; only the current window (15:00) should fire.
        results = kb.materialize_routines(conn, now=t_1200 + 3 * 3600 + 1800)
        assert results[0][1] is not None
        # Only one new task total (not 3 replayed windows).
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE 'routine-%'"
        ).fetchone()[0]
    assert count == 2


def test_materialize_routine_paused_does_nothing(kanban_home):
    with kb.connect() as conn:
        rid = kb.create_routine(conn, title="Paused", cron_expr="0 9 * * *")
        kb.update_routine_status(conn, rid, "paused")
        results = kb.materialize_routines(conn)
    assert results == []


# ---------------------------------------------------------------------------
# Concurrency policies
# ---------------------------------------------------------------------------

def test_materialize_routine_coalesce_returns_existing(kanban_home):
    with kb.connect() as conn:
        rid = kb.create_routine(
            conn,
            title="Coalesce me",
            cron_expr="0 9 * * *",
            concurrency="coalesce",
        )
        now = int(time.time())
        first_id = kb.materialize_routines(conn, now=now)[0][1]
        # Complete the first instance; coalesce should still return it.
        kb.complete_task(conn, first_id)
        second_id = kb.materialize_routines(conn, now=now + 1)[0][1]
        assert second_id == first_id


def test_materialize_routine_allow_stacks(kanban_home):
    with kb.connect() as conn:
        kb.create_routine(
            conn,
            title="Stackable",
            cron_expr="0 9 * * *",
            concurrency="allow",
        )
        now = int(time.time())
        first_id = kb.materialize_routines(conn, now=now)[0][1]
        second_id = kb.materialize_routines(conn, now=now + 1)[0][1]
        assert second_id is not None
        assert second_id != first_id


# ---------------------------------------------------------------------------
# CLI integration (lightweight)
# ---------------------------------------------------------------------------

def test_cli_routine_create_and_list(kanban_home, capsys):
    from hermes_cli.kanban import _cmd_routine_create, _cmd_routine_list
    import argparse

    args = argparse.Namespace(
        title="Weekly report",
        cron="0 9 * * 1",
        assignee="scribe",
        body=None,
        priority=0,
        skills=[],
        concurrency="skip_if_active",
        catch_up="skip_missed",
        json=False,
    )
    assert _cmd_routine_create(args) == 0

    args_list = argparse.Namespace(status=None, json=False)
    assert _cmd_routine_list(args_list) == 0
    captured = capsys.readouterr()
    assert "Weekly report" in captured.out


def test_cli_routine_pause_resume(kanban_home):
    from hermes_cli.kanban import _cmd_routine_create, _cmd_routine_pause, _cmd_routine_resume
    import argparse

    args = argparse.Namespace(
        title="X",
        cron="0 9 * * *",
        assignee=None,
        body=None,
        priority=0,
        skills=[],
        concurrency="skip_if_active",
        catch_up="skip_missed",
        json=False,
    )
    _cmd_routine_create(args)
    with kb.connect() as conn:
        rid = kb.list_routines(conn)[0]["id"]

    assert _cmd_routine_pause(argparse.Namespace(routine_id=rid)) == 0
    with kb.connect() as conn:
        assert kb.get_routine(conn, rid)["status"] == "paused"

    assert _cmd_routine_resume(argparse.Namespace(routine_id=rid)) == 0
    with kb.connect() as conn:
        assert kb.get_routine(conn, rid)["status"] == "active"


def test_cli_routine_delete(kanban_home):
    from hermes_cli.kanban import _cmd_routine_create, _cmd_routine_delete
    import argparse

    args = argparse.Namespace(
        title="To delete",
        cron="0 9 * * *",
        assignee=None,
        body=None,
        priority=0,
        skills=[],
        concurrency="skip_if_active",
        catch_up="skip_missed",
        json=False,
    )
    _cmd_routine_create(args)
    with kb.connect() as conn:
        rid = kb.list_routines(conn)[0]["id"]
    assert _cmd_routine_delete(argparse.Namespace(routine_id=rid)) == 0
    with kb.connect() as conn:
        assert kb.get_routine(conn, rid) is None


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------

def test_dispatch_once_materializes_routine(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        kb.create_routine(
            conn,
            title="Dispatchable",
            cron_expr="0 9 * * *",
            assignee="alice",
        )
        res = kb.dispatch_once(conn, dry_run=True)
    # The materialized task should be in the ready queue and seen by dry-run.
    assert len(res.spawned) == 1
    assert res.spawned[0][1] == "alice"
