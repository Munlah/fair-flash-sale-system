#Inventory & Reservation Service
# --------------------------------
# Reserves a unit of stock as a single atomic Redis operation (Lua script),
# so concurrent requests can never both succeed for the last item. Each
# reservation is backed by a short-lived key; if checkout never completes,
# the reservation key expires and the unit must be explicitly released back
# to the pool via /release (called by the Gateway on a failed checkout).

import os
import time
import uuid

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Inventory & Reservation Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=6379,
    decode_responses=True,
)

RESERVATION_TIMEOUT = int(os.getenv("RESERVATION_TIMEOUT", "300"))  #seconds

#Atomic: check stock -> decrement -> record reservation, all in one Redis call.
RESERVE_SCRIPT = """
local stock = redis.call('GET', KEYS[1])

if not stock then
    return {'ERROR', 'missing_product'}
end

if tonumber(stock) <= 0 then
    return {'ERROR', 'sold_out'}
end

redis.call('DECR', KEYS[1])
redis.call('HSET', KEYS[2], 'product_id', ARGV[3], 'created_at', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[2])

return {'OK', ARGV[1]}
"""

reserve_inventory = redis_client.register_script(RESERVE_SCRIPT)


@app.get("/health")
def health():
    try:
        redis_client.ping()
        return {"status": "healthy"}
    except Exception:
        return {"status": "unhealthy"}


@app.post("/stock/{product_id}/{quantity}")
def add_stock(product_id: str, quantity: int):
    redis_client.set(f"stock:{product_id}", quantity)
    return {"product": product_id, "stock": quantity}


@app.post("/reserve/{product_id}")
def reserve(product_id: str):
    reservation_id = str(uuid.uuid4())

    result = reserve_inventory(
        keys=[f"stock:{product_id}", f"reservation:{reservation_id}"],
        args=[reservation_id, RESERVATION_TIMEOUT, product_id, str(time.time())],
    )

    status = result[0]
    if status == "OK":
        redis_client.setex(
            f"reservation-product:{reservation_id}",
            RESERVATION_TIMEOUT + 3600,
            product_id,
        )
        redis_client.zadd(
            "reservation_expirations",
            {reservation_id: time.time() + RESERVATION_TIMEOUT},
        )
        return {"success": True, "reservation_id": reservation_id}
    return {"success": False, "reason": result[1]}


@app.post("/release/{reservation_id}")
def release(reservation_id: str):
    reservation_key = f"reservation:{reservation_id}"
    reservation = redis_client.hgetall(reservation_key)
    product = reservation.get("product_id") or redis_client.get(
        f"reservation-product:{reservation_id}"
    )

    if not product:
        return {"success": False, "reason": "reservation_not_found"}

    redis_client.incr(f"stock:{product}")
    redis_client.delete(reservation_key)
    redis_client.delete(f"reservation-product:{reservation_id}")
    redis_client.zrem("reservation_expirations", reservation_id)
    return {"success": True, "released_product": product}


@app.get("/stock/{product_id}")
def get_stock(product_id: str):
    stock = redis_client.get(f"stock:{product_id}")
    if stock is None:
        return {"product": product_id, "stock": 0}
    return {"product": product_id, "stock": int(stock)}
