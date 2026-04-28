from __future__ import annotations

import re
from threading import Thread

from trading_bot.bot import TradingBot

HELP_TEXT = """
Interactive commands:
  help                 Show this help text.
  status               Show the latest cycle summary and current operator controls.
  pause                Pause automatic cycles.
  resume               Resume automatic cycles.
  run                  Request an immediate cycle.
  note <text>          Add a persistent operator note for the model.
  focus <symbols>      Focus discovery/decisioning on symbols, for example: focus GOLD,NATGAS
  avoid <symbols>      Exclude symbols from new trading, for example: avoid NAS100,SPY
  close <symbol>       Queue a manual close request for the next cycle.
  ask <question>       Ask the model about the bot's current state.
  clear                Clear notes, focus, avoid, and pending close requests.
  quit                 Stop the bot and exit.

Any other text is interpreted by the model into one or more bot actions.
""".strip()


def start_interactive_console(bot: TradingBot) -> Thread:
    def worker() -> None:
        print("Interactive terminal enabled. Type 'help' for commands.", flush=True)
        while not bot.stop_event.is_set():
            try:
                raw = input("bot> ").strip()
            except EOFError:
                bot.request_stop()
                return
            except KeyboardInterrupt:
                print("", flush=True)
                bot.request_stop()
                return

            if not raw:
                continue

            command, _, remainder = raw.partition(" ")
            cmd = command.lower()
            text = remainder.strip()

            try:
                if cmd in {"help", "?"}:
                    print(HELP_TEXT, flush=True)
                elif cmd == "status":
                    print(bot.get_status_report(), flush=True)
                elif cmd == "pause":
                    bot.pause()
                    print("Paused automatic cycles.", flush=True)
                elif cmd == "resume":
                    bot.resume()
                    print("Resumed automatic cycles.", flush=True)
                elif cmd == "run":
                    bot.request_run_now()
                    print("Requested an immediate cycle.", flush=True)
                elif cmd == "note":
                    if not text:
                        print("A note is required.", flush=True)
                        continue
                    bot.add_operator_note(text)
                    bot.request_run_now()
                    print("Stored note for the model.", flush=True)
                elif cmd == "focus":
                    if not text:
                        print("Focus requires one or more symbols or a short phrase.", flush=True)
                        continue
                    added = bot.add_focus_symbols(_extract_symbols(text))
                    bot.add_operator_note(f"Operator focus request: {text}")
                    bot.request_run_now()
                    if added:
                        print(f"Focused symbols: {', '.join(added)}", flush=True)
                    else:
                        print("Stored focus note for the model.", flush=True)
                elif cmd == "avoid":
                    if not text:
                        print("Avoid requires one or more symbols or a short phrase.", flush=True)
                        continue
                    added = bot.add_excluded_symbols(_extract_symbols(text))
                    bot.add_operator_note(f"Operator exclusion request: {text}")
                    bot.request_run_now()
                    if added:
                        print(f"Excluded symbols from new trades: {', '.join(added)}", flush=True)
                    else:
                        print("Stored avoid note for the model.", flush=True)
                elif cmd == "close":
                    if not text:
                        print("A symbol is required for close.", flush=True)
                        continue
                    symbol = bot.queue_forced_close(text)
                    bot.add_operator_note(f"Operator requested closing {symbol} on the next cycle")
                    bot.request_run_now()
                    print(f"Queued manual close request for {symbol}.", flush=True)
                elif cmd == "ask":
                    if not text:
                        print("A question is required.", flush=True)
                        continue
                    answer = bot.answer_operator_question(text)
                    print(answer, flush=True)
                elif cmd == "clear":
                    bot.clear_operator_guidance()
                    print("Cleared operator guidance and pending close requests.", flush=True)
                elif cmd in {"quit", "exit"}:
                    bot.request_stop()
                    print("Stopping bot.", flush=True)
                    return
                else:
                    for line in _handle_model_interpreted_input(bot, raw):
                        print(line, flush=True)
            except Exception as exc:
                print(f"Command failed: {exc}", flush=True)

    thread = Thread(target=worker, name="bot-terminal-console", daemon=True)
    thread.start()
    return thread


def _extract_symbols(text: str) -> list[str]:
    return re.findall(r"\b[A-Za-z][A-Za-z0-9]{0,14}\b", text.upper())


def _handle_model_interpreted_input(bot: TradingBot, raw: str) -> list[str]:
    envelope = bot.interpret_operator_input(raw)
    lines = [f"Model interpreted input: {envelope.summary}"]
    if not envelope.actions:
        bot.add_operator_note(raw)
        bot.request_run_now()
        lines.append("No concrete action was returned; stored it as a model note.")
        return lines

    for action in envelope.actions:
        lines.append(_apply_interpreted_action(bot, action.action, action.content, action.symbols))
    return lines


def _apply_interpreted_action(
    bot: TradingBot,
    action: str,
    content: str | None,
    symbols: list[str],
) -> str:
    if action == "status":
        return bot.get_status_report()
    if action == "pause":
        bot.pause()
        return "Paused automatic cycles."
    if action == "resume":
        bot.resume()
        return "Resumed automatic cycles."
    if action == "run":
        bot.request_run_now()
        return "Requested an immediate cycle."
    if action == "note":
        note = (content or "").strip()
        if not note:
            note = "Operator provided free-form guidance."
        bot.add_operator_note(note)
        bot.request_run_now()
        return f"Stored note for the model: {note}"
    if action == "focus":
        added = bot.add_focus_symbols(symbols)
        if content:
            bot.add_operator_note(f"Operator focus request: {content}")
        bot.request_run_now()
        if added:
            return f"Focused symbols: {', '.join(added)}"
        return "Stored focus guidance for the model."
    if action == "avoid":
        added = bot.add_excluded_symbols(symbols)
        if content:
            bot.add_operator_note(f"Operator exclusion request: {content}")
        bot.request_run_now()
        if added:
            return f"Excluded symbols from new trades: {', '.join(added)}"
        return "Stored avoid guidance for the model."
    if action == "close":
        target = (symbols[0] if symbols else (content or "")).strip().upper()
        if not target:
            raise ValueError("Model requested CLOSE without a symbol")
        symbol = bot.queue_forced_close(target)
        bot.add_operator_note(f"Operator requested closing {symbol} on the next cycle")
        bot.request_run_now()
        return f"Queued manual close request for {symbol}."
    if action == "clear":
        bot.clear_operator_guidance()
        return "Cleared operator guidance and pending close requests."
    if action == "quit":
        bot.request_stop()
        return "Stopping bot."
    if action == "ask":
        question = (content or "").strip() or "What is the current runtime state?"
        return bot.answer_operator_question(question)

    bot.add_operator_note(content or action)
    bot.request_run_now()
    return f"Stored fallback note for unhandled action '{action}'."
