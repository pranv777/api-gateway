"""Demo Product Service. In-memory data; exists only to give the gateway something to route to."""

import asyncio

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="Product Service")


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(gt=0, allow_inf_nan=False)


class Product(ProductIn):
    id: int


_products: dict[int, Product] = {
    1: Product(id=1, name="Keyboard", price=49.99),
    2: Product(id=2, name="Mouse", price=24.5),
    3: Product(id=3, name="Monitor", price=189.0),
}
_next_id = 4


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/products", response_model=list[Product])
async def list_products(
    delay: float = Query(
        0,
        ge=0,
        le=30,
        description="Demo only: sleep first, to exercise gateway timeouts",
    ),
) -> list[Product]:
    if delay:
        await asyncio.sleep(delay)
    return list(_products.values())


@app.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: int) -> Product:
    product = _products.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.post("/products", response_model=Product, status_code=201)
async def create_product(body: ProductIn) -> Product:
    global _next_id
    product = Product(id=_next_id, **body.model_dump())
    _products[product.id] = product
    _next_id += 1
    return product
