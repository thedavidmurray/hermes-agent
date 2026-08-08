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


def test_connect_migrates_legacy_db_adds_routines_table(tmp_path):
    """A pre-routines DB must gain the routines table + index on connect."""
    import sqlite3

    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "routines" in tables
        assert "idx_routines_board_status" in indexes
        # Legacy rows survive the migration.
        count = migrated.execute(
            "SELECT COUNT(*) FROM tasks WHERE id = 'legacy'"
        ).fetchone()[0]
    assert count == 1


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


def test_materialize_routine_resume_restores(kanban_home):
    """Pause stops materialization; resume restores it (new window -> new task)."""
    with kb.connect() as conn:
        rid = kb.create_routine(
            conn,
            title="Resumable",
            cron_expr="0 9 * * *",
            concurrency="allow",
        )
        now = int(time.time())
        first_id = kb.materialize_routines(conn, now=now)[0][1]
        assert first_id is not None

        # Paused: no materialization at all, even at a new window.
        kb.update_routine_status(conn, rid, "paused")
        assert kb.materialize_routines(conn, now=now + 86400) == []

        # Resumed: a later window materializes a fresh task.
        kb.update_routine_status(conn, rid, "active")
        second_id = kb.materialize_routines(conn, now=now + 86400)[0][1]
        assert second_id is not None
        assert second_id != first_id
        assert kb.get_routine(conn, rid)["status"] == "active"


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


def test_cli_routine_run_now_materializes(kanban_home):
    from hermes_cli.kanban import _cmd_routine_create, _cmd_routine_run_now
    import argparse

    args = argparse.Namespace(
        title="On demand",
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
    assert _cmd_routine_run_now(argparse.Namespace(routine_id=rid, json=False)) == 0
    with kb.connect() as conn:
        tasks = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key LIKE ?",
            (f"routine-{rid}-%",),
        ).fetchall()
    assert len(tasks) == 1


def test_cli_routine_run_now_json_and_errors(kanban_home, capsys):
    from hermes_cli.kanban import _cmd_routine_create, _cmd_routine_run_now
    import argparse
    import json as _json

    args = argparse.Namespace(
        title="JSON output",
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

    # Missing routine -> exit 1.
    assert _cmd_routine_run_now(argparse.Namespace(routine_id="r_missing", json=False)) == 1

    # Paused routine -> exit 1.
    with kb.connect() as conn:
        kb.update_routine_status(conn, rid, "paused")
    assert _cmd_routine_run_now(argparse.Namespace(routine_id=rid, json=False)) == 1
    with kb.connect() as conn:
        kb.update_routine_status(conn, rid, "active")

    # JSON output is parseable and names the materialized task.
    capsys.readouterr()  # drain earlier create/error output
    assert _cmd_routine_run_now(argparse.Namespace(routine_id=rid, json=True)) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["routine_id"] == rid
    assert payload["task_id"].startswith("t_")


def test_cli_routine_slash_dispatcher(kanban_home):
    """End-to-end /kanban routine dispatch through the real argparse tree."""
    from hermes_cli.kanban import run_slash

    # Unknown action -> usage error path.
    out = run_slash("routine bogus")
    assert "invalid choice" in out or "usage" in out

    # Create via slash, then list via slash.
    created = run_slash("routine create 'Slash report' --cron '0 9 * * 1' --assignee scribe")
    assert "created" in created
    listed = run_slash("routine list")
    assert "Slash report" in listed
    assert "scribe" in listed

    # Pause/resume/delete via slash against the created routine.
    with kb.connect() as conn:
        rid = kb.list_routines(conn)[0]["id"]
    assert "paused" in run_slash(f"routine pause {rid}")
    assert "resumed" in run_slash(f"routine resume {rid}")
    assert "deleted" in run_slash(f"routine delete {rid}")
    with kb.connect() as conn:
        assert kb.get_routine(conn, rid) is None


def test_cli_routine_slash_run_now(kanban_home):
    """run-now via slash materializes a task through the real dispatcher."""
    from hermes_cli.kanban import run_slash

    run_slash("routine create 'On demand' --cron '0 9 * * *'")
    with kb.connect() as conn:
        rid = kb.list_routines(conn)[0]["id"]
    out = run_slash(f"routine run-now {rid}")
    assert "Materialized task" in out
    with kb.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE ?",
            (f"routine-{rid}-%",),
        ).fetchone()[0]
    assert count == 1


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
