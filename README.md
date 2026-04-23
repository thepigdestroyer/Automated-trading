# CFD Trading Bot

This workspace now contains a configurable CFD bot that:

- supports both Trading212 and IG execution,
- supports OpenAI, Azure OpenAI, and OpenRouter for trade decisioning,
- pulls recent market headlines at runtime,
- computes technical indicators from recent price history,
- can optionally ingest ProRealTime sidecar data from a local CSV or JSON snapshot,
- asks OpenAI for a structured trade decision,
- applies deterministic risk rules before any order is sent,
- supports interactive operator control from the terminal while it runs.

## What It Uses

- Trading212 execution: the vendored [`trading212-api`](./trading212-api) repo, via its legacy CFD surface
- IG execution: IG Labs REST API
- Model execution: OpenAI, Azure OpenAI, or OpenRouter through the unified OpenAI Python client
- News: Google News RSS queries at runtime
- News breadth: source-filtered and analysis-focused Google News RSS queries, including Bloomberg and other financial outlets when available
- Indicators: Yahoo Finance OHLCV data via `yfinance`
- ProRealTime sidecar data: local CSV/JSON snapshot ingestion for screener, alert, and quote confirmation
- Model decisioning: OpenAI Responses API with structured outputs

## Safety Defaults

- `TRADING212_MODE=demo` by default
- `IG_MODE=demo` by default
- `DRY_RUN=true` by default
- live trading requires `ALLOW_LIVE_TRADING=true`
- shorting is disabled by default with `ALLOW_SHORTS=false`
- daily-loss flattening is enabled by default
- max holding time defaults to 72 hours

That matters because CFD automation is high risk, and the Trading212 repo itself is unofficial.

## Setup

1. Create and activate a virtual environment.
2. Install the bot:

```bash
pip install -e .
```

3. Ensure Firefox is installed locally. The vendored Trading212 login flow uses Selenium with Firefox in headless mode.
4. Copy `.env.example` to `.env` and fill in at least:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=...
TRADING212_USERNAME=...
TRADING212_PASSWORD=...
TRADING212_MODE=demo
DRY_RUN=true
FIREFOX_BINARY_PATH=C:\Program Files\Mozilla Firefox\firefox.exe
TRADING212_HEADLESS=true
TRADING212_SAVE_COOKIES=true
TRADING212_IMPORT_FIREFOX_COOKIES=true
TRADING212_FIREFOX_PROFILE_PATH=
```

If you want to use Azure OpenAI for the model instead of the standard OpenAI API, set:

```dotenv
LLM_PROVIDER=azure_openai
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_BASE_URL=https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/
OPENAI_MODEL=your-deployment-name
```

When `LLM_PROVIDER=azure_openai`, `OPENAI_MODEL` must be your Azure deployment name, not the raw model family name.

If you want to use OpenRouter with Moonshot Kimi K2.5, set:

```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_SITE_URL=https://your-app-or-site.example
OPENROUTER_APP_TITLE=CFD Trading Bot
OPENAI_MODEL=moonshotai/kimi-k2.5
```

`OPENROUTER_SITE_URL` and `OPENROUTER_APP_TITLE` are optional, but OpenRouter recommends them for app attribution. In the OpenRouter catalog, Kimi K2.5 is exposed as `moonshotai/kimi-k2.5`.

If you want to use IG instead of Trading212, set:

```dotenv
BROKER=ig
IG_USERNAME=...
IG_PASSWORD=...
IG_API_KEY=...
IG_MODE=demo
IG_EPICS=
DRY_RUN=true
```

`IG_EPICS` is optional. If you leave it blank, the bot will try to resolve EPICs automatically through IG market search.

If you want to include ProRealTime as a secondary confirmation layer, set:

```dotenv
PRT_ENABLED=true
PRT_DATA_PATH=prorealtime/snapshot.csv
PRT_MAX_DATA_AGE_SECONDS=900
PRT_CONFIRMATION_MODE=advisory
PRT_EXPORT_WATCHLIST_PATH=prorealtime/watchlist.csv
```

`PRT_DATA_PATH` should point to a local CSV or JSON file. A sample CSV is included at [`prorealtime/snapshot.example.csv`](./prorealtime/snapshot.example.csv). The bot treats ProRealTime as a secondary timing and confirmation layer; news, account state, and the bot's own research stay primary.

Additional deterministic guardrails are configurable via:

```dotenv
MAX_DAILY_LOSS_PCT=0.03
AUTO_FLATTEN_ON_DAILY_LOSS=true
DEFAULT_MAX_HOLDING_HOURS=72
TRAILING_EXIT_ACTIVATION_PCT=
TRAILING_EXIT_PCT=
MAX_SYMBOL_VOLATILITY_20D=
ENTRY_START_HOUR_UTC=0
ENTRY_END_HOUR_UTC=24
```

## Run

### Trading212 On Windows

For Trading212, use native Windows PowerShell instead of WSL. The repository now includes [`run_trading212_windows.ps1`](./run_trading212_windows.ps1), which:

- creates or reuses `.venv-win`
- installs the bot into that Windows virtualenv
- forces `BROKER=trading212` for that run
- auto-detects Firefox if `FIREFOX_BINARY_PATH` is not set in `.env`
- can force a visible Firefox session for first login
- can clear stale Trading212 cookies before retrying

From PowerShell in the repo root:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_trading212_windows.ps1 -Once -LogLevel INFO
```

