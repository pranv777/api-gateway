"""Demo Order Service. In-memory data; exists only to give the gateway something to route to."""

import asyncio

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="Order Service")


class OrderIn(BaseModel):
    product_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=1000)


class Order(OrderIn):
    id: int
    status: str = "created"


_orders: dict[int, Order] = {
    1: Order(id=1, product_id=1, quantity=2, status="shipped"),
    2: Order(id=2, product_id=3, quantity=1),
}
_next_id = 3


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/orders", response_model=list[Order])
async def list_orders(
    delay: float = Query(
        0,
        ge=0,
        le=30,
        description="Demo only: sleep first, to exercise gateway timeouts",
    ),
) -> list[Order]:
    if delay:
        await asyncio.sleep(delay)
    return list(_orders.values())


@app.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: int) -> Order:
    order = _orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/orders", response_model=Order, status_code=201)
async def create_order(body: OrderIn) -> Order:
    global _next_id
    order = Order(id=_next_id, **body.model_dump())
    _orders[order.id] = order
    _next_id += 1
    return order
