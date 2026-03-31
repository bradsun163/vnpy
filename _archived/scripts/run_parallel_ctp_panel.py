from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[1]
PACKAGE_ROOT = REPO_ROOT.parent / "vnpy-4.3.0-packages"
DEFAULT_RUNTIME_PYTHON = Path(r"C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe")
DEFAULT_ARTIFACT_ROOT = Path.home() / ".vntrader" / "paper_mvp"
DEFAULT_CONFIG_PATH = REPO_ROOT / "examples" / "paper_trading_mvp" / "live_ctp_paper_config.json"


def bootstrap_import_paths() -> None:
    for path in (REPO_ROOT, PACKAGE_ROOT):
        if path.exists():
            path_text = str(path)
            if path_text not in sys.path:
                sys.path.insert(0, path_text)


bootstrap_import_paths()

from flask import Flask, jsonify, render_template, request  # noqa: E402

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a real-time browser panel for the vn.py parallel paper-trading runtime."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--artifacts-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--dump-json", action="store_true")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--stream-limit", type=int, default=80)
    return parser.parse_args(argv)


def get_local_now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def get_beijing_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        current = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if current.tzinfo is not None:
        return current.astimezone().replace(tzinfo=None)
    return current


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


def safe_load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl_tail(path: Path, limit: int) -> List[dict]:
    if not path.exists():
        return []

    buffer: deque[str] = deque(maxlen=max(limit, 1))
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    buffer.append(line)
    except OSError:
        return []

    rows: List[dict] = []
    for line in buffer:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def read_jsonl_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []

    rows: List[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def list_run_dirs(artifact_root: Path) -> List[Path]:
    if not artifact_root.exists():
        return []
    return sorted(
        [path for path in artifact_root.iterdir() if path.is_dir() and path.name.startswith("live_")],
        key=lambda current: current.stat().st_mtime,
        reverse=True,
    )


def resolve_run_dir(artifact_root: Path, run_name: str) -> Path | None:
    candidates = list_run_dirs(artifact_root)
    if not candidates:
        return None
    if run_name:
        explicit = artifact_root / run_name
        if explicit.exists() and explicit.is_dir():
            return explicit
    return candidates[0]


def split_vt_symbol(vt_symbol: str) -> tuple[str, str] | tuple[None, None]:
    if not vt_symbol or "." not in vt_symbol:
        return None, None
    symbol, exchange = vt_symbol.split(".", 1)
    return symbol, exchange


def summarize_logic(config: dict) -> List[str]:
    runtime = dict(config.get("runtime", {}))
    strategy = dict(config.get("strategy", {}))
    risk = dict(config.get("risk", {}))
    universe = [str(item) for item in config.get("universe", [])]

    max_loss = risk.get("max_session_realized_loss", "")
    consecutive = risk.get("max_consecutive_losing_trades", "")

    return [
        f"Universe: {', '.join(universe) if universe else 'N/A'}",
        f"Selection window: {runtime.get('selection_window_minutes', 'N/A')} minutes with min {runtime.get('min_ticks_per_symbol', 'N/A')} ticks per symbol.",
        f"Breakout trigger: opening range plus/minus {strategy.get('breakout_buffer_ticks', 'N/A')} tick buffer.",
        f"Entry cutoff: {risk.get('no_new_entry_after', 'N/A')} Beijing time; flat-after: {strategy.get('flat_after', 'N/A')} Beijing time.",
        f"Risk caps: max {risk.get('max_selected_symbols', 'N/A')} selected symbols, {risk.get('max_trades_per_symbol', 'N/A')} trade per symbol, position size {risk.get('max_position_per_symbol', 'N/A')}.",
        f"Hard stops: realized loss {max_loss} and consecutive losing trades {consecutive}.",
    ]


def build_streams(run_dir: Path, limit: int) -> Dict[str, List[dict]]:
    names = [
        "logs",
        "orders",
        "trades",
        "positions",
        "risk_events",
        "signals",
        "recorder",
        "accounts",
        "strategy_states",
    ]
    streams: Dict[str, List[dict]] = {}
    for name in names:
        streams[name] = read_jsonl_tail(run_dir / f"{name}.jsonl", limit)
    return streams


def dedupe_symbol_states(summary: dict) -> List[dict]:
    payload = summary.get("strategy_states", {}) or {}
    strategies = payload.get("strategies", []) if isinstance(payload, dict) else []
    if not isinstance(strategies, list):
        return []
    return strategies


def build_symbol_list(summary: dict, streams: Dict[str, List[dict]]) -> List[str]:
    symbols: List[str] = []
    for vt_symbol in summary.get("selected_symbols", []):
        if vt_symbol and vt_symbol not in symbols:
            symbols.append(vt_symbol)
    for row in summary.get("selection_rankings", []):
        vt_symbol = row.get("vt_symbol")
        if vt_symbol and vt_symbol not in symbols:
            symbols.append(vt_symbol)
    for state in dedupe_symbol_states(summary):
        vt_symbol = state.get("vt_symbol")
        if vt_symbol and vt_symbol not in symbols:
            symbols.append(vt_symbol)
    for trade in streams.get("trades", []):
        vt_symbol = trade.get("data", {}).get("vt_symbol")
        if vt_symbol and vt_symbol not in symbols:
            symbols.append(vt_symbol)
    for order in streams.get("orders", []):
        vt_symbol = order.get("data", {}).get("vt_symbol")
        if vt_symbol and vt_symbol not in symbols:
            symbols.append(vt_symbol)
    return symbols


def build_equity_curve(run_dir: Path, summary: dict) -> Dict[str, Any]:
    account_rows = read_jsonl_rows(run_dir / "accounts.jsonl")
    if account_rows:
        prioritized_rows: List[dict] = []
        fallback_rows: List[dict] = []
        for row in account_rows:
            data = row.get("data", {})
            gateway_name = str(data.get("gateway_name", "") or "").upper()
            if gateway_name and gateway_name != "PAPER":
                prioritized_rows.append(row)
            else:
                fallback_rows.append(row)

        selected_account_rows: List[dict] = prioritized_rows or fallback_rows
        points: List[dict] = []
        selected_gateway_name: str = ""
        for row in selected_account_rows:
            data = row.get("data", {})
            timestamp = parse_datetime(row.get("timestamp") or data.get("datetime"))
            if not timestamp:
                continue
            if not selected_gateway_name:
                selected_gateway_name = str(data.get("gateway_name", "") or "")
            points.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "balance": float(data.get("balance", 0) or 0),
                    "available": float(data.get("available", 0) or 0),
                    "frozen": float(data.get("frozen", 0) or 0),
                }
            )

        if points:
            points.sort(key=lambda item: item["timestamp"])
            return {
                "source": f"simnow_account_balance:{selected_gateway_name or 'ACCOUNT'}",
                "warning": "",
                "points": points,
            }

    trade_rows = read_jsonl_rows(run_dir / "trades.jsonl")
    position_rows = read_jsonl_rows(run_dir / "positions.jsonl")
    symbol_states = summary.get("risk", {}).get("session", {}).get("symbol_states", {})

    realized_timeline: Dict[str, float] = {}
    applied_symbols: set[str] = set()
    cumulative_realized: float = 0.0
    ordered_trades = sorted(
        trade_rows,
        key=lambda row: parse_datetime(row.get("data", {}).get("datetime") or row.get("timestamp")) or datetime.min,
    )
    for row in ordered_trades:
        data = row.get("data", {})
        timestamp = parse_datetime(data.get("datetime") or row.get("timestamp"))
        vt_symbol = data.get("vt_symbol")
        if not timestamp:
            continue
        if str(data.get("offset", "")).lower() == "close" and vt_symbol and vt_symbol not in applied_symbols:
            cumulative_realized += float(symbol_states.get(vt_symbol, {}).get("realized_pnl", 0) or 0)
            applied_symbols.add(vt_symbol)
        realized_timeline[timestamp.isoformat()] = cumulative_realized

    current_positions: Dict[str, dict] = {}
    floating_timeline: Dict[str, float] = {}
    for row in sorted(position_rows, key=lambda item: parse_datetime(item.get("timestamp")) or datetime.min):
        data = row.get("data", {})
        timestamp = parse_datetime(row.get("timestamp"))
        vt_positionid = data.get("vt_positionid")
        if not timestamp or not vt_positionid:
            continue
        current_positions[vt_positionid] = data
        floating_timeline[timestamp.isoformat()] = float(
            sum(float(position.get("pnl", 0) or 0) for position in current_positions.values())
        )

    points: List[dict] = []
    current_realized: float = 0.0
    current_floating: float = 0.0
    all_timestamps = sorted(set(realized_timeline) | set(floating_timeline))
    for timestamp in all_timestamps:
        if timestamp in realized_timeline:
            current_realized = realized_timeline[timestamp]
        if timestamp in floating_timeline:
            current_floating = floating_timeline[timestamp]
        combined = current_realized + current_floating
        points.append(
            {
                "timestamp": timestamp,
                "balance": combined,
                "available": combined,
                "frozen": 0.0,
                "realized": current_realized,
                "floating": current_floating,
            }
        )

    realized = (
        summary.get("risk", {})
        .get("session", {})
        .get("session_realized_pnl", 0)
    )
    warning = (
        "Using reconstructed equity because no account event stream is available for this run. "
        f"The curve combines final realized trade closures with open-position PnL. Current realized session PnL snapshot: {realized}."
    )
    return {
        "source": "realized_plus_position_proxy",
        "warning": warning,
        "points": points,
    }


