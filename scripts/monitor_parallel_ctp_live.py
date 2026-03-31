from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


OUTPUT_ROOT = Path.home() / ".vntrader" / "paper_mvp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor the latest paper trading live run and display heartbeat, status, trades, and logs."
    )
    parser.add_argument("--run-name", default="", help="Specific live_* directory name under ~/.vntrader/paper_mvp")
    parser.add_argument("--follow", action="store_true", help="Refresh continuously")
    parser.add_argument("--interval", type=float, default=2.0, help="Refresh interval in seconds when --follow is used")
    parser.add_argument("--lines", type=int, default=8, help="Number of recent rows to show per stream")
    return parser.parse_args()


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def format_dt(value: datetime | None) -> str:
    if not value:
        return "-"
    return value.isoformat(sep=" ", timespec="seconds")


def format_age(value: datetime | None) -> str:
    if not value:
        return "unknown"

    now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
    delta = now - value
    seconds = int(max(delta.total_seconds(), 0))
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def last_lines(path: Path, count: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return [line.rstrip("\n") for line in lines[-count:]]


def load_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback or {})

    for _ in range(5):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            time.sleep(0.1)

    return dict(fallback or {})


def load_jsonl_tail(path: Path, count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in last_lines(path, count):
        text = line.strip()
        if not text:
            continue
        try:
            rows.append(json.loads(text))
        except json.JSONDecodeError:
            rows.append({"raw": text})
    return rows


def file_mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime)


def resolve_run_dir(run_name: str) -> Path:
    if run_name:
        run_dir = OUTPUT_ROOT / run_name
        if not run_dir.exists():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")
        return run_dir

    candidates = sorted(
        [path for path in OUTPUT_ROOT.iterdir() if path.is_dir() and path.name.startswith("live_")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No live_* directories found under {OUTPUT_ROOT}")
    return candidates[0]


def get_live_runner_pids() -> list[int]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'paper_trading_mvp\\.live_runner' } | Select-Object -ExpandProperty ProcessId",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return []
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]


def extract_latest_timestamp(summary: dict[str, Any], streams: Iterable[list[dict[str, Any]]]) -> datetime | None:
    candidates: list[datetime] = []

    candidates.append(parse_dt(summary.get("created_at")))
    candidates.append(parse_dt(summary.get("closed_at")))
    heartbeat = summary.get("heartbeat") or {}
    if isinstance(heartbeat, dict):
        candidates.append(parse_dt(heartbeat.get("local_time")))
        candidates.append(parse_dt(heartbeat.get("beijing_time")))

    for note in summary.get("notes", [])[-10:]:
        if isinstance(note, str) and len(note) >= 19:
            candidates.append(parse_dt(note.split(" ", 1)[0]))

    for rows in streams:
        for row in rows:
            candidates.append(parse_dt(row.get("timestamp")))
            data = row.get("data")
            if isinstance(data, dict):
                candidates.append(parse_dt(data.get("datetime")))
                candidates.append(parse_dt(data.get("time")))

    valid = [item for item in candidates if item is not None]
    if not valid:
        return None
    return max(valid)


def position_lines(summary: dict[str, Any]) -> list[str]:
    positions = summary.get("open_positions", []) or []
    lines: list[str] = []
    for position in positions:
        vt_symbol = position.get("vt_symbol", "?")
        direction = position.get("direction", "?")
        volume = position.get("volume", 0)
        price = position.get("price", 0)
        pnl = position.get("pnl", 0)
        if volume:
            lines.append(f"{vt_symbol} {direction} vol={volume} price={price} pnl={pnl}")
    return lines or ["none"]


def render_event_rows(title: str, rows: list[dict[str, Any]]) -> list[str]:
    output = [title]
    if not rows:
        output.append("  (none)")
        return output

    for row in rows:
        timestamp = row.get("timestamp", "-")
        if "raw" in row:
            output.append(f"  {timestamp} {row['raw']}")
            continue

        if row.get("type") == "risk_event":
            output.append(f"  {timestamp} {row.get('message', '')}")
            continue

        data = row.get("data")
        if isinstance(data, dict):
            msg = data.get("msg") or data.get("vt_symbol") or data.get("symbol") or json.dumps(data, ensure_ascii=False)
            output.append(f"  {timestamp} {msg}")
        else:
            output.append(f"  {timestamp} {str(data or row)}")
    return output


