"""#56: one `harvey run` at a time, and each outbox row sent at most once.

On 2026-10-19 two `harvey run` processes were live at once with approved
rows due. A second run now refuses to start and names the first; the lock
dies with its process, kill -9 included. And the sender claims a row
(approved -> sending) before it handles it, so two senders that read the
same row can't both send it. A row a crash leaves 'sending' is marked
failed at the next start, never sent again on its own.
"""

import asyncio
import os
import subprocess
import sys
import time

import pytest

import harvey.dashboard as dashboard
import harvey.main as main_mod
from harvey import single_instance
from harvey.state import StateManager
from tests.test_call_timeout import bounded
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)

# Holds the lock at argv[1], says so, then waits until it is killed.
HOLDER = (
    "import sys, time\n"
    "from harvey.single_instance import acquire\n"
    "acquire(sys.argv[1])\n"
    "print('held', flush=True)\n"
    "time.sleep(120)\n"
)

# Tries the lock at argv[1] from another process: 'free' or 'held <pid>'.
PROBE = (
    "import sys\n"
    "from harvey.single_instance import AlreadyRunning, acquire\n"
    "try:\n"
    "    acquire(sys.argv[1])\n"
    "    print('free')\n"
    "except AlreadyRunning as e:\n"
    "    print('held', e.pid)\n"
)


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    path = tmp_path / "harvey.lock"
    monkeypatch.setattr(single_instance, "LOCK_FILE", path)
    return path


@pytest.fixture
def first_run(lock_file):
    """Another process, standing in for a harvey run, holding the lock."""
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_file)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def probe(path) -> str:
    out = subprocess.run([sys.executable, "-c", PROBE, str(path)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


# ── the single-instance lock ──


def test_a_second_run_finds_the_lock_held_and_names_the_first(first_run, lock_file):
    with pytest.raises(single_instance.AlreadyRunning) as refused:
        single_instance.acquire(lock_file)
    assert refused.value.pid == str(first_run.pid)
    message = str(refused.value)
    assert f"Harvey is already running (PID {first_run.pid})." in message
    assert "send the same emails twice" in message
    assert single_instance.holder(lock_file) == str(first_run.pid)


def test_a_second_harvey_run_exits_with_the_message_and_no_heartbeat(
        first_run, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    monkeypatch.setattr(main_mod, "_run_with_signals", lambda: started.append(1))
    with pytest.raises(SystemExit) as exit_:
        main_mod.main()
    assert exit_.value.code == 1
    assert f"Harvey is already running (PID {first_run.pid})." in capsys.readouterr().err
    assert started == [], "no heartbeat starts"


def test_harvey_run_holds_the_lock_while_it_runs_and_lets_go_after(
        lock_file, monkeypatch):
    seen = []

    async def run():
        seen.append(probe(lock_file))

    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    monkeypatch.setattr(main_mod, "_run_with_signals", run)
    main_mod.main()
    assert seen == [f"held {os.getpid()}"]
    assert probe(lock_file) == "free"
    assert single_instance.holder(lock_file) is None


def test_a_killed_run_leaves_no_stale_lock(lock_file):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_file)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    proc.kill()                      # kill -9: no cleanup code runs
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5  # Windows frees a dead process's locks soon, not at once
    while True:
        try:
            fd = single_instance.acquire(lock_file)
            break
        except single_instance.AlreadyRunning:
            assert time.monotonic() < deadline, "the dead run's lock was never released"
            time.sleep(0.1)
    single_instance.release(fd)


def test_no_lock_file_means_no_harvey_running(lock_file):
    assert not lock_file.exists()
    assert single_instance.holder(lock_file) is None


@pytest.mark.asyncio
async def test_the_dashboard_will_not_start_a_second_harvey(first_run, monkeypatch):
    monkeypatch.setattr(dashboard, "_check_harvey_pid", lambda: None)

    def never(*_a, **_k):
        raise AssertionError("the dashboard started a second harvey run")

    monkeypatch.setattr(dashboard.subprocess, "Popen", never)
    answer = await dashboard.start_harvey()
    assert answer["success"] is False
    assert f"Harvey is already running (PID {first_run.pid}) from a terminal" in answer["message"]


# ── each row sent at most once ──


class SlowProvider(FakeProvider):
    """Takes a moment to send, as a real provider does."""

    async def send_email(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await super().send_email(*args, **kwargs)


async def approved_row(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await make_sender(state, FakeProvider())._run_native()   # staged for review
    await state.approve_outbox()
    [due] = [r for r in await state.get_outbox(status="approved") if r["step"] == 1]
    return due


@pytest.mark.asyncio
async def test_two_senders_at_once_send_a_row_once(state):
    due = await approved_row(state)
    provider = SlowProvider()
    first, second = make_sender(state, provider), make_sender(state, provider)
    await asyncio.gather(first._drain_due(), second._drain_due())
    assert [m["to"] for m in provider.sent] == ["jane@acme.com"]
    assert (await state.get_outbox_item(due["id"]))["status"] == "sent"


@pytest.mark.asyncio
async def test_only_an_approved_row_can_be_claimed_and_only_once(state):
    due = await approved_row(state)
    assert await state.claim_outbox_item(due["id"]) is True
    assert (await state.get_outbox_item(due["id"]))["status"] == "sending"
    assert await state.claim_outbox_item(due["id"]) is False, "already taken"
    [later] = await state.get_outbox(status="approved")
    await state.update_outbox_item(later["id"], status="cancelled")
    assert await state.claim_outbox_item(later["id"]) is False, "cancelled"


@pytest.mark.asyncio
async def test_a_provider_that_raises_leaves_the_row_failed_not_sending(state):
    due = await approved_row(state)

    class Raising(FakeProvider):
        async def send_email(self, *_a, **_k):
            raise RuntimeError("connection reset")

    await make_sender(state, Raising())._drain_due()
    row = await state.get_outbox_item(due["id"])
    assert row["status"] == "failed"
    assert row["error"] == "send raised: connection reset"


@pytest.mark.asyncio
async def test_a_row_left_sending_is_failed_at_start_and_never_resent(
        state, monkeypatch, caplog):
    from harvey.config import EnvConfig, HarveyConfig
    from tests.test_brain_provider import BASE

    due = await approved_row(state)
    assert await state.claim_outbox_item(due["id"])   # then the run was killed
    monkeypatch.setattr(main_mod, "load_config", lambda: HarveyConfig(**BASE))
    monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    stop = asyncio.Event()
    stop.set()
    await bounded(main_mod.heartbeat(stop), 15, "the heartbeat did not stop")

    row = await state.get_outbox_item(due["id"])
    assert row["status"] == "failed"
    assert row["error"] == StateManager.INTERRUPTED_SEND
    assert any("to jane@acme.com was interrupted while sending" in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