Recommended first recovery attempt:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_trading212_windows.ps1 -Once -Headed -FreshSession -LogLevel INFO
```

That opens Firefox visibly, deletes the saved `_cookies.csv` session, and writes a fresh cookie cache if the Trading212 web app loads correctly. After that, retry the normal headless run.

If Selenium still cannot bootstrap the Trading212 app, the bot now also tries to import Trading212 cookies from your normal Firefox profile before launching WebDriver. That works only after you have logged into Trading212 manually in standard Firefox on the same Windows account. `TRADING212_FIREFOX_PROFILE_PATH` is optional and only needed if you want to force a non-default Firefox profile.

Interactive mode:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_trading212_windows.ps1 -Interactive -LogLevel INFO
```

Override symbols for a quick test:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_trading212_windows.ps1 -Once -Symbols "TSLA,NVDA" -LogLevel INFO
```

If the visible Windows run still shows `Couldn't load data`, the remaining blocker is Trading212's site/session on that machine or network rather than the bot code. In that case, log into Trading212 manually in the same Windows Firefox profile first and confirm the app loads before retrying the bot.

One cycle:

```bash
t212-cfd-bot --once
```

Continuous loop:

```bash
t212-cfd-bot
```

Continuous loop with terminal interaction:

```bash
t212-cfd-bot --interactive
```

Override the universe for a quick run:

```bash
t212-cfd-bot --once --symbols TSLA,NVDA,QQQ
```

## Terminal Interaction

When you run with `--interactive`, the bot opens a terminal prompt alongside the normal log output.

Available commands:

- `help`
- `status`
- `pause`
- `resume`
- `run`
- `note <text>`
- `focus <symbols>`
- `avoid <symbols>`
- `close <symbol>`
- `ask <question>`
- `clear`
- `quit`

`note`, `focus`, and `avoid` feed operator guidance into the model payload for the next cycle. `close <symbol>` queues a manual close request for the next cycle. `ask <question>` uses the model to answer questions about the bot's current runtime state.

Free-form terminal input is also model-interpreted. For example, you can type plain English like `close gold if we're still flat-to-down and focus on silver` and the model will translate that into concrete bot actions plus operator guidance for the next cycle.

## Terminal Output

During each cycle the bot now prints:

- the detected market regime and research summary
- ProRealTime sidecar status when available
- guardrail status, including daily drawdown and entry-window state
- the active symbols chosen for the cycle
- each model trade idea with confidence and thesis
- each approved execution plan with symbol, side, and reason
- each execution result as it happens

That means you can watch the terminal and see what the bot is trying to trade and why without opening the JSON log immediately.

## How The Bot Decides

The bot can work in two universe modes:

- explicit universe: you set `SYMBOLS`
- auto-discovery: leave `SYMBOLS` empty and set `AUTO_DISCOVER_SYMBOLS=true`

When auto-discovery is enabled, the model first proposes a shortlist from a built-in multi-asset universe, then the broker layer filters those symbols for tradeability before indicator collection.

If `PRT_ENABLED=true`, the bot also reads a local ProRealTime sidecar snapshot and attaches any matching per-symbol PRT context to the decision payload. That PRT context can include quote overlays, screener rank, trend bias, and entry signals. The prompts explicitly treat this as secondary confirmation only.

The built-in autonomous universe currently spans:

- liquid large-cap equities
- major ETFs
- major indices
- major FX pairs
- metals
- energy markets