def build_screen(run_dir: Path, lines: int, previous_summary: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    summary = load_json(run_dir / "session_summary.json", fallback=previous_summary)
    trades = load_jsonl_tail(run_dir / "trades.jsonl", lines)
    risks = load_jsonl_tail(run_dir / "risk_events.jsonl", lines)
    logs = load_jsonl_tail(run_dir / "logs.jsonl", lines)
    orders = load_jsonl_tail(run_dir / "orders.jsonl", lines)
    positions = load_jsonl_tail(run_dir / "positions.jsonl", lines)
    signals = load_jsonl_tail(run_dir / "signals.jsonl", lines)

    latest_event = extract_latest_timestamp(summary, [trades, risks, logs, orders, positions, signals])
    latest_disk_write = max(
        [
            item
            for item in [
                file_mtime(run_dir / "session_summary.json"),
                file_mtime(run_dir / "logs.jsonl"),
                file_mtime(run_dir / "orders.jsonl"),
                file_mtime(run_dir / "trades.jsonl"),
                file_mtime(run_dir / "positions.jsonl"),
                file_mtime(run_dir / "risk_events.jsonl"),
                file_mtime(run_dir / "signals.jsonl"),
                file_mtime(run_dir / "recorder.jsonl"),
            ]
            if item is not None
        ],
        default=None,
    )
    pids = get_live_runner_pids()

    run_name = summary.get("run_name", run_dir.name)
    status = summary.get("status", "unknown")
    created_at = parse_dt(summary.get("created_at"))
    heartbeat = summary.get("heartbeat") or {}
    heartbeat_local = heartbeat.get("local_time", "-") if isinstance(heartbeat, dict) else "-"
    heartbeat_beijing = heartbeat.get("beijing_time", "-") if isinstance(heartbeat, dict) else "-"
    selected_symbols = ", ".join(summary.get("selected_symbols", []) or []) or "-"
    order_count = summary.get("order_count", 0)
    trade_count = summary.get("trade_count", 0)
    last_notes = summary.get("notes", [])[-5:]

    lines_out: list[str] = []
    lines_out.append(f"Run: {run_name}")
    lines_out.append(f"Dir: {run_dir}")
    lines_out.append(f"Status: {status}")
    lines_out.append(f"Created: {format_dt(created_at)}")
    lines_out.append(f"Last Business Event: {format_dt(latest_event)} | age={format_age(latest_event)}")
    lines_out.append(f"Last Disk Write: {format_dt(latest_disk_write)} | age={format_age(latest_disk_write)}")
    lines_out.append(f"Heartbeat Local: {heartbeat_local}")
    lines_out.append(f"Heartbeat Beijing: {heartbeat_beijing}")
    lines_out.append(f"Live runner PIDs: {pids or 'none'}")
    lines_out.append(f"Selected symbols: {selected_symbols}")
    lines_out.append(f"Orders: {order_count} | Trades: {trade_count}")
    lines_out.append("")
    lines_out.append("Open Positions")
    for item in position_lines(summary):
        lines_out.append(f"  {item}")
    lines_out.append("")
    lines_out.append("Recent Notes")
    if last_notes:
        for note in last_notes:
            lines_out.append(f"  {note}")
    else:
        lines_out.append("  (none)")
    lines_out.append("")
    lines_out.extend(render_event_rows("Recent Signals", signals[-lines:]))
    lines_out.append("")
    lines_out.extend(render_event_rows("Recent Trades", trades[-lines:]))
    lines_out.append("")
    lines_out.extend(render_event_rows("Recent Risk Events", risks[-lines:]))
    lines_out.append("")
    lines_out.extend(render_event_rows("Recent Logs", logs[-lines:]))
    lines_out.append("")
    lines_out.extend(render_event_rows("Recent Orders", orders[-lines:]))
    return "\n".join(lines_out), summary


def main() -> int:
    args = parse_args()
    previous_summary: dict[str, Any] = {}

    while True:
        run_dir = resolve_run_dir(args.run_name)
        screen, previous_summary = build_screen(run_dir, args.lines, previous_summary)
        if args.follow:
            clear_screen()
        print(screen)

        if not args.follow:
            return 0

        time.sleep(max(args.interval, 0.5))


if __name__ == "__main__":
    raise SystemExit(main())