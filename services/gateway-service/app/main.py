"""
Gateway Service - Entry point for all user requests
==========================================================================
"""

import asyncio
import os
import uuid
import time
import logging
import httpx
import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

# ==========================================================================
# Configuration
# ==========================================================================

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8000")
CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout-service:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-gateway:8000")  # NEW
ADMISSION_URL = os.getenv("ADMISSION_URL", "http://admission:8000")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================================================
# Initialize FastAPI app
# ==========================================================================

app = FastAPI(title="Gateway Service", version="1.0.0")

#Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================================================
# Redis Client
# ==========================================================================

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True
)

TOKEN_TTL_SECONDS = 600
executor = ThreadPoolExecutor(max_workers=10)

async def redis_async(func, *args, **kwargs):
    """Helper to run sync Redis commands in thread pool"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

# ==========================================================================
# Data Models
# ==========================================================================

class QueueJoinResponse(BaseModel):
    token: str
    status: str
    position: int

class TokenStatusResponse(BaseModel):
    status: str
    position: Optional[int] = None

class PurchaseResponse(BaseModel):
    success: bool
    order: Optional[dict] = None
    reason: Optional[str] = None



# ==========================================================================
# Endpoints
# ==========================================================================

@app.get("/health")
async def health():
    """Health check"""
    try:
        await redis_async(redis_client.ping)
        return {"status": "healthy", "service": "gateway", "redis": "connected"}
    except Exception:
        return {"status": "degraded", "service": "gateway", "redis": "disconnected"}


async def fetch_service_health(client, name, url):
    """Return a normalized health result without hiding an unavailable service."""
    try:
        response = await client.get(f"{url}/health")
        response.raise_for_status()
        data = response.json()
        return {
            "service": name,
            "status": data.get("status", "unknown"),
            "available": True,
            "details": data,
        }
    except Exception as exc:
        return {
            "service": name,
            "status": "unavailable",
            "available": False,
            "details": {"error": str(exc)},
        }


@app.get("/dashboard/snapshot")
async def dashboard_snapshot(product_id: str = "phone"):
    """Collect the live values needed by the demonstration dashboard."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        health_results = await asyncio.gather(
            fetch_service_health(client, "gateway", "http://localhost:8000"),
            fetch_service_health(client, "inventory", INVENTORY_URL),
            fetch_service_health(client, "checkout", CHECKOUT_URL),
            fetch_service_health(client, "payment", PAYMENT_URL),
            fetch_service_health(client, "admission", ADMISSION_URL),
        )

        snapshot = {
            "services": health_results,
            "product_id": product_id,
            "stock": None,
            "queue": None,
            "orders": None,
            "payments": None,
            "payment_config": None,
        }

        try:
            snapshot["stock"] = (
                await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            ).json()
        except Exception:
            pass

        try:
            snapshot["queue"] = (
                await client.get(f"{ADMISSION_URL}/queue/{product_id}/length")
            ).json()
        except Exception:
            pass

        try:
            snapshot["orders"] = (await client.get(f"{CHECKOUT_URL}/orders/count")).json()
        except Exception:
            pass

        try:
            snapshot["payments"] = (await client.get(f"{PAYMENT_URL}/stats")).json()
        except Exception:
            pass

        payment_health = next(
            (service for service in health_results if service["service"] == "payment"),
            None,
        )
        if payment_health:
            snapshot["payment_config"] = payment_health["details"].get("config")

        return snapshot


@app.post("/queue/join", response_model=QueueJoinResponse)
async def join_queue(user_id: str, product_id: str):
    """
    Add user to queue with spam prevention using an atomic SET NX.
    """
    user_product_key = f"active_token:{user_id}:{product_id}"
    
    #Check stock before joining queue
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            response.raise_for_status()
            stock_data = response.json()
            if stock_data.get("stock", 0) <= 0:
                raise HTTPException(status_code=410, detail="Product is sold out")
    except httpx.RequestError as exc:
        logger.warning(f"Stock check for %s returned HTTP %s; allowing queue join", product_id, exc)
    except ValueError:
        logger.warning(f"Could not check stock for %s; allowing queue join: %s", product_id)
    
    #Generate token
    token = str(uuid.uuid4())
    
    #Atomic check: ensure user doesn't already have a token for this product
    token_claimed = await redis_async(
        redis_client.set,
        user_product_key,
        token,
        ex=TOKEN_TTL_SECONDS,
        nx=True
    )
    
    if not token_claimed:
        existing_token = await redis_async(redis_client.get, user_product_key)
        token_data = await redis_async(redis_client.hgetall, f"token:{existing_token}")
        queue_key = f"queue:{product_id}"
        
        actual_status = token_data.get("status", "waiting")
        
        if actual_status in ("admitted", "completed"):
            position = None
        else:
            position = await redis_async(redis_client.zrank, queue_key, existing_token)
        
        return QueueJoinResponse(
            token=existing_token,
            status=actual_status,
            position=position if position is not None else 0
        )
    
    #Store token data
    arrival_ms = int(time.time() * 1000)
    
    pipe = redis_client.pipeline()
    pipe.zadd(f"queue:{product_id}", {token: arrival_ms})
    pipe.hset(
        f"token:{token}",
        mapping={
            "user_id": user_id,
            "product_id": product_id,
            "arrival_ms": arrival_ms,
            "status": "waiting"
        }
    )
    pipe.expire(f"token:{token}", TOKEN_TTL_SECONDS)
    await redis_async(pipe.execute)
    
    position = await redis_async(redis_client.zrank, f"queue:{product_id}", token)
    
    return QueueJoinResponse(token=token, status="waiting", position=position or 0)


