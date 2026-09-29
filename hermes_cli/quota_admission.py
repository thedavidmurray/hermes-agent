"""Opt-in, shared admission for scheduled provider work.

Domains are operator labels, never inferred account identities. Only structured
rate-limit failures close a domain; this does not estimate monetary spend.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
# Internal dispatcher-to-worker receipt, not a feature switch. Consumed before tools run.
WORKER_RECEIPT_ENV = "_HERMES_QUOTA_ADMISSION_RECEIPT"


@dataclass(frozen=True)
class Policy:
    path: Path
    domain: str
    cooldown: float
    probe_lease: float


@dataclass(frozen=True)
class Admission:
    allowed: bool
    receipt: dict | None = None
    retry_at: float | None = None
    reason: str = ""


def _positive(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"quota_admission.{name} must be positive")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"quota_admission.{name} must be positive")
    return number


def policy_for(*, provider: str | None = None, config: dict | None = None) -> Policy | None:
    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    settings = config.get("quota_admission") or {}
    if settings.get("enabled") is not True:
        return None
    model = config.get("model")
    provider = provider or (model.get("provider") if isinstance(model, dict) else None)
    domain = (settings.get("provider_domains") or {}).get(provider) or settings.get("default_domain")
    if not domain:
        return None
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("quota_admission domain must be a nonempty label")
    path = Path(settings.get("state_file") or (get_hermes_home() / "quota-admission.json")).expanduser()
    if not path.is_absolute():
        raise ValueError("quota_admission.state_file must be absolute")
    return Policy(path, domain.strip(), _positive(settings.get("cooldown_seconds", 900), "cooldown_seconds"),
                  _positive(settings.get("probe_lease_seconds", 1800), "probe_lease_seconds"))


@contextmanager
def _state(path: Path, *, write: bool = True):
    # auth's native lock is cross-process and bounded; the RLock also serializes threads.
    from hermes_cli.auth import _file_lock, fcntl, msvcrt
    if fcntl is None and msvcrt is None:
        raise OSError("quota admission requires a native file-lock backend")
    with _LOCK, _file_lock(path.with_suffix(path.suffix + ".lock"), threading.local(), 5,
                          "quota admission state lock timed out"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"version": 1, "domains": {}}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("domains"), dict):
            raise ValueError("invalid quota admission state")
        yield data
        if write:
            atomic_json_write(path, data, mode=0o600, fsync_dir=True)


def _entry(data: dict, domain: str) -> dict:
    row = data["domains"].setdefault(domain, {"generation": 0, "until": 0, "probe": None})
    if (not isinstance(row, dict) or not isinstance(row.get("generation"), int)
            or not isinstance(row.get("until"), (int, float)) or not math.isfinite(row["until"])
            or (row.get("probe") is not None and not isinstance(row["probe"], dict))):
        raise ValueError("invalid quota admission domain state")
    return row


def admit(policy: Policy | None, *, reserve: bool = True) -> Admission:
    if policy is None:
        return Admission(True)
    now = time.time()
    try:
        with _state(policy.path, write=reserve) as data:
            row = _entry(data, policy.domain)
            if row["until"] > now:
                return Admission(False, retry_at=row["until"], reason="provider_quota_hold")
            probe = row.get("probe")
            if probe and float(probe["expires"]) > now:
                return Admission(False, retry_at=max(probe["expires"], now + 60), reason="provider_quota_probe")
            if not reserve:
                return Admission(True)
            recovery = bool(row["until"] or probe)
            token = uuid.uuid4().hex
            if recovery:
                row["generation"] += 1
                row["probe"] = {"token": token, "expires": now + policy.probe_lease}
            receipt = {"path": str(policy.path), "domain": policy.domain, "generation": row["generation"],
                       "token": token, "probe": recovery, "cooldown": policy.cooldown}
            return Admission(True, receipt=receipt)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Configured admission fails closed; never discard damaged state and hammer the provider.
        logger.warning("Quota admission unavailable for %s: %s", policy.domain, exc)
        return Admission(False, retry_at=now + 60, reason="provider_quota_state_unavailable")


def _owns(row: dict, receipt: dict) -> bool:
    if row["generation"] != receipt["generation"]:
        return False
    probe = row.get("probe")
    return (bool(probe) and probe.get("token") == receipt["token"]) if receipt["probe"] else not probe


def failure_evidence(*, result: dict | None = None, error: BaseException | None = None) -> tuple[bool, float | None]:
    """A typed quota failure and its absolute reset; never inspect generated content."""
    now = time.time()
    if isinstance(result, dict) and (result.get("failed") or result.get("completed") is False):
        if result.get("failure_reason") in {"rate_limit", "upstream_rate_limit"}:
            hint = result.get("failure_resets_at")
            if isinstance(hint, (int, float)) and not isinstance(hint, bool) and math.isfinite(hint):
                return True, max(now, float(hint))
            return True, None
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    from cron.quota_hold import hold_seconds_from_failure
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, AuthError) and is_rate_limited_auth_error(error):
            seconds = hold_seconds_from_failure(error)
            return True, now + seconds if seconds is not None else None
        error = error.__cause__ or error.__context__
    return False, None


def settle(receipt: dict | None, *, result: dict | None = None, error: BaseException | None = None) -> None:
    if receipt is None:
        return
    quota, reset_at = failure_evidence(result=result, error=error)
    success = (isinstance(result, dict) and result.get("completed") is True
               and not any(result.get(k) for k in ("failed", "partial", "interrupted")))
    now = time.time()
    try:
        with _state(Path(receipt["path"])) as data:
            row = _entry(data, receipt["domain"])
            if not _owns(row, receipt):
                return
            if quota or (receipt["probe"] and not success):
                hold = reset_at if reset_at is not None else now + receipt["cooldown"]
                if receipt["probe"]:
                    # A failed recovery probe proves the provider's reset hint stale (pooled
                    # gateways report their shortest per-minute window while the pool stays
                    # exhausted): floor the hold at cooldown instead of one probe per tick.
                    hold = max(hold, now + receipt["cooldown"])
                row["until"] = max(row["until"], hold)
                row["generation"] += 1
                row["probe"] = None
            elif success and receipt["probe"]:
                row.update(until=0, probe=None, generation=row["generation"] + 1)
    except (OSError, ValueError, TypeError, KeyError):
        logger.exception("Could not persist quota admission outcome for %s", receipt.get("domain"))


def take_worker_receipt() -> dict | None:
    raw = os.environ.pop(WORKER_RECEIPT_ENV, None)
    return json.loads(raw) if raw else None


def finish_worker(cli, result: dict | None = None) -> None:
    settle(getattr(cli, "_quota_admission_receipt", None), result=result,
           error=getattr(cli, "_credentials_error", None))
    cli._quota_admission_receipt = None
