from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from logging import INFO
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type

from vnpy.event import Event, EventEngine
from vnpy.trader.app import BaseApp
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_POSITION, EVENT_TICK, EVENT_TRADE
from vnpy.trader.object import PositionData, TickData, TradeData
from vnpy.trader.setting import SETTINGS
from vnpy.trader.utility import get_file_path, load_json, save_json
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_paperaccount.engine import PaperEngine

from .reporting import PaperTradingReporter
from .risk import SessionRiskController
from .selection import UniverseSelector


DEFAULT_OUTPUT_ROOT = Path.home().joinpath(".vntrader", "paper_mvp")
SYMBOL_PATTERN = re.compile(r"^(?P<root>[A-Za-z]+)(?P<month>\d+)?$")


class IsolatedPaperEngine(PaperEngine):
    setting_filename = "paper_mvp_paper_account_setting.json"
    data_filename = "paper_mvp_paper_account_data.json"


class IsolatedPaperAccountApp(BaseApp):
    app_name = "PaperAccount"
    app_module = __name__
    app_path = Path(__file__).parent
    display_name = "模拟交易"
    engine_class = IsolatedPaperEngine
    widget_name = "PaperManager"
    icon_name = "paper.ico"


def load_plain_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_gateway_class(gateway_name: str) -> Tuple[Type, str]:
    normalized: str = gateway_name.upper()
    if normalized == "CTP":
        from vnpy_ctp import CtpGateway

        return CtpGateway, "CTP"
    if normalized == "CTPTEST":
        from vnpy_ctptest import CtptestGateway

        return CtptestGateway, "CTPTEST"
    raise ValueError(f"Unsupported gateway: {gateway_name}")


