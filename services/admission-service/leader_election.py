"""
Leader Election using Redis
Only one admission controller instance runs the admission loop at a time
"""
import asyncio
import os
import socket
import time
import redis.asyncio as redis

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
LEADER_KEY = "admission:leader"
LEADER_TTL = 10  #Leader lock expires after 10 seconds
HOSTNAME = socket.gethostname()  #Unique pod identifier

class LeaderElection:
    def __init__(self):
        self.client = None
        self.is_leader = False
    
    async def connect(self):
        """Connect to Redis"""
        self.client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True
        )
    
    async def try_acquire_leadership(self) -> bool:
        """Try to become the leader"""
        if not self.client:
            await self.connect()
        
        #Try to set the leader key with NX (only if it doesn't exist)
        acquired = await self.client.set(
            LEADER_KEY,
            HOSTNAME,
            ex=LEADER_TTL,
            nx=True
        )
        
        if acquired:
            self.is_leader = True
            print(f"✓ [{HOSTNAME}] Became leader")
            return True
        
        #Check if we're still the leader (in case of renewal)
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            #Renew our leadership
            await self.client.expire(LEADER_KEY, LEADER_TTL)
            self.is_leader = True
            return True
        
        self.is_leader = False
        return False
    
    async def renew_leadership(self):
        """Renew leadership lock"""
        if not self.is_leader:
            return
        
        if not self.client:
            await self.connect()
        
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            await self.client.expire(LEADER_KEY, LEADER_TTL)
            print(f"✓ [{HOSTNAME}] Renewed leadership")
        else:
            self.is_leader = False
            print(f"⚠ [{HOSTNAME}] Lost leadership")
    
    async def release_leadership(self):
        """Release leadership (graceful shutdown)"""
        if not self.is_leader:
            return
        
        if not self.client:
            return
        
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            await self.client.delete(LEADER_KEY)
            print(f"✓ [{HOSTNAME}] Released leadership")
        
        self.is_leader = False
    
    async def close(self):
        """Close Redis connection"""
        if self.client:
            await self.client.aclose()

async def run_with_leader_election(admission_loop_func):
    """
    Run the admission loop with leader election
    Only the leader executes the loop
    """
    leader = LeaderElection()
    
    try:
        while True:
            #Try to become leader
            is_leader = await leader.try_acquire_leadership()
            
            if is_leader:
                #Run one iteration of the admission loop
                try:
                    await admission_loop_func()
                except Exception as e:
                    print(f"✗ [{HOSTNAME}] Error in admission loop: {e}")
                
                #Renew leadership every iteration
                await leader.renew_leadership()
                
                #Sleep based on tick rate
                await asyncio.sleep(1)
            else:
                #Standby mode, wait longer before trying again
                current_leader = await leader.client.get(LEADER_KEY)
                print(f"⏸ [{HOSTNAME}] Standby mode (leader: {current_leader})")
                await asyncio.sleep(5)
    
    except KeyboardInterrupt:
        print(f"\n⚠ [{HOSTNAME}] Shutting down...")
        await leader.release_leadership()
    
    finally:
        await leader.close()