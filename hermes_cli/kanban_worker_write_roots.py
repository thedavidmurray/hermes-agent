"""Pin a kanban worker's file-tool writes to its own ground.

Workers already start with ``cwd=workspace`` and ``TERMINAL_CWD=workspace``,
and the kanban guidance says "don't modify files outside it unless the task
explicitly asks". That sentence does not hold. In the week to 2026-10-03,
scratch-workspace workers on the ``edgeless`` board wrote their completion
artifacts straight into the shared repo checkout: ``t_2d3c5e6d`` edited
``scripts/lib/rss_triage_*.py`` and ``triage_core.py``, ``t_5d6d9eb6`` created
four files under ``scripts/``, ``t_eed8f0ac`` wrote ``reports/number-guard-
dry-runs/``, ``t_098919a1`` wrote ``marimo-notebooks/``. None of those task
bodies named the checkout; the paths came from skills and profile prompts
that hard-code it (129 skill files, several hundred profile files). Most of
the 500 dirty entries cleaned off that checkout on 2026-10-01 were worker
output of this kind.

``HERMES_WRITE_SAFE_ROOT`` (``agent/file_safety.py``) already hard-blocks
``write_file`` / ``patch`` outside the roots it lists. The dispatcher sets it
per worker to the MINIMUM task-owned ground:

* the task workspace (scratch dir, ``dir:<path>`` or the task's worktree),
* the task's attachments dir (``kanban_attach`` / handoff files),
* the owning profile's scratch dir (``<profile home>/cache/scratch``, where
  workers are told to put temp files and probes),
* ``kanban.worker_write_roots`` (operator extras: a vault, a ``.runtime`` dir).

The Hermes root is deliberately NOT a root (t_a71b270c). On a typical install
it holds the installed ``hermes-agent`` source the gateway imports, every
profile's ``config.yaml``, every board's ``kanban.db`` and every other task's
workspace; whitelisting it made the pin a no-op for exactly the paths whose
corruption bricks the swarm. For the same reason any root that equals or
contains the Hermes root or the owning profile home (a ``dir:~/.hermes``
workspace, an over-broad operator extra) is dropped. Skills and memory keep
working: ``skill_manage`` and the memory tool do not route through
``file_safety``'s safe-root check.

A task that must edit a repo gets an explicit ``dir:`` or ``worktree``
workspace, which is then its write root. ``kanban.worker_write_sandbox: false``
turns the pin off, and an operator-set ``HERMES_WRITE_SAFE_ROOT`` in the
dispatcher's own environment is left untouched.

NOT a filesystem sandbox: the terminal tool (and anything it launches) runs as
the same OS user and can still write anywhere, including every path denied
here. The pin gives file tools a clear refusal plus an audit trail, nothing more.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, MutableMapping, Optional

SAFE_ROOT_ENV = "HERMES_WRITE_SAFE_ROOT"
SANDBOX_KEY = "worker_write_sandbox"
EXTRA_ROOTS_KEY = "worker_write_roots"

_log = logging.getLogger(__name__)


def _kanban_cfg() -> Mapping[str, Any]:
    """The live ``kanban:`` block, or ``{}`` when config cannot be read."""
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban")
    except Exception:
        return {}
    return kcfg if isinstance(kcfg, Mapping) else {}


def _hermes_root() -> Optional[str]:
    try:
        import hermes_constants

        return str(hermes_constants.get_default_hermes_root())
    except Exception:
        return None


def _contains(outer: str, inner: str) -> bool:
    """True when ``inner`` equals ``outer`` or lies below it (both realpath'd)."""
    o = os.path.realpath(os.path.expanduser(outer))
    i = os.path.realpath(os.path.expanduser(inner))
    return i == o or i.startswith(o.rstrip(os.sep) + os.sep)


def worker_write_roots(
    task: Any, workspace: Optional[str], *, board: Optional[str] = None,
    kanban_cfg: Optional[Mapping[str, Any]] = None, profile_home: Optional[str] = None,
) -> list[str]:
    """Absolute dirs a worker for ``task`` may write under, deduplicated, in order.

    Never the Hermes root, the profile home, or any dir that contains either.
    """
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    umbrellas = [p for p in (_hermes_root(), profile_home) if p]
    candidates: list[tuple[str, str]] = []
    if workspace and os.path.isabs(workspace):
        candidates.append(("workspace", workspace))
    from hermes_cli import kanban_db as _kb

    candidates.append(("attachments", str(_kb.task_attachments_dir(task.id, board=board))))
    if profile_home and os.path.isabs(profile_home):
        candidates.append(("scratch", os.path.join(profile_home, "cache", "scratch")))
    extras = cfg.get(EXTRA_ROOTS_KEY) or []
    if isinstance(extras, str):
        extras = [extras]
    for extra in extras:
        if not extra:
            continue
        path = os.path.expanduser(str(extra))
        if not os.path.isabs(path):
            _log.warning("kanban.%s: ignoring relative root %r", EXTRA_ROOTS_KEY, extra)
            continue
        candidates.append((f"kanban.{EXTRA_ROOTS_KEY}", path))

    roots: list[str] = []
    for label, path in candidates:
        if any(_contains(path, u) for u in umbrellas):
            _log.warning(
                "kanban worker write roots: dropping %s %r — it contains the Hermes root or "
                "profile home (installed source, configs, board DBs)", label, path,
            )
            continue
        roots.append(path)
    return list(dict.fromkeys(roots))


def apply_worker_write_roots(
    env: MutableMapping[str, str], task: Any, workspace: Optional[str], *, board: Optional[str] = None,
) -> None:
    """Set ``HERMES_WRITE_SAFE_ROOT`` in a worker's ``env`` unless the operator already did
    or ``kanban.worker_write_sandbox`` is false. ``env["HERMES_HOME"]`` (the owning profile
    home, set by ``_default_spawn``) scopes the scratch root."""
    if env.get(SAFE_ROOT_ENV):
        return
    cfg = _kanban_cfg()
    if not cfg.get(SANDBOX_KEY, True):
        return
    roots = worker_write_roots(
        task, workspace, board=board, kanban_cfg=cfg, profile_home=env.get("HERMES_HOME") or None,
    )
    env[SAFE_ROOT_ENV] = os.pathsep.join(roots)
