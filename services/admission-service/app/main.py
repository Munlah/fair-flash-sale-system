"""Admission controller for the flash-sale waiting room.

The service reads waiting tokens from Redis and moves a limited number to
``admitted`` on every tick. The windowed mode shuffles users inside a closed
time window while keeping older windows ahead of newer ones.
"""

import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from leader_election import run_with_leader_election

# The controller and gateway share this Redis instance.
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=6379,
    decode_responses=True,
)

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8000")
WINDOW_MS = int(os.getenv("WINDOW_MS", "3000"))
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "1"))
BASE_RATE = int(os.getenv("BASE_RATE", "5"))
TRACKED_PRODUCTS = [p for p in os.getenv("TRACKED_PRODUCTS", "phone").split(",") if p]
SUPPORTED_MODES = {"windowed", "fifo", "random", "none"}
ADMISSION_MODE = os.getenv("ADMISSION_MODE", "windowed").lower()
if ADMISSION_MODE not in SUPPORTED_MODES:
    print(f"Unsupported ADMISSION_MODE={ADMISSION_MODE!r}; using windowed")
    ADMISSION_MODE = "windowed"

print(
    "Config loaded: "
    f"ADMISSION_MODE={ADMISSION_MODE}, WINDOW_MS={WINDOW_MS}, "
    f"TICK_SECONDS={TICK_SECONDS}, BASE_RATE={BASE_RATE}"
)


async def get_capacity(product_id: str) -> int:
    """Use current stock to limit how many users can be admitted."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{INVENTORY_URL}/stock/{product_id}")
            resp.raise_for_status()
            stock = resp.json().get("stock", 0)
            print(f"Got stock for {product_id}: {stock}")
    except (httpx.HTTPError, ValueError) as e:
        print(f"Failed to get capacity for {product_id}: {e}")
        return 0

    if stock <= 0:
        return 0
    return min(BASE_RATE, stock)


def update_token_status(product_id: str, token: str) -> bool:
    """Remove a token from the queue and mark it admitted atomically."""
    pipe = redis_client.pipeline()
    pipe.zrem(f"queue:{product_id}", token)
    pipe.hset(f"token:{token}", "status", "admitted")
    removed, _ = pipe.execute()
    return bool(removed)


def admit_batch_windowed(product_id: str, capacity: int) -> int:
    """Admit a deterministic random selection from each closed time window."""
    if capacity <= 0:
        return 0

    waiting = redis_client.zrange(f"queue:{product_id}", 0, -1, withscores=True)
    
    if not waiting:
        return 0

    print(f"Processing {len(waiting)} waiting tokens for {product_id}")
    now_ms = int(time.time() * 1000)

    windows: dict[int, list[str]] = {}
    for token, arrival in waiting:
        w_start = (int(arrival) // WINDOW_MS) * WINDOW_MS
        windows.setdefault(w_start, []).append(token)

    admitted = 0
    for w_start in sorted(windows):
        window_end = w_start + WINDOW_MS
        
        if window_end > now_ms:
            print(f"Window {w_start} not closed yet (ends {window_end}, now {now_ms})")
            break
        
        if admitted >= capacity:
            break

        cohort = windows[w_start]
        rng = random.Random(w_start)
        rng.shuffle(cohort)

        for token in cohort:
            if admitted >= capacity:
                break
            if update_token_status(product_id, token):
                admitted += 1
                print(f"✓ [WINDOWED] Admitted token {token[:8]}... from window {w_start}")

    if admitted > 0:
        print(f"=== Admitted {admitted} tokens for {product_id} ===")
    
    return admitted


def admit_batch_fifo(product_id: str, capacity: int) -> int:
    """Admit the oldest waiting tokens first."""
    if capacity <= 0:
        return 0

    waiting = redis_client.zrange(f"queue:{product_id}", 0, capacity - 1)
    admitted = sum(
        update_token_status(product_id, token)
        for token in waiting
    )
    if admitted:
        print(f"=== [FIFO] Admitted {admitted} tokens for {product_id} ===")
    return admitted


def admit_batch_random(product_id: str, capacity: int) -> int:
    """Admit a random sample of waiting tokens."""
    if capacity <= 0:
        return 0

    waiting = list(redis_client.zrange(f"queue:{product_id}", 0, -1))
    random.shuffle(waiting)
    admitted = sum(
        update_token_status(product_id, token)
        for token in waiting[:capacity]
    )
    if admitted:
        print(f"=== [RANDOM] Admitted {admitted} tokens for {product_id} ===")
    return admitted


def admit_batch_none(product_id: str, capacity: int) -> int:
    """Admit every waiting token without rate limiting."""
    waiting = redis_client.zrange(f"queue:{product_id}", 0, -1)
    admitted = sum(
        update_token_status(product_id, token)
        for token in waiting
    )
    if admitted:
        print(f"=== [NONE] Admitted {admitted} tokens for {product_id} ===")
    return admitted


ADMISSION_FUNCTIONS = {
    "windowed": admit_batch_windowed,
    "fifo": admit_batch_fifo,
    "random": admit_batch_random,
    "none": admit_batch_none,
}


async def admission_loop_iteration():
    """Single iteration of the admission loop (called by leader election)"""
    for product_id in TRACKED_PRODUCTS:
        queue_size = redis_client.zcard(f"queue:{product_id}")
        
        if queue_size > 0:
            print(f"Queue for {product_id}: {queue_size} waiting")
            capacity = (
                queue_size
                if ADMISSION_MODE == "none"
                else await get_capacity(product_id)
            )
            ADMISSION_FUNCTIONS[ADMISSION_MODE](product_id, capacity)


async def admission_loop():
    """Main admission loop with leader election"""
    print(f"🚀 ADMISSION LOOP STARTED ({ADMISSION_MODE}) WITH LEADER ELECTION 🚀")
    
    # Run admission loop with leader election
    await run_with_leader_election(admission_loop_iteration)


admission_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global admission_task
    print("=" * 50)
    print(f"STARTING ADMISSION CONTROLLER (MODE: {ADMISSION_MODE})")
    print("LEADER ELECTION: ENABLED")
    print("=" * 50)
    
    try:
        redis_client.ping()
        print("✓ Redis connected")
    except Exception as e:
        print(f"✗ Redis connection failed: {e}")
    
    admission_task = asyncio.create_task(admission_loop())
    print("✓ Background task started with leader election")
    
    yield
    
    print("Shutting down...")
    if admission_task:
        admission_task.cancel()


app = FastAPI(title="Admission Controller", lifespan=lifespan)

# The browser dashboard calls this service for queue statistics.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Report whether the controller can reach Redis."""
    try:
        redis_client.ping()
        return {
            "status": "healthy",
            "redis": "connected",
            "mode": ADMISSION_MODE,
            "leader_election": "enabled",  # ← ADD THIS
        }
    except redis.RedisError:
        return {
            "status": "unhealthy",
            "redis": "disconnected",
            "mode": ADMISSION_MODE,
        }


@app.get("/queue/{product_id}/length")
def queue_length(product_id: str):
    """Return the number of users still waiting for a product."""
    count = redis_client.zcard(f"queue:{product_id}")
    return {"product_id": product_id, "waiting": count, "mode": ADMISSION_MODE}


@app.get("/mode")
def get_mode():
    """Return the active admission strategy."""
    return {
        "mode": ADMISSION_MODE,
        "available_modes": sorted(SUPPORTED_MODES),
    }