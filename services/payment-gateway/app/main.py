"""simple in-memory mock payment provider used across the demo and load tests. keeping everything in memory makes local setup easy, but keep in mind that restarting the pod clears out the history. it handles the usual mock gateway quirks: random failures, network delays, idempotency keys, and payment status checks."""

import os
import asyncio
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

#in-memory payment storage (Redis would be used in production)
payments: Dict[str, dict] = {}

#configuration
FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0.3"))  #30% for the demo
TIMEOUT_RATE = float(os.getenv("PAYMENT_TIMEOUT_RATE", "0.05"))  #5% by default
MIN_DELAY = float(os.getenv("PAYMENT_MIN_DELAY", "0.5"))
MAX_DELAY = float(os.getenv("PAYMENT_MAX_DELAY", "2.0"))


class ChargeRequest(BaseModel):
    idempotency_key: str
    amount: float
    user_id: str
    product_id: str


class ChargeResponse(BaseModel):
    payment_id: str
    status: str  #"succeeded", "failed", "processing"
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
    """process a payment charge with realistic failure scenarios. idempotent: Same idempotency_key returns the same result"""
    
    #check if payment already exists(idempotency)
    existing = payments.get(request.idempotency_key)
    if existing and existing["status"] == "succeeded":
        return ChargeResponse(**existing)
    
    #simulate network delay
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    await asyncio.sleep(delay)
    
    #simulate timeout(gateway doesn't respond)
    if random.random() < TIMEOUT_RATE:
        #in reality this would hang, but we'll raise 503
        #the gateway will timeout waiting and not know the result
        await asyncio.sleep(5)  #extra delay before timeout
        raise HTTPException(status_code=503, detail="Gateway timeout")
    
    #simulate random failures
    if random.random() < FAILURE_RATE:
        payment_id = f"pay_{uuid.uuid4().hex[:16]}"
        payment_record = {
            "payment_id": payment_id,
            "status": "failed",
            "amount": request.amount,
            "user_id": request.user_id,
            "reason": "insufficient_funds"  #or card_declined, etc.
        }
        return ChargeResponse(**payment_record)
    
    #success, record payment
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
    """check payment status by idempotency key, used for verification after timeouts"""
    if idempotency_key not in payments:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    payment = payments[idempotency_key]
    return ChargeResponse(**payment)


@app.get("/stats")
async def get_stats():
    """get payment statistics"""
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
    """reset all payments (for testing)"""
    payments.clear()
    return {"message": "All payments cleared"}