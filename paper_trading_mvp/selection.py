from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from vnpy.trader.object import TickData


@dataclass
class SymbolSelectionStats:
    vt_symbol: str
    first_tick_time: Optional[datetime] = None
    last_tick_time: Optional[datetime] = None
    first_price: float = 0.0
    last_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    first_volume: float = 0.0
    last_volume: float = 0.0
    tick_count: int = 0

    def update(self, tick: TickData) -> None:
        if not tick.last_price:
            return

        tick_dt: datetime = tick.datetime or datetime.now()

        if not self.tick_count:
            self.first_tick_time = tick_dt
            self.first_price = tick.last_price
            self.high_price = tick.last_price
            self.low_price = tick.last_price
            self.first_volume = max(tick.volume, 0)

        self.last_tick_time = tick_dt
        self.last_price = tick.last_price
        self.high_price = max(self.high_price, tick.last_price)
        self.low_price = min(self.low_price, tick.last_price)
        self.last_volume = max(tick.volume, self.last_volume)
        self.tick_count += 1

    @property
    def volume_delta(self) -> float:
        return max(self.last_volume - self.first_volume, 0)

    @property
    def range_ratio(self) -> float:
        if not self.first_price:
            return 0.0
        return max((self.high_price - self.low_price) / self.first_price, 0.0)

    @property
    def score(self) -> float:
        # Favor contracts that are both active and moving.
        return (self.volume_delta + max(self.tick_count - 1, 0)) * self.range_ratio

    def eligible(self, min_ticks: int) -> bool:
        return self.tick_count >= min_ticks and self.first_price > 0 and self.high_price >= self.low_price

    def to_dict(self) -> dict:
        return {
            "vt_symbol": self.vt_symbol,
            "tick_count": self.tick_count,
            "first_price": self.first_price,
            "last_price": self.last_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "volume_delta": self.volume_delta,
            "range_ratio": round(self.range_ratio, 8),
            "score": round(self.score, 8),
            "first_tick_time": self.first_tick_time.isoformat() if self.first_tick_time else None,
            "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
        }


class UniverseSelector:
    def __init__(
        self,
        universe: Iterable[str],
        selection_minutes: int,
        top_n: int,
        min_ticks: int,
    ) -> None:
        self.selection_minutes: int = selection_minutes
        self.top_n: int = top_n
        self.min_ticks: int = min_ticks
        self.window_start: Optional[datetime] = None
        self.stats: Dict[str, SymbolSelectionStats] = {
            vt_symbol: SymbolSelectionStats(vt_symbol) for vt_symbol in universe
        }

    def record_tick(self, tick: TickData) -> None:
        stats: Optional[SymbolSelectionStats] = self.stats.get(tick.vt_symbol)
        if not stats:
            return

        stats.update(tick)

        tick_dt: datetime = tick.datetime or datetime.now()
        if not self.window_start:
            self.window_start = tick_dt

    def is_window_complete(self, now: Optional[datetime] = None) -> bool:
        if not self.window_start:
            return False

        current: datetime = now or datetime.now()
        return current >= self.window_start + timedelta(minutes=self.selection_minutes)

    def rankings(self) -> List[dict]:
        rows: List[SymbolSelectionStats] = [
            stats for stats in self.stats.values() if stats.eligible(self.min_ticks)
        ]
        rows.sort(key=lambda item: (item.score, item.volume_delta, item.tick_count), reverse=True)
        return [stats.to_dict() for stats in rows]

    def selected_symbols(self) -> List[str]:
        return [row["vt_symbol"] for row in self.rankings()[: self.top_n]]
