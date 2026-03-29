from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, List

from vnpy.trader.constant import Direction
from vnpy.trader.object import PositionData, TradeData


def parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


@dataclass
class SymbolRiskState:
    vt_symbol: str
    net_position: float = 0
    avg_price: float = 0
    entry_time: datetime | None = None
    entry_count: int = 0
    realized_pnl: float = 0
    last_trade_time: datetime | None = None


class SessionRiskController:
    def __init__(self, risk_config: dict) -> None:
        self.max_order_size: int = int(risk_config.get("max_order_size", 1))
        self.max_position_per_symbol: int = int(risk_config.get("max_position_per_symbol", 1))
        self.max_selected_symbols: int = int(risk_config.get("max_selected_symbols", 1))
        self.max_trades_per_symbol: int = int(risk_config.get("max_trades_per_symbol", 1))
        self.max_holding_minutes: int = int(risk_config.get("max_holding_minutes", 0))
        self.max_session_realized_loss: float = float(risk_config.get("max_session_realized_loss", 0))
        self.max_consecutive_losing_trades: int = int(risk_config.get("max_consecutive_losing_trades", 0))
        self.no_new_entry_after: str = str(risk_config.get("no_new_entry_after", "14:50"))
        self.force_flat_on_hard_stop: bool = bool(risk_config.get("force_flat_on_hard_stop", True))

        self.no_new_entry_after_time: time = parse_clock(self.no_new_entry_after)

        self.contract_sizes: Dict[str, float] = {}
        self.symbol_states: Dict[str, SymbolRiskState] = {}
        self.session_realized_pnl: float = 0
        self.consecutive_losing_trades: int = 0
        self.entry_window_closed: bool = False
        self.hard_stop_triggered: bool = False
        self.hard_stop_reason: str = ""
        self._emitted_messages: set[str] = set()

    def set_contract_size(self, vt_symbol: str, size: float) -> None:
        self.contract_sizes[vt_symbol] = max(float(size), 1)

    def get_symbol_state(self, vt_symbol: str) -> SymbolRiskState:
        state: SymbolRiskState | None = self.symbol_states.get(vt_symbol)
        if state is None:
            state = SymbolRiskState(vt_symbol=vt_symbol)
            self.symbol_states[vt_symbol] = state
        return state

    def sync_position(self, position: PositionData) -> None:
        state: SymbolRiskState = self.get_symbol_state(position.vt_symbol)
        signed_volume: float = float(position.volume)
        if position.direction == Direction.SHORT:
            signed_volume *= -1

        state.net_position = signed_volume
        if signed_volume == 0:
            state.avg_price = 0
            state.entry_time = None
        else:
            state.avg_price = float(position.price)
    def on_trade(self, trade: TradeData) -> List[str]:
        state: SymbolRiskState = self.get_symbol_state(trade.vt_symbol)
        state.last_trade_time = trade.datetime

        signed_volume: float = float(trade.volume)
        if trade.direction == Direction.SHORT:
            signed_volume *= -1

        contract_size: float = self.contract_sizes.get(trade.vt_symbol, 1)
        previous_position: float = state.net_position
        new_position: float = previous_position + signed_volume

        messages: List[str] = []

        if previous_position == 0 or previous_position * signed_volume > 0:
            total_volume: float = abs(previous_position) + abs(signed_volume)
            if total_volume:
                state.avg_price = (
                    abs(previous_position) * state.avg_price + abs(signed_volume) * float(trade.price)
                ) / total_volume
            state.net_position = new_position
            if previous_position == 0 and new_position != 0:
                state.entry_time = trade.datetime
                state.entry_count += 1
                if state.entry_count >= self.max_trades_per_symbol > 0:
                    message = f"{trade.vt_symbol} reached max_trades_per_symbol={self.max_trades_per_symbol}"
                    if message not in self._emitted_messages:
                        self._emitted_messages.add(message)
                        messages.append(message)
            return messages

        closed_volume: float = min(abs(previous_position), abs(signed_volume))
        pnl_sign: float = 1 if previous_position > 0 else -1
        realized_pnl: float = (float(trade.price) - state.avg_price) * closed_volume * contract_size * pnl_sign

        state.realized_pnl += realized_pnl
        self.session_realized_pnl += realized_pnl
        state.net_position = new_position

        if new_position == 0:
            state.avg_price = 0
            state.entry_time = None

            if realized_pnl < 0:
                self.consecutive_losing_trades += 1
            else:
                self.consecutive_losing_trades = 0
        else:
            state.avg_price = float(trade.price)
            if previous_position * new_position < 0:
                state.entry_time = trade.datetime
                state.entry_count += 1

        messages.extend(self.evaluate(trade.datetime or datetime.now()))
        return messages

    def evaluate(self, now: datetime) -> List[str]:
        messages: List[str] = []

        if not self.entry_window_closed and now.time() >= self.no_new_entry_after_time:
            self.entry_window_closed = True
            messages.append(f"entry window closed at {self.no_new_entry_after}")

        if (
            self.max_session_realized_loss > 0
            and self.session_realized_pnl <= -self.max_session_realized_loss
            and not self.hard_stop_triggered
        ):
            self.hard_stop_triggered = True
            self.hard_stop_reason = (
                f"session realized pnl {self.session_realized_pnl:.2f} <= -{self.max_session_realized_loss:.2f}"
            )
            messages.append(f"hard stop triggered: {self.hard_stop_reason}")

        if (
            self.max_consecutive_losing_trades > 0
            and self.consecutive_losing_trades >= self.max_consecutive_losing_trades
            and not self.hard_stop_triggered
        ):
            self.hard_stop_triggered = True
            self.hard_stop_reason = (
                f"consecutive losing trades {self.consecutive_losing_trades} >= {self.max_consecutive_losing_trades}"
            )
            messages.append(f"hard stop triggered: {self.hard_stop_reason}")

        unique_messages: List[str] = []
        for message in messages:
            if message in self._emitted_messages:
                continue
            self._emitted_messages.add(message)
            unique_messages.append(message)
        return unique_messages

    def symbol_entry_allowed(self, vt_symbol: str, now: datetime) -> bool:
        state: SymbolRiskState = self.get_symbol_state(vt_symbol)
        if self.hard_stop_triggered:
            return False
        if self.entry_window_closed or now.time() >= self.no_new_entry_after_time:
            return False
        if self.max_trades_per_symbol > 0 and state.entry_count >= self.max_trades_per_symbol:
            return False
        return True

    def should_force_flat(self, vt_symbol: str, now: datetime) -> bool:
        state: SymbolRiskState = self.get_symbol_state(vt_symbol)
        if self.force_flat_on_hard_stop and self.hard_stop_triggered and state.net_position:
            return True
        if (
            self.max_holding_minutes > 0
            and state.net_position
            and state.entry_time
            and now >= state.entry_time + timedelta(minutes=self.max_holding_minutes)
        ):
            return True
        return False

    def has_open_positions(self) -> bool:
        return any(bool(state.net_position) for state in self.symbol_states.values())

    def snapshot(self) -> dict:
        return {
            "hard_stop_triggered": self.hard_stop_triggered,
            "hard_stop_reason": self.hard_stop_reason,
            "entry_window_closed": self.entry_window_closed,
            "no_new_entry_after": self.no_new_entry_after,
            "session_realized_pnl": round(self.session_realized_pnl, 2),
            "consecutive_losing_trades": self.consecutive_losing_trades,
            "symbol_states": {
                vt_symbol: {
                    "net_position": state.net_position,
                    "avg_price": round(state.avg_price, 6),
                    "entry_count": state.entry_count,
                    "realized_pnl": round(state.realized_pnl, 2),
                    "entry_time": state.entry_time.isoformat() if state.entry_time else None,
                    "last_trade_time": state.last_trade_time.isoformat() if state.last_trade_time else None,
                }
                for vt_symbol, state in self.symbol_states.items()
            },
        }