"""
Gateway Service - Entry point for all user requests
==========================================================================
"""

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
import asyncio

# Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8001")
CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://localhost:8002")
logger = logging.getLogger(__name__)

app = FastAPI(title="Gateway Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sync Redis client (simpler, more reliable)
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True
)

# Thread pool for running sync Redis in async context
executor = ThreadPoolExecutor(max_workers=10)

async def redis_async(func, *args, **kwargs):
    """Helper to run sync Redis commands in thread pool"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

# Data Models
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

@app.get("/health")
async def health():
    """Health check"""
    try:
        await redis_async(redis_client.ping)
        return {"status": "healthy", "service": "gateway", "redis": "connected"}
    except:
        return {"status": "degraded", "service": "gateway", "redis": "disconnected"}

@app.post("/queue/join", response_model=QueueJoinResponse)
async def join_queue(user_id: str, product_id: str = "phone"):
    """
    Add user to queue with spam prevention using an atomic SET NX.
    """
    user_product_key = f"active_token:{user_id}:{product_id}"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            if response.status_code == 200:
                stock_data = response.json()
                if stock_data.get("stock", 0) <= 0:
                    raise HTTPException(status_code=410, detail="Product is sold out")
            else:
                logger.warning(
                    "Stock check for %s returned HTTP %s; allowing queue join",
                    product_id,
                    response.status_code
                )
    except HTTPException:
        raise
    except (httpx.RequestError, ValueError) as exc:
        logger.warning(
            "Could not check stock for %s; allowing queue join: %s",
            product_id,
            exc
        )
    
    token = str(uuid.uuid4())
    token_claimed = await redis_async(
        redis_client.set,
        user_product_key,
        token,
        ex=3600,
        nx=True
    )

    if not token_claimed:
        existing_token = await redis_async(redis_client.get, user_product_key)
        if existing_token:
            token_data = await redis_async(redis_client.hgetall, f"token:{existing_token}")
            if token_data and token_data.get("status") != "completed":
                queue_key = f"queue:{product_id}"
                actual_status = token_data.get("status", "waiting")
                position = None
                if actual_status == "waiting":
                    position = await redis_async(redis_client.zrank, queue_key, existing_token)
                return QueueJoinResponse(
                    token=existing_token,
                    status=actual_status,
                    position=position if position is not None else 0
                )

        await redis_async(redis_client.delete, user_product_key)
        token_claimed = await redis_async(
            redis_client.set,
            user_product_key,
            token,
            ex=3600,
            nx=True
        )
        if not token_claimed:
            existing_token = await redis_async(redis_client.get, user_product_key)
            if not existing_token:
                raise HTTPException(status_code=409, detail="Unable to create queue token")
            token_data = await redis_async(redis_client.hgetall, f"token:{existing_token}")
            actual_status = token_data.get("status", "waiting")
            position = None
            if actual_status == "waiting":
                position = await redis_async(
                    redis_client.zrank,
                    f"queue:{product_id}",
                    existing_token
                )
            return QueueJoinResponse(
                token=existing_token,
                status=actual_status,
                position=position if position is not None else 0
            )

    arrival_ms = int(time.time() * 1000)
    queue_key = f"queue:{product_id}"
    
    await redis_async(
        redis_client.zadd,
        queue_key,
        mapping={token: arrival_ms}
    )
    
    # Store token metadata
    token_data = {
        "user_id": user_id,
        "product_id": product_id,
        "status": "waiting",
        "arrival_ms": str(arrival_ms)
    }
    await redis_async(redis_client.hset, f"token:{token}", mapping=token_data)
    
    # Get position
    position = await redis_async(redis_client.zrank, queue_key, token)
    
    return QueueJoinResponse(
        token=token,
        status="waiting",
        position=position if position is not None else 0
    )

@app.get("/queue/status/{token}", response_model=TokenStatusResponse)
async def check_status(token: str):
    """Check token status"""
    token_data = await redis_async(redis_client.hgetall, f"token:{token}")
    
    if not token_data:
        raise HTTPException(status_code=404, detail="Token not found")
    
    status = token_data.get("status", "waiting")
    position = None
    
    if status == "waiting":
        product_id = token_data.get("product_id", "phone")
        queue_key = f"queue:{product_id}"
        position = await redis_async(redis_client.zrank, queue_key, token)
        if position is None:
            position = 0
    
    return TokenStatusResponse(status=status, position=position)

@app.post("/purchase", response_model=PurchaseResponse)
async def purchase(token: str):
    """
    Complete purchase with bug fixes:
    1. Check for existing order before reserving
    2. Atomic status transition
    3. Detect sold-out properly
    4. Release reservation on checkout failure
    """
    token_data = await redis_async(redis_client.hgetall, f"token:{token}")
    
    if not token_data:
        return PurchaseResponse(success=False, reason="Token not found")
    
    status = token_data.get("status")
    
    # Check if already completed
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
    
    # Atomic status transition using Lua script
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
    
    # Check for existing order (prevents stock leak on retry)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{CHECKOUT_URL}/order/by-idempotency/{token}")
            if response.status_code == 200:
                await redis_async(redis_client.hset, f"token:{token}", "status", "completed")
                return PurchaseResponse(success=True, order=response.json())
            if response.status_code != 404:
                await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
                return PurchaseResponse(success=False, reason="Error checking existing order")
    except (httpx.RequestError, ValueError) as e:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=f"Error checking existing order: {e}")
    
    # Reserve stock
    reservation_id = None
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
                if not reservation_id:
                    await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
                    return PurchaseResponse(success=False, reason="Invalid reservation response")
            else:
                await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
                try:
                    error_detail = response.json().get("detail", "Reservation failed")
                except ValueError:
                    error_detail = f"Reservation failed (HTTP {response.status_code})"
                return PurchaseResponse(success=False, reason=error_detail)
    except httpx.TimeoutException:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason="Reservation timeout")
    except (httpx.RequestError, ValueError) as exc:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=f"Reservation error: {exc}")
    
    # Create order
    order_created = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{CHECKOUT_URL}/order",
                json={
                    "user_id": user_id,
                    "product_id": product_id,
                    "reservation_id": reservation_id,
                    "idempotency_key": token
                }
            )
            response.raise_for_status()
            order_data = response.json()
            order_created = True
    except (httpx.HTTPError, ValueError):
        return PurchaseResponse(success=False, reason="Order creation failed")
    finally:
        if reservation_id is not None and not order_created:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    release_response = await client.post(
                        f"{INVENTORY_URL}/release/{reservation_id}"
                    )
                    release_response.raise_for_status()
            except httpx.HTTPError:
                logger.warning(
                    "Failed to release reservation %s after checkout failure",
                    reservation_id,
                    exc_info=True
                )
            await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
    
    # Success
    await redis_async(redis_client.hset, f"token:{token}", "status", "completed")
    user_product_key = f"active_token:{user_id}:{product_id}"
    await redis_async(redis_client.delete, user_product_key)
    
    return PurchaseResponse(success=True, order=order_data)

@app.get("/stock/{product_id}")
async def get_stock(product_id: str):
    """Proxy to inventory service"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stock/{product_id}/{quantity}")
async def set_stock(product_id: str, quantity: int):
    """Proxy to inventory service"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{INVENTORY_URL}/stock/{product_id}/{quantity}")
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))