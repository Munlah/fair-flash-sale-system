"""Latency-group load test for the fair flash-sale gateway.

Run with:
    locust -f locust/latency_groups.py --host http://localhost:8000
"""

import random
import time

from locust import HttpUser, between, task


class LatencyGroupUser(HttpUser):
    """Base flow shared by users with different simulated network delays."""

    abstract = True
    wait_time = between(0.5, 2)
    network_delay = 0.0
    group = "unknown"

    def on_start(self):
        self.user_id = f"{self.group}-{random.randint(100000, 999999)}"
        self.token = None
        self.join_time = None
        time.sleep(self.network_delay)

    @task
    def full_purchase_flow(self):
        if self.token:
            return

        self.join_time = time.monotonic()
        time.sleep(self.network_delay)
        with self.client.post(
            "/queue/join",
            params={"user_id": self.user_id, "product_id": "phone"},
            name=f"join_queue_{self.group}",
            catch_response=True,
        ) as response:
            if response.status_code == 410:
                response.failure("sold out")
                return
            if response.status_code != 200:
                response.failure(f"join failed: {response.status_code}")
                return
            try:
                self.token = response.json().get("token")
            except ValueError:
                response.failure("invalid join response")
                return
            if not self.token:
                response.failure("missing token")
                return
            response.success()

        for _ in range(30):
            time.sleep(1 + self.network_delay)
            with self.client.get(
                f"/queue/status/{self.token}",
                name=f"poll_status_{self.group}",
                catch_response=True,
            ) as response:
                if response.status_code == 404:
                    response.success()
                    self.token = None
                    return
                if response.status_code != 200:
                    response.failure(f"poll failed: {response.status_code}")
                    return
                try:
                    status = response.json().get("status")
                except ValueError:
                    response.failure("invalid status response")
                    return
                response.success()
                if status == "admitted":
                    break
        else:
            return

        time.sleep(self.network_delay)
        with self.client.post(
            "/purchase",
            params={"token": self.token},
            name=f"purchase_{self.group}",
            catch_response=True,
        ) as response:
            elapsed_ms = int((time.monotonic() - self.join_time) * 1000)
            if response.status_code != 200:
                response.failure(f"purchase failed: {response.status_code}")
                return
            try:
                result = response.json()
            except ValueError:
                response.failure("invalid purchase response")
                return
            if result.get("success"):
                response.success()
                response.request_meta["response_time"] = elapsed_ms
            else:
                reason = str(result.get("reason", "purchase failed"))
                normalized_reason = reason.lower().replace("_", " ")
                if "sold out" in normalized_reason:
                    response.success()
                else:
                    response.failure(reason)


class LowLatencyUser(LatencyGroupUser):
    network_delay = 0.01
    group = "low"


class MediumLatencyUser(LatencyGroupUser):
    network_delay = 0.1
    group = "medium"


class HighLatencyUser(LatencyGroupUser):
    network_delay = 0.3
    group = "high"