def filter_symbol_rows(rows: Iterable[dict], vt_symbol: str) -> List[dict]:
    filtered: List[dict] = []
    for row in rows:
        data = row.get("data", {}) if isinstance(row, dict) else {}
        if data.get("vt_symbol") == vt_symbol or row.get("vt_symbol") == vt_symbol:
            filtered.append(row)
        elif row.get("type") == "selection_complete" and vt_symbol in row.get("selected_symbols", []):
            filtered.append(row)
    return filtered


def build_chart_payload(vt_symbol: str, summary: dict, streams: Dict[str, List[dict]], lookback_days: int) -> Dict[str, Any]:
    if not vt_symbol:
        return {
            "symbol": "",
            "bars": [],
            "coverage": {},
            "trade_markers": [],
            "signal_markers": [],
            "levels": {},
        }

    symbol, exchange_text = split_vt_symbol(vt_symbol)
    if not symbol or not exchange_text:
        return {
            "symbol": vt_symbol,
            "bars": [],
            "coverage": {},
            "trade_markers": [],
            "signal_markers": [],
            "levels": {},
        }

    start = get_beijing_now() - timedelta(days=max(lookback_days, 1))
    end = get_beijing_now() + timedelta(minutes=1)

    bars: List[dict] = []
    coverage: Dict[str, Any] = {
        "lookback_days": lookback_days,
        "bar_count": 0,
        "oldest": None,
        "newest": None,
        "coverage_days": 0.0,
        "meets_week_requirement": False,
        "warning": "",
    }

    try:
        database = get_database()
        bar_rows = database.load_bar_data(
            symbol,
            Exchange(exchange_text),
            Interval.MINUTE,
            start,
            end,
        )
    except Exception as exc:
        bar_rows = []
        coverage["warning"] = f"Database load failed: {exc}"

    for bar in bar_rows:
        bar_time = parse_datetime(bar.datetime)
        if not bar_time:
            continue
        bars.append(
            {
                "timestamp": bar_time.isoformat(),
                "open": float(bar.open_price),
                "high": float(bar.high_price),
                "low": float(bar.low_price),
                "close": float(bar.close_price),
                "volume": float(bar.volume),
            }
        )

    if bars:
        coverage["bar_count"] = len(bars)
        coverage["oldest"] = bars[0]["timestamp"]
        coverage["newest"] = bars[-1]["timestamp"]
        oldest_dt = parse_datetime(coverage["oldest"])
        newest_dt = parse_datetime(coverage["newest"])
        if oldest_dt and newest_dt:
            coverage_days = round((newest_dt - oldest_dt).total_seconds() / 86400, 3)
            coverage["coverage_days"] = coverage_days
            coverage["meets_week_requirement"] = coverage_days >= 6.5
            if not coverage["meets_week_requirement"]:
                coverage["warning"] = (
                    coverage["warning"]
                    or "The current database does not yet contain a full week of 1-minute history for this symbol."
                )
    else:
        coverage["warning"] = coverage["warning"] or "No 1-minute bars are currently available in the vn.py database for this symbol."

    trade_markers: List[dict] = []
    for row in filter_symbol_rows(streams.get("trades", []), vt_symbol):
        data = row.get("data", {})
        when = parse_datetime(data.get("datetime") or row.get("timestamp"))
        if not when:
            continue
        trade_markers.append(
            {
                "timestamp": when.isoformat(),
                "price": float(data.get("price", 0) or 0),
                "direction": data.get("direction", ""),
                "offset": data.get("offset", ""),
                "volume": float(data.get("volume", 0) or 0),
            }
        )

    signal_markers: List[dict] = []
    for row in filter_symbol_rows(streams.get("signals", []), vt_symbol):
        when = parse_datetime(row.get("timestamp"))
        if not when:
            continue
        signal_markers.append(
            {
                "timestamp": when.isoformat(),
                "signal": row.get("signal") or row.get("type", "signal"),
                "selected_symbols": row.get("selected_symbols", []),
            }
        )

    levels: Dict[str, Any] = {}
    for state in dedupe_symbol_states(summary):
        if state.get("vt_symbol") != vt_symbol:
            continue
        variables = state.get("variables", {})
        levels = {
            "opening_high": variables.get("opening_high"),
            "opening_low": variables.get("opening_low"),
            "last_signal": variables.get("last_signal"),
            "pos": variables.get("pos"),
            "entry_done": variables.get("entry_done"),
            "range_ready": variables.get("range_ready"),
        }
        break

    return {
        "symbol": vt_symbol,
        "bars": bars,
        "coverage": coverage,
        "trade_markers": trade_markers,
        "signal_markers": signal_markers,
        "levels": levels,
    }


