"""Run admission-mode experiments and calculate latency-group fairness.

Prerequisites:
    pip install -r locust/requirements.txt
    pip install -r experiments/requirements.txt

Run:
    python experiments/runner.py
"""

import asyncio
import csv
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import asyncpg
import httpx
import pandas as pd
import redis


GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8001")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://flash_user:flash_pass@localhost:5432/flash_db",
)
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
INITIAL_STOCK = int(os.getenv("INITIAL_STOCK", "100"))
TEST_DURATION = os.getenv("TEST_DURATION", "60s")
TOTAL_USERS = int(os.getenv("TOTAL_USERS", "300"))
SPAWN_RATE = int(os.getenv("SPAWN_RATE", "50"))
MODES = ("windowed", "fifo", "random", "none")
OUTPUT_DIR = Path(os.getenv("EXPERIMENT_OUTPUT_DIR", "experiments/results"))


class ExperimentRunner:
    def __init__(self):
        self.redis_client = redis.Redis(host=REDIS_HOST, decode_responses=True)
        self.results = []
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async def reset_system(self):
        self.redis_client.flushall()
        conn = await asyncpg.connect(POSTGRES_DSN)
        try:
            await conn.execute("DELETE FROM orders")
        finally:
            await conn.close()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{INVENTORY_URL}/stock/phone/{INITIAL_STOCK}"
            )
            response.raise_for_status()

    def restart_admission_service(self, mode):
        subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate", "admission-service"],
            check=True,
            env={**os.environ, "ADMISSION_MODE": mode},
        )
        time.sleep(5)

    def run_locust(self, mode):
        csv_prefix = OUTPUT_DIR / f"locust_{mode}_{self.timestamp}"
        command = [
            "locust",
            "-f",
            "locust/latency_groups.py",
            "--host",
            GATEWAY_URL,
            "--users",
            str(TOTAL_USERS),
            "--spawn-rate",
            str(SPAWN_RATE),
            "--run-time",
            TEST_DURATION,
            "--headless",
            "--csv",
            str(csv_prefix),
            "--html",
            str(csv_prefix.with_suffix(".html")),
        ]
        subprocess.run(command, check=True)
        return self.parse_locust_stats(csv_prefix.with_name(csv_prefix.name + "_stats.csv"), mode)

    @staticmethod
    def parse_locust_stats(stats_path, mode):
        metrics = {"mode": mode, "groups": {}}
        with stats_path.open(newline="", encoding="utf-8") as stats_file:
            for row in csv.DictReader(stats_file):
                name = row["Name"]
                if not name.startswith("purchase_"):
                    continue
                group = name.removeprefix("purchase_")
                requests = int(row["Request Count"])
                failures = int(row["Failure Count"])
                metrics["groups"][group] = {
                    "attempts": requests,
                    "success": requests - failures,
                    "latency_p50": float(row["50%"]),
                    "latency_p95": float(row["95%"]),
                }
        for group in ("low", "medium", "high"):
            metrics["groups"].setdefault(
                group,
                {"attempts": 0, "success": 0, "latency_p50": 0, "latency_p95": 0},
            )
        return metrics

    async def check_integrity(self):
        async with httpx.AsyncClient() as client:
            stock_response = await client.get(f"{INVENTORY_URL}/stock/phone")
            stock_response.raise_for_status()
            stock = stock_response.json().get("stock", 0)
        conn = await asyncpg.connect(POSTGRES_DSN)
        try:
            order_count = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE product_id = 'phone'"
            )
            duplicate_count = await conn.fetchval(
                "SELECT COUNT(*) - COUNT(DISTINCT idempotency_key) FROM orders"
            )
        finally:
            await conn.close()
        sold = INITIAL_STOCK - stock
        return max(order_count - sold, 0), duplicate_count

    @staticmethod
    def jain_index(values):
        if not values or sum(value * value for value in values) == 0:
            return 0.0
        return sum(values) ** 2 / (len(values) * sum(value * value for value in values))

    async def run_mode(self, mode):
        await self.reset_system()
        self.restart_admission_service(mode)
        metrics = self.run_locust(mode)
        rates = [
            group_data["success"] / group_data["attempts"]
            if group_data["attempts"] else 0.0
            for group_data in (metrics["groups"][group] for group in ("low", "medium", "high"))
        ]
        oversold, duplicates = await self.check_integrity()
        self.results.append({
            "mode": mode,
            "timestamp": datetime.now().isoformat(),
            "low_success_rate": rates[0],
            "medium_success_rate": rates[1],
            "high_success_rate": rates[2],
            "jain_fairness_index": self.jain_index(rates),
            "total_success": sum(data["success"] for data in metrics["groups"].values()),
            "oversold_units": oversold,
            "duplicate_orders": duplicates,
            "queue_remaining": self.redis_client.zcard("queue:phone"),
            "low_p50_ms": metrics["groups"]["low"]["latency_p50"],
            "medium_p50_ms": metrics["groups"]["medium"]["latency_p50"],
            "high_p50_ms": metrics["groups"]["high"]["latency_p50"],
            "low_p95_ms": metrics["groups"]["low"]["latency_p95"],
            "medium_p95_ms": metrics["groups"]["medium"]["latency_p95"],
            "high_p95_ms": metrics["groups"]["high"]["latency_p95"],
        })

    def save_results(self):
        output = OUTPUT_DIR / f"results_{self.timestamp}.csv"
        pd.DataFrame(self.results).to_csv(output, index=False)
        return output

    def generate_graphs(self):
        if not self.results:
            return
        import matplotlib.pyplot as plt
        import seaborn as sns

        dataframe = pd.DataFrame(self.results)
        sns.set_theme(style="whitegrid")
        for column, title, filename in (
            (
                "jain_fairness_index",
                "Fairness by Admission Strategy",
                "fairness_comparison",
            ),
            (
                "low_success_rate",
                "Low-Latency Success Rate",
                "low_success_rate",
            ),
        ):
            ax = sns.barplot(data=dataframe, x="mode", y=column)
            ax.set_title(title)
            figure = ax.get_figure()
            figure.tight_layout()
            figure.savefig(OUTPUT_DIR / f"{filename}_{self.timestamp}.png", dpi=200)
            plt.close(figure)

    async def run(self):
        for mode in MODES:
            await self.run_mode(mode)
            await asyncio.sleep(5)
        print(f"Results: {self.save_results()}")
        self.generate_graphs()


if __name__ == "__main__":
    asyncio.run(ExperimentRunner().run())
