"""Single-instance lock for the trading bot process.

Uses ``fcntl.flock`` (LOCK_EX | LOCK_NB) on a lock file. Because the lock is
held on an open file descriptor, the OS automatically releases it when the
process dies — even on ``kill -9`` — so stale lock files do not block restarts.

Usage:

    with acquire_single_instance_lock("bot.pid") as lock:
        run_bot()

If another process already holds the lock, ``LockHeldError`` is raised.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path
from typing import IO, Optional

LOGGER = logging.getLogger(__name__)


class LockHeldError(RuntimeError):
    """Raised when another live process already holds the bot lock."""

    def __init__(self, lock_path: str, holder_pid: Optional[int] = None):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        msg = f"another bot instance already holds {lock_path}"
        if holder_pid is not None:
            msg += f" (pid {holder_pid})"
        super().__init__(msg)


class SingleInstanceLock:
    """Context manager wrapping an exclusive flock on a file."""

    def __init__(self, lock_path: str | os.PathLike[str]):
        self.lock_path = str(lock_path)
        self._fd: Optional[IO[str]] = None
        self._acquired = False

    def acquire(self) -> "SingleInstanceLock":
        # Open in r+ if exists, else create. We keep the file around so that
        # the recorded PID survives normal restarts; flock cares only about
        # the FD lifetime, not the file's existence.
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                # Try to read the holder's PID for a friendlier error message.
                holder_pid: Optional[int] = None
                try:
                    fd.seek(0)
                    raw = fd.read().strip()
                    if raw:
                        holder_pid = int(raw.splitlines()[0].strip())
                except (OSError, ValueError):
                    holder_pid = None
                fd.close()
                raise LockHeldError(self.lock_path, holder_pid) from None
            fd.close()
            raise
        # Truncate and write our PID for diagnostics.
        try:
            fd.seek(0)
            fd.truncate()
            fd.write(f"{os.getpid()}\n")
            fd.flush()
        except OSError:
            # Non-fatal — the lock itself is what matters.
            LOGGER.debug("could not write pid to %s", self.lock_path)
        self._fd = fd
        self._acquired = True
        LOGGER.info("acquired single-instance lock %s (pid %d)", self.lock_path, os.getpid())
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        fd = self._fd
        self._acquired = False
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fd.close()
        except OSError:
            pass
        # Best-effort removal of the pid file. If another instance has already
        # claimed it (race), ignore — flock is the source of truth.
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        LOGGER.info("released single-instance lock %s", self.lock_path)

    # Context-manager protocol -------------------------------------------------
    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_single_instance_lock(lock_path: str | os.PathLike[str]) -> SingleInstanceLock:
    """Convenience helper: build and acquire a :class:`SingleInstanceLock`."""
    return SingleInstanceLock(lock_path).acquire()
