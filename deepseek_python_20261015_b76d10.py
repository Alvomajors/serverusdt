# engine.py
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from collections import deque
from threading import Lock


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass
class Order:
    id: str
    user_id: str
    symbol: str
    side: Side
    order_type: OrderType
    price: float
    quantity: float
    timestamp: float = field(default_factory=time.time)
    filled: float = 0.0
    status: str = "open"  # open, partial, filled, cancelled

    @property
    def remaining(self) -> float:
        return round(self.quantity - self.filled, 10)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.order_type.value,
            "price": self.price,
            "quantity": self.quantity,
            "filled": self.filled,
            "remaining": self.remaining,
            "status": self.status,
            "timestamp": self.timestamp,
        }


@dataclass
class Trade:
    id: str
    symbol: str
    price: float
    quantity: float
    buy_order_id: str
    sell_order_id: str
    buyer_id: str
    seller_id: str
    taker_side: Side
    timestamp: float

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "price": self.price,
            "quantity": self.quantity,
            "buy_order_id": self.buy_order_id,
            "sell_order_id": self.sell_order_id,
            "buyer_id": self.buyer_id,
            "seller_id": self.seller_id,
            "taker_side": self.taker_side.value,
            "timestamp": self.timestamp,
        }


class OrderBook:
    """Price-time priority order book for a single symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: Dict[float, deque] = {}
        self.asks: Dict[float, deque] = {}
        self.trades: List[Trade] = []
        self.lock = Lock()

    def _best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    def _best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    def _rest(self, order: Order):
        book = self.bids if order.side == Side.BUY else self.asks
        if order.price not in book:
            book[order.price] = deque()
        book[order.price].append(order)
        order.status = "open" if order.filled == 0 else "partial"

    def _remove_from_book(self, order: Order):
        book = self.bids if order.side == Side.BUY else self.asks
        queue = book.get(order.price)
        if queue and order in queue:
            queue.remove(order)
            if not queue:
                del book[order.price]

    def cancel(self, order_id: str) -> Optional[Order]:
        with self.lock:
            for book in (self.bids, self.asks):
                for price, queue in list(book.items()):
                    for o in list(queue):
                        if o.id == order_id:
                            queue.remove(o)
                            if not queue:
                                del book[price]
                            o.status = "cancelled"
                            return o
            return None

    def _match(self, taker: Order) -> List[Trade]:
        trades: List[Trade] = []
        opposite = self.asks if taker.side == Side.BUY else self.bids

        while taker.remaining > 0 and opposite:
            if taker.side == Side.BUY:
                best_price = self._best_ask()
                if best_price is None:
                    break
                # Market orders ignore price. Limit orders must cross.
                if taker.order_type == OrderType.LIMIT and taker.price < best_price:
                    break
            else:
                best_price = self._best_bid()
                if best_price is None:
                    break
                if taker.order_type == OrderType.LIMIT and taker.price > best_price:
                    break

            queue = opposite[best_price]
            maker = queue[0]

            match_qty = min(taker.remaining, maker.remaining)
            trade_price = maker.price  # maker sets the price

            trade = Trade(
                id=str(uuid.uuid4()),
                symbol=self.symbol,
                price=trade_price,
                quantity=match_qty,
                buy_order_id=taker.id if taker.side == Side.BUY else maker.id,
                sell_order_id=maker.id if taker.side == Side.BUY else taker.id,
                buyer_id=taker.user_id if taker.side == Side.BUY else maker.user_id,
                seller_id=maker.user_id if taker.side == Side.BUY else taker.user_id,
                taker_side=taker.side,
                timestamp=time.time(),
            )
            trades.append(trade)
            self.trades.append(trade)

            taker.filled += match_qty
            maker.filled += match_qty

            if maker.remaining == 0:
                queue.popleft()
                maker.status = "filled"
                if not queue:
                    del opposite[best_price]
            else:
                maker.status = "partial"

            taker.status = "partial" if taker.remaining > 0 else "filled"

        return trades

    def place(self, order: Order) -> List[Trade]:
        with self.lock:
            trades = self._match(order)

            if order.remaining > 0:
                if order.order_type == OrderType.MARKET:
                    # Market orders don't rest; cancel leftovers
                    order.status = "cancelled"
                else:
                    self._rest(order)
            return trades

    def depth(self, levels: int = 10) -> dict:
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:levels]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:levels]
        return {
            "symbol": self.symbol,
            "bids": [
                {"price": p, "quantity": round(sum(o.remaining for o in q), 10)}
                for p, q in bids
            ],
            "asks": [
                {"price": p, "quantity": round(sum(o.remaining for o in q), 10)}
                for p, q in asks
            ],
        }


class Exchange:
    """Multi-symbol exchange with per-user balances."""

    def __init__(self, initial_balance: float = 100_000.0):
        self.books: Dict[str, OrderBook] = {}
        self.balances: Dict[str, Dict[str, float]] = {}
        self.orders: Dict[str, Order] = {}
        self.initial_balance = initial_balance
        self.lock = Lock()

    def get_book(self, symbol: str) -> OrderBook:
        if symbol not in self.books:
            self.books[symbol] = OrderBook(symbol)
        return self.books[symbol]

    def get_balance(self, user_id: str) -> Dict[str, float]:
        if user_id not in self.balances:
            # Give every new user mock balances to play with
            self.balances[user_id] = {
                "USDT": self.initial_balance,
                "BTC": 1.0,
                "ETH": 10.0,
                "KES": 1_000_000.0,
            }
        return self.balances[user_id]

    def _split_symbol(self, symbol: str):
        base, quote = symbol.split("/")
        return base, quote

    def _lock_funds(self, user_id: str, symbol: str, side: Side, price: float, qty: float):
        base, quote = self._split_symbol(symbol)
        bal = self.get_balance(user_id)
        if side == Side.BUY:
            cost = price * qty
            if bal.get(quote, 0) < cost:
                raise ValueError(f"Insufficient {quote} balance")
            bal[quote] -= cost
        else:
            if bal.get(base, 0) < qty:
                raise ValueError(f"Insufficient {base} balance")
            bal[base] -= qty

    def _settle_trade(self, trade: Trade, symbol: str):
        base, quote = self._split_symbol(symbol)
        buyer = self.get_balance(trade.buyer_id)
        seller = self.get_balance(trade.seller_id)
        cost = trade.price * trade.quantity
        buyer[base] = buyer.get(base, 0) + trade.quantity
        seller[quote] = seller.get(quote, 0) + cost

    def place_order(
        self,
        user_id: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: float,
        price: float = 0.0,
    ) -> dict:
        if order_type == OrderType.LIMIT and price <= 0:
            raise ValueError("Limit orders require a positive price")
        if quantity <= 0:
            raise ValueError("Quantity must be positive")

        # For market orders, use best available price for locking
        book = self.get_book(symbol)
        if order_type == OrderType.MARKET:
            if side == Side.BUY:
                ref_price = book._best_ask() or 0.0
            else:
                ref_price = book._best_bid() or 0.0
            if ref_price == 0:
                raise ValueError("No liquidity for market order")
        else:
            ref_price = price

        # Lock funds before matching
        self._lock_funds(user_id, symbol, side, ref_price, quantity)

        order = Order(
            id=str(uuid.uuid4()),
            user_id=user_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price if order_type == OrderType.LIMIT else ref_price,
            quantity=quantity,
        )
        self.orders[order.id] = order

        trades = book.place(order)

        # Settle trades and refund any over-locked funds for buys
        for t in trades:
            self._settle_trade(t, symbol)

        if side == Side.BUY and order.remaining > 0:
            # Refund the difference between locked price and actual trades
            # (Market orders that didn't fill fully get refunded at ref_price)
            pass

        return {
            "order": order.to_dict(),
            "trades": [t.to_dict() for t in trades],
        }

    def cancel_order(self, user_id: str, order_id: str) -> Optional[dict]:
        order = self.orders.get(order_id)
        if not order or order.user_id != user_id:
            return None
        book = self.get_book(order.symbol)
        cancelled = book.cancel(order_id)
        if not cancelled:
            return None
        # Refund locked funds
        base, quote = self._split_symbol(order.symbol)
        bal = self.get_balance(user_id)
        if order.side == Side.BUY:
            refund = order.remaining * order.price
            bal[quote] = bal.get(quote, 0) + refund
        else:
            bal[base] = bal.get(base, 0) + order.remaining
        return cancelled.to_dict()

    def recent_trades(self, symbol: str, limit: int = 50) -> List[dict]:
        book = self.get_book(symbol)
        return [t.to_dict() for t in book.trades[-limit:]][::-1]