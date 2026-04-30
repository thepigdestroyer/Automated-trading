"""Single-instance lock for the trading bot process.

Uses ``fcntl.flock`` (LOCK_EX | LOCK_NB). Because flock is per-OFD and not
per-path, we MUST keep the lock file resident across the lifetime of every
bot instance — if the file is unlinked while a process holds the lock,
another process can open the path (creating a new inode) and acquire a
fresh lock on that inode, with both processes thinking they own the lock.

To make this robust we use **two paths**:

- ``<lock_path>``           — the flock target. Never unlinked. Created
                              once with ``O_CREAT`` if missing. Inode is
                              stable for the file's lifetime.
- ``<lock_path>.pid``       — diagnostic-only PID file. Written on
                              acquire; safe to delete or overwrite at any
                              time (the lock state does not depend on it).

If the caller passes ``"bot.pid"`` for backwards compatibility we still
treat it as the flock target and write the PID into ``"bot.pid"`` itself
(the legacy single-file behaviour); but we do NOT unlink it on release —
the file persists with stale content that the next acquire overwrites.

Usage:

    with acquire_single_instance_lock("bot.pid") as lock:
        run_bot()

If another LIVE process already holds the lock, ``LockHeldError`` is raised.
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
    """Context manager wrapping an exclusive flock on a file.

    The lock file is **never unlinked** (see module docstring). On release
    the FD is closed, which drops the kernel-level flock; the file remains
    on disk so that the next acquire opens the same inode and can detect a
    concurrent live holder via flock(LOCK_EX | LOCK_NB).
    """

    def __init__(self, lock_path: str | os.PathLike[str]):
        self.lock_path = str(lock_path)
        self.pid_path = str(lock_path) + ".pid" if not str(lock_path).endswith(".pid") else str(lock_path)
        self._fd: Optional[IO[str]] = None
        self._acquired = False

    def acquire(self) -> "SingleInstanceLock":
        # Open in r+ if exists, else create. We keep the file around so that
        # the inode is stable; flock is per-OFD but on Linux a different
        # process opening the SAME inode and trying flock will get EWOULDBLOCK
        # while we hold it.
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
            os.fsync(fd.fileno())
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
        # IMPORTANT: do NOT unlink the lock file. Unlinking would leave any
        # already-acquired flock pinned to an orphan inode, and a new process
        # opening the path would create a fresh inode and successfully
        # acquire a *separate* flock on it — yielding two concurrent holders.
        # The next acquire just opens the same inode and overwrites the PID.
        LOGGER.info("released single-instance lock %s", self.lock_path)

    # Context-manager protocol -------------------------------------------------
    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_single_instance_lock(lock_path: str | os.PathLike[str]) -> SingleInstanceLock:
    """Convenience helper: build and acquire a :class:`SingleInstanceLock`."""
    return SingleInstanceLock(lock_path).acquire()
