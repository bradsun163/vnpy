from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable

from vnpy.event import Event, EventEngine
from vnpy.trader.event import EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TRADE
from vnpy_ctastrategy.base import EVENT_CTA_LOG


EVENT_RECORDER_LOG = "eRecorderLog"
EVENT_RECORDER_UPDATE = "eRecorderUpdate"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _jsonable(val)
            for key, val in vars(value).items()
            if not key.startswith("_")
        }
    return value


class PaperTradingReporter:
    def __init__(self, output_root: Path, run_name: str) -> None:
        self.output_dir: Path = output_root.joinpath(run_name)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.order_count: int = 0
        self.trade_count: int = 0
        self.latest_positions: Dict[str, dict] = {}

        self.summary: dict = {
            "run_name": run_name,
            "created_at": datetime.now().isoformat(),
            "status": "created",
            "heartbeat": {},
            "selected_symbols": [],
            "selection_rankings": [],
            "recorder": {},
            "risk": {},
            "notes": [],
        }
        self.write_summary()

    def attach(self, event_engine: EventEngine) -> None:
        event_engine.register(EVENT_LOG, self.process_log_event)
        event_engine.register(EVENT_CTA_LOG, self.process_log_event)
        event_engine.register(EVENT_RECORDER_LOG, self.process_log_event)
        event_engine.register(EVENT_RECORDER_UPDATE, self.process_recorder_event)
        event_engine.register(EVENT_ORDER, self.process_order_event)
        event_engine.register(EVENT_TRADE, self.process_trade_event)
        event_engine.register(EVENT_POSITION, self.process_position_event)

    def append_jsonl(self, filename: str, payload: dict) -> None:
        path: Path = self.output_dir.joinpath(filename)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_jsonable(payload), ensure_ascii=False) + "\n")

    def note(self, message: str) -> None:
        stamped_message: str = f"{datetime.now().isoformat()} {message}"
        self.summary["notes"].append(stamped_message)
        self.write_summary()

    def record_selection(self, rankings: Iterable[dict], selected_symbols: Iterable[str]) -> None:
        rankings_list = list(rankings)
        selected_list = list(selected_symbols)

        self.summary["selected_symbols"] = selected_list
        self.summary["selection_rankings"] = rankings_list
        self.append_jsonl(
            "signals.jsonl",
            {
                "timestamp": datetime.now(),
                "type": "selection_complete",
                "selected_symbols": selected_list,
                "rankings": rankings_list,
            },
        )
        self.write_summary()

    def set_status(self, status: str) -> None:
        self.summary["status"] = status
        self.write_summary()

    def set_heartbeat(self, payload: dict) -> None:
        self.summary["heartbeat"] = _jsonable(payload)
        self.write_summary()

    def set_risk_snapshot(self, snapshot: dict) -> None:
        self.summary["risk"] = _jsonable(snapshot)
        self.write_summary()

    def set_recorder_snapshot(self, snapshot: dict) -> None:
        current: dict = dict(self.summary.get("recorder", {}))
        current.update(_jsonable(snapshot))
        self.summary["recorder"] = current
        self.write_summary()

    def record_risk_event(self, message: str, payload: Dict[str, Any] | None = None) -> None:
        event: dict = {
            "timestamp": datetime.now(),
            "type": "risk_event",
            "message": message,
        }
        if payload:
            event["payload"] = payload
        self.append_jsonl("risk_events.jsonl", event)
        self.note(f"RISK {message}")

    def process_log_event(self, event: Event) -> None:
        self.append_jsonl(
            "logs.jsonl",
            {
                "timestamp": datetime.now(),
                "type": event.type,
                "data": event.data,
            },
        )

    def process_recorder_event(self, event: Event) -> None:
        self.append_jsonl(
            "recorder.jsonl",
            {
                "timestamp": datetime.now(),
                "type": event.type,
                "data": event.data,
            },
        )

    def process_order_event(self, event: Event) -> None:
        self.order_count += 1
        self.summary["order_count"] = self.order_count
        self.append_jsonl("orders.jsonl", {"timestamp": datetime.now(), "data": event.data})
        self.write_summary()

    def process_trade_event(self, event: Event) -> None:
        self.trade_count += 1
        self.summary["trade_count"] = self.trade_count
        self.append_jsonl("trades.jsonl", {"timestamp": datetime.now(), "data": event.data})
        self.write_summary()

    def process_position_event(self, event: Event) -> None:
        data: dict = _jsonable(event.data)
        vt_positionid: str = data.get("vt_positionid", f"{data.get('vt_symbol', 'unknown')}.{data.get('direction', 'NA')}")
        self.latest_positions[vt_positionid] = data
        self.append_jsonl("positions.jsonl", {"timestamp": datetime.now(), "data": event.data})
        self.summary["open_positions"] = list(self.latest_positions.values())
        self.write_summary()

    def write_summary(self) -> None:
        path: Path = self.output_dir.joinpath("session_summary.json")
        temp_path: Path = self.output_dir.joinpath("session_summary.json.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(_jsonable(self.summary), handle, ensure_ascii=False, indent=4)
        temp_path.replace(path)

    def close(self, extra_summary: Dict[str, Any] | None = None) -> None:
        if extra_summary:
            self.summary.update(_jsonable(extra_summary))
        self.summary["closed_at"] = datetime.now().isoformat()
        self.write_summary()
