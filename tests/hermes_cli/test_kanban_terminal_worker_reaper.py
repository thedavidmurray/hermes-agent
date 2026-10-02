"""Invariant: a worker that outlives its own terminal transition is still
reachable and gets reaped by the dispatcher (issue #111791).

``complete_task`` wipes ``tasks.worker_pid`` and the ``running``-only reclaim
sweeps never look at a ``done`` card again, so a worker that called
``kanban_complete`` and then hung (holding deleted ``state.db`` sidecar fds)
was invisible to every command. The closed ``task_runs`` row now keeps the pid
and its spawn fingerprint, and ``reap_terminal_workers`` ends such a worker on
the next tick — never a recycled PID, never a legacy row without a fingerprint.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    return proc


def _completed_card_with_worker(conn, proc, *, ended_ago: int = 600) -> tuple[str, int]:
    tid = kb.create_task(conn, title="finished", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    kbd._set_worker_pid(conn, tid, proc.pid)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id) is True
    # Default: the run closed long enough ago that the reaper's grace window has passed.
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?", (ended_ago, run_id))
    return tid, run_id


def test_worker_alive_after_completion_is_reaped_on_dispatch_tick(conn):
    proc = _sleeper()
    try:
        tid, run_id = _completed_card_with_worker(conn, proc)
        # The evidence the running-only sweeps lost lives on the closed run row.
        run = conn.execute("SELECT worker_pid, worker_started_at, ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["ended_at"] is not None and run["worker_pid"] == proc.pid and run["worker_started_at"] is not None

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=False, max_spawn=0)

        assert result.reaped_terminal_workers == [tid]
        assert proc.wait(timeout=10) is not None
        run = conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] is None and run["worker_started_at"] is None
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "terminal_worker_reaped" in kinds
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_dry_run_dispatch_skips_signal_but_keeps_db_bookkeeping(conn, monkeypatch):
    """dry_run=True must NOT send SIGTERM to a live worker (D1 fix).

    A dry-run tick that finds a live host-local worker defers the reap: no
    termination signal is delivered, the run row KEEPS its pid/fingerprint
    evidence (clearing it without killing would orphan a live process
    forever), and nothing is reported as reaped. The next real tick reaps it
    normally, so the reap is deferred rather than destroyed.

    Signal 0 (liveness probe) is allowed — it does not affect the process.
    """
    proc = _sleeper()
    terminations = []
    original_kill = os.kill

    def tracking_kill(pid, sig):
        if sig in (signal.SIGTERM, signal.SIGKILL):
            terminations.append((pid, sig))
        original_kill(pid, sig)

    monkeypatch.setattr(os, "kill", tracking_kill)
    try:
        tid, run_id = _completed_card_with_worker(conn, proc)
        run = conn.execute("SELECT worker_pid, worker_started_at, ended_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)

        # The core assertion: dry_run never terminates a live worker.
        assert terminations == [], f"dry_run dispatched termination signals: {terminations}"
        assert proc.poll() is None, "dry_run killed the live worker"
        assert result.reaped_terminal_workers == []
        # Evidence retained so a real tick can still reap it.
        run = conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid and run["worker_started_at"] is not None

        # A real tick reaps exactly as before: the fix defers, it does not disable.
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=False, max_spawn=0)
        assert result.reaped_terminal_workers == [tid]
        assert [pid for pid, _ in terminations] == [proc.pid]
        assert proc.wait(timeout=10) is not None
        run = conn.execute("SELECT worker_pid, worker_started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] is None and run["worker_started_at"] is None
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "terminal_worker_reaped" in kinds
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_dry_run_dispatch_never_calls_the_signal_path(conn):
    """``signal_fn`` is the injectable proof of D1: under ``dry_run`` the
    reaper's signal path must never be reached at all, so a harness can pass a
    raising stub and assert safety by construction rather than by discipline."""
    def explode(pid, sig):
        raise AssertionError(f"dry_run invoked the signal path: pid={pid} sig={sig}")

    proc = _sleeper()
    try:
        tid, run_id = _completed_card_with_worker(conn, proc)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)
        run = conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid
        assert proc.poll() is None

        # Direct call: no exception escapes, and the row is left intact.
        assert kbd.reap_terminal_workers(conn, signal_fn=explode, dry_run=True) == []
        assert proc.poll() is None
        run = conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_dry_run_does_not_time_out_a_live_worker(conn):
    """``enforce_max_runtime`` is a second host-signal path reachable from a
    dry-run tick (it targets *running* workers, not just post-run stragglers).
    A live over-limit worker must survive a dry-run tick untouched: the
    ``timed_out`` bookkeeping clears ``worker_pid`` and resets the phase, which
    only makes sense once the worker is gone."""
    proc = _sleeper()
    signals = []

    def signal_fn(pid, sig):
        signals.append((pid, sig))
        os.kill(pid, sig)

    try:
        tid = kb.create_task(conn, title="over limit", assignee="coder")
        kb.claim_task(conn, tid, claimer=kb._claimer_id())
        run_id = kb._current_run_id(conn, tid)
        kbd._set_worker_pid(conn, tid, proc.pid)
        conn.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET started_at = started_at - 600 WHERE id=?", (run_id,))

        assert kbd.enforce_max_runtime(conn, signal_fn=signal_fn, dry_run=True) == []
        assert signals == [], f"dry_run dispatched termination signals: {signals}"
        assert proc.poll() is None

        row = conn.execute("SELECT status, worker_pid FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "running", "dry_run reset a live worker's phase"
        assert row["worker_pid"] == proc.pid

        # The real path still terminates it, so the fix defers rather than disables.
        assert tid in kbd.enforce_max_runtime(conn, signal_fn=signal_fn)
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_dry_run_still_retires_a_worker_that_already_exited(conn):
    """A worker that is already gone needs no signal, so its bookkeeping still
    runs under ``dry_run`` — otherwise a dry-run tick could not report what a
    real tick would reclaim."""
    tid = kb.create_task(conn, title="dead over limit", assignee="coder")
    kb.claim_task(conn, tid, claimer=kb._claimer_id())
    run_id = kb._current_run_id(conn, tid)
    kbd._set_worker_pid(conn, tid, 999_999)  # no such process
    conn.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (tid,))
    conn.execute("UPDATE task_runs SET started_at = started_at - 600 WHERE id=?", (run_id,))

    def explode(pid, sig):
        raise AssertionError(f"dry_run invoked the signal path: pid={pid} sig={sig}")

    assert tid in kbd.enforce_max_runtime(conn, signal_fn=explode, dry_run=True)
    row = conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["worker_pid"] is None


def test_recycled_pid_and_legacy_row_are_never_signalled(conn):
    stranger = _sleeper()
    legacy = _sleeper()
    try:
        tid, run_id = _completed_card_with_worker(conn, stranger)
        # PID reuse: the recorded fingerprint belongs to a process that no longer exists.
        conn.execute("UPDATE task_runs SET worker_started_at = worker_started_at - 1000000 WHERE id=?", (run_id,))
        _, legacy_run = _completed_card_with_worker(conn, legacy)
        conn.execute("UPDATE task_runs SET worker_started_at = NULL WHERE id=?", (legacy_run,))

        assert kbd.reap_terminal_workers(conn) == []

        assert stranger.poll() is None and legacy.poll() is None
        # Stale evidence is dropped so the row is not rescanned; the legacy row is left alone.
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()["worker_pid"] is None
        assert conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (legacy_run,)).fetchone()["worker_pid"] == legacy.pid
    finally:
        for p in (stranger, legacy):
            p.kill()
            p.wait()


def test_fresh_terminal_run_is_left_alone_until_grace_passes(conn):
    """A worker is still alive for a moment after its own kanban_complete returns
    (final turn, session persistence): a just-closed run is not signalled."""
    proc = _sleeper()
    signals = []

    def signal_fn(pid, sig):
        signals.append((pid, sig))
        os.kill(pid, sig)

    try:
        tid, run_id = _completed_card_with_worker(conn, proc, ended_ago=0)

        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == []

        assert signals == [] and proc.poll() is None
        run = conn.execute("SELECT worker_pid FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["worker_pid"] == proc.pid  # evidence kept for a later tick
        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at - ? WHERE id=?",
            (kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS, run_id),
        )
        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == [tid]
        assert [pid for pid, _ in signals] == [proc.pid]
    finally:
        proc.kill()
        proc.wait()


def test_one_failing_row_does_not_abort_the_sweep(conn):
    """A signal failure on one run is logged and skipped; the other rows are still reaped."""
    broken, healthy = _sleeper(), _sleeper()
    try:
        _completed_card_with_worker(conn, broken)
        tid, _ = _completed_card_with_worker(conn, healthy)

        def signal_fn(pid, sig):
            if pid == broken.pid:
                raise RuntimeError("boom")
            healthy.kill()

        assert kbd.reap_terminal_workers(conn, signal_fn=signal_fn) == [tid]
    finally:
        for p in (broken, healthy):
            p.kill()
            p.wait()
