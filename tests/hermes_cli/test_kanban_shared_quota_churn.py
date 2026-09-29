"""Worker churn against an exhausted shared free pool (edgeless board, 2026-09-28/29).

Uses only the stock dispatcher, the real exit-75 (rate-limited) reclaim sweep and the stock
per-task ``rate_limit_cooldown``. Without a shared admission domain every ready task burns its
own doomed spawn per cooldown window, so churn scales with the ready queue. With the profiles'
``quota_admission`` blocks honoured, the pool is closed once and probed once per cooldown.

Deliberately imports nothing from ``hermes_cli.quota_admission`` at module level so the test
runs (and fails on the churn assertion) on a tree without the feature.
"""
import importlib
import time
from datetime import datetime, timezone

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.config import atomic_config_write

TICK = 300  # gateway dispatch_interval_seconds
FAKE_PID = 4_000_000


def test_exhausted_free_pool_is_probed_once_per_cooldown_not_once_per_task(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)  # stock 300s
    atomic_config_write(home / "config.yaml", {})
    # The live fleet block (34 profiles), verbatim apart from the state_file location.
    block = {"enabled": True, "state_file": str(tmp_path / "hermes-quota-admission.json"),
             "provider_domains": {"custom:runtime-local": "free-pool", "moa": "free-pool",
                                  "custom:runtime-nvidia": "free-pool", "custom:runtime-nous": "free-pool"},
             "cooldown_seconds": 1500, "probe_lease_seconds": 600}
    profiles = {"verifier": "custom:runtime-local", "anomaly": "custom:runtime-nvidia",
                "scout": "custom:runtime-nous", "builder": "moa"}
    for name, provider in profiles.items():
        (home / "profiles" / name).mkdir(parents=True)
        atomic_config_write(home / "profiles" / name / "config.yaml",
                            {"model": {"provider": provider, "default": "x"}, "quota_admission": block})

    clock = [float(int(datetime.now(timezone.utc).timestamp()))]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    # Every spawned worker is already dead by the next tick and left the rate-limit exit trailer.
    monkeypatch.setattr(kbd, "_worker_alive", lambda *a, **k: False)
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: ("rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)

    try:
        qa = importlib.import_module("hermes_cli.quota_admission")
    except ImportError:
        qa = None
    spawned = []

    def spawn(task, workspace, board=None):
        # The worker hits FreeLLMAPI "All models exhausted" (~60s reset hint) and exits 75; its
        # CLI epilogue settles the dispatcher's receipt when the feature exists.
        spawned.append(task.id)
        if qa is not None:
            qa.settle(getattr(task, "_quota_admission_receipt", None), result={
                "failed": True, "failure_reason": "rate_limit", "failure_resets_at": clock[0] + 60})
        return FAKE_PID + len(spawned)

    kb.init_db()
    per_tick = []
    with kbc.connect() as conn:
        for name in profiles:
            for i in range(3):
                kb.create_task(conn, title=f"{name} {i}", assignee=name)
        for _ in range(12):  # one hour of 5-minute ticks
            before = len(spawned)
            kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
            per_tick.append(len(spawned) - before)
            clock[0] += TICK

    # 12 ready tasks, pool exhausted for the whole hour: the first worker closes the domain, one
    # recovery probe follows the 60s hint, and each failed probe floors the hold at cooldown
    # (1500s = 5 ticks): 4 spawns in the hour instead of one per task per cooldown window.
    assert per_tick == [1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], f"doomed spawns per tick: {per_tick}"


def test_malformed_quota_block_holds_only_its_own_profile(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    atomic_config_write(home / "config.yaml", {})
    good = {"enabled": True, "state_file": str(tmp_path / "q.json"), "default_domain": "free-pool"}
    bad = dict(good, state_file="relative/q.json")  # policy_for raises ValueError
    for name, block in (("good", good), ("bad", bad)):
        (home / "profiles" / name).mkdir(parents=True)
        atomic_config_write(home / "profiles" / name / "config.yaml",
                            {"model": {"provider": "x", "default": "x"}, "quota_admission": block})
    spawned = []
    kb.init_db()
    with kbc.connect() as conn:
        kb.create_task(conn, title="good", assignee="good")
        kb.create_task(conn, title="bad", assignee="bad")
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, w, board=None: spawned.append(t.assignee) or FAKE_PID,
                                reconcile_orphans=False)
    assert spawned == ["good"]
    assert [r for _t, r in res.respawn_guarded] == ["quota_admission_config_invalid"]
