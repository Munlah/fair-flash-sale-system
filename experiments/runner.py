"""Compare admission modes with the latency-group Locust test.
Each run resets the demo data, runs Locust, checks that the database and
inventory still agree, and writes CSV/graph files to experiments/results.
This is an experiment script, not a unit-test runner.
"""

import asyncio
import argparse
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8005")
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
PAYMENT_FAILURE_RATE = os.getenv("PAYMENT_FAILURE_RATE", "0.2")
MODES = ("windowed", "fifo", "random", "none")
OUTPUT_DIR = Path(
    os.getenv("EXPERIMENT_OUTPUT_DIR", str(PROJECT_ROOT / "experiments" / "results"))
)


class ExperimentRunner:
    def __init__(self, modes=None, users=None, spawn_rate=None, run_time=None,
                 locust_file=None, payment_failure_rate=None):
        self.redis_client = redis.Redis(host=REDIS_HOST, decode_responses=True)
        self.results = []
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.modes = tuple(modes or MODES)
        self.users = users or TOTAL_USERS
        self.spawn_rate = spawn_rate or SPAWN_RATE
        self.run_time = run_time or TEST_DURATION
        self.payment_failure_rate = payment_failure_rate or PAYMENT_FAILURE_RATE
        self.locust_file = Path(locust_file or PROJECT_ROOT / "locust" / "latency_groups.py")
        if not self.locust_file.is_absolute():
            self.locust_file = PROJECT_ROOT / self.locust_file
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async def reset_system(self):
        """Clear test state and put a known amount of stock back in Redis."""
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
        """Restart only the admission service so its mode setting is applied."""
        subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate", "admission-service"],
            check=True,
            env={**os.environ, "ADMISSION_MODE": mode},
            cwd=PROJECT_ROOT,
        )
        time.sleep(5)

    def restart_payment_service(self):
        """Use a lower failure rate for repeatable automated experiments."""
        subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate", "payment-gateway"],
            check=True,
            env={**os.environ, "PAYMENT_FAILURE_RATE": self.payment_failure_rate},
            cwd=PROJECT_ROOT,
        )
        time.sleep(5)

    def run_locust(self, mode):
        """Run the selected Locust scenario and read its CSV summary."""
        csv_prefix = OUTPUT_DIR / f"locust_{mode}_{self.timestamp}"
        command = [
            "locust",
            "-f",
            str(self.locust_file),
            "--host",
            GATEWAY_URL,
            "--users",
            str(self.users),
            "--spawn-rate",
            str(self.spawn_rate),
            "--run-time",
            self.run_time,
            "--headless",
            "--csv",
            str(csv_prefix),
            "--html",
            str(csv_prefix.with_suffix(".html")),
        ]
        subprocess.run(command, check=True, cwd=PROJECT_ROOT)
        return self.parse_locust_stats(csv_prefix.with_name(csv_prefix.name + "_stats.csv"), mode)

    @staticmethod
    def parse_locust_stats(stats_path, mode):
        """Convert Locust's group rows into the metrics used by the report."""
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
        """Check that stock, orders, and idempotency keys remain consistent."""
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
        """Return a simple 0..1 fairness score for the latency groups."""
        if not values or sum(value * value for value in values) == 0:
            return 0.0
        return sum(values) ** 2 / (len(values) * sum(value * value for value in values))

    async def run_mode(self, mode):
        """Run one admission mode from reset through integrity verification."""
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
        """Save one compact CSV row for every mode."""
        output = OUTPUT_DIR / f"results_{self.timestamp}.csv"
        pd.DataFrame(self.results).to_csv(output, index=False)
        return output

    def generate_graphs(self):
        """Create the two charts used in the report and presentation."""
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
        self.restart_payment_service()
        for mode in self.modes:
            await self.run_mode(mode)
            await asyncio.sleep(5)
        print(f"Results: {self.save_results()}")
        self.generate_graphs()


async def check_prerequisites():
    """Fail early with a useful message when the local services are absent."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{GATEWAY_URL}/health")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Gateway is not reachable at {GATEWAY_URL}. "
            "Start Docker Compose with '.\\start-demo.bat', or set "
            "GATEWAY_URL=http://localhost:8000 for a Kubernetes port-forward."
        ) from exc
    try:
        self_check = redis.Redis(host=REDIS_HOST, decode_responses=True)
        self_check.ping()
    except redis.RedisError as exc:
        raise RuntimeError(f"Redis is not reachable at {REDIS_HOST}:6379") from exc


def parse_args():
    """Read the small set of options useful during a classroom/demo run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--users", type=int, default=None, help="Locust user count")
    parser.add_argument("--spawn-rate", type=int, default=None)
    parser.add_argument("--run-time", default=None, help="Locust duration, e.g. 30s")
    parser.add_argument(
        "--payment-failure-rate",
        default=PAYMENT_FAILURE_RATE,
        help="Payment failure rate for experiments (default: 20%%)",
    )
    parser.add_argument(
        "--locust-file",
        default=str(PROJECT_ROOT / "locust" / "latency_groups.py"),
        help="Locust file to execute",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip gateway and Redis checks",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        arguments = parse_args()
        if not arguments.skip_preflight:
            asyncio.run(check_prerequisites())
        asyncio.run(
            ExperimentRunner(
                modes=arguments.modes,
                users=arguments.users,
                spawn_rate=arguments.spawn_rate,
                run_time=arguments.run_time,
                locust_file=arguments.locust_file,
                payment_failure_rate=arguments.payment_failure_rate,
            ).run()
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Experiment could not start: {exc}") from exc
