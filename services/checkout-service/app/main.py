"""Handles order creation, retrieval, and durable storage in PostgreSQL. It uses connection pooling to handle concurrency spikes, auto-creates tables on boot, and relies on database-level UNIQUE constraints alongside idempotency keys to guarantee no duplicate orders go through—even when flooded with requests."""

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

#configuration: read from environment variables

#build DATABASE_URL from individual env vars (matches Kubernetes secret)
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_USER = os.getenv("POSTGRES_USER", "flash")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "flashpass")
POSTGRES_DB = os.getenv("POSTGRES_DB", "orders")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:5432/{POSTGRES_DB}"

db_pool: Optional[asyncpg.Pool] = None

#lifespan: manage startup and shutdown

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles the app's lifecycle: spins up the database connection pool on boot, makes sure the orders table is created if it's missing, and cleanly closes the pool when the app shuts down."""
    global db_pool
    
    try:
        #create connection pool
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=5,
            max_size=20,
            command_timeout=10
        )
        print(f"Database pool created: {DATABASE_URL}")
        
        #create orders table if it doesn't exist
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE SEQUENCE IF NOT EXISTS orders_order_id_seq
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    order_id VARCHAR(50) UNIQUE NOT NULL
                        DEFAULT ('ord-' || nextval('orders_order_id_seq')),
                    user_id VARCHAR(255) NOT NULL,
                    product_id VARCHAR(255) NOT NULL,
                    reservation_id VARCHAR(255) NOT NULL,
                    idempotency_key VARCHAR(255) UNIQUE NOT NULL,
                    status VARCHAR(50) DEFAULT 'CONFIRMED',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            await conn.execute("""
                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS id BIGSERIAL
            """)
            await conn.execute("""
                ALTER TABLE orders
                ADD COLUMN IF NOT EXISTS order_id VARCHAR(50)
            """)
            await conn.execute("""
                UPDATE orders
                SET order_id = 'ord-' || id::text
                WHERE order_id IS NULL
            """)
            await conn.execute("""
                ALTER TABLE orders
                ALTER COLUMN order_id SET DEFAULT
                    ('ord-' || nextval('orders_order_id_seq'))
            """)
            await conn.execute("""
                ALTER TABLE orders
                ALTER COLUMN order_id SET NOT NULL
            """)
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS orders_id_key
                ON orders(id)
            """)
            
            #create index for faster lookups
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_idempotency_key 
                ON orders(idempotency_key)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_user_id 
                ON orders(user_id)
            """)
            
            print("Orders table ready")
            
    except Exception as e:
        print(f"Failed to initialize database: {e}")
        raise
    
    yield
    
    #shutdown: close pool
    if db_pool:
        await db_pool.close()
        print("Database pool closed")

#initialize FastAPI application

app = FastAPI(title="Checkout Service", version="1.0.0", lifespan=lifespan)

#enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#data Models: define request/response structures
class OrderRequest(BaseModel):
    """request body for creating an order"""
    user_id: str
    product_id: str
    reservation_id: str
    idempotency_key: str  #prevents duplicate orders (usually the token)

class OrderResponse(BaseModel):
    """response after creating or retrieving an order"""
    id: int
    user_id: str
    product_id: str
    reservation_id: str
    idempotency_key: str
    status: str
    created_at: str  #changed to str for JSON serialization

#health check endpoint

@app.get("/health")
async def health():
    """health check endpoint: verifies database connection is working"""
    try:
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return {"status": "healthy", "service": "checkout", "database": "connected"}
        else:
            return {"status": "unhealthy", "service": "checkout", "database": "not_initialized"}
    except Exception as e:
        return {"status": "unhealthy", "service": "checkout", "error": str(e)}

#order creation: create new order with idempotency

@app.post("/order", response_model=OrderResponse)
async def create_order(order: OrderRequest):
    """creates a new order while preventing duplicates using the idempotency_key (usually the queue token). even if retries or concurrent requests hit this at the exact same time, Postgres's UNIQUE constraint ensures only a single order gets written."""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            
            #check if order already exists (idempotency)
            existing = await conn.fetchrow(
                """
                SELECT id, user_id, product_id, reservation_id, 
                       idempotency_key, status, created_at
                FROM orders
                WHERE idempotency_key = $1
                """,
                order.idempotency_key
            )
            
            if existing:
                #order already exists: return it
                return OrderResponse(
                    id=existing['id'],
                    user_id=existing['user_id'],
                    product_id=existing['product_id'],
                    reservation_id=existing['reservation_id'],
                    idempotency_key=existing['idempotency_key'],
                    status=existing['status'],
                    created_at=existing['created_at'].isoformat()
                )
            
            #create new order with ON CONFLICT to handle race conditions
            result = await conn.fetchrow(
                """
                INSERT INTO orders (user_id, product_id, reservation_id, idempotency_key, status, created_at)
                VALUES ($1, $2, $3, $4, 'CONFIRMED', NOW())
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id, user_id, product_id, reservation_id, idempotency_key, status, created_at
                """,
                order.user_id,
                order.product_id,
                order.reservation_id,
                order.idempotency_key
            )
            
            if result:
                #successfully created new order
                return OrderResponse(
                    id=result['id'],
                    user_id=result['user_id'],
                    product_id=result['product_id'],
                    reservation_id=result['reservation_id'],
                    idempotency_key=result['idempotency_key'],
                    status=result['status'],
                    created_at=result['created_at'].isoformat()
                )
            else:
                #ON CONFLICT triggered: fetch the existing order
                existing = await conn.fetchrow(
                    """
                    SELECT id, user_id, product_id, reservation_id, 
                           idempotency_key, status, created_at
                    FROM orders
                    WHERE idempotency_key = $1
                    """,
                    order.idempotency_key
                )
                
                if existing:
                    return OrderResponse(
                        id=existing['id'],
                        user_id=existing['user_id'],
                        product_id=existing['product_id'],
                        reservation_id=existing['reservation_id'],
                        idempotency_key=existing['idempotency_key'],
                        status=existing['status'],
                        created_at=existing['created_at'].isoformat()
                    )
                else:
                    raise HTTPException(status_code=500, detail="Order creation failed")
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating order: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

