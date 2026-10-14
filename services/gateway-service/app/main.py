# services/gateway-service/app/main.py
#
# Gateway / Waiting-Room Service
# ------------------------------
# Single entry point for customers. Instead of letting clients hit
# inventory/checkout directly, every customer first joins a queue and
# receives a token. The Admission Controller (a separate service) decides
# when each token is allowed through. Only once a token's status is
# "admitted" will this service forward a purchase attempt downstream.

import os
import time
import uuid

import httpx
import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Gateway / Waiting-Room Service")

# Allow the browser demo to call this service from its own local port.
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

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8000")
CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout-service:8000")

TOKEN_TTL_SECONDS = 600  # a queue token expires if unused this long


@app.get("/health")
def health():
    """Report whether the gateway can reach Redis."""
    try:
        redis_client.ping()
        return {"status": "healthy"}
    except Exception:
        return {"status": "unhealthy"}


@app.post("/queue/join")
def join_queue(user_id: str, product_id: str):
    """Create a token and store the customer's arrival time in Redis."""
    token = str(uuid.uuid4())
    arrival_ms = int(time.time() * 1000)

    pipe = redis_client.pipeline()
    pipe.zadd(f"queue:{product_id}", {token: arrival_ms})
    pipe.hset(
        f"token:{token}",
        mapping={
            "user_id": user_id,
            "product_id": product_id,
            "arrival_ms": arrival_ms,
            "status": "waiting",
        },
    )
    pipe.expire(f"token:{token}", TOKEN_TTL_SECONDS)
    pipe.execute()

    position = redis_client.zrank(f"queue:{product_id}", token)
    return {"token": token, "status": "waiting", "position": position}


@app.get("/queue/status/{token}")
def queue_status(token: str):
    """Return the current state and queue position for one token."""
    data = redis_client.hgetall(f"token:{token}")
    if not data:
        raise HTTPException(status_code=404, detail="token_not_found_or_expired")

    result = {"status": data["status"]}
    if data["status"] == "waiting":
        result["position"] = redis_client.zrank(f"queue:{data['product_id']}", token)
    return result


@app.post("/purchase")
async def purchase(token: str):
    """Reserve stock and create an order after admission."""
    data = redis_client.hgetall(f"token:{token}")
    if not data:
        raise HTTPException(status_code=404, detail="token_not_found_or_expired")
    if data["status"] not in ("admitted", "completed"):
        raise HTTPException(status_code=403, detail="not_admitted_yet")
    if data["status"] == "completed":
        # The token remains valid so a retry can return the same idempotent order.
        pass

    product_id = data["product_id"]
    user_id = data["user_id"]

    async with httpx.AsyncClient(timeout=10) as client:
        reserve_resp = await client.post(f"{INVENTORY_URL}/reserve/{product_id}")
        reservation = reserve_resp.json()

        if not reservation.get("success"):
            return {"success": False, "stage": "reserve", "reason": reservation.get("reason")}

        checkout_resp = await client.post(
            f"{CHECKOUT_URL}/orders",
            json={
                "user_id": user_id,
                "product_id": product_id,
                "reservation_id": reservation["reservation_id"],
                "idempotency_key": token,
            },
        )

        if checkout_resp.status_code >= 400:
            await client.post(f"{INVENTORY_URL}/release/{reservation['reservation_id']}")
            return {"success": False, "stage": "checkout", "reason": checkout_resp.text}

        redis_client.hset(f"token:{token}", "status", "completed")
        return {"success": True, "order": checkout_resp.json()}


@app.get("/stock/{product_id}")
async def stock(product_id: str):
    """Proxy a stock lookup to the inventory service."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
        return resp.json()
