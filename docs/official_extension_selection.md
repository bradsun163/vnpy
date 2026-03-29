# Official Extension Selection

This document records which official vn.py ecosystem packages should be treated as the baseline for the current paper-trading project, so future work does not reimplement features that already exist upstream.

## Use Now

- `vnpy_ctp`
  - purpose: official CTP gateway for live market data and order routing
  - status on this machine: installed and bootstrapped successfully on the parallel `4.3.0` package line

- `vnpy_ctastrategy`
  - purpose: official CTA strategy engine for initialization, lifecycle, strategy loading, and real-time execution
  - status on this machine: installed and bootstrapped successfully on the parallel `4.3.0` package line

- `vnpy_paperaccount`
  - purpose: official local paper-trading execution module
  - status on this machine: installed and bootstrapped successfully on the parallel `4.3.0` package line

- `vnpy_riskmanager`
  - purpose: official pre-trade risk manager for order flow, active orders, duplicate order checks, order validity, cancel limits, and other front-end risk rules
  - status on this machine: installed from source successfully on Python `3.12`, bootstrapped successfully with `CTP + CTA + PaperAccount`, and now wired into the current live paper runner with isolated `paper_mvp_risk_manager_setting.json`
  - decision: baseline live-paper stack component; repository-level risk code should now focus only on session-state controls that official pre-trade risk does not cover

- `vnpy_datamanager`
  - purpose: official historical data inspection, import, and export app
  - status on this machine: installed successfully and bootstrapped successfully together with `CTP + CTA + PaperAccount + RiskManager`
  - decision: should be the default path for local historical data inspection and later validation-stage data checks

- `vnpy_datarecorder`
  - purpose: official market data recording app for tick/bar persistence
  - status on this machine: installed successfully and bootstrapped successfully after also installing official `vnpy_spreadtrading`; now wired into the live paper runner with isolated `paper_mvp_data_recorder_setting.json`
  - decision: should be the default path for market-data accumulation instead of building a custom recorder layer

- `vnpy_spreadtrading`
  - purpose: official spread trading module
  - status on this machine: installed successfully as a required dependency for the current `vnpy_datarecorder` package import path
  - decision: not needed for the current strategy logic itself, but should remain in the parallel package set so `vnpy_datarecorder` loads cleanly

## Use Soon

- `vnpy_ctabacktester`
  - purpose: official CTA research and backtesting app
  - decision: should be used for CTA strategy research workflows, but it is not required for the current live-paper runtime itself

## Not Needed Yet

- `vnpy_scripttrader`
  - useful for script-style trading and REPL workflows, but not necessary for the current CTA-centered paper pipeline

- `vnpy_portfoliostrategy`
  - useful once the strategy model becomes truly multi-contract or portfolio-level

- `vnpy_webtrader`, `vnpy_rpcservice`, `vnpy_portfoliomanager`
  - useful for multi-process, remote, or team workflows, but premature for the current single-machine paper phase

## Gap That Still Needs Custom Code

The official `vnpy_riskmanager` module covers pre-trade risk checks well, but it does not replace all session-level controls needed for this project.

Custom code is still justified for:

- session realized PnL hard stops
- consecutive losing trade halts
- maximum holding time per position
- time-of-day entry cutoffs that are strategy-session specific
- paper-session summaries and custom risk artifacts tied to this repository

Decision rule:

- if a feature blocks an order before it is sent, prefer `vnpy_riskmanager`
- if a feature depends on realized trades, session state, or strategy-specific holding logic, keep it in the repository-level paper runtime