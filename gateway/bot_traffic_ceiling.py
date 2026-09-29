"""Fleet-wide bot-traffic ceiling and the bot-only kill switch (``BOT_ESTOP``).

``BotLoopGuard`` is in-memory per gateway process and only cools down, so N profile gateways
replying to each other are bounded by N budgets that refill forever. Two file-based controls under
the Hermes root, shared by every gateway, sit in front of it:

* ``BOT_ESTOP`` sentinel (profile home or fleet root, same lookup as ``agent/estop.py``). While it
  exists bot-authored inbound is dropped and Discord output loses its bot ``<@id>`` wake tokens.
  Humans are unaffected. Operator: ``touch ~/.hermes/BOT_ESTOP`` / ``rm ~/.hermes/BOT_ESTOP``.
* Ceiling: ``<root>/bot_traffic.db`` counts admitted bot messages per (platform, chat) across the
  fleet over a sliding window. The message past ``max_events`` LATCHES by creating ``BOT_ESTOP`` with
  O_EXCL, so exactly one gateway logs the operator alert; nothing resumes until the file is removed.
  The alert is a log line, never a chat post, so it cannot feed the traffic it reports.

Settings: config.yaml ``gateway.bot_traffic_ceiling`` (``enabled``, ``max_events``, ``window_seconds``),
re-read on every call.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from agent import estop
from gateway.bot_loop_guard import _as_bool, _as_positive, _as_positive_int

logger = logging.getLogger(__name__)

SENTINEL_NAME = "BOT_ESTOP"
DB_NAME = "bot_traffic.db"

# Per-process "already logged this engagement" flag for the drop notice (the alert itself is O_EXCL-deduped).
_drop_logged = threading.Event()


@dataclass(frozen=True)
class CeilingSettings:
    enabled: bool = True
    max_events: int = 10
    window_seconds: float = 600.0


def settings_from_config(cfg) -> CeilingSettings:
    from hermes_cli.config import cfg_get

    block = cfg_get(cfg, "gateway", "bot_traffic_ceiling", default=None)
    defaults = CeilingSettings()
    if not isinstance(block, dict):
        return defaults
    return CeilingSettings(
        enabled=_as_bool(block.get("enabled"), defaults.enabled),
        max_events=_as_positive_int(block.get("max_events"), defaults.max_events),
        window_seconds=_as_positive(block.get("window_seconds"), defaults.window_seconds),
    )


def load_settings() -> CeilingSettings:
    try:
        from hermes_cli.config import load_config_readonly

        return settings_from_config(load_config_readonly())
    except Exception:
        return CeilingSettings()


def is_engaged() -> bool:
    """True while any ``BOT_ESTOP`` sentinel exists (fail safe on stat errors). One or two ``os.stat``."""
    return estop.is_engaged(SENTINEL_NAME)


def note_drop(source) -> None:
    """Log the first bot message dropped per engagement in this process; re-armed when the switch clears."""
    if _drop_logged.is_set():
        return
    _drop_logged.set()
    logger.warning(
        "BOT_ESTOP engaged: dropping bot-authored messages (first: %s chat %s from %s). Humans are unaffected; "
        "remove %s to resume bot admission.",
        getattr(getattr(source, "platform", None), "value", ""), getattr(source, "chat_id", ""),
        getattr(source, "user_id", ""), latch_path(),
    )


def rearm_drop_notice() -> None:
    _drop_logged.clear()


def latch_path() -> Path:
    return estop._canonical_root() / SENTINEL_NAME


def latch(reason: str) -> bool:
    """Create the fleet-root ``BOT_ESTOP``. True only for the one caller that created it."""
    path = latch_path()
    body = json.dumps({"engaged_at": datetime.now(timezone.utc).isoformat(), "reason": reason}) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return True


class BotTrafficCeiling:
    """Sliding-window count of admitted bot messages per chat, shared by every gateway via SQLite."""

    def __init__(
        self,
        settings: Callable[[], CeilingSettings] = load_settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._clock = clock

    def _db_path(self) -> Path:
        return estop._canonical_root() / DB_NAME

    def _connect(self) -> sqlite3.Connection:
        path = self._db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_events (conv TEXT NOT NULL, message_id TEXT NOT NULL, "
            "profile TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (conv, message_id, profile))"
        )
        return conn

    def admit(self, platform: str, chat_id: str, message_id: Optional[str], profile: str) -> Tuple[bool, str]:
        """Count one bot message. ``(allowed, state)``; state is ``disabled``, ``ok``, ``duplicate``,
        ``latched`` (this message crossed the ceiling) or ``error`` (counter unusable: fail closed)."""
        settings = self._settings()
        if not settings.enabled:
            return True, "disabled"
        now = self._clock()
        conv = f"{platform}:{chat_id}"
        # Each gateway process is its own profile home; without multiplex the adapter profile is "".
        profile = profile or str(estop._hermes_home())
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM bot_events WHERE ts <= ?", (now - settings.window_seconds,))
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO bot_events (conv, message_id, profile, ts) VALUES (?, ?, ?, ?)",
                    (conv, str(message_id or uuid.uuid4().hex), profile, now),
                ).rowcount
                count = conn.execute("SELECT COUNT(*) FROM bot_events WHERE conv = ?", (conv,)).fetchone()[0]
                conn.execute("COMMIT")
        except (OSError, sqlite3.Error) as exc:
            logger.error("Bot traffic ceiling counter unusable (%s); dropping bot message in %s", exc, conv)
            return False, "error"
        if not inserted:
            return True, "duplicate"
        if count <= settings.max_events:
            return True, "ok"
        reason = (f"bot traffic ceiling: {count} bot messages in {conv} within {settings.window_seconds:.0f}s "
                  f"(max {settings.max_events}); crossed on {profile}")
        try:
            created = latch(reason)
        except OSError as exc:
            logger.error("Bot traffic ceiling could not write %s (%s); dropping bot message", latch_path(), exc)
            return False, "latched"
        if created:
            logger.error(
                "BOT TRAFFIC CEILING LATCHED: %s. Every gateway now drops bot-authored messages; humans are "
                "unaffected. Investigate, then `rm %s` to re-arm.", reason, latch_path(),
            )
        return False, "latched"
