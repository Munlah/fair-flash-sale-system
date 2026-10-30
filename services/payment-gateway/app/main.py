"""Small mock payment provider used by the demo and load tests.

Payments are intentionally in memory. That keeps the demo simple, but it also
means a payment pod restart clears its local payment records.
"""

"""
Payment Gateway Service - Mock payment processor
==========================================================================
Simulates realistic payment gateway behavior including:
- Network delays
- Random timeouts and failures
- Idempotent charge processing
- Payment status verification
"""

import os
import random
import time
import uuid
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Payment Gateway Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory payment storage (Redis would be used in production)
payments: Dict[str, dict] = {}

# Configuration
FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0.1"))  # 10% by default
TIMEOUT_RATE = float(os.getenv("PAYMENT_TIMEOUT_RATE", "0.05"))  # 5% by default
MIN_DELAY = float(os.getenv("PAYMENT_MIN_DELAY", "0.5"))
MAX_DELAY = float(os.getenv("PAYMENT_MAX_DELAY", "2.0"))


class ChargeRequest(BaseModel):
    idempotency_key: str
    amount: float
    user_id: str
    product_id: str


class ChargeResponse(BaseModel):
    payment_id: str
    status: str  # "succeeded", "failed", "processing"
    amount: float
    user_id: str


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "payment-gateway",
        "config": {
            "failure_rate": FAILURE_RATE,
            "timeout_rate": TIMEOUT_RATE
        }
    }


@app.post("/charge", response_model=ChargeResponse)
async def charge(request: ChargeRequest):
    """
    Process a payment charge with realistic failure scenarios
    
    Idempotent: Same idempotency_key returns the same result
    """
    
    # Check if payment already exists (idempotency)
    if request.idempotency_key in payments:
        existing = payments[request.idempotency_key]
        return ChargeResponse(**existing)
    
    # Simulate network delay
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    time.sleep(delay)
    
    # Simulate timeout (gateway doesn't respond)
    if random.random() < TIMEOUT_RATE:
        # In reality this would hang, but we'll raise 503
        # The gateway will timeout waiting and not know the result
        time.sleep(5)  # Extra delay before timeout
        raise HTTPException(status_code=503, detail="Gateway timeout")
    
    # Simulate random failures
    if random.random() < FAILURE_RATE:
        payment_id = f"pay_{uuid.uuid4().hex[:16]}"
        payment_record = {
            "payment_id": payment_id,
            "status": "failed",
            "amount": request.amount,
            "user_id": request.user_id,
            "reason": "insufficient_funds"  # or card_declined, etc.
        }
        payments[request.idempotency_key] = payment_record
        return ChargeResponse(**payment_record)
    
    # Success - record payment
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    payment_record = {
        "payment_id": payment_id,
        "status": "succeeded",
        "amount": request.amount,
        "user_id": request.user_id
    }
    payments[request.idempotency_key] = payment_record
    
    return ChargeResponse(**payment_record)


@app.get("/status/{idempotency_key}", response_model=ChargeResponse)
async def get_payment_status(idempotency_key: str):
    """
    Check payment status by idempotency key
    
    Used for verification after timeouts
    """
    if idempotency_key not in payments:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    payment = payments[idempotency_key]
    return ChargeResponse(**payment)


@app.get("/stats")
async def get_stats():
    """Get payment statistics"""
    total = len(payments)
    succeeded = sum(1 for p in payments.values() if p["status"] == "succeeded")
    failed = sum(1 for p in payments.values() if p["status"] == "failed")
    
    return {
        "total_payments": total,
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": succeeded / total if total > 0 else 0
    }


@app.delete("/reset")
async def reset_payments():
    """Reset all payments (for testing)"""
    payments.clear()
    return {"message": "All payments cleared"}