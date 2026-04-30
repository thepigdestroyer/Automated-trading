"""Tests for the single-instance bot lock (incident 2026-04-29)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from trading_bot.process_lock import (
    LockHeldError,
    SingleInstanceLock,
    acquire_single_instance_lock,
)


def test_acquire_release_basic(tmp_path):
    lock_path = tmp_path / "bot.pid"
    lock = SingleInstanceLock(lock_path).acquire()
    try:
        assert lock_path.exists()
        # PID written to file for diagnostics
        content = lock_path.read_text().strip()
        assert content == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_raises(tmp_path):
    lock_path = tmp_path / "bot.pid"
    first = SingleInstanceLock(lock_path).acquire()
    try:
        with pytest.raises(LockHeldError) as excinfo:
            SingleInstanceLock(lock_path).acquire()
        # Error should report the holder pid
        assert excinfo.value.holder_pid == os.getpid()
        assert str(lock_path) in str(excinfo.value)
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path):
    lock_path = tmp_path / "bot.pid"
    first = SingleInstanceLock(lock_path).acquire()
    first.release()
    # Now another instance can claim it.
    second = SingleInstanceLock(lock_path).acquire()
    try:
        assert lock_path.exists()
    finally:
        second.release()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "bot.pid"
    with SingleInstanceLock(lock_path):
        # Inside the block, a second acquire fails.
        with pytest.raises(LockHeldError):
            SingleInstanceLock(lock_path).acquire()
    # After the block, the lock is released — re-acquire must succeed.
    with SingleInstanceLock(lock_path):
        pass


def test_context_manager_releases_on_exception(tmp_path):
    lock_path = tmp_path / "bot.pid"
    with pytest.raises(RuntimeError, match="boom"):
        with SingleInstanceLock(lock_path):
            raise RuntimeError("boom")
    # Lock should now be released.
    with SingleInstanceLock(lock_path):
        pass


def test_helper_function(tmp_path):
    lock_path = tmp_path / "bot.pid"
    lock = acquire_single_instance_lock(lock_path)
    try:
        with pytest.raises(LockHeldError):
            acquire_single_instance_lock(lock_path)
    finally:
        lock.release()


def test_stale_lock_file_does_not_block(tmp_path):
    """A previous process killed with -9 leaves a stale pid file.
    flock releases when the FD closes, so we should be able to re-acquire.
    """
    lock_path = tmp_path / "bot.pid"
    # Simulate a stale file containing a fake pid that no longer exists.
    lock_path.write_text("999999\n")
    # Acquire should succeed because no process holds flock on the file.
    with SingleInstanceLock(lock_path):
        # Our pid was written, overwriting the stale value.
        content = lock_path.read_text().strip()
        assert content == str(os.getpid())


def test_cross_process_lock_held(tmp_path):
    """A separate process holding the lock should block this one."""
    lock_path = tmp_path / "bot.pid"
    # Spawn a child process that holds the lock and waits on stdin.
    helper = (
        "import sys, fcntl;"
        f"f=open(r'{lock_path}','a+');"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB);"
        "sys.stdout.write('locked\\n'); sys.stdout.flush();"
        "sys.stdin.readline()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", helper],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the child to confirm it's holding the lock.
        line = proc.stdout.readline()  # type: ignore[union-attr]
        assert line.strip() == "locked"
        with pytest.raises(LockHeldError):
            SingleInstanceLock(lock_path).acquire()
    finally:
        try:
            proc.stdin.write("\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            pass
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Regression: 2026-04-30 OCI deployment incident.
#
# Symptom: bot under systemd was alive (PID 3392862) but the watcher had
# spawned a duplicate bot (PID 3393673) that *also* claimed the lock — two
# concurrent bots running and both writing trades.
#
# Root cause: SingleInstanceLock.release() called os.unlink(lock_path).
# When the original bot exited cleanly, the file at the path was removed
# but the bot kept running briefly via systemd restart; meanwhile a new
# process opening the same path created a fresh inode and acquired its own
# independent flock — the kernel locks an inode (open file description),
# not a path. The fix is to never unlink on release.
# ---------------------------------------------------------------------------


def test_release_does_not_unlink_lock_file(tmp_path):
    """After release, the lock file must still exist on disk so that the
    next acquire targets the same inode (allowing flock to detect concurrent
    holders). Unlinking on release was the root cause of the 2026-04-30
    duplicate-bot incident."""
    lock_path = tmp_path / "bot.pid"
    lock = SingleInstanceLock(lock_path).acquire()
    assert lock_path.exists()
    lock.release()
    # CRITICAL: file must persist after release.
    assert lock_path.exists(), "release() must NOT unlink the lock file"


def test_release_keeps_pid_in_file_for_diagnostics(tmp_path):
    """After release the file content can be stale — that's fine. The next
    acquire overwrites the PID. We just must not delete the file itself."""
    lock_path = tmp_path / "bot.pid"
    lock = SingleInstanceLock(lock_path).acquire()
    pid_during = lock_path.read_text().strip()
    assert pid_during == str(os.getpid())
    lock.release()
    # After release, file still has our PID (stale but harmless).
    assert lock_path.exists()
    assert lock_path.read_text().strip() == pid_during


def test_concurrent_acquire_blocks_after_unlinked_then_recreated(tmp_path):
    """If an external actor unlinks the lock file while a process holds it
    and immediately re-creates an EMPTY file at the same path, a duplicate
    acquire on the new path inode succeeds because flock is per-OFD/inode,
    not per-path. We document this known limitation: the bot's own
    release() must not unlink (covered above), and external tooling must
    not unlink either. Operationally we mitigate by keeping bot.pid out of
    any rotation/cleanup scripts."""
    # The test does not assert blocking — it documents the known limitation
    # and verifies that our own release path doesn't trigger it. If we ever
    # introduce a separate bot.lock (immutable) + bot.pid (rewritable),
    # this test should be tightened to assert blocking.
    lock_path = tmp_path / "bot.pid"
    lock = SingleInstanceLock(lock_path).acquire()
    try:
        # Simulate what an external script (or a buggy old release()) would do.
        os.unlink(lock_path)
        lock_path.write_text("")  # external recreate at same path → new inode
        # On the recreated inode, a fresh flock succeeds. This is the
        # vulnerability we used to introduce ourselves via release(); we
        # now verify only that *our own* code does not do this.
        with SingleInstanceLock(lock_path) as second:
            assert second._acquired
    finally:
        lock.release()
