from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from typing import Any

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_LOG, EVENT_POSITION
from vnpy.trader.setting import SETTINGS
from vnpy.trader.utility import load_json
from vnpy_ctp import CtpGateway


DAY_SESSION_WINDOWS: list[tuple[int, int]] = [
    (9 * 60, 10 * 60 + 15),
    (10 * 60 + 30, 11 * 60 + 30),
    (13 * 60 + 30, 15 * 60),
]
DEFAULT_NIGHT_SESSION_START_MINUTE: int = 21 * 60
DEFAULT_NIGHT_SESSION_END_MINUTE: int = 2 * 60 + 30


def make_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: make_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: make_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def load_connect_setting(connect_file: str) -> dict[str, Any]:
    path = Path(connect_file)
    if path.is_absolute():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return load_json(connect_file)


def get_beijing_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def is_china_futures_trading_window(dt: datetime) -> bool:
    current_minute: int = minute_of_day(dt)
    if any(start <= current_minute < end for start, end in DAY_SESSION_WINDOWS):
        return True
    if current_minute >= DEFAULT_NIGHT_SESSION_START_MINUTE:
        return True
    return current_minute < DEFAULT_NIGHT_SESSION_END_MINUTE


def sanitize_setting(setting: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in setting.items()
        if key not in {"密码", "前置组合候选", "__label__"}
    }


def build_ctp_setting_variants(
    connect_setting: dict[str, Any],
    include_all_candidates: bool,
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []

    primary: dict[str, Any] = dict(connect_setting)
    primary["__label__"] = "primary"
    variants.append(primary)

    if include_all_candidates:
        for index, candidate in enumerate(connect_setting.get("前置组合候选", []), start=1):
            if not isinstance(candidate, dict):
                continue

            td_address: str = str(candidate.get("交易服务器", "")).strip()
            md_address: str = str(candidate.get("行情服务器", "")).strip()
            if not td_address or not md_address:
                continue

            variant: dict[str, Any] = dict(connect_setting)
            variant["交易服务器"] = td_address
            variant["行情服务器"] = md_address
            if candidate.get("柜台环境"):
                variant["柜台环境"] = str(candidate.get("柜台环境"))
            variant["__label__"] = str(
                candidate.get("名称") or candidate.get("备注") or f"candidate_{index}"
            )
            variants.append(variant)

    broker_id: str = str(connect_setting.get("经纪商代码", "")).strip()
    app_id: str = str(connect_setting.get("产品名称", "")).strip()
    auth_code: str = str(connect_setting.get("授权编码", "")).strip()
    if broker_id != "9999" or app_id != "simnow_client_test" or auth_code != "0000000000000000":
        return variants

    preferred_environment: str = (
        "实盘" if is_china_futures_trading_window(get_beijing_now()) else "测试"
    )
    alternate_environment: str = "测试" if preferred_environment == "实盘" else "实盘"

    expanded: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for variant in variants:
        td_address: str = str(variant.get("交易服务器", "")).strip()
        md_address: str = str(variant.get("行情服务器", "")).strip()
        label: str = str(variant.get("__label__", "candidate"))

        for environment in (preferred_environment, alternate_environment):
            key: tuple[str, str, str] = (td_address, md_address, environment)
            if key in seen:
                continue

            expanded_variant: dict[str, Any] = dict(variant)
            expanded_variant["柜台环境"] = environment
            expanded_variant["__label__"] = f"{label}|env={environment}"
            expanded.append(expanded_variant)
            seen.add(key)

    return expanded


def clear_flow_cache() -> int:
    flow_dir: Path = Path.home().joinpath(".vntrader", "ctp")
    if not flow_dir.exists():
        return 0

    removed_count: int = 0
    for flow_file in flow_dir.glob("*.con"):
        try:
            flow_file.unlink(missing_ok=True)
            removed_count += 1
        except PermissionError:
            continue
    return removed_count


def append_jsonl(output_dir: Path, filename: str, payload: dict[str, Any]) -> None:
    path = output_dir.joinpath(filename)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(make_jsonable(payload), ensure_ascii=False) + "\n")


def close_main_engine(main_engine: MainEngine, attempt_summary: dict[str, Any], timeout_seconds: int = 5) -> None:
    errors: list[str] = []

    def do_close() -> None:
        try:
            main_engine.close()
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            errors.append(str(exc))

    close_thread = Thread(target=do_close, daemon=True)
    close_thread.start()
    close_thread.join(timeout_seconds)

    if errors:
        attempt_summary["close_error"] = errors[0]
    elif close_thread.is_alive():
        attempt_summary["close_timeout_seconds"] = timeout_seconds


