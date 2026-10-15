"""
Gateway Service - Entry point for all user requests
==========================================================================
"""

import os
import uuid
import time
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
    Add user to queue with spam prevention
    """
    user_product_key = f"active_token:{user_id}:{product_id}"
    
    # Check for existing token
    existing_token = await redis_async(redis_client.get, user_product_key)
    if existing_token:
        token_data = await redis_async(redis_client.hgetall, f"token:{existing_token}")
        if token_data and token_data.get("status") != "completed":
            queue_key = f"queue:{product_id}"
            position = await redis_async(redis_client.zrank, queue_key, existing_token)
            return QueueJoinResponse(
                token=existing_token,
                status="waiting",
                position=position if position is not None else 0
            )
    
    # Create new token
    token = str(uuid.uuid4())
    arrival_ms = int(time.time() * 1000)
    queue_key = f"queue:{product_id}"
    
    # Add to queue - FIX: Use mapping parameter
    # Add to queue - FIX: Use mapping parameter
    result = await redis_async(
        redis_client.zadd,
        queue_key,
        mapping={token: arrival_ms}
    )
    print(f"✓ ZADD result: {result}, token={token[:8]}, queue={queue_key}, score={arrival_ms}")

    # Verify it was added
    queue_size = await redis_async(redis_client.zcard, queue_key)
    print(f"✓ Queue size after add: {queue_size}")
    print(f"DEBUG: Added token {token[:8]} to queue, result={result}")
    
    # Store token metadata
    token_data = {
        "user_id": user_id,
        "product_id": product_id,
        "status": "waiting",
        "arrival_ms": str(arrival_ms)
    }
    await redis_async(redis_client.hset, f"token:{token}", mapping=token_data)
    
    # Store active token (1 hour expiry)
    await redis_async(redis_client.setex, user_product_key, 3600, token)
    
    # Get position
    position = await redis_async(redis_client.zrank, queue_key, token)
    print(f"DEBUG: Token {token[:8]} position in queue: {position}")
    
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
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
            return PurchaseResponse(success=False, reason="Error checking existing order")
    except Exception as e:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason=str(e))
    
    # Reserve stock
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{INVENTORY_URL}/reserve/{product_id}")
            response.raise_for_status()
            reserve_data = response.json()
            reservation_id = reserve_data.get("reservation_id")
    except:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason="Product sold out")
    
    # Create order
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
    except:
        await redis_async(redis_client.hset, f"token:{token}", "status", "admitted")
        return PurchaseResponse(success=False, reason="Order creation failed")
    
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