def build_listener_summary(summary: dict, run_dir: Path, config_path: Path) -> Dict[str, Any]:
    heartbeat = summary.get("heartbeat", {}) or {}
    risk_session = summary.get("risk", {}).get("session", {})
    selection_completed = heartbeat.get("selection_completed")
    if selection_completed is None:
        selection_completed = bool(summary.get("selected_symbols")) or summary.get("status") in {
            "running",
            "completed",
            "selection_failed",
            "risk_limit_triggered",
            "interrupted",
        }
    return {
        "status": summary.get("status", "unknown"),
        "run_name": summary.get("run_name", run_dir.name),
        "run_dir": str(run_dir),
        "pid": heartbeat.get("pid"),
        "gateway": heartbeat.get("gateway") or "CTP",
        "config_path": heartbeat.get("config_path") or str(config_path),
        "selected_symbols": summary.get("selected_symbols", []),
        "order_count": int(summary.get("order_count", 0) or 0),
        "trade_count": int(summary.get("trade_count", 0) or 0),
        "session_realized_pnl": float(risk_session.get("session_realized_pnl", 0) or 0),
        "hard_stop_triggered": bool(risk_session.get("hard_stop_triggered", False)),
        "heartbeat_local_time": heartbeat.get("local_time"),
        "heartbeat_beijing_time": heartbeat.get("beijing_time"),
        "selection_completed": selection_completed,
    }