def run_attempt(
    output_dir: Path,
    setting: dict[str, Any],
    timeout_seconds: int,
    attempt_index: int,
) -> dict[str, Any]:
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)

    attempt_summary: dict[str, Any] = {
        "attempt_index": attempt_index,
        "label": str(setting.get("__label__", f"attempt_{attempt_index}")),
        "setting": sanitize_setting(setting),
        "started_at": datetime.now().isoformat(),
        "log_messages": [],
        "accounts": [],
        "positions": [],
        "contract_count": 0,
        "flow_cache_cleared": clear_flow_cache(),
    }

    def on_log(event: Event) -> None:
        log = event.data
        message: str = str(getattr(log, "msg", log))
        attempt_summary["log_messages"].append(message)
        append_jsonl(
            output_dir,
            "logs.jsonl",
            {"timestamp": datetime.now(), "attempt_index": attempt_index, "data": log},
        )

    def on_contract(event: Event) -> None:
        contract = event.data
        attempt_summary["contract_count"] = int(attempt_summary.get("contract_count", 0)) + 1
        append_jsonl(
            output_dir,
            "contracts.jsonl",
            {"timestamp": datetime.now(), "attempt_index": attempt_index, "data": contract},
        )

    def on_account(event: Event) -> None:
        account = make_jsonable(event.data)
        attempt_summary["accounts"] = [account]
        append_jsonl(
            output_dir,
            "accounts.jsonl",
            {"timestamp": datetime.now(), "attempt_index": attempt_index, "data": event.data},
        )

    def on_position(event: Event) -> None:
        position = make_jsonable(event.data)
        positions: list[dict[str, Any]] = list(attempt_summary.get("positions", []))
        positions.append(position)
        attempt_summary["positions"] = positions
        append_jsonl(
            output_dir,
            "positions.jsonl",
            {"timestamp": datetime.now(), "attempt_index": attempt_index, "data": event.data},
        )

    event_engine.register(EVENT_LOG, on_log)
    event_engine.register(EVENT_CONTRACT, on_contract)
    event_engine.register(EVENT_ACCOUNT, on_account)
    event_engine.register(EVENT_POSITION, on_position)

    try:
        main_engine.connect(setting, "CTP")

        deadline: float = time.time() + timeout_seconds
        while time.time() < deadline:
            if attempt_summary["contract_count"]:
                break
            time.sleep(1)

        attempt_summary["status"] = (
            "connected" if attempt_summary["contract_count"] else "no_contracts"
        )
        attempt_summary["closed_at"] = datetime.now().isoformat()
    finally:
        close_main_engine(main_engine, attempt_summary)

    return attempt_summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connect-file", default="connect_ctp.json")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--all-candidates", action="store_true")
    args = parser.parse_args()

    SETTINGS["log.active"] = True
    SETTINGS["log.console"] = True
    SETTINGS["log.file"] = True

    connect_setting: dict[str, Any] = load_connect_setting(args.connect_file)

    run_name: str = datetime.now().strftime("official_ctp_diag_%Y%m%d_%H%M%S")
    output_dir: Path = Path.home().joinpath(".vntrader", "diagnostics", run_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    setting_variants: list[dict[str, Any]] = build_ctp_setting_variants(
        connect_setting,
        args.all_candidates,
    )
    summary: dict[str, Any] = {
        "run_name": run_name,
        "created_at": datetime.now().isoformat(),
        "connect_file": str(args.connect_file),
        "timeout_seconds": args.timeout,
        "all_candidates": bool(args.all_candidates),
        "variant_count": len(setting_variants),
        "connect_setting": sanitize_setting(connect_setting),
        "attempts": [],
    }

    for attempt_index, setting in enumerate(setting_variants, start=1):
        attempt_summary = run_attempt(output_dir, setting, args.timeout, attempt_index)
        summary["attempts"].append(attempt_summary)
        if attempt_summary["status"] == "connected":
            summary["status"] = "connected"
            break

    if "status" not in summary:
        summary["status"] = "no_contracts"

    summary["contract_count"] = sum(
        int(item.get("contract_count", 0)) for item in summary["attempts"]
    )
    summary["closed_at"] = datetime.now().isoformat()

    path = output_dir.joinpath("summary.json")
    with path.open("w", encoding="utf-8") as handle:
        json.dump(make_jsonable(summary), handle, ensure_ascii=False, indent=4)

    print(json.dumps(make_jsonable(summary), ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "connected" else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
