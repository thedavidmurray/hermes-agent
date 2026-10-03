"""Tests: kanban worker spawn pins HERMES_WRITE_SAFE_ROOT to the task's own ground.

Scratch-workspace workers were writing completion artifacts straight into a
shared repo checkout named by skills and profile prompts, not by the task
(see hermes_cli/kanban_worker_write_roots.py). ``_default_spawn`` sets
``HERMES_WRITE_SAFE_ROOT`` to workspace + attachments dir + profile scratch dir
+ ``kanban.worker_write_roots``, which ``agent/file_safety.py`` enforces for
``write_file`` / ``patch``.

t_a71b270c: the first cut also whitelisted the whole Hermes root, which holds
the installed source, every config and every board DB — the pin allowed
exactly the writes that brick the swarm. The regressions below build a real
Hermes-shaped tree and drive the env ``_default_spawn`` produces through the
real ``write_file`` / ``patch`` tool handlers.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest


def _make_task(kb, *, assignee: str = "w", workspace_kind: str = "scratch", task_id: str = "t_roots"):
    return kb.Task(
        id=task_id,
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


def _home(tmp_path, monkeypatch, kanban_yaml: str = ""):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n" + kanban_yaml, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return root


def _spawn_env(kb, monkeypatch, workspace: str, *, task_id: str = "t_roots") -> dict:
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured: dict = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    # Scoped: the file tools' shell backend needs the real Popen afterwards.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "Popen", fake_popen)
        kbd._default_spawn(_make_task(kb, task_id=task_id), workspace)
    return captured["env"]


def _roots(env: dict) -> list[str]:
    return env["HERMES_WRITE_SAFE_ROOT"].split(os.pathsep)


def test_worker_gets_workspace_attachments_and_scratch_but_never_hermes_root(monkeypatch, tmp_path):
    root = _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db as kb
    import hermes_constants

    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _spawn_env(kb, monkeypatch, str(workspace))
    roots = _roots(env)

    assert roots[0] == str(workspace)
    assert str(kb.task_attachments_dir("t_roots")) in roots
    assert os.path.join(env["HERMES_HOME"], "cache", "scratch") in roots
    hermes_root = os.path.realpath(str(hermes_constants.get_default_hermes_root()))
    assert hermes_root == os.path.realpath(str(root))
    real_roots = [os.path.realpath(r) for r in roots]
    assert hermes_root not in real_roots
    assert os.path.realpath(env["HERMES_HOME"]) not in real_roots
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


def test_extra_root_or_workspace_containing_hermes_root_is_dropped(monkeypatch, tmp_path):
    """An operator extra (or a dir: workspace) that equals or contains the Hermes root
    would silently re-open installed source / configs / board DBs; it is dropped."""
    root = _home(tmp_path, monkeypatch)
    from hermes_cli import kanban_worker_write_roots as wr
    from hermes_cli import kanban_db as kb

    vault = tmp_path / "vault"
    cfg = {"worker_write_roots": [str(root), str(tmp_path), str(vault), "relative/dir"]}
    roots = wr.worker_write_roots(
        _make_task(kb), str(tmp_path), kanban_cfg=cfg, profile_home=str(root / "profiles" / "w"),
    )
    assert str(root) not in roots
    assert str(tmp_path) not in roots
    assert "relative/dir" not in roots
    assert str(vault) in roots

    # The profile home itself is an umbrella too (config.yaml, .env, state.db live there).
    cfg = {"worker_write_roots": [str(root / "profiles" / "w")]}
    roots = wr.worker_write_roots(
        _make_task(kb), None, kanban_cfg=cfg, profile_home=str(root / "profiles" / "w"),
    )
    assert str(root / "profiles" / "w") not in roots


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
    assert "the Hermes home" not in KANBAN_GUIDANCE


# ---------------------------------------------------------------------------
# End-to-end: real Hermes-shaped tree, _default_spawn env, real tool handlers.
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_tree(tmp_path, monkeypatch):
    """A Hermes root laid out like a live install, with the worker's scratch
    workspace at the real board path."""
    real_tmp = tmp_path.resolve()
    root = _home(real_tmp, monkeypatch)
    board = root / "kanban" / "boards" / "edgeless"
    files = {
        "installed_source": root / "hermes-agent" / "run_agent.py",
        "installed_module": root / "hermes-agent" / "hermes_cli" / "kanban_db_dispatch.py",
        "root_config": root / "config.yaml",
        "profile_config": root / "profiles" / "w" / "config.yaml",
        "other_profile_config": root / "profiles" / "other" / "config.yaml",
        "other_profile_skill": root / "profiles" / "other" / "skills" / "x" / "SKILL.md",
        "board_db": board / "kanban.db",
        "default_board_db": root / "kanban.db",
        "other_workspace": board / "workspaces" / "t_other" / "notes.md",
        "unrelated_repo": real_tmp / "claude-projects" / "hermes-agent" / "run_agent.py",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("ORIGINAL\n", encoding="utf-8")
    workspace = board / "workspaces" / "t_e2e"
    workspace.mkdir(parents=True)
    return {"root": root, "workspace": workspace, "files": files}


def _as_worker(monkeypatch, env: dict) -> None:
    """Make this process look like the spawned worker for the file tools."""
    for key in ("HERMES_HOME", "HERMES_WRITE_SAFE_ROOT", "TERMINAL_CWD"):
        if env.get(key):
            monkeypatch.setenv(key, env[key])
    import tools.file_tools as ft

    ft.clear_file_ops_cache()


@pytest.mark.parametrize("handler", ["write_file", "patch"])
def test_spawned_worker_handlers_deny_control_state_and_allow_own_ground(
    monkeypatch, hermes_tree, handler,
):
    from hermes_cli import kanban_db as kb
    from tools import file_tools as ft

    workspace = hermes_tree["workspace"]
    env = _spawn_env(kb, monkeypatch, str(workspace), task_id="t_e2e")
    attachments = kb.task_attachments_dir("t_e2e")
    _as_worker(monkeypatch, env)
    tid = f"wr-e2e-{handler}"

    def call(path: str, old: str = "ORIGINAL") -> dict:
        if handler == "write_file":
            ft._handle_read_file({"path": path}, task_id=tid)  # so staleness never masks the pin
            raw = ft._handle_write_file({"path": path, "content": "PWNED\n"}, task_id=tid)
        else:
            raw = ft._handle_patch(
                {"mode": "replace", "path": path, "old_string": old, "new_string": "PWNED"},
                task_id=tid,
            )
        return json.loads(raw)

    for name, path in hermes_tree["files"].items():
        before = path.read_text(encoding="utf-8")
        # old_string is real file content, so a patch denial cannot be a "no match" error.
        result = call(str(path), old=before.splitlines()[0])
        assert result.get("error"), f"{handler} must deny {name} ({path}): {result}"
        assert "denied" in result["error"].lower() or "refusing" in result["error"].lower(), (
            f"{handler} on {name} failed for the wrong reason: {result}")
        assert path.read_text(encoding="utf-8") == before, f"{name} was modified"

    # Own ground stays writable: workspace, attachments dir, profile scratch dir.
    scratch = os.path.join(env["HERMES_HOME"], "cache", "scratch")
    for own in (workspace / "out.md", attachments / "handoff.md", os.path.join(scratch, "probe.txt")):
        own = str(own)
        os.makedirs(os.path.dirname(own), exist_ok=True)
        with open(own, "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL\n")
        if handler == "write_file":
            ft._handle_read_file({"path": own}, task_id=tid)
        result = call(own)
        assert not result.get("error"), f"{handler} must allow {own}: {result}"
        with open(own, encoding="utf-8") as fh:
            assert "PWNED" in fh.read()