You can narrow the discovery scope with `ALLOWED_ASSET_CLASSES`, for example:

```dotenv
ALLOWED_ASSET_CLASSES=index,forex,metal,energy
```

The autonomous research pipeline can also be left on with:

```dotenv
ENABLE_RESEARCH_SUBAGENTS=true
ALLOW_POSITION_REVERSALS=true
```

With that enabled, the bot uses three model roles each cycle:

- a macro market researcher that scans market headlines and proposes candidate CFD markets
- a symbol researcher that reviews each active instrument, including open positions
- a final portfolio manager that decides whether to `BUY`, `SELL`, `CLOSE`, or `HOLD`

For each active symbol, the bot collects:

- recent technical indicators: SMA20, SMA50, EMA12, EMA26, RSI14, MACD, ATR14, 1-day return, 5-day return, 20-day volatility
- recent symbol-specific headlines
- market-wide headlines
- analysis-focused headlines and source-filtered headlines from configured domains such as Bloomberg, Reuters, CNBC, MarketWatch, Yahoo Finance, FT, and WSJ
- optional ProRealTime sidecar context: price overlay, trend bias, entry signal, screener rank, support/resistance, and notes
- current account funds and open positions when Trading212 credentials are available
- current account funds and open positions when IG credentials are available

The OpenAI model returns a structured list of `BUY`, `SELL`, `HOLD`, or `CLOSE` decisions. The bot then enforces:

- confidence threshold
- max open positions
- max new trades per cycle
- per-trade risk sizing
- per-trade notional cap
- cooldown per symbol
- live-trading opt-in
- short-selling opt-in
- optional ProRealTime-required entry confirmation
- optional volatility cap for new entries
- daily loss cap with optional forced flattening
- max holding time
- optional trailing exit
- UTC entry window for new positions

If `ALLOW_SHORTS=true`, `SELL` is treated as opening a short CFD. If an opposite-direction position is already open and `ALLOW_POSITION_REVERSALS=true`, the risk layer will close the existing position first and then open the new one in the opposite direction.

## News Sources

The news layer still runs through Google News RSS, but it now issues multiple query types per market:

- a broad market or symbol query
- an analysis-focused version of that query
- source-filtered versions of the query for configured domains

By default, the preferred domains are:

- `bloomberg.com`
- `reuters.com`
- `cnbc.com`
- `marketwatch.com`
- `finance.yahoo.com`
- `ft.com`
- `wsj.com`

The resulting headlines carry `source_domain`, `source_priority`, and `query_label` metadata so the model can weight high-quality financial outlets and analysis-focused hits more heavily.

## Important Limitations

- The newer Playwright-based `src/` implementation in the cloned repo is not currently safe to trust for unattended order dispatch. This bot intentionally uses the older documented CFD methods instead.
- The upstream repo does not clearly document short-side payload semantics. This bot assumes short orders can be encoded by a negative quantity, but keeps shorting off by default until you verify it in demo mode.
- IG support uses the official REST API and now attempts to auto-resolve EPICs, but market-search ambiguity can still pick the wrong instrument if a ticker maps to multiple similar markets.
- Autonomous multi-asset discovery is constrained to the built-in instrument catalog so that Yahoo Finance symbols, news queries, and broker search terms stay aligned. If you want additional commodities or markets, add them to the catalog rather than relying on arbitrary free-form symbols.
- ProRealTime is integrated as a sidecar file feed here, not as a direct broker/API session. That is deliberate: it keeps the bot independent of unsupported or fragile platform automation assumptions.
- IG trade confirmations are polled from `/confirms/{dealReference}` after order acknowledgement. If the confirmation is delayed beyond the configured timeout, the bot records the acknowledgement and moves on.
- News and price data come from external public sources at runtime. If those sources fail, the bot will skip or degrade the cycle rather than guess.
- Exits are handled with attached stop-loss and take-profit distances plus model-driven `CLOSE` decisions and deterministic guardrails for daily loss, max holding time, and optional trailing exits. This is meaningfully safer than the initial scaffold, but it is still not a substitute for careful demo validation.

## Logs

Each run writes a JSON cycle log into `DECISION_LOG_DIR` so you can inspect:

- the account snapshot used
- the operator context active for that cycle
- the ProRealTime sidecar snapshot used for that cycle
- the guardrail state active for that cycle
- the market research subagent output
- symbol contexts sent to the model
- the symbol research subagent output
- the model decision
- approved execution plans
- broker responses or dry-run outputs
