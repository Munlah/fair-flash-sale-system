"""Leader Election using Redis, Only one admission controller instance runs the admission loop at a time"""
import asyncio
import os
import socket
import time
import redis.asyncio as redis

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379
LEADER_KEY = "admission:leader"
LEADER_TTL = 10  #leader lock expires after 10 seconds
HOSTNAME = socket.gethostname()  #unique pod identifier

class LeaderElection:
    def __init__(self):
        self.client = None
        self.is_leader = False
    
    async def connect(self):
        """connect to Redis"""
        self.client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True
        )
    
    async def try_acquire_leadership(self) -> bool:
        """try to become the leader"""
        if not self.client:
            await self.connect()
        
        #try to set the leader key with NX (only if it doesn't exist)
        acquired = await self.client.set(
            LEADER_KEY,
            HOSTNAME,
            ex=LEADER_TTL,
            nx=True
        )
        
        if acquired:
            self.is_leader = True
            print(f"[{HOSTNAME}] Became leader")
            return True
        
        #check if we're still the leader (in case of renewal)
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            #renew our leadership
            await self.client.expire(LEADER_KEY, LEADER_TTL)
            self.is_leader = True
            return True
        
        self.is_leader = False
        return False
    
    async def renew_leadership(self):
        """renew leadership lock"""
        if not self.is_leader:
            return
        
        if not self.client:
            await self.connect()
        
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            await self.client.expire(LEADER_KEY, LEADER_TTL)
            print(f"[{HOSTNAME}] Renewed leadership")
        else:
            self.is_leader = False
            print(f"[{HOSTNAME}] Lost leadership")
    
    async def release_leadership(self):
        """release leadership (graceful shutdown)"""
        if not self.is_leader:
            return
        
        if not self.client:
            return
        
        current_leader = await self.client.get(LEADER_KEY)
        if current_leader == HOSTNAME:
            await self.client.delete(LEADER_KEY)
            print(f"[{HOSTNAME}] Released leadership")
        
        self.is_leader = False
    
    async def close(self):
        """close Redis connection"""
        if self.client:
            await self.client.aclose()

async def run_with_leader_election(admission_loop_func):
    """run the admission loop with leader election, only the leader executes the loop"""
    leader = LeaderElection()
    
    try:
        while True:
            #try to become leader
            is_leader = await leader.try_acquire_leadership()
            
            if is_leader:
                #run one iteration of the admission loop
                try:
                    await admission_loop_func()
                except Exception as e:
                    print(f"[{HOSTNAME}] Error in admission loop: {e}")
                
                #renew leadership every iteration
                await leader.renew_leadership()
                
                #sleep based on tick rate
                await asyncio.sleep(1)
            else:
                #standby mode, wait longer before trying again
                current_leader = await leader.client.get(LEADER_KEY)
                print(f"⏸ [{HOSTNAME}] Standby mode (leader: {current_leader})")
                await asyncio.sleep(5)
    
    except KeyboardInterrupt:
        print(f"\n [{HOSTNAME}] Shutting down...")
        await leader.release_leadership()
    
    finally:
        await leader.close()