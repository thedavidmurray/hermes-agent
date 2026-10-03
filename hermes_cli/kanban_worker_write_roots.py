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
``write_file`` / ``patch`` outside the roots it lists. The dispatcher now sets
it per worker to:

* the task workspace (scratch dir, ``dir:<path>`` or the task's worktree),
* the task's attachments dir (``kanban_attach`` / handoff files),
* the Hermes root (skills, memory, profile caches; the documented default),
* ``kanban.worker_write_roots`` (operator extras: a vault, a ``.runtime`` dir).

A task that must edit a repo gets an explicit ``dir:`` or ``worktree``
workspace, which is then its write root. ``kanban.worker_write_sandbox: false``
turns the pin off, and an operator-set ``HERMES_WRITE_SAFE_ROOT`` in the
dispatcher's own environment is left untouched. Defense-in-depth, not a
boundary: the terminal tool can still write anywhere.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, MutableMapping, Optional

SAFE_ROOT_ENV = "HERMES_WRITE_SAFE_ROOT"
SANDBOX_KEY = "worker_write_sandbox"
EXTRA_ROOTS_KEY = "worker_write_roots"


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


def worker_write_roots(
    task: Any, workspace: Optional[str], *, board: Optional[str] = None,
    kanban_cfg: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Absolute dirs a worker for ``task`` may write under, deduplicated, in order."""
    cfg = _kanban_cfg() if kanban_cfg is None else kanban_cfg
    roots: list[str] = []
    if workspace and os.path.isabs(workspace):
        roots.append(workspace)
    from hermes_cli import kanban_db as _kb

    roots.append(str(_kb.task_attachments_dir(task.id, board=board)))
    root = _hermes_root()
    if root:
        roots.append(root)
    extras = cfg.get(EXTRA_ROOTS_KEY) or []
    if isinstance(extras, str):
        extras = [extras]
    roots.extend(os.path.expanduser(str(e)) for e in extras if e)
    return list(dict.fromkeys(roots))


def apply_worker_write_roots(
    env: MutableMapping[str, str], task: Any, workspace: Optional[str], *, board: Optional[str] = None,
) -> None:
    """Set ``HERMES_WRITE_SAFE_ROOT`` in a worker's ``env`` unless the operator already did
    or ``kanban.worker_write_sandbox`` is false."""
    if env.get(SAFE_ROOT_ENV):
        return
    cfg = _kanban_cfg()
    if not cfg.get(SANDBOX_KEY, True):
        return
    env[SAFE_ROOT_ENV] = os.pathsep.join(worker_write_roots(task, workspace, board=board, kanban_cfg=cfg))
