# services/checkout-service/app/main.py
#
# Checkout / Order Service
# --------------------------
# Finalises a purchase durably in PostgreSQL. Every request must carry an
# idempotency_key (the Gateway uses the customer's queue token). Retries
# with the same key return the original result instead of creating a
# duplicate order -- enforced twice, so it holds up even under real
# concurrency:
#   1. A Redis cache keyed by idempotency_key (fast path for repeat calls).
#   2. A UNIQUE constraint on idempotency_key in Postgres (source of truth --
#      catches two concurrent retries that both miss the Redis cache).

import json
import os

import asyncpg
import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Checkout / Order Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL")

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=6379,
    decode_responses=True,
)

IDEMPOTENCY_TTL = 3600  # seconds


@app.get("/health")
def health():
    return {"status": "healthy"}


async def connect():
    return await asyncpg.connect(DATABASE_URL)


def _serialise(row: dict) -> dict:
    out = dict(row)
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = str(out["created_at"])
    return out


@app.post("/orders")
async def create_order(order: dict):
    idempotency_key = order.get("idempotency_key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_key_required")

    cache_key = f"idempotency:{idempotency_key}"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    db = await connect()
    try:
        result = await db.fetchrow(
            """
            INSERT INTO orders (idempotency_key, user_id, product_id, reservation_id, status)
            VALUES ($1, $2, $3, $4, 'CONFIRMED')
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            idempotency_key,
            order["user_id"],
            order["product_id"],
            order["reservation_id"],
        )

        if result is None:
            # Lost the race to a concurrent retry with the same key --
            # that's fine, fetch the row it already created.
            result = await db.fetchrow(
                "SELECT * FROM orders WHERE idempotency_key = $1", idempotency_key
            )
    finally:
        await db.close()

    response = _serialise(dict(result))
    redis_client.set(cache_key, json.dumps(response), ex=IDEMPOTENCY_TTL)
    return response


@app.get("/orders/{user_id}")
async def get_user_orders(user_id: str):
    db = await connect()
    try:
        rows = await db.fetch(
            "SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC", user_id
        )
    finally:
        await db.close()
    return [_serialise(dict(row)) for row in rows]
