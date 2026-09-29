"""Native cron and Kanban use the same configured admission domain."""
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron import scheduler as sched
from cron.jobs import create_job, get_job
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli import quota_admission as q
from hermes_cli.config import atomic_config_write


def _cron(job, home, result, deliveries):
    with patch.object(sched, "_hermes_home", home), \
         patch.object(sched, "_launch_external_cron_worker", return_value=False), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
             "provider": "openai-codex", "api_key": "test", "base_url": "https://unused.invalid/v1"}), \
         patch.object(sched, "_deliver_result", side_effect=lambda *a, **kw: deliveries.append(a[1])), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = result
        assert sched.run_one_job(dict(job))
        return agent_cls.return_value.run_conversation.call_count


def test_cron_failure_holds_sibling_jobs_and_kanban_then_one_worker_reopens(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {"model": {"provider": "openai-codex", "default": "test"}, "quota_admission": {
        "enabled": True, "default_domain": "subscription", "cooldown_seconds": 60,
        "provider_domains": {"independent": "separate"}, "probe_lease_seconds": 120,
    }}
    atomic_config_write(home / "config.yaml", cfg)
    clock = [datetime.now(timezone.utc).timestamp()]
    monkeypatch.setattr(q, "time", SimpleNamespace(time=lambda: clock[0]))
    first = create_job("first", "every 1m", deliver="local")
    second = create_job("second", "every 1m", deliver="local")
    once = create_job("one shot", "1m", deliver="local")
    deliveries = []
    assert _cron(first, home, {"failed": True, "failure_reason": "rate_limit",
        "failure_resets_at": clock[0] + 300, "final_response": "Provider unavailable"}, deliveries) == 1
    assert len(deliveries) == 1
    for job in (second, once):
        before = get_job(job["id"])
        assert _cron(before, home, {"completed": True}, deliveries) == 0
        after = get_job(job["id"])
        assert after.get("last_run_at") == before.get("last_run_at")
        assert after.get("repeat") == before.get("repeat")
        assert datetime.fromisoformat(after["next_run_at"]).timestamp() >= clock[0] + 300
    assert len(deliveries) == 1
    no_agent = create_job(None, "every 1m", script="watchdog.py", no_agent=True, deliver="local")
    with patch.object(sched, "_run_job_script_with_claim_heartbeat", return_value=(True, "[SILENT]")) as script:
        assert _cron(no_agent, home, {}, deliveries) == 0
        script.assert_called_once()
    assert get_job(no_agent["id"])["last_status"] == "ok"
    assert len(deliveries) == 1
    kb.init_db()
    spawned = []
    def spawn(task, workspace, board=None):
        spawned.append(task)
        return None
    with kbc.connect() as conn:
        tasks = [kb.create_task(conn, title=f"work {i}", assignee="default") for i in range(2)]
        outcome = kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert not spawned
        assert {tid for tid, why in outcome.respawn_guarded if why == "provider_quota_hold"} == set(tasks)
        independent = kb.create_task(conn, title="separate provider", assignee="default",
            model_override="test", provider_override="independent")
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert [task.id for task in spawned] == [independent]
        clock[0] += 301
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
        assert len(spawned) == 2
        # Native worker completion consumes its receipt, and only typed success releases the probe.
        cli = SimpleNamespace(_quota_admission_receipt=spawned[1]._quota_admission_receipt)
        q.finish_worker(cli, {"completed": True, "final_response": "done"})
        assert cli._quota_admission_receipt is None
        assert q.admit(q.policy_for(), reserve=False).allowed
    assert _cron(get_job(second["id"]), home, {"completed": True, "final_response": "done"}, deliveries) == 1


def test_native_worker_receipt_and_typed_startup_failure(tmp_path, monkeypatch):
    """The real spawn environment hands the fenced receipt to the one-shot CLI epilogue."""
    from pathlib import Path
    from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    atomic_config_write(home / "config.yaml", {"quota_admission": {"enabled": False}})
    worker_home = home / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    atomic_config_write(worker_home / "config.yaml", {"model": {"provider": "anthropic"}, "quota_admission": {
        "enabled": True, "provider_domains": {"anthropic": "shared"}, "cooldown_seconds": 60,
    }})
    kb.init_db()
    captured = []
    def spawn(task, workspace, board=None):
        # Run the native env construction while replacing only actual process creation.
        def popen(*args, **kwargs):
            captured.append(kwargs["env"])
            kwargs["stdout"].close()
            return SimpleNamespace(pid=None)
        with patch.object(kbd.subprocess, "Popen", side_effect=popen):
            return kbd._default_spawn(task, workspace, board=board)
    with kbc.connect() as conn:
        kb.create_task(conn, title="worker", assignee="worker")
        kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
    assert len(captured) == 1
    receipt = json.loads(captured[0][q.WORKER_RECEIPT_ENV])
    assert receipt["path"] == str(worker_home / "quota-admission.json")
    assert receipt["domain"] == "shared"
    assert captured[0]["HERMES_HOME"] == str(worker_home)
    monkeypatch.setenv(q.WORKER_RECEIPT_ENV, captured[0][q.WORKER_RECEIPT_ENV])
    monkeypatch.setenv("HERMES_KANBAN_TASK", captured[0]["HERMES_KANBAN_TASK"])
    import cli as cli_module
    monkeypatch.setattr(cli_module, "_should_seed_interactive", lambda *a, **kw: False)
    monkeypatch.setattr(cli_module, "_collect_query_images", lambda query, image: (query, []))
    monkeypatch.setattr(cli_module, "_collect_kanban_task_images", lambda images: [])
    monkeypatch.setattr(cli_module, "_finalize_single_query", lambda cli: None)
    cli = SimpleNamespace(_claim_active_session=lambda *a, **kw: True,
        _ensure_runtime_credentials=lambda: False, _credentials_rate_limited=True,
        _credentials_error=AuthError("quota", code=CODEX_RATE_LIMITED_CODE, retry_after=800))
    with pytest.raises(SystemExit) as exit_info:
        cli_module._run_single_query_mode(cli, "work", None, True, True)
    assert exit_info.value.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert q.WORKER_RECEIPT_ENV not in os.environ
    assert q.admit(q.Policy(worker_home / "quota-admission.json", "shared", 60, 120)).retry_at >= datetime.now(timezone.utc).timestamp() + 798
    # A content-only failure and EX_TEMPFAIL's other reasons cannot close a fresh domain.
    other = q.Policy(home / "quota-admission.json", "other", 60, 120)
    q.settle(q.admit(other).receipt, result={"failed": True, "failure_reason": "billing",
        "final_response": "rate_limit retry after 9999s"})
    assert q.admit(other).allowed


def test_free_pool_domain_stops_per_tick_respawn_churn(tmp_path, monkeypatch):
    """Worker churn 2026-09-28: every FreeLLMAPI-terminated profile shares one ``free-pool``
    domain. The pool 429s with a ~60s reset hint while staying exhausted for hours; before the
    probe floor the dispatcher admitted one doomed probe every tick (5 min)."""
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")  # isolate the domain hold
    atomic_config_write(home / "config.yaml", {"quota_admission": {"enabled": False}})
    shared = {"enabled": True, "state_file": str(tmp_path / "shared-quota.json"),
              "provider_domains": {"custom:runtime-local": "free-pool", "custom:runtime-nvidia": "free-pool"},
              "cooldown_seconds": 1800, "probe_lease_seconds": 600}
    for name, provider in (("verifier", "custom:runtime-local"), ("anomaly", "custom:runtime-nvidia")):
        (home / "profiles" / name).mkdir(parents=True)
        atomic_config_write(home / "profiles" / name / "config.yaml",
                            {"model": {"provider": provider, "default": "x"}, "quota_admission": shared})
    clock = [datetime.now(timezone.utc).timestamp()]
    monkeypatch.setattr(q, "time", SimpleNamespace(time=lambda: clock[0]))
    kb.init_db()
    spawned = []

    def spawn(task, workspace, board=None):
        # The worker 429s ~50s in and its CLI epilogue settles the receipt (exit 75).
        spawned.append(task.id)
        q.settle(task._quota_admission_receipt, result={
            "failed": True, "failure_reason": "rate_limit", "failure_resets_at": clock[0] + 60})
        return None

    per_tick = []
    with kbc.connect() as conn:
        for i in range(4):
            kb.create_task(conn, title=f"v{i}", assignee="verifier")
            kb.create_task(conn, title=f"a{i}", assignee="anomaly")
        for _tick in range(8):  # 8 dispatcher ticks, 300s apart = 40 minutes
            before = len(spawned)
            kbd.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False)
            per_tick.append(len(spawned) - before)
            for tid in spawned[before:]:  # the dispatcher's sweep re-queues exit-75 runs as ready
                conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                             "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,))
                conn.commit()
            clock[0] += 300
    # First spawn closes the shared domain for the 60s hint, one recovery probe on the next
    # tick, then its failure holds the whole pool for cooldown (1800s = 6 ticks).
    assert per_tick[0] == 1
    assert per_tick[1] == 1
    assert sum(per_tick[2:7]) == 0
    assert per_tick[7] == 1
