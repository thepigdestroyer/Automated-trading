from __future__ import annotations

import argparse
import logging

from t212_cfd_bot.bot import TradingBot
from t212_cfd_bot.config import Settings
from t212_cfd_bot.terminal import start_interactive_console


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-broker CFD bot using OpenAI, headlines, and technical indicators."
    )
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
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)

    settings = Settings()
    if args.symbols:
        override_symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        settings = settings.model_copy(
            update={"symbols": override_symbols, "auto_discover_symbols": False}
        )

    bot = TradingBot(settings)
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
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
