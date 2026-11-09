#!/usr/bin/env python3
"""We're testing cluster recovery and inventory safety when pods get killed mid-sale. Quick heads up: run this against a proper NodePort or Service URL. If you use kubectl port-forward, you'll just end up tunneling to one specific pod, which misses the whole point of testing pod restarts."""

import argparse
import csv
import json
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

NAMESPACE = "dffs"
PRODUCT = "phone"
SCENARIOS = {
    "gateway": "gateway",
    "inventory": "inventory",
    "checkout": "checkout",
    "payment": "payment-gateway",
    "admission-leader": None,
}


def kubectl(*args, check=True):
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def redis_cli(*args):
    return kubectl(
        "exec", "-n", NAMESPACE, "deploy/redis", "--", "redis-cli", *args
    )


def get_pods(app_label):
    data = json.loads(
        kubectl(
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            f"app={app_label}",
            "-o",
            "json",
        )
    )
    pods = []
    for pod in data.get("items", []):
        statuses = pod.get("status", {}).get("containerStatuses", []) or []
        pods.append(
            {
                "name": pod["metadata"]["name"],
                "ready": bool(statuses) and all(
                    status.get("ready") for status in statuses
                ),
                "terminating": bool(pod["metadata"].get("deletionTimestamp")),
            }
        )
    return pods


def current_leader():
    value = redis_cli("GET", "admission:leader")
    return value if value and value != "(nil)" else ""


def snapshot(gateway_url, retries=30):
    for _ in range(retries):
        try:
            response = requests.get(
                f"{gateway_url}/dashboard/snapshot",
                params={"product_id": PRODUCT},
                timeout=5,
            )
            response.raise_for_status()
            snapshot_data = response.json()
            stock_data = snapshot_data.get("stock") or {}
            order_data = snapshot_data.get("orders") or {}
            if "stock" in stock_data and "total_orders" in order_data:
                return int(stock_data["stock"]), int(order_data["total_orders"])
        except (requests.RequestException, ValueError, TypeError):
            pass
        time.sleep(1)
    raise RuntimeError("could not read stock/orders from /dashboard/snapshot")


def held_units():
    return int(redis_cli("ZCARD", "reservation_expirations") or 0)


def classify(response_or_error, purchase=False):
    if isinstance(response_or_error, Exception):
        return "error"
    if response_or_error.status_code >= 500:
        return "error"
    if response_or_error.status_code >= 400:
        return "client"
    if purchase:
        try:
            return "ok" if response_or_error.json().get("success") else "business"
        except (ValueError, TypeError):
            return "error"
    return "ok"


