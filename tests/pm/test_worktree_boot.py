"""Worktree workers borrow committed dependencies, never the parent's write identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pm import environments as envs

pytestmark = pytest.mark.platforms("any")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.hooksPath=", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _commit_environment(home: Path, root: Path, name: str = "generation") -> Path:
    state = home / "installs" / envs.install_key(root)
    generation = state / "environments" / name / "venv"
    generation.mkdir(parents=True)
    (generation.parent / ".lease-managed").touch()
    (generation / "pyvenv.cfg").write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n", encoding="utf-8")
    envs.site_packages(generation).mkdir(parents=True)
    (state / "facts.json").write_text(json.dumps({
        "packages": {"venv": {"environment": str(generation)}}}), encoding="utf-8")
    return generation.resolve()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    import hermes_constants

    owner = tmp_path / "owner"
    root = owner / "hermes-agent"
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--allow-empty", "-m", "fixture")
    worktree = tmp_path / "worktree with spaces"
    _git(root, "worktree", "add", "--detach", str(worktree))
    home = tmp_path / "task-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(owner / "tools"))
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: owner)
    generation = _commit_environment(owner, root)
    return owner, root, worktree, home, generation


def test_worktree_selects_parent_generation_without_sharing_write_identity(layout):
    _owner, root, worktree, home, generation = layout
    assert envs.install_key(worktree) != envs.install_key(root)
    assert envs.install_state_dir(worktree) == home / "installs" / envs.install_key(worktree)
    assert envs.runtime_facts_path(worktree).parent == envs.install_state_dir(worktree)
    assert envs.committed_venv(worktree) == generation
    assert envs.selected_venv(worktree) == generation
    assert not envs.install_state_dir(worktree).exists()


def test_worktree_store_python_boot_leases_parent_without_recovery(layout, monkeypatch):
    from hermes_cli import runtime_state

    owner, root, worktree, _home, generation = layout
    monkeypatch.setattr(sys, "prefix", str(owner / "tools" / "python"))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(runtime_state, "recover_publication",
                        lambda *_: pytest.fail("borrower recovered owner's publication"))
    facts = owner / "installs" / envs.install_key(root) / "facts.json"
    before = facts.read_bytes()
    envs.activate_dependencies(worktree)
    assert sys.path[:2] == [str(worktree.resolve()), str(envs.site_packages(generation))]
    assert facts.read_bytes() == before
    assert list((generation.parent / ".leases").glob("*"))
    assert not envs.install_state_dir(worktree).exists()
    assert not (facts.parent / ".install.lock").exists()


def test_fresh_non_worktree_still_refuses_store_python(layout, monkeypatch):
    owner, _root, _worktree, home, _generation = layout
    fresh = home / "fresh-install"
    fresh.mkdir()
    monkeypatch.setattr(sys, "prefix", str(owner / "tools" / "python"))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert envs.committed_venv(fresh) is None
    with pytest.raises(RuntimeError, match="no dependency environment is committed"):
        envs.activate_dependencies(fresh)


def test_worktree_own_committed_environment_wins(layout):
    _owner, _root, worktree, home, _generation = layout
    own = _commit_environment(home, worktree, "own")
    assert envs.committed_venv(worktree) == own


@pytest.mark.parametrize("contents", ["{", "{}", '{"packages":{"venv":{"environment":"/escape"}}}'])
def test_worktree_damaged_or_empty_own_record_never_falls_back(layout, contents):
    _owner, _root, worktree, _home, _generation = layout
    path = envs.runtime_facts_path(worktree)
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    if contents == "{}":
        assert envs.committed_venv(worktree) is None
    else:
        with pytest.raises(RuntimeError):
            envs.committed_venv(worktree)


def test_parent_without_committed_record_is_not_a_fallback(layout):
    owner, root, worktree, _home, _generation = layout
    (owner / "installs" / envs.install_key(root) / "facts.json").unlink()
    assert envs.committed_venv(worktree) is None


def test_parent_generation_outside_its_install_is_rejected(layout):
    owner, root, worktree, home, _generation = layout
    facts = owner / "installs" / envs.install_key(root) / "facts.json"
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(home)}}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing or outside"):
        envs.committed_venv(worktree)


def test_unregistered_git_pointer_cannot_borrow(layout):
    _owner, _root, worktree, home, _generation = layout
    impostor = home / "impostor"
    impostor.mkdir()
    (impostor / ".git").write_bytes((worktree / ".git").read_bytes())
    assert envs.committed_venv(impostor) is None


def test_active_home_parent_generation_can_be_borrowed(layout):
    _owner, root, worktree, home, _generation = layout
    own_parent = _commit_environment(home, root, "task-parent")
    assert envs.committed_venv(worktree) == own_parent


def test_real_child_boots_with_per_task_home(layout):
    owner, _root, worktree, home, _generation = layout
    code = '''
import os, sys
from pathlib import Path
import hermes_constants
from pm import environments as e
owner, worktree = map(Path, sys.argv[1:])
hermes_constants._get_platform_default_hermes_home = lambda: owner
sys.prefix = sys.base_prefix = str(owner / 'tools' / 'python')
e.activate_dependencies(worktree)
assert sys.path[0] == str(worktree.resolve())
print('WORKTREE_BOOT_OK', e.committed_venv(worktree))
'''
    child_env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home)}
    child = subprocess.run([sys.executable, "-c", code, str(owner), str(worktree)],
                           env=child_env, capture_output=True, text=True, timeout=30, check=False)
    assert child.returncode == 0, child.stdout + child.stderr
    assert "WORKTREE_BOOT_OK" in child.stdout
    assert not envs.install_state_dir(worktree).exists()
