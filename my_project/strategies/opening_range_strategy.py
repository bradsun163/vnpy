from __future__ import annotations

from datetime import datetime, time, timedelta

from vnpy_ctastrategy import BarData, BarGenerator, CtaTemplate, OrderData, StopOrder, TickData, TradeData


def _parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


class OpeningRangeBreakoutStrategy(CtaTemplate):
    author = "GitHub Copilot"

    opening_minutes = 5
    breakout_buffer_ticks = 1
    fixed_size = 1
    flat_after = "14:55"
    no_new_entry_after = "14:40"
    max_holding_minutes = 0
    trade_enabled = False
    risk_stop_active = False
    force_exit_requested = False

    opening_high = 0.0
    opening_low = 0.0
    bar_count = 0
    range_ready = False
    entry_done = False
    last_signal = "IDLE"
    entry_time_text = ""

    parameters = [
        "opening_minutes",
        "breakout_buffer_ticks",
        "fixed_size",
        "flat_after",
        "no_new_entry_after",
        "max_holding_minutes",
        "trade_enabled",
    ]
    variables = [
        "opening_high",
        "opening_low",
        "bar_count",
        "range_ready",
        "entry_done",
        "last_signal",
        "risk_stop_active",
        "force_exit_requested",
        "entry_time_text",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = BarGenerator(self.on_bar)
        self.flat_after_time = _parse_clock(self.flat_after)
        self.no_new_entry_after_time = _parse_clock(self.no_new_entry_after)
        self.entry_datetime = None

    def update_setting(self, setting: dict) -> None:
        super().update_setting(setting)
        self.flat_after_time = _parse_clock(self.flat_after)
        self.no_new_entry_after_time = _parse_clock(self.no_new_entry_after)

    def on_init(self) -> None:
        self.write_log("策略初始化完成，等待实时开盘区间")

    def on_start(self) -> None:
        self.write_log("策略启动，默认先观察不下单")
        self.put_event()

    def on_stop(self) -> None:
        self.write_log("策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        if not tick.last_price:
            return
        if not tick.bid_price_1 or not tick.ask_price_1:
            return
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.cancel_all()

        if self.force_exit_requested:
            self._flatten_position(bar)
            self.last_signal = "RISK_FORCE_EXIT"
            self.put_event()
            return

        if self.bar_count < self.opening_minutes:
            self.bar_count += 1
            self.opening_high = max(self.opening_high, bar.high_price)
            self.opening_low = bar.low_price if not self.opening_low else min(self.opening_low, bar.low_price)

            if self.bar_count == self.opening_minutes:
                self.range_ready = True
                self.last_signal = "RANGE_READY"
                self.write_log(
                    f"开盘区间构建完成 high={self.opening_high:.2f} low={self.opening_low:.2f}"
                )

            self.put_event()
            return

        if bar.datetime.time() >= self.flat_after_time:
            self._flatten_position(bar)
            self.put_event()
            return

        if self._holding_time_exceeded(bar.datetime):
            self._flatten_position(bar)
            self.last_signal = "MAX_HOLD_EXIT"
            self.put_event()
            return

        if not self.range_ready:
            self.put_event()
            return

        entry_allowed: bool = (
            self.trade_enabled
            and not self.risk_stop_active
            and bar.datetime.time() < self.no_new_entry_after_time
        )

        pricetick: float = self.get_pricetick() or 0
        long_trigger: float = self.opening_high + pricetick * self.breakout_buffer_ticks
        short_trigger: float = self.opening_low - pricetick * self.breakout_buffer_ticks

        if self.pos == 0 and not self.entry_done:
            if not entry_allowed:
                self.last_signal = "ENTRY_BLOCKED"
                self.put_event()
                return
            if bar.high_price >= long_trigger:
                self.buy(max(bar.close_price, long_trigger), self.fixed_size)
                self.entry_done = True
                self.last_signal = "BUY_BREAKOUT"
            elif bar.low_price <= short_trigger:
                self.short(min(bar.close_price, short_trigger), self.fixed_size)
                self.entry_done = True
                self.last_signal = "SHORT_BREAKOUT"
        elif self.pos > 0 and bar.low_price <= self.opening_low:
            self.sell(min(bar.close_price, self.opening_low), abs(self.pos))
            self.last_signal = "EXIT_LONG"
        elif self.pos < 0 and bar.high_price >= self.opening_high:
            self.cover(max(bar.close_price, self.opening_high), abs(self.pos))
            self.last_signal = "EXIT_SHORT"

        self.put_event()

    def _flatten_position(self, bar: BarData) -> None:
        if self.pos > 0:
            self.sell(bar.close_price, abs(self.pos))
            self.last_signal = "FORCE_EXIT_LONG"
        elif self.pos < 0:
            self.cover(bar.close_price, abs(self.pos))
            self.last_signal = "FORCE_EXIT_SHORT"

    def _holding_time_exceeded(self, bar_datetime: datetime) -> bool:
        if self.max_holding_minutes <= 0:
            return False
        if not self.entry_datetime or not self.pos:
            return False
        return bar_datetime >= self.entry_datetime + timedelta(minutes=self.max_holding_minutes)

    def on_order(self, order: OrderData) -> None:
        self.put_event()

    def on_trade(self, trade: TradeData) -> None:
        if self.pos:
            self.entry_datetime = trade.datetime
            self.entry_time_text = trade.datetime.isoformat() if trade.datetime else ""
        else:
            self.entry_datetime = None
            self.entry_time_text = ""
            self.force_exit_requested = False
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        self.put_event()
