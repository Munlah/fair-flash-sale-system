"""
Payment Reconciler - Verifies pending_verification transactions
Queries payment gateway to confirm if charge succeeded
"""
import asyncio
import os
import httpx
import redis.asyncio as redis

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-gateway:8000")
CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")

async def reconcile_pending_transactions():
    """Find and verify pending_verification transactions"""
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    
    try:
        cursor = 0
        reconciled_count = 0
        
        while True:
            cursor, keys = await client.scan(cursor, match="token:*", count=100)
            
            for key in keys:
                token_data = await client.hgetall(key)
                status = token_data.get("status")
                
                if status == "pending_verification":
                    token = key.split(":", 1)[1]
                    user_id = token_data.get("user_id")
                    product_id = token_data.get("product_id")
                    
                    print(f"🔍 Verifying transaction: {token}")
                    
                    # Check payment status
                    async with httpx.AsyncClient(timeout=5.0) as http_client:
                        try:
                            payment_resp = await http_client.get(
                                f"{PAYMENT_URL}/status/{token}"
                            )
                            
                            if payment_resp.status_code == 200:
                                payment_data = payment_resp.json()
                                
                                if payment_data.get("status") == "succeeded":
                                    # Payment succeeded - complete the order
                                    print(f"✓ Payment succeeded, completing order for {token}")
                                    
                                    # Get reservation_id (should be stored in token data)
                                    reservation_id = token_data.get("reservation_id")
                                    
                                    # Create order
                                    order_resp = await http_client.post(
                                        f"{CHECKOUT_URL}/order",
                                        json={
                                            "user_id": user_id,
                                            "product_id": product_id,
                                            "reservation_id": reservation_id or "reconciled",
                                            "idempotency_key": token
                                        }
                                    )
                                    
                                    if order_resp.status_code == 200:
                                        await client.hset(f"token:{token}", "status", "completed")
                                        reconciled_count += 1
                                        print(f"✓ Order completed for {token}")
                                
                                elif payment_data.get("status") == "failed":
                                    # Payment failed - release reservation
                                    print(f"✗ Payment failed, releasing reservation for {token}")
                                    
                                    reservation_id = token_data.get("reservation_id")
                                    if reservation_id:
                                        await http_client.post(
                                            f"{INVENTORY_URL}/release/{reservation_id}"
                                        )
                                    
                                    await client.hset(f"token:{token}", "status", "admitted")
                                    reconciled_count += 1
                        
                        except Exception as e:
                            print(f"⚠ Failed to verify {token}: {e}")
            
            if cursor == 0:
                break
        
        if reconciled_count > 0:
            print(f"✓ Reconciled {reconciled_count} transactions")
    
    finally:
        await client.aclose()

async def run_reconciler():
    """Run reconciler loop"""
    print("🔄 Payment reconciler started")
    
    while True:
        try:
            await reconcile_pending_transactions()
        except Exception as e:
            print(f"✗ Reconciler error: {e}")
        
        await asyncio.sleep(30)  # Run every 30 seconds

if __name__ == "__main__":
    asyncio.run(reconcile_pending_transactions())