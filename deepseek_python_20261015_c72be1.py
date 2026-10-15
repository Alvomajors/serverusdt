# main.py
import asyncio
import json
from typing import List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import Exchange, Side, OrderType

app = FastAPI(title="Mock Exchange")
exchange = Exchange(initial_balance=100_000.0)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BTC/KES"]

# ---- WebSocket connection manager ----
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---- Schemas ----
class OrderRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    symbol: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    quantity: float = Field(..., gt=0)
    price: float = Field(0.0, ge=0)


# ---- Routes ----
@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/symbols")
def get_symbols():
    return {"symbols": SYMBOLS}


@app.get("/api/balance/{user_id}")
def get_balance(user_id: str):
    return exchange.get_balance(user_id)


@app.get("/api/depth/{symbol:path}")
def get_depth(symbol: str, levels: int = Query(10, ge=1, le=50)):
    book = exchange.get_book(symbol)
    return book.depth(levels)


@app.get("/api/trades/{symbol:path}")
def get_trades(symbol: str, limit: int = Query(50, ge=1, le=200)):
    return exchange.recent_trades(symbol, limit)


@app.get("/api/orders/{user_id}")
def get_user_orders(user_id: str):
    return [o.to_dict() for o in exchange.orders.values() if o.user_id == user_id]


@app.post("/api/order")
async def place_order(req: OrderRequest):
    try:
        result = exchange.place_order(
            user_id=req.user_id,
            symbol=req.symbol,
            side=req.side,
            order_type=req.order_type,
            quantity=req.quantity,
            price=req.price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Broadcast order book + trade updates
    book = exchange.get_book(req.symbol)
    await manager.broadcast({
        "type": "depth",
        "symbol": req.symbol,
        "data": book.depth(20),
    })
    for t in result["trades"]:
        await manager.broadcast({"type": "trade", "data": t})

    return result


@app.delete("/api/order/{user_id}/{order_id}")
async def cancel_order(user_id: str, order_id: str):
    result = exchange.cancel_order(user_id, order_id)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found or not cancellable")
    book = exchange.get_book(result["symbol"])
    await manager.broadcast({
        "type": "depth",
        "symbol": result["symbol"],
        "data": book.depth(20),
    })
    return result


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send a snapshot on connect
    for sym in SYMBOLS:
        await ws.send_text(json.dumps({
            "type": "depth",
            "symbol": sym,
            "data": exchange.get_book(sym).depth(20),
        }))
    try:
        while True:
            await ws.receive_text()  # ignore client messages
    except WebSocketDisconnect:
        manager.disconnect(ws)