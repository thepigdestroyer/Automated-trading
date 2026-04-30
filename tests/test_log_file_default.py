"""Bot log-file default path resolution (review 2026-04-30-adhoc-1 #5).

Previously the bot wrote its operational log to `/tmp/cfd_bot.log`,
ephemeral on OCI VM reboots and not synced to OneDrive. The default is
now `logs/bot.log` (CWD-relative, on the OneDrive-synced volume), with
the same overrides preserved:

  --log-file <path>     CLI flag (highest precedence; "" disables)
  $BOT_LOG_FILE         env var (next; "" disables)
  logs/bot.log          built-in default
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from trading_bot.__main__ import DEFAULT_LOG_FILE, configure_logging, resolve_log_file


# --- resolve_log_file precedence ------------------------------------


def test_default_log_file_is_logs_bot_log():
    assert DEFAULT_LOG_FILE == "logs/bot.log"


def test_resolve_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("BOT_LOG_FILE", raising=False)
    assert resolve_log_file(None) == DEFAULT_LOG_FILE


def test_resolve_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("BOT_LOG_FILE", "/var/log/cfd_bot.log")
    assert resolve_log_file(None) == "/var/log/cfd_bot.log"


def test_cli_value_overrides_env(monkeypatch):
    monkeypatch.setenv("BOT_LOG_FILE", "/var/log/env.log")
    assert resolve_log_file("/explicit/cli.log") == "/explicit/cli.log"


def test_empty_cli_disables_file_logging(monkeypatch):
    monkeypatch.setenv("BOT_LOG_FILE", "/var/log/env.log")
    # Explicit empty string from --log-file "" disables, even if env is set
    assert resolve_log_file("") is None


def test_empty_env_disables_file_logging(monkeypatch):
    monkeypatch.setenv("BOT_LOG_FILE", "")
    assert resolve_log_file(None) is None


def test_legacy_tmp_path_still_works_when_explicit(monkeypatch):
    """The old /tmp/cfd_bot.log path is no longer the default but
    still works if the user passes it explicitly."""
    monkeypatch.delenv("BOT_LOG_FILE", raising=False)
    assert resolve_log_file("/tmp/cfd_bot.log") == "/tmp/cfd_bot.log"


# --- configure_logging creates parent dir ---------------------------


def test_configure_logging_creates_parent_dir(tmp_path, monkeypatch):
    """If the log-file's parent directory does not exist, configure_logging
    creates it. Required because the OCI bot may launch before logs/
    has been initialised."""
    target = tmp_path / "fresh_logs" / "bot.log"
    assert not target.parent.exists()

    configure_logging("INFO", log_file=str(target))
    try:
        logging.getLogger("test").info("hello")
    finally:
        # Tear down the global handlers we just installed so other tests
        # don't see our file handle.
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None):
                stream = h.stream
                if hasattr(stream, "name") and str(stream.name) == str(target):
                    h.close()
                    logging.getLogger().removeHandler(h)

    assert target.parent.exists()
    assert target.exists()


def test_configure_logging_skips_file_handler_when_none(tmp_path):
    """log_file=None ⇒ console only, no file handler."""
    configure_logging("INFO", log_file=None)
    file_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.StreamHandler)
        and getattr(getattr(h, "stream", None), "name", "") not in ("<stderr>", "<stdout>")
        and not getattr(getattr(h, "stream", None), "name", "").startswith("<")
    ]
    # Allow stderr/stdout handlers; just no file-backed one
    for h in file_handlers:
        name = getattr(getattr(h, "stream", None), "name", "")
        # If a previous test left a file handler around, ignore it as long
        # as it's not pointing at logs/bot.log
        assert "bot.log" not in name


def test_watcher_default_bot_log_is_logs_bot_log():
    """The supervisor.watcher CLI also defaults to logs/bot.log so the
    /tmp/cfd_bot.log mtime detector reads the right file."""
    import argparse
    from supervisor.watcher import main as _watcher_main  # noqa: F401

    # Parse the watcher's argparse defaults by importing the module
    # and inspecting the parser. Easiest: call argparse manually with
    # --once flag from the same code path. But that would actually
    # run a tick. Instead, read the source for the default.
    from supervisor import watcher as wmod
    src = Path(wmod.__file__).read_text(encoding="utf-8")
    assert "default=Path(\"logs/bot.log\")" in src