#order retrieval: get order by various keys

@app.get("/order/{order_id}", response_model=OrderResponse)
async def get_order(order_id: int):
    """get order by database ID"""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                SELECT id, user_id, product_id, reservation_id, 
                       idempotency_key, status, created_at
                FROM orders
                WHERE id = $1
                """,
                order_id
            )
            
            if not result:
                raise HTTPException(status_code=404, detail="Order not found")
            
            return OrderResponse(
                id=result['id'],
                user_id=result['user_id'],
                product_id=result['product_id'],
                reservation_id=result['reservation_id'],
                idempotency_key=result['idempotency_key'],
                status=result['status'],
                created_at=result['created_at'].isoformat()
            )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving order: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/order/by-idempotency/{idempotency_key}", response_model=OrderResponse)
async def get_order_by_idempotency(idempotency_key: str):
    """Get order by idempotency key (usually the queue token),this endpoint is used by the gateway to check if an order already exists before attempting to reserve stock (fixes the retry leak bug)."""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                SELECT id, user_id, product_id, reservation_id, 
                       idempotency_key, status, created_at
                FROM orders
                WHERE idempotency_key = $1
                """,
                idempotency_key
            )
            
            if not result:
                raise HTTPException(status_code=404, detail="Order not found")
            
            return OrderResponse(
                id=result['id'],
                user_id=result['user_id'],
                product_id=result['product_id'],
                reservation_id=result['reservation_id'],
                idempotency_key=result['idempotency_key'],
                status=result['status'],
                created_at=result['created_at'].isoformat()
            )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving order: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/order/by-token/{token}", response_model=OrderResponse)
async def get_order_by_token(token: str):
    """get order by queue token (alias for by-idempotency endpoint)"""
    return await get_order_by_idempotency(token)

@app.get("/orders/user/{user_id}")
async def get_user_orders(user_id: str):
    """get all orders for a specific user"""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            results = await conn.fetch(
                """
                SELECT id, user_id, product_id, reservation_id, 
                       idempotency_key, status, created_at
                FROM orders
                WHERE user_id = $1
                ORDER BY created_at DESC
                """,
                user_id
            )
            
            orders = [
                {
                    "id": row['id'],
                    "user_id": row['user_id'],
                    "product_id": row['product_id'],
                    "reservation_id": row['reservation_id'],
                    "idempotency_key": row['idempotency_key'],
                    "status": row['status'],
                    "created_at": row['created_at'].isoformat()
                }
                for row in results
            ]
            
            return {"user_id": user_id, "orders": orders, "count": len(orders)}
    
    except Exception as e:
        print(f"Error retrieving user orders: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

#statistics: get order counts

@app.get("/orders/count")
async def get_order_count():
    """get total number of orders, used by the UI dashboard to display real order count"""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM orders")
            return {"total_orders": count}
    
    except Exception as e:
        print(f"Error counting orders: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/orders/count/product/{product_id}")
async def get_product_order_count(product_id: str):
    """get number of orders for a specific product"""
    
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database pool not initialized")
    
    try:
        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE product_id = $1",
                product_id
            )
            return {"product_id": product_id, "order_count": count}
    
    except Exception as e:
        print(f"Error counting product orders: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")