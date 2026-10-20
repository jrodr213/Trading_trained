"""Deterministic local whole-share accounting; no brokerage dependencies."""
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from numbers import Integral
from .config import Config
from .data import Bar

CENT = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Order:
    id: str
    client_order_id: str | None
    symbol: str
    side: str
    quantity: int
    submitted_at: datetime | None
    status: str = "pending"
    reason: str = ""
    reserved_cash: Decimal = Decimal(0)
    reserved_shares: int = 0


class Simulator:
    def __init__(self, config: Config, clock):
        self.config, self.clock = config, clock
        self.cash = money(config.initial_cash)
        self.holdings = dict.fromkeys(config.symbols, 0)
        self.book_cost = dict.fromkeys(config.symbols, Decimal(0))
        self.prices: dict[str, Decimal] = {}
        self.volumes: dict[str, Decimal] = {}
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.slippage_cost = Decimal(0)
        self.orders: dict[str, Order] = {}
        self.clients: dict[str, str] = {}
        self.events: list[dict] = []
        self.fills: list[dict] = []
        self.history: list[dict] = []

    def event(self, kind, at, **values):
        self.events.append({"kind": kind, "market_time": at, "wall_time": self.clock.now(), **values})

    @property
    def available_cash(self):
        return self.cash - sum(o.reserved_cash for o in self.orders.values() if o.status == "pending")

    def available_shares(self, symbol):
        return self.holdings[symbol] - sum(o.reserved_shares for o in self.orders.values()
                                           if o.status == "pending" and o.symbol == symbol)

    def fee(self, quantity):
        return money(Decimal(self.config.fee_per_order) + quantity * Decimal(self.config.fee_per_share))

    def execution_price(self, raw, side):
        direction = 1 if side == "buy" else -1
        return (raw * (1 + direction * Decimal(self.config.slippage_bps) / 10000)).quantize(
            Decimal(self.config.execution_price_tick), rounding=ROUND_HALF_UP)

    def submit(self, symbol: str, side: str, quantity: int, at: datetime | None,
               allowed: bool, client_order_id: str | None = None) -> Order:
        if client_order_id is not None and client_order_id in self.clients:
            existing = self.orders[self.clients[client_order_id]]
            if (existing.symbol, existing.side, existing.quantity) != (symbol, side, quantity):
                raise ValueError("client_order_id already identifies a different request")
            return existing
        order = Order(f"o{len(self.orders) + 1:06d}", client_order_id, symbol, side, quantity, at)
        reason = ""
        if symbol not in self.holdings:
            reason = "unknown symbol"
        elif side not in ("buy", "sell"):
            reason = "invalid side"
        elif isinstance(quantity, bool) or not isinstance(quantity, Integral) or quantity <= 0:
            reason = "quantity must be a positive integer"
        elif quantity > self.config.max_order_shares:
            reason = "order exceeds size limit"
        elif not allowed or symbol not in self.prices:
            reason = "required data unavailable, unexpectedly stale, session closed, or environment ended"
        elif side == "buy":
            reserve = money(self.execution_price(self.prices[symbol], side) * quantity) + self.fee(quantity)
            if reserve > self.available_cash:
                reason = "insufficient available cash"
            else:
                order.reserved_cash = reserve
        elif quantity > self.available_shares(symbol):
            reason = "insufficient available shares"
        else:
            order.reserved_shares = quantity
        if reason:
            order.status, order.reason = "rejected", reason
        self.orders[order.id] = order
        if client_order_id is not None:
            self.clients[client_order_id] = order.id
        self.event(order.status, at, order=asdict(order))
        return order

    def cancel(self, order_id: str, at=None, reason="canceled by caller") -> bool:
        order = self.orders.get(order_id)
        if order is None or order.status != "pending":
            return False
        order.status, order.reason = "canceled", reason
        order.reserved_cash, order.reserved_shares = Decimal(0), 0
        self.event("canceled", at, order_id=order_id, reason=reason)
        return True

    def cancel_all(self, at, reason):
        for order in self.orders.values():
            self.cancel(order.id, at, reason)

    def process_open(self, bar: Bar):
        # Call BEFORE marking the completed bar or exposing any of its OHLCV.
        prior_volume = self.volumes.get(bar.symbol, Decimal("NaN"))
        capacity = (int(prior_volume * Decimal(self.config.yahoo_volume_participation))
                    if prior_volume.is_finite() and prior_volume > 0 else 0)
        for order in self.orders.values():
            if order.status != "pending" or order.symbol != bar.symbol:
                continue
            if self.config.provider == "yahoo" and order.quantity > capacity:
                continue  # Capacity uses previous observed volume, never this bar's future volume.
            if order.submitted_at is None or bar.start < order.submitted_at:
                continue
            price = self.execution_price(bar.open, order.side)
            gross, fee = money(price * order.quantity), self.fee(order.quantity)
            own_reserve = order.reserved_cash
            reason = ""
            if price <= 0 or gross <= 0:
                reason = "execution price rounds to zero"
            elif order.side == "buy" and gross + fee > self.available_cash + own_reserve:
                reason = "opening gap unaffordable; whole order rejected"
            elif order.side == "sell" and (order.quantity > self.holdings[bar.symbol] or fee > gross + self.available_cash):
                reason = "execution shares/cash unavailable"
            if reason:
                order.status, order.reason = "rejected", reason
                order.reserved_cash, order.reserved_shares = Decimal(0), 0
                self.event("rejected", bar.start, order_id=order.id, reason=reason)
                continue
            if order.side == "buy":
                self.cash -= gross + fee
                self.book_cost[bar.symbol] += gross + fee
                self.holdings[bar.symbol] += order.quantity
            else:
                held = self.holdings[bar.symbol]
                allocated = self.book_cost[bar.symbol] if held == order.quantity else money(self.book_cost[bar.symbol] * order.quantity / held)
                self.book_cost[bar.symbol] -= allocated
                self.holdings[bar.symbol] -= order.quantity
                self.cash += gross - fee
                self.realized += gross - fee - allocated
            self.fees += fee
            slippage = money(abs(price - bar.open) * order.quantity)
            self.slippage_cost += slippage
            order.status = "filled"
            order.reserved_cash, order.reserved_shares = Decimal(0), 0
            fill = {"order_id": order.id, "symbol": bar.symbol, "side": order.side,
                    "quantity": int(order.quantity), "price": price, "fee": fee,
                    "slippage_cost": slippage, "market_time": bar.start, "wall_time": self.clock.now()}
            if self.config.provider == "yahoo":
                fill["execution_quality"] = "ESTIMATED OHLCV FILL; not a verified exchange execution"
                fill["volume_warning"] = "low previously observed volume; no liquidity guarantee" if prior_volume < self.config.low_volume_threshold else None
            self.fills.append(fill)
            if self.config.provider == "yahoo":
                capacity -= order.quantity
            self.event("filled", bar.start, **{k: v for k, v in fill.items() if k not in ("market_time", "wall_time")})
            assert self.cash >= 0 and self.available_cash >= 0 and self.holdings[bar.symbol] >= 0

    def portfolio(self) -> dict:
        value = sum((self.prices.get(s, Decimal(0)) * q for s, q in self.holdings.items()), Decimal(0))
        return {"cash": self.cash, "available_cash": self.available_cash,
                "holdings": dict(self.holdings),
                "available_shares": {s: self.available_shares(s) for s in self.holdings},
                "average_cost": {s: self.book_cost[s] / q if q else Decimal(0) for s, q in self.holdings.items()},
                "realized_pnl": money(self.realized),
                "unrealized_pnl": money(value - sum(self.book_cost.values())),
                "fees": self.fees, "slippage_cost": self.slippage_cost,
                "equity": money(self.cash + value)}
