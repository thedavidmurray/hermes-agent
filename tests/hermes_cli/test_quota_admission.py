"""Shared quota state is durable, fenced, and admits one recovery probe."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from hermes_cli import quota_admission as q
from hermes_cli.config import atomic_config_write
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_shared_hold_restart_concurrency_and_fenced_recovery(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q, "time", SimpleNamespace(time=lambda: clock[0]))
    policy = q.Policy(tmp_path / "shared.json", "subscription", 30, 10)
    assert q.admit(policy, reserve=False).allowed
    assert not policy.path.exists()
    one = q.admit(policy).receipt
    stale = q.admit(policy).receipt
    q.settle(one, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 1100})
    persisted = policy.path.read_bytes()
    assert q.admit(policy, reserve=False).retry_at == 1100
    assert policy.path.read_bytes() == persisted
    assert q.admit(policy).retry_at == 1100
    assert q.admit(q.Policy(policy.path, "other", 30, 10)).allowed
    # Independent interpreters share the persisted state and its native file lock.
    code = '''import json, sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict
from hermes_cli import quota_admission as q
q.time = SimpleNamespace(time=lambda: 1200.0)
print(json.dumps(asdict(q.admit(q.Policy(Path(sys.argv[1]), "subscription", 30, 10)))))
'''
    def probe(_):
        result = subprocess.run([sys.executable, "-c", code, str(policy.path)], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(probe, range(6)))
    winners = [a for a in outcomes if a["allowed"]]
    assert len(winners) == 1
    clock[0] = 1200
    probe_receipt = winners[0]["receipt"]
    q.settle(stale, result={"completed": True})
    assert not q.admit(policy).allowed
    q.settle(probe_receipt, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 2000})
    assert q.admit(policy).retry_at == 2000
    clock[0] = 2001
    recovery = q.admit(policy).receipt
    clock[0] += 20  # the reserving process remains live; bounded expiry still wins
    replacement = q.admit(policy).receipt
    q.settle(recovery, result={"completed": True})
    assert not q.admit(policy).allowed
    q.settle(replacement, result={"completed": True})
    assert q.admit(policy).allowed
    q.settle(stale, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 9000})
    assert q.admit(policy).allowed
    # An abandoned ownerless reservation can be reclaimed after restart; old token cannot settle it.
    q.settle(q.admit(policy).receipt, result={"failed": True, "failure_reason": "rate_limit"})
    clock[0] += 31
    abandoned = q.admit(policy).receipt
    clock[0] += 11
    replacement = q.admit(policy).receipt
    q.settle(abandoned, result={"completed": True})
    assert not q.admit(policy).allowed
    q.settle(replacement, result={"failed": True, "failure_reason": "timeout"})
    assert q.admit(policy).retry_at == clock[0] + policy.cooldown


def test_explicit_profile_config_typed_evidence_and_corrupt_state(tmp_path, monkeypatch):
    from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE
    homes = [tmp_path / name for name in ("a", "b")]
    for home, domain in zip(homes, ("shared", "independent")):
        home.mkdir()
        atomic_config_write(home / "config.yaml", {"model": {"provider": "openai-codex"}, "quota_admission": {
            "enabled": True, "state_file": str(tmp_path / "state.json"),
            "provider_domains": {"openai-codex": domain}, "cooldown_seconds": 45,
        }})
    for index in (0, 1, 0):
        token = set_hermes_home_override(str(homes[index]))
        try:
            policy = q.policy_for()
            assert policy.domain == ("shared" if index == 0 else "independent")
            if index == 0 and not policy.path.exists():
                receipt = q.admit(policy).receipt
                q.settle(receipt, error=AuthError("quota", code=CODEX_RATE_LIMITED_CODE, retry_after=300))
            assert q.admit(policy).allowed is (index == 1)
            assert q.policy_for(provider="unmapped") is None
        finally:
            reset_hermes_home_override(token)
    assert q.policy_for(config={}) is None
    assert q.failure_evidence(error=RuntimeError("HTTP 429 retry after 100s")) == (False, None)
    assert q.failure_evidence(result={"completed": True, "final_response": "rate_limit retry after 100s"}) == (False, None)
    assert q.failure_evidence(result={"failed": True, "failure_reason": "timeout"}) == (False, None)
    path = tmp_path / "bad.json"
    path.write_text("broken")
    assert not q.admit(q.Policy(path, "shared", 20, 20)).allowed
    assert path.read_text() == "broken"


def test_failed_recovery_probe_holds_at_least_cooldown(tmp_path, monkeypatch):
    """A pooled gateway (FreeLLMAPI "All models exhausted") reports its shortest per-minute
    window as the reset (~60s) while the pool stays exhausted for hours. The first failure
    honours that hint; a recovery probe failing again proves it stale, so the hold is floored
    at cooldown_seconds instead of admitting one doomed probe per dispatch tick."""
    clock = [1000.0]
    monkeypatch.setattr(q, "time", SimpleNamespace(time=lambda: clock[0]))
    policy = q.Policy(tmp_path / "shared.json", "free-pool", 1800, 600)
    first = q.admit(policy).receipt
    q.settle(first, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 1060})
    assert q.admit(policy, reserve=False).retry_at == 1060  # first failure: provider hint wins
    clock[0] = 1300  # next dispatch tick
    probe = q.admit(policy).receipt
    assert probe["probe"]
    clock[0] = 1350
    q.settle(probe, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 1410})
    assert q.admit(policy, reserve=False).retry_at == 1350 + 1800
    # A longer provider hint still wins over the floor.
    clock[0] = 3200
    probe = q.admit(policy).receipt
    q.settle(probe, result={"failed": True, "failure_reason": "rate_limit", "failure_resets_at": 9000})
    assert q.admit(policy, reserve=False).retry_at == 9000
    # A successful probe still reopens the domain at once.
    clock[0] = 9001
    q.settle(q.admit(policy).receipt, result={"completed": True})
    assert q.admit(policy, reserve=False).allowed
