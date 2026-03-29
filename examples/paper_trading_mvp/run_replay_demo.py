from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd


DEFAULT_OUTPUT_ROOT = Path.home().joinpath(".vntrader", "paper_mvp")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def jsonable(value):
    if isinstance(value, dict):
        return {key: jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def find_column(columns: List[str], candidates: List[str]) -> str:
    normalized = {column.lower(): column for column in columns}
    for candidate in candidates:
        original = normalized.get(candidate.lower())
        if original:
            return original
    raise KeyError(f"Missing required column. Tried: {candidates}")


def load_bars(csv_path: Path) -> pd.DataFrame:
    for encoding in ["utf-8", "gbk", "gb2312"]:
        try:
            frame = pd.read_csv(csv_path, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UnicodeDecodeError("utf-8", b"", 0, 1, f"Unable to decode {csv_path}")

    datetime_col = find_column(frame.columns.tolist(), ["datetime", "date", "timestamp", "time", "日期时间", "时间"])
    open_col = find_column(frame.columns.tolist(), ["open", "open_price", "开盘价", "开盘"])
    high_col = find_column(frame.columns.tolist(), ["high", "high_price", "最高价", "最高"])
    low_col = find_column(frame.columns.tolist(), ["low", "low_price", "最低价", "最低"])
    close_col = find_column(frame.columns.tolist(), ["close", "close_price", "收盘价", "收盘", "last_price"])
    volume_col = find_column(frame.columns.tolist(), ["volume", "成交量"])

    result = pd.DataFrame(
        {
            "datetime": pd.to_datetime(frame[datetime_col]),
            "open": pd.to_numeric(frame[open_col]),
            "high": pd.to_numeric(frame[high_col]),
            "low": pd.to_numeric(frame[low_col]),
            "close": pd.to_numeric(frame[close_col]),
            "volume": pd.to_numeric(frame[volume_col]),
        }
    )
    return result.sort_values("datetime").reset_index(drop=True)


def generate_synthetic_bars() -> pd.DataFrame:
    rows: List[dict] = []
    timestamps = pd.date_range("2026-03-30 09:00:00", periods=180, freq="1min")
    close_price = 100.0

    for index, timestamp in enumerate(timestamps):
        if index < 5:
            drift = 0.15 if index % 2 == 0 else -0.10
        elif index < 35:
            drift = 0.35
        elif index < 60:
            drift = -0.22
        elif index < 120:
            drift = 0.08
        else:
            drift = -0.05

        open_price = close_price
        close_price = round(close_price + drift, 2)
        high_price = round(max(open_price, close_price) + 0.25, 2)
        low_price = round(min(open_price, close_price) - 0.25, 2)
        volume = 1000 + index * 25

        rows.append(
            {
                "datetime": timestamp,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume,
            }
        )

    return pd.DataFrame(rows)


def score_contract(frame: pd.DataFrame) -> Tuple[float, float, float]:
    volume_delta = max(frame["volume"].iloc[-1] - frame["volume"].iloc[0], 0)
    opening_price = frame["close"].iloc[0]
    range_ratio = 0.0 if opening_price == 0 else (frame["high"].max() - frame["low"].min()) / opening_price
    score = (volume_delta + len(frame)) * range_ratio
    return score, volume_delta, range_ratio


def simulate_symbol(vt_symbol: str, frame: pd.DataFrame, config: dict) -> Tuple[List[dict], List[dict], dict]:
    selection_bars: int = int(config["selection_bars"])
    breakout_buffer_ticks: int = int(config["breakout_buffer_ticks"])
    order_size: int = int(config["order_size"])
    flat_after = datetime.strptime(config["flat_after"], "%H:%M").time()
    pricetick: float = float(config.get("pricetick", 1))
    contract_size: int = int(config.get("size", 1))

    opening = frame.iloc[:selection_bars]
    opening_high = float(opening["high"].max())
    opening_low = float(opening["low"].min())
    long_trigger = opening_high + pricetick * breakout_buffer_ticks
    short_trigger = opening_low - pricetick * breakout_buffer_ticks

    signals: List[dict] = []
    trades: List[dict] = []

    position = 0
    entry_price = 0.0
    entry_done = False
    realized_pnl = 0.0

    for row in frame.iloc[selection_bars:].itertuples(index=False):
        row_time = row.datetime.to_pydatetime()

        if row_time.time() >= flat_after and position != 0:
            exit_side = "SELL" if position > 0 else "COVER"
            exit_price = float(row.close)
            pnl = (exit_price - entry_price) * contract_size * order_size
            if position < 0:
                pnl *= -1
            realized_pnl += pnl
            trades.append(
                {
                    "datetime": row_time.isoformat(),
                    "vt_symbol": vt_symbol,
                    "action": exit_side,
                    "price": exit_price,
                    "volume": abs(position),
                    "pnl": round(pnl, 2),
                }
            )
            signals.append({
                "datetime": row_time.isoformat(),
                "vt_symbol": vt_symbol,
                "signal": "FORCE_FLAT",
            })
            position = 0
            break

        if position == 0 and not entry_done:
            if row.high >= long_trigger:
                entry_price = max(float(row.close), long_trigger)
                position = order_size
                entry_done = True
                trades.append(
                    {
                        "datetime": row_time.isoformat(),
                        "vt_symbol": vt_symbol,
                        "action": "BUY",
                        "price": entry_price,
                        "volume": order_size,
                        "pnl": 0.0,
                    }
                )
                signals.append({
                    "datetime": row_time.isoformat(),
                    "vt_symbol": vt_symbol,
                    "signal": "BUY_BREAKOUT",
                })
            elif row.low <= short_trigger:
                entry_price = min(float(row.close), short_trigger)
                position = -order_size
                entry_done = True
                trades.append(
                    {
                        "datetime": row_time.isoformat(),
                        "vt_symbol": vt_symbol,
                        "action": "SHORT",
                        "price": entry_price,
                        "volume": order_size,
                        "pnl": 0.0,
                    }
                )
                signals.append({
                    "datetime": row_time.isoformat(),
                    "vt_symbol": vt_symbol,
                    "signal": "SHORT_BREAKOUT",
                })
        elif position > 0 and row.low <= opening_low:
            exit_price = min(float(row.close), opening_low)
            pnl = (exit_price - entry_price) * contract_size * order_size
            realized_pnl += pnl
            trades.append(
                {
                    "datetime": row_time.isoformat(),
                    "vt_symbol": vt_symbol,
                    "action": "SELL",
                    "price": exit_price,
                    "volume": order_size,
                    "pnl": round(pnl, 2),
                }
            )
            signals.append({
                "datetime": row_time.isoformat(),
                "vt_symbol": vt_symbol,
                "signal": "EXIT_LONG",
            })
            position = 0
        elif position < 0 and row.high >= opening_high:
            exit_price = max(float(row.close), opening_high)
            pnl = (entry_price - exit_price) * contract_size * order_size
            realized_pnl += pnl
            trades.append(
                {
                    "datetime": row_time.isoformat(),
                    "vt_symbol": vt_symbol,
                    "action": "COVER",
                    "price": exit_price,
                    "volume": order_size,
                    "pnl": round(pnl, 2),
                }
            )
            signals.append({
                "datetime": row_time.isoformat(),
                "vt_symbol": vt_symbol,
                "signal": "EXIT_SHORT",
            })
            position = 0

    summary = {
        "vt_symbol": vt_symbol,
        "opening_high": opening_high,
        "opening_low": opening_low,
        "trade_count": len(trades),
        "final_position": position,
        "realized_pnl": round(realized_pnl, 2),
    }
    return signals, trades, summary


def main() -> int:
    config_path = Path(__file__).with_name("replay_demo_config.json")
    config = load_config(config_path)

    output_root = Path(config.get("output_root", str(DEFAULT_OUTPUT_ROOT)))
    if not output_root.is_absolute():
        output_root = Path.home().joinpath(output_root)
    output_dir = output_root.joinpath(datetime.now().strftime("replay_%Y%m%d_%H%M%S"))
    output_dir.mkdir(parents=True, exist_ok=True)

    selection_rows: List[dict] = []
    loaded_frames: Dict[str, pd.DataFrame] = {}
    data_notes: List[str] = []

    for contract in config["contracts"]:
        vt_symbol = contract["vt_symbol"]
        csv_file = Path(contract["csv_file"])
        if csv_file.exists():
            frame = load_bars(csv_file)
            data_notes.append(f"{vt_symbol} loaded from {csv_file}")
        elif config.get("allow_synthetic_if_missing", False):
            frame = generate_synthetic_bars()
            data_notes.append(f"{vt_symbol} used synthetic bars because {csv_file} was not found")
        else:
            raise FileNotFoundError(f"CSV file not found for {vt_symbol}: {csv_file}")

        if len(frame) < int(config["selection_bars"]) + 5:
            continue

        loaded_frames[vt_symbol] = frame
        score, volume_delta, range_ratio = score_contract(frame.iloc[: int(config["selection_bars"])] )
        selection_rows.append(
            {
                "vt_symbol": vt_symbol,
                "score": round(score, 8),
                "volume_delta": round(volume_delta, 2),
                "range_ratio": round(range_ratio, 8),
            }
        )

    selection_rows.sort(key=lambda item: item["score"], reverse=True)
    selected = [row["vt_symbol"] for row in selection_rows[: int(config["top_n"])] ]

    all_signals: List[dict] = []
    all_trades: List[dict] = []
    symbol_summaries: List[dict] = []

    for contract in config["contracts"]:
        vt_symbol = contract["vt_symbol"]
        if vt_symbol not in selected:
            continue
        frame = loaded_frames[vt_symbol]
        symbol_config = {
            **config,
            "pricetick": contract.get("pricetick", 1),
            "size": contract.get("size", 1),
        }
        signals, trades, summary = simulate_symbol(vt_symbol, frame, symbol_config)
        all_signals.extend(signals)
        all_trades.extend(trades)
        symbol_summaries.append(summary)

    pd.DataFrame(selection_rows).to_csv(output_dir.joinpath("selection.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(all_signals).to_csv(output_dir.joinpath("signals.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(all_trades).to_csv(output_dir.joinpath("trades.csv"), index=False, encoding="utf-8-sig")

    session_summary = {
        "created_at": datetime.now().isoformat(),
        "selected_symbols": selected,
        "selection_rows": selection_rows,
        "symbol_summaries": symbol_summaries,
        "data_notes": data_notes,
    }
    with output_dir.joinpath("session_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(jsonable(session_summary), handle, ensure_ascii=False, indent=4)

    print(f"Replay summary written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
