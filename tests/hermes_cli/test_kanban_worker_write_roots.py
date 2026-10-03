"""Tests: kanban worker spawn pins HERMES_WRITE_SAFE_ROOT to the task's own ground.

Scratch-workspace workers were writing completion artifacts straight into a
shared repo checkout named by skills and profile prompts, not by the task
(see hermes_cli/kanban_worker_write_roots.py). ``_default_spawn`` now sets
``HERMES_WRITE_SAFE_ROOT`` to workspace + attachments dir + Hermes root +
``kanban.worker_write_roots``, which ``agent/file_safety.py`` enforces for
``write_file`` / ``patch``.
"""

from __future__ import annotations

import os
import subprocess


def _make_task(kb, *, assignee: str = "w", workspace_kind: str = "scratch"):
    return kb.Task(
        id="t_roots",
        title="write roots",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind=workspace_kind,
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _home(tmp_path, monkeypatch, kanban_yaml: str = "") -> "os.PathLike":
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n" + kanban_yaml, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return root


def _spawn_env(kb, monkeypatch, workspace: str) -> dict:
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured: dict = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_make_task(kb), workspace)
    return captured["env"]


def _roots(env: dict) -> list[str]:
    return env["HERMES_WRITE_SAFE_ROOT"].split(os.pathsep)


def test_worker_gets_workspace_attachments_and_hermes_root(monkeypatch, tmp_path):
    _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db as kb
    import hermes_constants

    workspace = tmp_path / "ws"
    workspace.mkdir()
    roots = _roots(_spawn_env(kb, monkeypatch, str(workspace)))

    assert roots[0] == str(workspace)
    assert str(kb.task_attachments_dir("t_roots")) in roots
    assert str(hermes_constants.get_default_hermes_root()) in roots
    assert len(roots) == len(set(roots))


def test_pin_blocks_file_tool_writes_outside_the_roots(monkeypatch, tmp_path):
    """The env the dispatcher builds is what file_safety reads: a write under the
    workspace passes, a write into a repo the task never named is denied."""
    _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db as kb
    from agent import file_safety

    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = tmp_path / "shared-trunk" / "scripts"
    repo.mkdir(parents=True)
    env = _spawn_env(kb, monkeypatch, str(workspace))

    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", env["HERMES_WRITE_SAFE_ROOT"])
    assert file_safety.get_write_denied_error(str(workspace / "out.md")) is None
    denial = file_safety.get_write_denied_error(str(repo / "x.py"))
    assert denial and "HERMES_WRITE_SAFE_ROOT" in denial


def test_operator_extra_roots_are_added_and_user_expanded(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _home(tmp_path, monkeypatch, f"kanban:\n  worker_write_roots:\n    - {vault}\n    - ~/runtime-state\n")
    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()
    roots = _roots(_spawn_env(kb, monkeypatch, str(workspace)))

    assert str(vault) in roots
    assert os.path.expanduser("~/runtime-state") in roots
    assert "~/runtime-state" not in roots


def test_sandbox_off_sets_nothing(monkeypatch, tmp_path):
    _home(tmp_path, monkeypatch, "kanban:\n  worker_write_sandbox: false\n")
    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _spawn_env(kb, monkeypatch, str(workspace))

    assert "HERMES_WRITE_SAFE_ROOT" not in env


def test_operator_set_safe_root_is_left_alone(monkeypatch, tmp_path):
    """An operator sandboxing the whole gateway keeps their roots; we never widen them."""
    _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_worker_write_roots as wr
    from hermes_cli import kanban_db as kb

    env = {"HERMES_WRITE_SAFE_ROOT": "/opt/data"}
    wr.apply_worker_write_roots(env, _make_task(kb), str(tmp_path / "ws"))

    assert env["HERMES_WRITE_SAFE_ROOT"] == "/opt/data"


def test_relative_or_missing_workspace_still_pins_the_rest(monkeypatch, tmp_path):
    _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_worker_write_roots as wr
    from hermes_cli import kanban_db as kb

    roots = wr.worker_write_roots(_make_task(kb), "relative/ws", kanban_cfg={})

    assert "relative/ws" not in roots
    assert roots and roots[0] == str(kb.task_attachments_dir("t_roots"))


def test_guidance_tells_workers_not_to_route_around_the_pin():
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert "HERMES_WRITE_SAFE_ROOT" in KANBAN_GUIDANCE
    assert "--workspace dir:<path>" in KANBAN_GUIDANCE
