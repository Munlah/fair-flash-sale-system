"""
Locust Load Test for Fair Flash Sale System
Simulates realistic user behavior:
1. Join the queue
2. Poll status every 500ms until admitted
3. Purchase when admitted
4. Check stock
"""

import random
import time
from locust import HttpUser, task, between, events


class FlashSaleUser(HttpUser):
    """Simulates a user trying to buy in a flash sale"""
    
    #Wait 0.5-2 seconds between actions
    wait_time = between(0.5, 2)
    
    def on_start(self):
        """Called when a user starts - join the queue"""
        self.token = None
        self.user_id = f"user_{random.randint(1000, 9999)}"
        self.admitted = False
        self.purchased = False
        
        #Join the queue immediately
        self.join_queue()
    
    def join_queue(self):
        """Join the waiting room queue"""
        with self.client.post(
            "/queue/join",
            params={"user_id": self.user_id, "product_id": "phone"},
            catch_response=True
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.token = data.get("token")
                self.position = data.get("position", "unknown")
                response.success()
                print(f"✓ {self.user_id} joined queue (position: {self.position})")
            else:
                response.failure(f"Failed to join queue: {response.text}")
    
    @task(10)
    def check_status(self):
        """Poll admission status (most frequent action)"""
        if not self.token or self.purchased:
            return
        
        with self.client.get(
            f"/queue/status/{self.token}",
            catch_response=True,
            name="/queue/status/[token]"
        ) as response:
            if response.status_code == 200:
                data = response.json()
                status = data.get("status")
                
                if status == "admitted" and not self.admitted:
                    self.admitted = True
                    print(f"✓ {self.user_id} ADMITTED!")
                    response.success()
                    #Try to purchase immediately
                    self.purchase()
                elif status == "waiting":
                    response.success()
                else:
                    response.success()
            else:
                response.failure(f"Status check failed: {response.text}")
    
    def purchase(self):
        """Attempt to complete the purchase"""
        if not self.token or self.purchased:
            return
        
        with self.client.post(
            "/purchase",
            params={"token": self.token},
            catch_response=True,
            name="/purchase"
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    self.purchased = True
                    order_id = data.get("order", {}).get("id")
                    print(f"✓ {self.user_id} PURCHASED! Order ID: {order_id}")
                    response.success()
                else:
                    reason = data.get("reason", "unknown")
                    response.failure(f"Purchase failed: {reason}")
            else:
                response.failure(f"Purchase request failed: {response.text}")
    
    @task(1)
    def check_stock(self):
        """Occasionally check remaining stock"""
        with self.client.get(
            "/stock/phone",
            catch_response=True,
            name="/stock/[product]"
        ) as response:
            if response.status_code == 200:
                data = response.json()
                stock = data.get("stock", 0)
                response.success()
            else:
                response.failure(f"Stock check failed: {response.text}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Called when the load test starts"""
    print("\n" + "="*50)
    print("LOAD TEST STARTING")
    print("="*50)
    print(f"Target: {environment.host}")
    print(f"Users: {environment.runner.target_user_count if hasattr(environment.runner, 'target_user_count') else 'N/A'}")
    print("="*50 + "\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Called when the load test stops"""
    print("\n" + "="*50)
    print("LOAD TEST COMPLETE")
    print("="*50)
    print("Check the Locust web UI for detailed results")
    print("="*50 + "\n")