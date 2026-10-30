"""
Reservation Reaper - Cleans up expired reservations
Runs every 30 seconds to find reservations past their TTL and releases them
"""
import asyncio
import os
import time
import redis.asyncio as redis

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
RESERVATION_TTL = 300  # 5 minutes

async def reap_expired_reservations():
    """Find and release expired reservations"""
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    
    try:
        expired_count = 0

        expired = await client.zrangebyscore(
            "reservation_expirations",
            "-inf",
            time.time(),
        )

        for reservation_id in expired:
            reservation_key = f"reservation:{reservation_id}"
            reservation = await client.hgetall(reservation_key)
            product_id = reservation.get("product_id") or await client.get(
                f"reservation-product:{reservation_id}"
            )

            if not product_id:
                await client.zrem("reservation_expirations", reservation_id)
                continue

            released_key = f"released:{reservation_id}"
            if not await client.get(released_key):
                await client.setex(released_key, 3600, "1")
                await client.incr(f"stock:{product_id}")
                expired_count += 1
                print(
                    f"✓ Released expired reservation: {reservation_id} "
                    f"(product: {product_id})"
                )

            await client.delete(reservation_key)
            await client.delete(f"reservation-product:{reservation_id}")
            await client.zrem("reservation_expirations", reservation_id)
        
        if expired_count > 0:
            print(f"✓ Reaped {expired_count} expired reservations")
        
    finally:
        await client.aclose()

async def run_reaper():
    """Run reaper loop"""
    print("🧹 Reservation reaper started")
    
    while True:
        try:
            await reap_expired_reservations()
        except Exception as e:
            print(f"✗ Reaper error: {e}")
        
        await asyncio.sleep(30)  # Run every 30 seconds

if __name__ == "__main__":
    asyncio.run(reap_expired_reservations())