class Load:
    def __init__(self, gateway_url, users, run_id):
        self.gateway_url = gateway_url
        self.users = users
        self.run_id = run_id
        self.events = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = []

    def record(self, operation, outcome):
        with self.lock:
            self.events.append((time.time(), operation, outcome))

    def worker(self, worker_id):
        attempt = 0
        while not self.stop.is_set():
            attempt += 1
            user = f"{self.run_id}-w{worker_id}-{attempt}"
            try:
                response = requests.post(
                    f"{self.gateway_url}/queue/join",
                    params={"user_id": user, "product_id": PRODUCT},
                    timeout=5,
                )
            except requests.RequestException as error:
                self.record("join", classify(error))
                time.sleep(0.5)
                continue
            self.record("join", classify(response))
            if response.status_code != 200:
                time.sleep(0.5)
                continue

            token = response.json().get("token")
            admitted = False
            for _ in range(80):
                if self.stop.is_set():
                    return
                try:
                    status = requests.get(
                        f"{self.gateway_url}/queue/status/{token}", timeout=5
                    )
                    self.record("status", classify(status))
                    if status.status_code == 200 and status.json().get("status") in {
                        "admitted",
                        "completed",
                    }:
                        admitted = True
                        break
                except (requests.RequestException, ValueError):
                    self.record("status", "error")
                time.sleep(0.5)
            if not admitted:
                continue

            try:
                purchase = requests.post(
                    f"{self.gateway_url}/purchase",
                    params={"token": token},
                    timeout=15,
                )
                self.record("purchase", classify(purchase, purchase=True))
            except requests.RequestException as error:
                self.record("purchase", classify(error))

    def start(self):
        for worker_id in range(self.users):
            thread = threading.Thread(
                target=self.worker, args=(worker_id,), daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def finish(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=20)


def run_once(gateway_url, scenario, args, run_number):
    run_id = f"fi{int(time.time())}{run_number}"
    stock_response = requests.post(
        f"{gateway_url}/stock/{PRODUCT}/{args.stock}", timeout=5
    )
    stock_response.raise_for_status()
    redis_cli("DEL", f"queue:{PRODUCT}")
    time.sleep(2)
    stock_start, orders_start = snapshot(gateway_url)
    held_start = held_units()

    load = Load(gateway_url, args.users, run_id)
    load.start()
    time.sleep(args.warmup)

    if scenario == "admission-leader":
        victim = current_leader()
        if not victim:
            load.finish()
            raise RuntimeError("no admission leader found")
        app_label = "admission"
        desired = len([pod for pod in get_pods(app_label) if pod["ready"]])
    else:
        app_label = SCENARIOS[scenario]
        ready = [
            pod
            for pod in get_pods(app_label)
            if pod["ready"] and not pod["terminating"]
        ]
        if not ready:
            load.finish()
            raise RuntimeError(f"no ready pod for {app_label}")
        victim = ready[0]["name"]
        desired = len(ready)

    killed_at = time.time()
    kubectl("delete", "pod", victim, "-n", NAMESPACE, "--wait=false")

    recovery = None
    while time.time() - killed_at < args.recovery_timeout:
        time.sleep(0.5)
        if scenario == "admission-leader":
            leader = current_leader()
            if leader and leader != victim:
                recovery = time.time() - killed_at
                break
        else:
            pods = get_pods(app_label)
            ready_now = [
                pod
                for pod in pods
                if pod["ready"]
                and not pod["terminating"]
                and pod["name"] != victim
            ]
            if len(ready_now) >= desired:
                recovery = time.time() - killed_at
                break

    recovered_at = killed_at + (
        recovery if recovery is not None else args.recovery_timeout
    )
    time.sleep(max(0, args.window - (time.time() - killed_at)))
    load.finish()
    time.sleep(args.settle)

    stock_end, orders_end = snapshot(gateway_url)
    held_end = held_units()
    new_orders = orders_end - orders_start
    invariant = stock_start + held_start == stock_end + held_end + new_orders
    window_events = [
        event
        for event in load.events
        if killed_at <= event[0] <= recovered_at + 5
    ]

    return {
        "scenario": scenario,
        "run": run_number,
        "victim": victim,
        "recovery_s": round(recovery, 1) if recovery is not None else "",
        "requests_in_window": len(window_events),
        "system_errors": sum(event[2] == "error" for event in window_events),
        "purchases_ok_total": sum(
            event[1] == "purchase" and event[2] == "ok" for event in load.events
        ),
        "new_orders": new_orders,
        "stock_start": stock_start,
        "stock_end": stock_end,
        "held_start": held_start,
        "held_end": held_end,
        "invariant_holds": invariant,
        "negative_stock": stock_end < 0,
    }


def summarise(rows):
    output = [
        "| Failure | Runs | Recovery (s) mean +/- sd | System errors (mean) | Invariant held |",
        "|---------|------:|---------:|---------:|---------:|---------:|",
    ]
    for scenario in dict.fromkeys(row["scenario"] for row in rows):
        scenario_rows = [row for row in rows if row["scenario"] == scenario]
        recoveries = [
            row["recovery_s"]
            for row in scenario_rows
            if row["recovery_s"] != ""
        ]
        deviation = statistics.stdev(recoveries) if len(recoveries) > 1 else 0.0
        recovery_text = (
            f"{statistics.mean(recoveries):.1f} +/- {deviation:.1f}"
            if recoveries
            else "no recovery"
        )
        invariant_count = sum(row["invariant_holds"] for row in scenario_rows)
        output.append(
            f"| {scenario} | {len(scenario_rows)} | {recovery_text} | "
            f"{statistics.mean(row['system_errors'] for row in scenario_rows):.1f} | "
            f"{invariant_count}/{len(scenario_rows)} |"
        )
    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument(
        "--scenarios",
        default="gateway,inventory,checkout,admission-leader",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--stock", type=int, default=2000)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--window", type=float, default=45)
    parser.add_argument("--settle", type=float, default=20)
    parser.add_argument("--recovery-timeout", type=float, default=120)
    parser.add_argument("--output-dir", default="experiments/results")
    args = parser.parse_args()

    gateway_url = args.gateway_url.rstrip("/")
    scenarios = [item.strip() for item in args.scenarios.split(",") if item.strip()]
    unknown = [scenario for scenario in scenarios if scenario not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    rows = []
    for scenario in scenarios:
        for run_number in range(1, args.repeats + 1):
            print(
                f"[{scenario}] run {run_number}/{args.repeats} ...",
                flush=True,
            )
            try:
                row = run_once(gateway_url, scenario, args, run_number)
            except Exception as error:
                print(f"  FAILED: {error}", flush=True)
                continue
            rows.append(row)
            print(
                f"  recovery={row['recovery_s']}s "
                f"errors={row['system_errors']} "
                f"invariant={row['invariant_holds']}",
                flush=True,
            )
            time.sleep(10)

    if not rows:
        raise SystemExit("no successful runs")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"failure_injection_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    summary = summarise(rows)
    summary_path = output_dir / f"failure_injection_{stamp}_summary.md"
    summary_path.write_text(summary + "\n", encoding="utf-8")
    print(f"\n{summary}\n\nCSV: {csv_path}\nSummary: {summary_path}")


if __name__ == "__main__":
    main()
