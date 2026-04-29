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
