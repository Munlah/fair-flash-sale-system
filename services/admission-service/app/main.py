import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

print(f"Config loaded: WINDOW_MS={WINDOW_MS}, TICK_SECONDS={TICK_SECONDS}, BASE_RATE={BASE_RATE}")


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


def admit_batch(product_id: str, capacity: int) -> int:
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
            pipe = redis_client.pipeline()
            pipe.zrem(f"queue:{product_id}", token)
            pipe.hset(f"token:{token}", "status", "admitted")
            pipe.execute()
            admitted += 1
            print(f"✓ Admitted token {token[:8]}... from window {w_start}")

    if admitted > 0:
        print(f"=== Admitted {admitted} tokens for {product_id} ===")
    
    return admitted


async def admission_loop():
    print("🚀 ADMISSION LOOP STARTED 🚀")
    iteration = 0
    
    while True:
        try:
            iteration += 1
            print(f"\n--- Tick {iteration} ---")
            
            for product_id in TRACKED_PRODUCTS:
                queue_size = redis_client.zcard(f"queue:{product_id}")
                
                if queue_size > 0:
                    print(f"Queue for {product_id}: {queue_size} waiting")
                    capacity = await get_capacity(product_id)
                    admit_batch(product_id, capacity)
                    
        except Exception as e:
            print(f"❌ Error in admission loop: {e}")
            import traceback
            traceback.print_exc()
        
        await asyncio.sleep(TICK_SECONDS)


admission_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global admission_task
    print("=" * 50)
    print("STARTING ADMISSION CONTROLLER")
    print("=" * 50)
    
    try:
        redis_client.ping()
        print("✓ Redis connected")
    except Exception as e:
        print(f"✗ Redis connection failed: {e}")
    
    admission_task = asyncio.create_task(admission_loop())
    print("✓ Background task started")
    
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
        return {"status": "healthy", "redis": "connected"}
    except redis.RedisError:
        return {"status": "unhealthy", "redis": "disconnected"}


@app.get("/queue/{product_id}/length")
def queue_length(product_id: str):
    """Return the number of users still waiting for a product."""
    count = redis_client.zcard(f"queue:{product_id}")
    return {"product_id": product_id, "waiting": count}