class LivePaperTradingRunner:
    def __init__(self, config_path: Path) -> None:
        self.config_path: Path = config_path
        self.config: dict = load_plain_json(config_path)
        self.runtime_config: dict = self.config["runtime"]
        self.strategy_config: dict = self.config["strategy"]
        self.risk_config: dict = self.config["risk"]
        self.paper_config: dict = self.config["paper"]
        self.recorder_config: dict = dict(self.config.get("recorder", {}))

        output_root: Path = Path(self.runtime_config.get("output_root", str(DEFAULT_OUTPUT_ROOT)))
        if not output_root.is_absolute():
            output_root = Path.home().joinpath(output_root)

        run_name: str = datetime.now().strftime("live_%Y%m%d_%H%M%S")
        self.reporter: PaperTradingReporter = PaperTradingReporter(output_root, run_name)
        self.risk_controller: SessionRiskController = SessionRiskController(self.risk_config)
        self.selector: UniverseSelector | None = None

        self.concrete_universe_specs: List[str] = []
        self.root_universe_specs: List[str] = []
        self.available_symbol_roots: Dict[str, str] = {}
        self.root_candidate_counts: Dict[str, int] = {}
        self.parse_universe_specs()

        self.event_engine: EventEngine | None = None
        self.main_engine: MainEngine | None = None
        self.cta_engine = None
        self.paper_engine = None
        self.official_risk_engine = None
        self.recorder_engine = None
        self.gateway_name: str = ""
        self.recorder_symbols: List[str] = []
        self.recorder_started_at: datetime | None = None
        self.recorder_phase: str = "disabled"
        self.strategy_names: Dict[str, str] = {}
        self.selected_symbols: List[str] = []
        self.selection_completed: bool = False
        self.status: str = "created"

    def parse_universe_specs(self) -> None:
        concrete_specs: List[str] = []
        root_specs: List[str] = []

        for raw_entry in self.config["universe"]:
            entry: str = str(raw_entry).strip().upper()
            if not entry:
                continue

            symbol_part, exchange_part = self.split_universe_entry(entry)
            match = SYMBOL_PATTERN.match(symbol_part)
            if not match:
                raise ValueError(f"Unsupported universe entry: {raw_entry}")

            month: str | None = match.group("month")
            normalized: str = f"{symbol_part}.{exchange_part}" if exchange_part else symbol_part

            if month:
                concrete_specs.append(normalized)
            else:
                root_specs.append(normalized)

        self.concrete_universe_specs = concrete_specs
        self.root_universe_specs = root_specs

    def split_universe_entry(self, entry: str) -> Tuple[str, str]:
        if "." not in entry:
            return entry, ""

        symbol_part, exchange_part = entry.split(".", 1)
        return symbol_part, exchange_part

    def extract_symbol_root(self, vt_symbol: str) -> str:
        symbol_part: str = vt_symbol.split(".", 1)[0]
        match = SYMBOL_PATTERN.match(symbol_part)
        if not match:
            return symbol_part
        return match.group("root").upper()

    def initialize_selector(self, universe: List[str]) -> None:
        self.selector = UniverseSelector(
            universe=universe,
            selection_minutes=int(self.runtime_config["selection_window_minutes"]),
            top_n=int(self.risk_config["max_selected_symbols"]),
            min_ticks=int(self.runtime_config["min_ticks_per_symbol"]),
        )

    def find_root_contract_candidates(self) -> Tuple[List[str], Dict[str, str], Dict[str, int]]:
        all_contracts = list(self.main_engine.get_all_contracts())
        resolved_symbols: List[str] = []
        symbol_roots: Dict[str, str] = {}
        root_candidate_counts: Dict[str, int] = {}

        for root_spec in self.root_universe_specs:
            root_symbol, exchange = self.split_universe_entry(root_spec)
            root_matches: List[str] = []

            for contract in all_contracts:
                contract_root: str = self.extract_symbol_root(contract.vt_symbol)
                if contract_root != root_symbol:
                    continue
                if exchange and contract.exchange.value.upper() != exchange:
                    continue
                root_matches.append(contract.vt_symbol)

            root_matches = sorted(set(root_matches))
            root_candidate_counts[root_spec] = len(root_matches)

            for vt_symbol in root_matches:
                if vt_symbol not in symbol_roots:
                    resolved_symbols.append(vt_symbol)
                    symbol_roots[vt_symbol] = root_spec

        return resolved_symbols, symbol_roots, root_candidate_counts

    def compute_selection_result(self) -> Tuple[List[dict], List[str]]:
        rankings: List[dict] = self.selector.rankings() if self.selector else []
        if not self.root_universe_specs:
            selected_symbols: List[str] = [row["vt_symbol"] for row in rankings[: int(self.risk_config["max_selected_symbols"] )]]
            return rankings, selected_symbols

        best_by_root: Dict[str, dict] = {}
        for row in rankings:
            vt_symbol: str = row["vt_symbol"]
            root_spec: str | None = self.available_symbol_roots.get(vt_symbol)
            if not root_spec or root_spec in best_by_root:
                continue

            resolved_row: dict = dict(row)
            resolved_row["requested_root"] = root_spec
            resolved_row["candidate_count"] = self.root_candidate_counts.get(root_spec, 0)
            best_by_root[root_spec] = resolved_row

        resolved_rankings: List[dict] = list(best_by_root.values())
        resolved_rankings.sort(
            key=lambda item: (item["score"], item["volume_delta"], item["tick_count"]),
            reverse=True,
        )
        selected_symbols = [
            row["vt_symbol"]
            for row in resolved_rankings[: int(self.risk_config["max_selected_symbols"])]
        ]
        return resolved_rankings, selected_symbols

    def configure_settings(self) -> None:
        SETTINGS["log.active"] = True
        SETTINGS["log.level"] = INFO
        SETTINGS["log.console"] = True
        SETTINGS["log.file"] = True
        SETTINGS["database.name"] = "sqlite"
        SETTINGS["datafeed.name"] = ""

    def reset_isolated_state(self) -> None:
        for filename in [
            "paper_mvp_cta_strategy_setting.json",
            "paper_mvp_cta_strategy_data.json",
            "paper_mvp_data_recorder_setting.json",
            "paper_mvp_paper_account_setting.json",
            "paper_mvp_paper_account_data.json",
            "paper_mvp_risk_manager_setting.json",
        ]:
            save_json(filename, {})

    def build_official_risk_settings(self) -> Dict[str, dict]:
        official_config: dict = dict(self.risk_config.get("official_riskmanager", {}))
        globally_active: bool = bool(official_config.get("active", True))
        max_selected_symbols: int = max(int(self.risk_config.get("max_selected_symbols", 1)), 1)
        max_position_per_symbol: int = max(int(self.risk_config.get("max_position_per_symbol", 1)), 1)

        return {
            "委托指令检查": {
                "active": globally_active and bool(official_config.get("order_validity_active", True)),
            },
            "委托规模检查": {
                "active": globally_active and bool(official_config.get("order_size_active", True)),
                "order_volume_limit": int(self.risk_config.get("max_order_size", 1)),
                "order_value_limit": float(official_config.get("order_value_limit", 1_000_000)),
            },
            "活动委托检查": {
                "active": globally_active and bool(official_config.get("active_order_active", True)),
                "active_order_limit": int(
                    official_config.get(
                        "active_order_limit",
                        max(10, max_selected_symbols * max_position_per_symbol * 4),
                    )
                ),
            },
            "每日上限检查": {
                "active": globally_active and bool(official_config.get("daily_limit_active", True)),
                "total_order_limit": int(official_config.get("total_order_limit", 1000)),
                "total_cancel_limit": int(official_config.get("total_cancel_limit", 500)),
                "total_trade_limit": int(official_config.get("total_trade_limit", 200)),
                "contract_order_limit": int(official_config.get("contract_order_limit", 100)),
                "contract_cancel_limit": int(official_config.get("contract_cancel_limit", 50)),
                "contract_trade_limit": int(official_config.get("contract_trade_limit", 20)),
            },
            "重复报单检查": {
                "active": globally_active and bool(official_config.get("duplicate_order_active", True)),
                "duplicate_order_limit": int(official_config.get("duplicate_order_limit", 3)),
            },
        }

    def configure_official_risk_manager(self) -> None:
        from vnpy_riskmanager import RiskEngine, RiskManagerApp
        from vnpy_riskmanager.base import EVENT_RISK_NOTIFY

        RiskEngine.setting_filename = "paper_mvp_risk_manager_setting.json"
        self.official_risk_engine = self.main_engine.add_app(RiskManagerApp)

        for rule_name, rule_setting in self.build_official_risk_settings().items():
            self.official_risk_engine.update_rule_setting(rule_name, rule_setting)

        self.event_engine.register(EVENT_RISK_NOTIFY, self.process_official_risk_event)
        self.reporter.note("official vnpy_riskmanager configured for pre-trade risk checks")

    def configure_official_recorder_engine(self) -> None:
        if not bool(self.recorder_config.get("enabled", False)):
            self.recorder_phase = "disabled"
            self.reporter.note("official vnpy_datarecorder disabled in config")
            return

        try:
            from vnpy_datarecorder import RecorderEngine, DataRecorderApp
        except ModuleNotFoundError as exc:
            self.recorder_phase = "unavailable"
            self.reporter.note(f"official vnpy_datarecorder unavailable: {exc}")
            return

        RecorderEngine.setting_filename = "paper_mvp_data_recorder_setting.json"
        self.recorder_engine = self.main_engine.add_app(DataRecorderApp)
        self.recorder_phase = "loaded"
        self.reporter.note("official vnpy_datarecorder loaded with isolated settings")

    def get_recorder_snapshot(self, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        tick_enabled: bool = bool(self.recorder_config.get("record_tick", True))
        bar_enabled: bool = bool(self.recorder_config.get("record_bar", True))

        snapshot: Dict[str, Any] = {
            "enabled": bool(self.recorder_engine),
            "startup_phase": self.recorder_phase,
            "tick_recording": tick_enabled,
            "bar_recording": bar_enabled,
            "shrink_to_selected_after_selection": bool(
                self.recorder_config.get("shrink_to_selected_after_selection", True)
            ),
            "requested_whitelist": list(self.recorder_config.get("whitelist", [])),
            "symbols": list(self.recorder_symbols),
            "setting_file": str(get_file_path("paper_mvp_data_recorder_setting.json")),
        }

        if self.recorder_started_at:
            snapshot["started_at"] = self.recorder_started_at

        if self.recorder_engine:
            tick_symbols: List[str] = sorted(list(self.recorder_engine.tick_recordings.keys()))
            bar_symbols: List[str] = sorted(list(self.recorder_engine.bar_recordings.keys()))
            snapshot["tick"] = tick_symbols
            snapshot["bar"] = bar_symbols
            snapshot["tick_symbols"] = tick_symbols
            snapshot["bar_symbols"] = bar_symbols

        if extra:
            snapshot.update(extra)

        return snapshot

    def resolve_recorder_symbols(self, available_symbols: List[str]) -> Tuple[List[str], List[str]]:
        whitelist: List[str] = [
            str(vt_symbol).strip()
            for vt_symbol in self.recorder_config.get("whitelist", [])
            if str(vt_symbol).strip()
        ]

        candidates: List[str] = whitelist or list(available_symbols)
        resolved: List[str] = []
        skipped: List[str] = []

        for vt_symbol in candidates:
            if self.main_engine.get_contract(vt_symbol):
                resolved.append(vt_symbol)
            else:
                skipped.append(vt_symbol)

        return resolved, skipped

    def configure_recorder_recordings(self, available_symbols: List[str]) -> None:
        if not self.recorder_engine:
            return

        tick_enabled: bool = bool(self.recorder_config.get("record_tick", True))
        bar_enabled: bool = bool(self.recorder_config.get("record_bar", True))
        if not tick_enabled and not bar_enabled:
            self.recorder_phase = "loaded_but_disabled"
            self.reporter.note("data recorder loaded but both tick and bar recording are disabled")
            self.reporter.set_recorder_snapshot(self.get_recorder_snapshot({"enabled": False, "reason": "tick and bar recording both disabled"}))
            return

        resolved, skipped = self.resolve_recorder_symbols(available_symbols)
        if skipped:
            self.reporter.note(f"data recorder skipped symbols without contract metadata: {skipped}")

        if not resolved:
            self.recorder_phase = "loaded_but_idle"
            self.reporter.note("data recorder found no eligible symbols to record")
            self.reporter.set_recorder_snapshot(self.get_recorder_snapshot({"enabled": False, "reason": "no eligible symbols"}))
            return

        for vt_symbol in resolved:
            if tick_enabled:
                self.recorder_engine.add_tick_recording(vt_symbol)
            if bar_enabled:
                self.recorder_engine.add_bar_recording(vt_symbol)

        self.recorder_symbols = resolved
        self.recorder_started_at = datetime.now()
        self.recorder_phase = "after_contracts_before_selection"
        self.reporter.set_recorder_snapshot(self.get_recorder_snapshot())
        self.reporter.note(
            f"data recorder armed for {resolved} (tick={tick_enabled}, bar={bar_enabled})"
        )

    def shrink_recorder_recordings(self, selected_symbols: List[str]) -> None:
        if not self.recorder_engine:
            return

        if not bool(self.recorder_config.get("shrink_to_selected_after_selection", True)):
            self.recorder_phase = "post_selection_kept_full_universe"
            self.reporter.set_recorder_snapshot(
                self.get_recorder_snapshot(
                    {
                        "selected_symbols": list(selected_symbols),
                        "preselection_symbols": list(self.recorder_symbols),
                    }
                )
            )
            self.reporter.note("data recorder kept full preselection universe after selection")
            return

        previous_symbols: List[str] = list(self.recorder_symbols)
        target_symbols: List[str] = [vt_symbol for vt_symbol in selected_symbols if vt_symbol in previous_symbols]
        removed_symbols: List[str] = [vt_symbol for vt_symbol in previous_symbols if vt_symbol not in target_symbols]

        for vt_symbol in removed_symbols:
            if vt_symbol in self.recorder_engine.tick_recordings:
                self.recorder_engine.remove_tick_recording(vt_symbol)
            if vt_symbol in self.recorder_engine.bar_recordings:
                self.recorder_engine.remove_bar_recording(vt_symbol)

        self.recorder_symbols = target_symbols
        self.recorder_phase = "post_selection_shrunk"
        self.reporter.set_recorder_snapshot(
            self.get_recorder_snapshot(
                {
                    "selected_symbols": list(selected_symbols),
                    "preselection_symbols": previous_symbols,
                    "removed_symbols": removed_symbols,
                }
            )
        )

        if removed_symbols:
            self.reporter.note(
                f"data recorder shrank from {previous_symbols} to {target_symbols}; removed {removed_symbols}"
            )
        else:
            self.reporter.note(f"data recorder selection shrink kept {target_symbols}")

    def get_official_risk_snapshot(self) -> Dict[str, Any]:
        if not self.official_risk_engine:
            return {}

        snapshot: Dict[str, Any] = {}
        for rule_name in self.official_risk_engine.get_all_rule_names():
            snapshot[rule_name] = self.official_risk_engine.get_rule_data(rule_name)
        return snapshot

    def publish_risk_snapshot(self) -> None:
        self.reporter.set_risk_snapshot(
            {
                "official_pretrade": self.get_official_risk_snapshot(),
                "session": self.risk_controller.snapshot(),
            }
        )

    def setup(self) -> None:
        self.configure_settings()
        self.reset_isolated_state()

        self.event_engine = EventEngine()
        self.main_engine = MainEngine(self.event_engine)

        gateway_class, self.gateway_name = get_gateway_class(self.config["gateway"]["name"])
        self.main_engine.add_gateway(gateway_class)

        self.configure_official_risk_manager()
        self.configure_official_recorder_engine()
        self.cta_engine = self.main_engine.add_app(CtaStrategyApp)
        self.paper_engine = self.main_engine.add_app(IsolatedPaperAccountApp)

        self.paper_engine.trade_slippage = int(self.paper_config["trade_slippage"])
        self.paper_engine.timer_interval = int(self.paper_config["timer_interval"])
        self.paper_engine.instant_trade = bool(self.paper_config["instant_trade"])

        self.cta_engine.setting_filename = "paper_mvp_cta_strategy_setting.json"
        self.cta_engine.data_filename = "paper_mvp_cta_strategy_data.json"
        self.cta_engine.init_engine()
        self.cta_engine.load_strategy_class_from_module("paper_trading_mvp.opening_range_strategy")

        self.reporter.attach(self.event_engine)
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)
        self.publish_risk_snapshot()
        self.reporter.note(f"runtime initialized with gateway={self.gateway_name}")

    def load_gateway_setting(self) -> dict:
        gateway_setting: dict = self.main_engine.get_default_setting(self.gateway_name)
        connect_file: str = self.config["gateway"]["connect_file"]

        if Path(connect_file).is_absolute():
            gateway_setting.update(load_plain_json(Path(connect_file)))
        else:
            gateway_setting.update(load_json(connect_file))
        return gateway_setting

    def connect_gateway(self) -> None:
        setting: dict = self.load_gateway_setting()
        self.main_engine.connect(setting, self.gateway_name)
        self.reporter.note(f"connect invoked for {self.gateway_name}")

    def wait_for_contracts(self) -> List[str]:
        deadline: float = time.time() + int(self.runtime_config["contract_wait_seconds"])
        concrete_specs: List[str] = list(self.concrete_universe_specs)

        while time.time() < deadline:
            concrete_available: List[str] = [
                vt_symbol for vt_symbol in concrete_specs if self.main_engine.get_contract(vt_symbol)
            ]

            root_available: List[str] = []
            root_symbol_map: Dict[str, str] = {}
            root_candidate_counts: Dict[str, int] = {}
            if self.root_universe_specs:
                root_available, root_symbol_map, root_candidate_counts = self.find_root_contract_candidates()

            roots_ready: bool = not self.root_universe_specs or all(
                root_candidate_counts.get(root_spec, 0) > 0 for root_spec in self.root_universe_specs
            )
            concrete_ready: bool = not concrete_specs or len(concrete_available) == len(concrete_specs)

            available: List[str] = concrete_available + [
                vt_symbol for vt_symbol in root_available if vt_symbol not in concrete_available
            ]

            if available and concrete_ready and roots_ready:
                self.available_symbol_roots = root_symbol_map
                self.root_candidate_counts = root_candidate_counts
                return available
            time.sleep(1)

        return []

    def install_strategies(self, available_symbols: List[str]) -> None:
        fixed_size: int = min(
            int(self.strategy_config["order_size"]),
            int(self.risk_config["max_order_size"]),
            int(self.risk_config["max_position_per_symbol"]),
        )

        for vt_symbol in available_symbols:
            contract = self.main_engine.get_contract(vt_symbol)
            if contract:
                self.risk_controller.set_contract_size(vt_symbol, contract.size)

            strategy_name: str = f"mvp_{vt_symbol.replace('.', '_').replace(':', '_')}"
            setting: dict = {
                "opening_minutes": int(self.runtime_config["selection_window_minutes"]),
                "breakout_buffer_ticks": int(self.strategy_config["breakout_buffer_ticks"]),
                "fixed_size": fixed_size,
                "flat_after": self.strategy_config["flat_after"],
                "no_new_entry_after": self.risk_config["no_new_entry_after"],
                "max_holding_minutes": int(self.risk_config["max_holding_minutes"]),
                "trade_enabled": False,
            }
            self.strategy_names[vt_symbol] = strategy_name
            self.cta_engine.add_strategy(
                "OpeningRangeBreakoutStrategy",
                strategy_name,
                vt_symbol,
                setting,
            )

        futures: dict = self.cta_engine.init_all_strategies()
        for future in futures.values():
            future.result(timeout=60)
        self.cta_engine.start_all_strategies()
        self.apply_risk_controls(datetime.now())

        self.reporter.note(f"strategies installed for {available_symbols}")

    def process_tick_event(self, event: Event) -> None:
        if self.selection_completed or not self.selector:
            return

        tick: TickData = event.data
        self.selector.record_tick(tick)

        if self.selector.is_window_complete(tick.datetime):
            self.complete_selection()

    def process_trade_event(self, event: Event) -> None:
        trade: TradeData = event.data
        self.record_risk_messages(self.risk_controller.on_trade(trade))
        self.apply_risk_controls(trade.datetime or datetime.now())

    def process_position_event(self, event: Event) -> None:
        position: PositionData = event.data
        self.risk_controller.sync_position(position)
        self.publish_risk_snapshot()

    def process_official_risk_event(self, event: Event) -> None:
        message: str = str(event.data)
        self.reporter.record_risk_event(f"official_riskmanager: {message}", self.get_official_risk_snapshot())

    def record_risk_messages(self, messages: List[str]) -> None:
        for message in messages:
            self.reporter.record_risk_event(
                message,
                {
                    "official_pretrade": self.get_official_risk_snapshot(),
                    "session": self.risk_controller.snapshot(),
                },
            )

    def apply_risk_controls(self, now: datetime) -> None:
        self.record_risk_messages(self.risk_controller.evaluate(now))

        if not self.cta_engine:
            self.publish_risk_snapshot()
            return

        for vt_symbol, strategy_name in self.strategy_names.items():
            strategy = self.cta_engine.strategies[strategy_name]
            selected: bool = vt_symbol in self.selected_symbols
            entry_allowed: bool = selected and self.risk_controller.symbol_entry_allowed(vt_symbol, now)
            force_flat: bool = self.risk_controller.should_force_flat(vt_symbol, now)

            strategy.trade_enabled = entry_allowed
            strategy.risk_stop_active = not entry_allowed
            strategy.force_exit_requested = force_flat
            strategy.put_event()

        self.publish_risk_snapshot()

    def complete_selection(self) -> None:
        if self.selection_completed:
            return

        rankings, selected_symbols = self.compute_selection_result()
        self.selected_symbols = selected_symbols

        self.reporter.record_selection(rankings, selected_symbols)

        if self.root_universe_specs:
            self.reporter.note(
                f"dynamic root resolution selected {selected_symbols} from roots {self.root_universe_specs}"
            )

        if not selected_symbols:
            self.status = "selection_failed"
            self.shrink_recorder_recordings([])
            self.reporter.note("selection completed but no eligible symbol satisfied the minimum tick threshold")
            self.selection_completed = True
            return

        self.selection_completed = True
        self.status = "running"
        self.shrink_recorder_recordings(selected_symbols)
        self.apply_risk_controls(datetime.now())
        self.reporter.note(f"trade enabled for {selected_symbols}")

    def run_loop(self) -> None:
        cutoff: str = self.strategy_config["flat_after"]
        cutoff_time = datetime.strptime(cutoff, "%H:%M").time()
        selection_timeout_seconds: int = int(self.runtime_config["selection_timeout_seconds"])

        while True:
            now: datetime = datetime.now()
            self.apply_risk_controls(now)

            if not self.selection_completed and self.selector.window_start:
                elapsed: float = (now - self.selector.window_start).total_seconds()
                if elapsed >= selection_timeout_seconds:
                    self.complete_selection()

            if self.risk_controller.hard_stop_triggered and not self.risk_controller.has_open_positions():
                self.reporter.note("hard risk stop triggered and all positions are flat")
                self.status = "risk_limit_triggered"
                break

            if now.time() >= cutoff_time:
                self.reporter.note("reached flat_after cutoff, stopping all strategies")
                break

            time.sleep(float(self.runtime_config["loop_sleep_seconds"]))

    def close(self) -> None:
        if self.cta_engine:
            self.cta_engine.stop_all_strategies()
        if self.main_engine:
            self.main_engine.close()

    def run(self) -> int:
        try:
            self.status = "starting"
            self.reporter.set_status(self.status)

            self.setup()
            self.connect_gateway()

            available_symbols: List[str] = self.wait_for_contracts()
            if not available_symbols:
                self.status = "gateway_unavailable"
                self.reporter.note("no contract information became available within the wait window")
                return 1

            self.initialize_selector(available_symbols)
            self.configure_recorder_recordings(available_symbols)
            self.install_strategies(available_symbols)
            self.status = "waiting_for_selection"
            self.reporter.set_status(self.status)
            self.run_loop()

            if self.status == "waiting_for_selection":
                self.complete_selection()

            if self.status not in {"selection_failed", "gateway_unavailable", "risk_limit_triggered"}:
                self.status = "completed"

            return 0 if self.status == "completed" else 1
        except KeyboardInterrupt:
            self.status = "interrupted"
            self.reporter.note("received keyboard interrupt")
            return 130
        finally:
            self.reporter.set_status(self.status)
            self.reporter.close(
                {
                    "gateway_name": self.gateway_name,
                    "config_path": str(self.config_path),
                    "isolated_cta_setting": str(get_file_path("paper_mvp_cta_strategy_setting.json")),
                    "isolated_data_recorder_setting": str(get_file_path("paper_mvp_data_recorder_setting.json")),
                    "isolated_paper_account_data": str(get_file_path("paper_mvp_paper_account_data.json")),
                    "isolated_risk_manager_setting": str(get_file_path("paper_mvp_risk_manager_setting.json")),
                }
            )
            self.close()


def run_from_path(config_path: Path) -> int:
    runner = LivePaperTradingRunner(config_path)
    return runner.run()


def main(argv: List[str] | None = None) -> int:
    args: List[str] = list(argv or sys.argv[1:])
    if args:
        config_path = Path(args[0])
    else:
        config_path = Path(__file__).resolve().parents[1].joinpath(
            "examples",
            "paper_trading_mvp",
            "live_ctp_paper_config.json",
        )
    return run_from_path(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
