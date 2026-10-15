"""#47: a model call's timeout really fires, and the heartbeat says so.

asyncio's timers run on time.monotonic(), which on macOS stops while the
machine sleeps. A Claude call that started just before the lid closed got its
whole 300 s again after waking: one ran 12 min 16 s with no timeout and no log
line. The timeout now counts the time the machine slept, the wait after the
kill is capped, and the heartbeat warns about any call still running past its
timeout.
"""

import asyncio
import inspect
import logging
import os
import signal
import sys
import time

import pytest

import harvey.brain as brain_mod
from harvey.brain import Brain

REAL_TIME = time.time
NEVER_EXITS = "import time; time.sleep(120)"
# Starts a grandchild that inherits stdout and stderr and keeps them open
# after this process is killed, and writes its pid to argv[1].
LEAVES_A_CHILD_HOLDING_THE_PIPES = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(120)\n"
)


def stand_in(monkeypatch, script, procs, *args, on_start=None):
    """Run ``script`` with Python where the brain would run the claude CLI."""
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*cmd, **kwargs):
        proc = await real_exec(sys.executable, "-c", script, *args, **kwargs)
        procs.append(proc)
        if on_start:
            on_start()
        return proc

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)


def wall_clock(monkeypatch):
    """The wall clock, with an offset the test can move forward."""
    offset = [0.0]
    monkeypatch.setattr(brain_mod.time, "time", lambda: REAL_TIME() + offset[0])
    return offset


async def bounded(coro, seconds, what):
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        pytest.fail(what)


def warnings_from(caplog, name):
    return [r.getMessage() for r in caplog.records
            if r.name == name and r.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_the_timeout_counts_the_time_the_machine_slept(monkeypatch, caplog):
    """The call starts, then the machine sleeps 400 s: the wall clock moves
    on, the event loop's clock does not. On waking the call is past its
    300 s timeout, so it is killed at once, not given 300 s more."""
    procs = []
    offset = wall_clock(monkeypatch)

    def sleep_400_seconds():
        asyncio.get_running_loop().call_later(0.2, offset.__setitem__, 0, 400.0)

    stand_in(monkeypatch, NEVER_EXITS, procs, on_start=sleep_400_seconds)
    caplog.set_level(logging.WARNING, logger="harvey.brain")
    brain = Brain(state=None)
    result = await bounded(
        brain.think("hi", timeout=300, max_retries=0, agent="writer", task="write_sequence"),
        15, "the 300 s timeout never fired after the machine slept 400 s")

    assert result == ""
    assert len(procs) == 1 and procs[0].returncode is not None  # killed and reaped
    [warning] = warnings_from(caplog, "harvey.brain")
    assert "writer/write_sequence" in warning and "300s timeout" in warning
    assert "asleep" in warning


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe inheritance")
@pytest.mark.asyncio
async def test_a_killed_call_does_not_wait_on_a_child_holding_its_pipes(
        monkeypatch, tmp_path):
    """On Python 3.12, Process.wait() returns only once every pipe is closed.
    A grandchild still holding them must not keep the timed-out call alive."""
    procs, pid_file = [], tmp_path / "grandchild.pid"
    stand_in(monkeypatch, LEAVES_A_CHILD_HOLDING_THE_PIPES, procs, str(pid_file))
    brain = Brain(state=None)
    try:
        result = await bounded(
            brain.think("hi", timeout=1, max_retries=0),
            15, "the timed-out call waited on a grandchild holding its pipes")
        assert result == ""
        assert procs[0].returncode is not None
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if procs:   # read both pipes to their end, so the transport closes
            await asyncio.wait_for(
                asyncio.gather(procs[0].stdout.read(), procs[0].stderr.read()), 10)


class HangingProcess:
    """A CLI that never answers until it is killed."""

    returncode = None

    async def communicate(self, data=None):
        await asyncio.Event().wait()

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_the_heartbeat_warns_once_about_a_call_past_its_timeout(monkeypatch, caplog):
    import harvey.main as main_mod

    async def fake_exec(*cmd, **kwargs):
        return HangingProcess()

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    offset = wall_clock(monkeypatch)
    caplog.set_level(logging.WARNING)
    brain = Brain(state=None)
    call = asyncio.ensure_future(
        brain.think("hi", timeout=300, max_retries=0, agent="writer", task="write_sequence"))
    for _ in range(100):
        if brain.running_calls():
            break
        await asyncio.sleep(0.01)
    assert [c.label for c in brain.running_calls()] == ["writer/write_sequence"]

    main_mod.warn_about_overdue_calls(brain)   # still inside its timeout
    assert warnings_from(caplog, "harvey") == []

    offset[0] = 400.0   # the machine slept; the brain has not looked yet
    main_mod.warn_about_overdue_calls(brain)
    main_mod.warn_about_overdue_calls(brain)   # once per call
    [warning] = warnings_from(caplog, "harvey")
    assert "Claude call (writer/write_sequence)" in warning
    assert "past its 300s timeout" in warning

    assert await bounded(call, 15, "the brain never cut the call off") == ""
    assert brain.running_calls() == []


@pytest.mark.asyncio
async def test_the_watchdog_checks_on_its_own(monkeypatch, caplog):
    import harvey.main as main_mod

    seen = []
    monkeypatch.setattr(main_mod, "BRAIN_WATCHDOG_SECONDS", 0.01)
    monkeypatch.setattr(main_mod, "warn_about_overdue_calls", seen.append)
    watchdog = asyncio.ensure_future(main_mod.watch_brain_calls("the brain"))
    for _ in range(100):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.01)
    watchdog.cancel()
    await asyncio.gather(watchdog, return_exceptions=True)
    assert seen[:2] == ["the brain", "the brain"]


@pytest.mark.asyncio
async def test_the_heartbeat_runs_the_watchdog_with_its_brain(monkeypatch, tmp_path):
    import harvey.main as main_mod
    from harvey.config import EnvConfig, HarveyConfig
    from harvey.state import StateManager
    from tests.test_brain_provider import BASE

    state = StateManager(str(tmp_path / "test.db"))
    monkeypatch.setattr(main_mod, "load_config", lambda: HarveyConfig(**BASE))
    monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    started = {}

    async def forever():
        await asyncio.Event().wait()

    def fake_watch(brain):
        started["brain"], started["coro"] = brain, forever()
        return started["coro"]

    monkeypatch.setattr(main_mod, "watch_brain_calls", fake_watch)
    stop = asyncio.Event()
    stop.set()
    await bounded(main_mod.heartbeat(stop), 15, "the heartbeat did not stop")
    assert isinstance(started["brain"], Brain)
    assert inspect.getcoroutinestate(started["coro"]) == inspect.CORO_CLOSED