def build_dashboard_state(
    artifact_root: Path,
    config_path: Path,
    run_name: str,
    symbol: str,
    lookback_days: int,
    stream_limit: int,
) -> Dict[str, Any]:
    run_dir = resolve_run_dir(artifact_root, run_name)
    config = safe_load_json(config_path, {})
    runs = list_run_dirs(artifact_root)

    base_state: Dict[str, Any] = {
        "machine_time": get_local_now().isoformat(),
        "beijing_time": get_beijing_now().isoformat(),
        "artifact_root": str(artifact_root),
        "config_path": str(config_path),
        "available_runs": [path.name for path in runs],
        "logic": summarize_logic(config),
        "summary": {},
        "listener": {},
        "streams": {},
        "strategy_states": [],
        "rankings": [],
        "symbols": [],
        "active_symbol": "",
        "chart": {},
        "equity": {"source": "none", "warning": "", "points": []},
        "warnings": [],
    }

    if not run_dir:
        base_state["warnings"].append("No live paper-trading run directory exists yet.")
        return base_state

    summary = safe_load_json(run_dir / "session_summary.json", {})
    streams = build_streams(run_dir, stream_limit)
    strategy_states = dedupe_symbol_states(summary)
    symbols = build_symbol_list(summary, streams)
    active_symbol = symbol if symbol in symbols else (symbols[0] if symbols else "")
    chart = build_chart_payload(active_symbol, summary, streams, lookback_days)
    equity = build_equity_curve(run_dir, summary)

    warnings: List[str] = []
    if chart.get("coverage", {}).get("warning"):
        warnings.append(chart["coverage"]["warning"])
    if equity.get("warning"):
        warnings.append(equity["warning"])

    base_state.update(
        {
            "summary": jsonable(summary),
            "listener": jsonable(build_listener_summary(summary, run_dir, config_path)),
            "streams": jsonable(streams),
            "strategy_states": jsonable(strategy_states),
            "rankings": jsonable(summary.get("selection_rankings", [])),
            "symbols": symbols,
            "active_symbol": active_symbol,
            "chart": jsonable(chart),
            "equity": jsonable(equity),
            "warnings": warnings,
        }
    )
    return base_state


