"""Store prewarming must not fan out machine-wide libproc scans unboundedly."""
import asyncio
import threading
from pathlib import Path

import pytest

from gateway import run_runtime


def test_prewarm_bounds_workers_and_keeps_loop_responsive(monkeypatch):
    lock = threading.Lock()
    wave_started = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    calls = []
    active = 0
    peak = 0

    def open_store(runner, home):
        nonlocal active, peak
        assert threading.get_ident() != loop_thread
        with lock:
            calls.append(home)
            active += 1
            peak = max(peak, active)
            if active >= 4:
                wave_started.set()
        try:
            assert release.wait(10), "test did not release blocked workers"
        finally:
            with lock:
                active -= 1
        if home.name == 'profile-17':
            raise RuntimeError('one unavailable store must not cancel its peers')
        return object()

    monkeypatch.setattr(run_runtime, 'open_profile_store', open_store)
    homes = [(f'profile-{i}', Path(f'/profiles/profile-{i}')) for i in range(32)]

    async def exercise():
        task = asyncio.create_task(run_runtime._prewarm_profile_stores(object(), homes))
        try:
            assert await asyncio.to_thread(wave_started.wait, 5)
            # This coroutine executes while the worker functions are blocked.
            assert not task.done()
            with lock:
                assert active == 4
        finally:
            release.set()
            await task

    asyncio.run(exercise())
    assert 1 < peak <= 4
    assert sorted(calls) == sorted(home for _, home in homes)


def test_cancelled_prewarm_does_not_submit_queued_profiles(monkeypatch):
    lock = threading.Lock()
    wave_started = threading.Event()
    release = threading.Event()
    calls = []

    def open_store(runner, home):
        with lock:
            calls.append(home)
            if len(calls) == 4:
                wave_started.set()
        assert release.wait(10), 'test did not release blocked workers'

    monkeypatch.setattr(run_runtime, 'open_profile_store', open_store)
    homes = [(str(i), Path(f'/profiles/{i}')) for i in range(32)]

    async def exercise():
        task = asyncio.create_task(run_runtime._prewarm_profile_stores(object(), homes))
        try:
            assert await asyncio.to_thread(wave_started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(exercise())  # joins the released executor workers
    assert len(calls) == 4


def test_prewarm_empty_set_is_a_noop(monkeypatch):
    def unexpected_open(*args):
        pytest.fail('an empty served set must not open any store')
    monkeypatch.setattr(run_runtime, 'open_profile_store', unexpected_open)
    assert asyncio.run(run_runtime._prewarm_profile_stores(object(), [])) is None
