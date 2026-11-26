"""Real process-exit regressions; only disposable children disable the pytest guard."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_child(tmp_path, source):
    env = dict(os.environ, HERMES_HOME=str(tmp_path), HERMES_GATEWAY_PROFILE="")
    # Never clear this in the pytest worker: a real os._exit belongs only in the child.
    env.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=12,
    )
    assert result.returncode == 75, result.stdout + result.stderr
    armed_at = float(next(line.split("=", 1)[1] for line in result.stderr.splitlines()
                          if line.startswith("armed_at=")))
    elapsed = time.monotonic() - armed_at
    assert elapsed < 2.0, f"hard exit exceeded 2s outer bound: {elapsed:.3f}s"
    print(f"child exit=75 armed-to-reap={elapsed:.3f}s")
    return result


@pytest.mark.parametrize("cause", ["event", "receipt_lock"])
def test_stop_impl_wedged_teardown_restarts_process(tmp_path, cause):
    result = run_child(tmp_path, '''
        import asyncio, faulthandler, threading, time, sys
        from gateway.run import GatewayRunner
        import gateway.run_shutdown as shutdown
        from hermes_state import SessionDB
        from hermes_state_local import local_receipt
        runner = object.__new__(GatewayRunner)
        # Force the real SessionDB writer-fallback read lock to be held by a worker.
        # This is a causal repro, not a claim about the historical gateway's stack.
        db = object.__new__(SessionDB)
        db._pinned_read_conn = lambda: None
        db._checkout_read_conn = lambda: None
        db._lock = threading.Lock()
        held = threading.Event()
        def hold_lock():
            with db._lock:
                held.set()
                threading.Event().wait()
        threading.Thread(target=hold_lock, daemon=True).start()
        assert held.wait(1)
        cause = %r
        # Only shorten the leash; do NOT supply an exit code or replace the watchdog.
        shutdown._effective_watchdog_leash = lambda runner: 0.2
        async def wedge(self, ctx):
            print("teardown-entered", flush=True)
            if cause == "receipt_lock":
                local_receipt(db, "test-receipt")
            else:
                threading.Event().wait()
        GatewayRunner._stop_begin_teardown = wedge
        GatewayRunner._shutdown_watchdog_snapshot = lambda *args: {}
        faulthandler.dump_traceback_later(0.1)
        print("armed_at=" + str(time.monotonic()), file=sys.stderr, flush=True)
        asyncio.run(runner._stop_impl())
    ''' % cause)
    assert "teardown-entered" in result.stdout
    if cause == "receipt_lock":
        assert "in _read_ctx" in result.stderr
        assert "in local_receipt" in result.stderr


@pytest.mark.parametrize("seam", ["snapshot", "dump", "logging", "flush", "status", "drain", "mark", "cleanup"])
def test_blocking_diagnostics_cannot_disable_exit(tmp_path, seam):
    result = run_child(tmp_path, '''
        import os, threading, time, sys
        from pathlib import Path
        import gateway.shutdown_watchdog as watchdog
        import gateway.status as status
        import hermes_logging
        import tools.environments.base as environments
        def wedge(*args, **kwargs):
            os.write(2, b"diagnostic-wedge-entered\\n")
            threading.Event().wait()
        seam = %r
        # Keep preceding diagnostics fast so each selected wedge is actually reached.
        watchdog._write_watchdog_dump = lambda *args, **kwargs: None
        watchdog.logger.critical = lambda *args, **kwargs: None
        status.remove_pid_file = lambda: None
        status.release_gateway_runtime_lock = lambda: None
        hermes_logging.drain_log_queue = lambda **kwargs: None
        watchdog._mark_exited_quietly = lambda *args: None
        snapshot = wedge if seam == "snapshot" else lambda: {}
        if seam == "dump": watchdog._write_watchdog_dump = wedge
        if seam == "logging": watchdog.logger.critical = wedge
        if seam == "flush":
            class BlockedStream:
                def write(self, value): return len(value)
                def flush(self): wedge()
            sys.stdout = BlockedStream()
        if seam == "status": status.remove_pid_file = wedge
        if seam == "drain": hermes_logging.drain_log_queue = wedge
        if seam == "mark": watchdog._mark_exited_quietly = wedge
        if seam == "cleanup": environments.kill_live_foreground_processes = wedge
        print("armed_at=" + str(time.monotonic()), file=sys.stderr, flush=True)
        watchdog.arm_shutdown_watchdog(0.2, snapshot_fn=snapshot, exit_code=75,
                                      dump_path=watchdog.get_shutdown_watchdog_dump_path())
        time.sleep(60)
    ''' % seam)
    assert "diagnostic-wedge-entered" in result.stderr