def create_app(
    artifact_root: Path,
    config_path: Path,
    default_run_name: str,
    default_symbol: str,
    default_lookback_days: int,
    default_stream_limit: int,
) -> Flask:
    app = Flask(__name__, template_folder=str(SCRIPT_PATH.parent / "templates"))

    @app.route("/")
    def index() -> str:
        return render_template(
            "parallel_ctp_panel.html",
            initial_run_name=default_run_name,
            initial_symbol=default_symbol,
            default_lookback_days=default_lookback_days,
            default_stream_limit=default_stream_limit,
        )

    @app.route("/api/dashboard")
    def dashboard() -> Any:
        run_name = str(request.args.get("run_name", default_run_name or ""))
        symbol = str(request.args.get("symbol", default_symbol or ""))
        lookback_days = int(request.args.get("lookback_days", default_lookback_days) or default_lookback_days)
        stream_limit = int(request.args.get("stream_limit", default_stream_limit) or default_stream_limit)

        state = build_dashboard_state(
            artifact_root=artifact_root,
            config_path=config_path,
            run_name=run_name,
            symbol=symbol,
            lookback_days=lookback_days,
            stream_limit=stream_limit,
        )
        return jsonify(state)

    @app.route("/api/health")
    def health() -> Any:
        return jsonify({"status": "ok", "url": request.host_url.rstrip("/")})

    return app


def resolve_runtime_python() -> Path:
    if DEFAULT_RUNTIME_PYTHON.exists():
        return DEFAULT_RUNTIME_PYTHON
    return Path(sys.executable).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    artifact_root = Path(args.artifacts_root).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()

    state = build_dashboard_state(
        artifact_root=artifact_root,
        config_path=config_path,
        run_name=args.run_name,
        symbol=args.symbol,
        lookback_days=args.lookback_days,
        stream_limit=args.stream_limit,
    )

    if args.dump_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    app = create_app(
        artifact_root=artifact_root,
        config_path=config_path,
        default_run_name=args.run_name,
        default_symbol=args.symbol,
        default_lookback_days=args.lookback_days,
        default_stream_limit=args.stream_limit,
    )

    url = f"http://{args.host}:{args.port}"
    runtime_python = resolve_runtime_python()
    print(f"Panel runtime python: {runtime_python}")
    print(f"Panel URL: {url}")
    print(f"Artifacts root: {artifact_root}")
    if args.open_browser:
        webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())