@app.get("/queue/status/{token}", response_model=TokenStatusResponse)
async def check_status(token: str):
    """Check token status"""
    data = await redis_async(redis_client.hgetall, f"token:{token}")
    
    if not data:
        raise HTTPException(status_code=404, detail="Token not found")
    
    status = data.get("status", "waiting")
    product_id = data.get("product_id", "phone")
    
    if status == "waiting":
        position = await redis_async(redis_client.zrank, f"queue:{product_id}", token)
        return TokenStatusResponse(status=status, position=position)
    
    return TokenStatusResponse(status=status)


@app.post("/purchase", response_model=PurchaseResponse)
async def purchase(token: str):
    """
    Complete purchase with payment gateway integration and fault tolerance
    """
    token_data = await redis_async(redis_client.hgetall, f"token:{token}")
    
    if not token_data:
        return PurchaseResponse(success=False, reason="Token not found")
    
    status = token_data.get("status")
    
    #Check if already completed
    if status == "completed":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{CHECKOUT_URL}/order/by-token/{token}")
                if response.status_code == 200:
                    return PurchaseResponse(success=True, order=response.json())
        except:
            pass
        return PurchaseResponse(success=False, reason="Purchase already completed")
    
    if status != "admitted":
        return PurchaseResponse(success=False, reason=f"Token not admitted (status: {status})")
    
    #Atomic transition to "processing"
    lua_script = """
    local token_key = KEYS[1]
    local current_status = redis.call('HGET', token_key, 'status')
    if current_status == 'admitted' then
        redis.call('HSET', token_key, 'status', 'processing')
        return 1
    else
        return 0
    end
    """
    
    transition_success = await redis_async(
        redis_client.eval,
        lua_script,
        1,
        f"token:{token}"
    )
    
    if not transition_success:
        return PurchaseResponse(success=False, reason="Purchase already in progress")
    
    user_id = token_data.get("user_id")
    product_id = token_data.get("product_id", "phone")
    reservation_id = None
    
    #Reserve stock
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{INVENTORY_URL}/reserve/{product_id}")
            if response.status_code == 200:
                reserve_data = response.json()
                if not reserve_data.get("success", False):
                    await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
                    return PurchaseResponse(
                        success=False,
                        reason=reserve_data.get("reason", "Reservation failed")
                    )
                reservation_id = reserve_data.get("reservation_id")
            else:
                await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
                return PurchaseResponse(success=False, reason="Reservation failed")
    except Exception as exc:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=f"Reservation error: {exc}")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payment_response = await client.post(
                f"{PAYMENT_URL}/charge",
                json={
                    "idempotency_key": token,
                    "amount": 999.99,
                    "user_id": user_id,
                    "product_id": product_id
                }
            )
            payment_response.raise_for_status()
            payment_data = payment_response.json()

        if payment_data.get("status") != "succeeded":
            if reservation_id:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        await client.post(f"{INVENTORY_URL}/release/{reservation_id}")
                except Exception:
                    pass
            await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
            return PurchaseResponse(
                success=False,
                reason=payment_data.get("reason", "Payment failed")
            )

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 503:
            await redis_async(
                redis_client.hset,
                f"token:{token}",
                "status",
                "pending_verification"
            )
            return PurchaseResponse(
                success=False,
                reason="Payment gateway timeout - verifying transaction",
                order={"status": "pending_verification", "token": token}
            )

        if reservation_id:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"{INVENTORY_URL}/release/{reservation_id}")
            except Exception:
                pass
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=f"Payment error: {exc}")

    except httpx.TimeoutException:
        await redis_async(
            redis_client.hset,
            f"token:{token}",
            "status",
            "pending_verification"
        )
        return PurchaseResponse(
            success=False,
            reason="Payment timeout - verifying transaction",
            order={"status": "pending_verification", "token": token}
        )

    except Exception as exc:
        if reservation_id:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(f"{INVENTORY_URL}/release/{reservation_id}")
            except Exception:
                pass

        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=f"Payment error: {exc}")
    
    # Create order
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            order_response = await client.post(
                f"{CHECKOUT_URL}/order",
                json={
                    "user_id": user_id,
                    "product_id": product_id,
                    "reservation_id": reservation_id,
                    "idempotency_key": token
                }
            )
            order_response.raise_for_status()
            order_data = order_response.json()
    except Exception as exc:
        logger.exception(
            "Order creation failed for token %s: %s",
            token,
            exc,
        )
        await redis_async(redis_client.hset, f"token:{token}", "status", "manual_review")
        return PurchaseResponse(
            success=False,
            reason=f"Order creation failed: {exc}",
        )
    
    #Success
    await redis_async(redis_client.hset, f"token:{token}", "status", "completed")
    user_product_key = f"active_token:{user_id}:{product_id}"
    await redis_async(redis_client.delete, user_product_key)
    
    return PurchaseResponse(success=True, order=order_data)


@app.get("/stock/{product_id}")
async def get_stock(product_id: str):
    """Proxy to inventory service"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/stock/{product_id}/{quantity}")
async def set_stock(product_id: str, quantity: int):
    """Proxy the demo stock control through the same public gateway."""
    if quantity < 0:
        raise HTTPException(status_code=400, detail="Stock cannot be negative")

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(
                f"{INVENTORY_URL}/stock/{product_id}/{quantity}"
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Inventory rejected the stock update"
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=detail,
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Inventory service unavailable: {exc}",
        ) from exc
