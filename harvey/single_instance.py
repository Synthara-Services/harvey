"""One `harvey run` at a time.

Two heartbeat loops against the same database would both drain the outbox
and could send the same email twice. `harvey run` takes an operating
system lock on data/harvey.lock for its whole life; a second one finds it
held and refuses to start. The system lets go of the lock when the process
ends, also on a crash or kill -9, so a stale lock never blocks the next
start. The file keeps the holder's PID so the refusal can name it.
"""

import os
from pathlib import Path

from harvey.paths import DATA_DIR

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_FILE = DATA_DIR / "harvey.lock"

# Windows locks are mandatory, so the locked byte sits past the PID: a
# second process can still read who holds the lock.
LOCK_OFFSET = 1024


class AlreadyRunning(Exception):
    """Another `harvey run` holds the lock."""

    def __init__(self, pid: str = ""):
        self.pid = pid
        who = f"Harvey is already running (PID {pid})." if pid else \
            "Harvey is already running in another process."
        super().__init__(
            f"{who} A second harvey run would send the same emails twice, so "
            "this one is not starting. Stop the running one first (Ctrl+C in "
            "its terminal, or Stop on the dashboard), or leave it running."
        )


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_pid(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    words = os.read(fd, 64).decode("ascii", "replace").split()
    return words[0] if words and words[0].isdigit() else ""


def acquire(path: Path | None = None) -> int:
    """Takes the lock for the rest of this process's life and returns its
    file descriptor. Raises AlreadyRunning when another process holds it."""
    path = Path(path or LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if not _try_lock(fd):
        pid = _read_pid(fd)
        os.close(fd)
        raise AlreadyRunning(pid)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid():<20}\n".encode("ascii"))  # one width: no stale digits
    return fd


def release(fd: int) -> None:
    """Lets go of the lock."""
    _unlock(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def holder(path: Path | None = None) -> str | None:
    """None when no `harvey run` holds the lock; otherwise the holder's PID,
    or "" when it can't be read."""
    path = Path(path or LOCK_FILE)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
        return _read_pid(fd)
    finally:
        os.close(fd)
