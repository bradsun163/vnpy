# Paper Trading MVP

This folder contains a minimal paper trading workflow built for the current vn.py 3.9.0 repository.

## Goals

- Keep the runtime headless and scriptable.
- Never send live orders by default.
- Use local paper execution through `PaperAccount`.
- Isolate MVP state from your existing CTA strategy files.
- Provide both a live gateway entry and an offline replay entry.

## Live Entry

Run:

```powershell
C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe .\examples\paper_trading_mvp\run_live_ctp_paper.py
```

What it does:

- boots `MainEngine + CTP/CTPTEST + official RiskManager + CTA + isolated PaperAccount`
- forces SQLite for this run
- loads official `DataRecorderApp` with isolated `paper_mvp_data_recorder_setting.json`
- expands root symbols like `MA.CZCE` into all currently available real-month contracts from that exchange
- watches the first 5 minutes of ticks
- ranks candidates by early-session activity
- resolves the most active real-month contract for each configured root before final trading selection
- enables trading only on the top ranked contracts
- trades a single-entry opening range breakout strategy
- writes logs, orders, trades, positions, official pre-trade risk snapshots, and a session summary under `.vntrader/paper_mvp`

Important:

- the script uses isolated files named `paper_mvp_*` and does not reuse your existing `cta_strategy_setting.json`
- the official pre-trade risk rules are stored separately in `paper_mvp_risk_manager_setting.json`
- the official recorder rules are stored separately in `paper_mvp_data_recorder_setting.json`
- if the external gateway does not provide contract info or ticks, the run exits safely without sending orders

## Hard Risk Controls

The live paper configs now combine two layers under the `risk` section:

- `official_riskmanager`: official pre-trade checks that intercept invalid or excessive orders before they are sent
- session-level controls in the repository runtime: realized PnL stop, max holding time, entry cutoff, and repeated-loss halt

Session-level limits still include:

- `max_trades_per_symbol`: maximum number of new entries per symbol for the session
- `max_holding_minutes`: maximum holding time before the strategy is forced to flatten
- `max_session_realized_loss`: session-level realized PnL hard stop
- `max_consecutive_losing_trades`: hard stop after repeated realized losses
- `no_new_entry_after`: stop opening new positions after this time
- `force_flat_on_hard_stop`: when true, request all open paper positions to be flattened after a hard-stop event

Risk state is written into each run's `session_summary.json`, including both `official_pretrade` and `session` snapshots, and any triggered hard-stop or official risk events are appended to `risk_events.jsonl`.

## Official Recorder

The live paper configs also include a `recorder` section for official `vnpy_datarecorder`:

- `enabled`: whether to load `DataRecorderApp`
- `record_tick`: record tick data for armed symbols
- `record_bar`: record 1-minute bars generated from ticks
- `shrink_to_selected_after_selection`: after the early-session ranking finishes, stop recording contracts that were not selected for trading
- `whitelist`: optional vt-symbol whitelist; if left empty, the recorder arms the full contract list that becomes available for this run

Recorder startup timing is fixed deliberately: it arms only after contract metadata is available, and it does so before the early-session selection window finishes. That preserves the first-stage ranking data while avoiding a pre-contract startup race.

With `shrink_to_selected_after_selection=true`, the recorder starts broad and then narrows itself after selection completes. In practice this means:

- the opening-window ranking still sees the full candidate universe
- after selection, only the contracts that will actually be traded keep generating new recorder traffic
- the run summary records both the preselection recorder symbols and the symbols kept after the shrink step

## Dynamic Root Universe

The `universe` section can now contain either concrete tradable contracts or product-root entries:

- concrete contract example: `MA605.CZCE`
- dynamic root example: `MA.CZCE`

When a root entry is used, the runner does not trade the root symbol itself. Instead it:

- waits for contract metadata from the gateway
- expands the root into the currently listed real-month contracts on that exchange
- observes early-session activity across those months
- keeps the most active real-month contract for that root as the tradable candidate

This is the safe way to follow the current active contract while still sending orders only to real exchange-listed month contracts.

The recorder state is written into each run's `session_summary.json` under `recorder`, and detailed recorder updates are appended to `recorder.jsonl`.

## Parallel 4.3.0 Entry

If you want to run the same MVP against the parallel official `4.3.0` package line instead of the local repo's embedded core, use:

```powershell
.\scripts\run_parallel_paper_mvp.ps1 -Mode Live -Gateway CTP
.\scripts\run_parallel_paper_mvp.ps1 -Mode Live -Gateway CTPTEST
.\scripts\run_parallel_paper_mvp.ps1 -Mode Replay
```

To start the launcher now but delay the actual run until a target local time, use:

```powershell
.\scripts\run_parallel_paper_mvp.ps1 -Mode Live -Gateway CTP -WaitUntilLocalTime 08:55
```

Timezone note:

- `-WaitUntilLocalTime` always uses the computer's local clock
- if the machine is currently on US Eastern Daylight Time (`UTC-4`), then Beijing `08:59` maps to local `20:59` on the previous evening

Behavior of delayed launch:

- the script waits until the next occurrence of the specified local time
- if you run it before that time, it starts later the same day
- if you run it after that time, it starts the next day
- launcher output is written to `.vntrader\paper_mvp\launcher_logs` so you can check what happened after waking up

What it does:

- uses the system Python interpreter directly
- prefixes `PYTHONPATH` with `C:\Users\bradsun\vnpy-4.3.0-packages` and the current repo root
- uses the official `vnpy_riskmanager`, `vnpy_datamanager`, `vnpy_datarecorder`, and `vnpy_spreadtrading` packages installed into the parallel package target
- keeps your custom `paper_trading_mvp` code available without letting the local `vnpy 3.9.0` source tree shadow the parallel `4.3.0` package line

## Replay Entry

Run:

```powershell
C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe .\examples\paper_trading_mvp\run_replay_demo.py
```

What it does:

- loads one or more minute-bar CSV files from `replay_demo_config.json`
- falls back to deterministic synthetic bars if the configured CSV is missing and `allow_synthetic_if_missing=true`
- ranks symbols with the same early-session logic
- runs a simplified opening range breakout paper simulation
- writes `selection.csv`, `signals.csv`, `trades.csv`, and `session_summary.json`

## Output

All runtime artifacts are written below:

- `C:\Users\bradsun\.vntrader\paper_mvp`

Each run gets its own timestamped folder.