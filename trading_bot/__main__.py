from __future__ import annotations

import argparse
import logging
import os
import signal

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.process_lock import LockHeldError, SingleInstanceLock
from trading_bot.terminal import start_interactive_console


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-broker CFD bot using OpenAI, headlines, and technical indicators."
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status")

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single decision/execution cycle and exit.",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        help="Comma-separated symbol override for this run.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Python log level, for example INFO or DEBUG.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enable terminal interaction while the bot is running.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Write logs to this file in addition to stderr.",
    )
    parser.add_argument(
        "--lock-file",
        type=str,
        default=None,
        help=(
            "Path to the single-instance lock/pid file. Defaults to env "
            "BOT_LOCK_FILE or 'bot.pid' in the current working directory. "
            "Use --no-lock to disable (testing only)."
        ),
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Disable the single-instance lock (testing only).",
    )
    return parser.parse_args()


def configure_logging(level: str, log_file: str | None = None) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.StreamHandler(
            open(log_file, "a", buffering=1, encoding="utf-8")
        ))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        force=True,  # override any handlers set by imported libraries
        handlers=handlers,
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level, log_file=args.log_file)

    if args.command == "status":
        from trading_bot.status import run_status
        raise SystemExit(run_status())

    settings = Settings()

    errors = settings.validate_runtime()
    if errors:
        for e in errors:
            print(f"CONFIG ERROR: {e}")
        raise SystemExit(2)

    if args.symbols:
        override_symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        settings = settings.model_copy(
            update={"symbols": override_symbols, "auto_discover_symbols": False}
        )

    # Single-instance lock — prevents the watcher's restart_bot from spawning
    # a second concurrent bot while the first is still running (incident
    # 2026-04-29: log_silence rapid-fire restart bug created 3 concurrent bots).
    lock_path = args.lock_file or os.environ.get("BOT_LOCK_FILE") or "bot.pid"
    lock: SingleInstanceLock | None = None
    if not args.no_lock:
        try:
            lock = SingleInstanceLock(lock_path).acquire()
        except LockHeldError as exc:
            logging.getLogger(__name__).error(
                "another bot instance is already running: %s", exc
            )
            print(f"ERROR: {exc}", flush=True)
            raise SystemExit(2) from None

    bot = TradingBot(settings)

    def _on_signal(signum, frame):
        logging.getLogger(__name__).warning(
            "signal %s received — requesting graceful stop", signum
        )
        bot.request_stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.interactive:
        start_interactive_console(bot)
    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        bot.request_stop()
    except Exception as exc:
        logger = logging.getLogger(__name__)
        if logger.isEnabledFor(logging.DEBUG):
            logger.exception("Bot failed")
        else:
            logger.error("%s", exc)
        if lock is not None:
            lock.release()
        raise SystemExit(1) from None
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
