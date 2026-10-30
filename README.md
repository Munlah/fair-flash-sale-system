# Distributed Fair Flash-Sale System (DFFS)

DFFS is a small distributed flash-sale demo. It puts customers into a waiting
room, admits them in fair time windows, reserves stock atomically, processes a
mock payment, and saves the order in PostgreSQL.

The project is designed to demonstrate two things:

1. **Fairness:** users who arrive in the same three-second window are shuffled,
   so a tiny network-speed advantage does not decide the result.
2. **Resilience:** Redis and PostgreSQL hold the important state, services run
   independently, and Kubernetes can restart service pods without stopping the
   whole sale.

## What is in the system?

| Service | Local port | Responsibility |
|---|---:|---|
| Web UI (Compose/static server) | 5500 | Dashboard and manual demo controls |
| Gateway | 8005 in Compose, 8000 in Kubernetes port-forward | Public API and purchase orchestration |
| Inventory | 8001 | Atomic reservations and stock |
| Checkout | 8002 | PostgreSQL order storage and idempotency |
| Payment gateway | 8003 | Mock payment provider with configurable failures |
| Admission controller | 8004 | Queue admission and leader election |
| Redis | 6379 | Queue, token, reservation, and leader state |
| PostgreSQL | 5432 | Durable order data |
| Locust web UI | 8089 | Load-test controls and live charts |

## Requirements

- Docker Desktop with Docker Compose
- For Kubernetes: `kubectl` and Minikube
- Python 3.11 or newer for Locust and the experiment runner
- PowerShell for the comprehensive Kubernetes test

## Fastest way to start on Windows

From the repository folder, run:

```powershell
.\start-demo.bat
```

The script:

1. Starts the Docker Compose services.
2. Rebuilds the application containers.
3. Sets the demo phone stock to 100.
4. Opens the dashboard at <http://localhost:5500>.
5. Prints the commands used to start Locust and the test suite.

The script does not delete Docker volumes. Existing orders and Redis data are
therefore kept between starts.

To stop the local demo:

```powershell
docker compose down
```

To stop it and remove the local PostgreSQL volume as well:

```powershell
docker compose down -v
```

That last command deletes local database data. It is not normally needed.

## Using the dashboard

Open <http://localhost:5500>.

The dashboard shows:

- Available stock and waiting queue length.
- Real order count from PostgreSQL.
- Payment success rate.
- Health of gateway, inventory, checkout, payment, and admission services.
- Token status as it changes from waiting to admitted.
- Successful and failed purchase messages.
- Service recovery messages when a container or pod is restarted.

The local dashboard talks to the Compose gateway on port 8005. The JavaScript
also tries port 8000, which is useful when the gateway is exposed with a
Kubernetes port-forward.

## Build Docker images manually

Build all application images:

```powershell
docker compose build
```

Start or recreate all services:

```powershell
docker compose up -d
```

Follow logs for one service:

```powershell
docker compose logs -f gateway-service
```

Check container status:

```powershell
docker compose ps
```

The Compose gateway is intentionally mapped to port 8005. This avoids
conflicting with the Kubernetes gateway port-forward on port 8000.

## Deploy to Kubernetes with Minikube

Start Minikube and select its Docker daemon:

```powershell
minikube start
minikube docker-env --shell powershell | Invoke-Expression
```

Build the images inside the Minikube Docker environment:

```powershell
docker build -t dffs-gateway:latest .\services\gateway-service
docker build -t dffs-admission:latest .\services\admission-service
docker build -t dffs-inventory:latest .\services\inventory-service
docker build -t dffs-checkout:latest .\services\checkout-service
docker build -t dffs-payment:latest .\services\payment-gateway
```

Apply the namespace and services:

```powershell
kubectl apply -f .\kubernetes\
kubectl rollout status deployment/gateway -n dffs
kubectl get pods -n dffs
```

The helper script performs the same setup:

```powershell
.\deploy-kubernetes.bat
```

Expose the Kubernetes gateway:

```powershell
kubectl port-forward -n dffs svc/gateway 8000:8000
```

The frontend is a static folder and is not deployed by the Kubernetes
manifests. Start it in a second terminal from the repository root:

```powershell
python -m http.server 5500 --directory frontend
```

Then visit <http://localhost:5500>. The frontend will use the gateway
port-forward on port 8000. In another terminal, initialize stock:

```powershell
Invoke-RestMethod -Method POST `
  -Uri "http://localhost:8000/stock/phone/100"
```

If a port-forward is already running, stop that terminal process before
starting another one on the same local port.

## Running Locust

Locust is the load-testing tool. It creates simulated users in three groups:

- `low`: 10 ms artificial network delay.
- `medium`: 100 ms delay.
- `high`: 300 ms delay.

The test measures whether slower users are treated fairly by the admission
algorithm. It is not a unit test and it may create real orders.

Install the Python dependencies once:

```powershell
python -m pip install -r .\locust\requirements.txt
python -m pip install -r .\experiments\requirements.txt
```

Start the Locust web interface against the local Compose gateway:

```powershell
locust -f .\locust\latency_groups.py --host http://localhost:8005
```

Open <http://localhost:8089>. Enter a user count and spawn rate, then start
the test. The live charts show request counts, failures, and response times.
The CSV and HTML reports are created only when `--csv` or `--html` is passed.

For a headless run:

```powershell
locust -f .\locust\latency_groups.py `
  --host http://localhost:8005 `
  --users 50 --spawn-rate 10 --run-time 60s --headless
```

## Running the experiment runner

The runner resets the demo state, runs Locust, checks database/inventory
integrity, and repeats the load test for each admission mode:

- `windowed`: the fair three-second cohort algorithm.
- `fifo`: oldest token first.
- `random`: random waiting-token selection.
- `none`: admits everyone without rate limiting.

Run all modes:

```powershell
python .\experiments\runner.py
```

Run selected modes with a shorter test:

```powershell
python .\experiments\runner.py --modes windowed fifo --users 30 --spawn-rate 10 --run-time 30s
```

Results are written to `experiments/results/`:

- `results_*.csv`: one row per admission mode.
- `locust_*_stats.csv`: Locust request statistics.
- `locust_*.html`: optional Locust report.
- `fairness_comparison_*.png`: Jain fairness index chart.
- `low_success_rate_*.png`: low-latency group chart.

The **Jain fairness index** is closer to 1 when success is distributed more
evenly between the latency groups. `oversold_units` and
`duplicate_orders` should both remain zero.

## Running the comprehensive Kubernetes test

This is the 24-check operational test suite. It checks infrastructure,
service health, queue admission, purchase persistence, idempotency, payment
behavior, pod recovery, Redis persistence, reservation release, concurrent
users, Redis leader election, and database consistency.

The normal demo uses a 30% simulated payment failure rate. The comprehensive
test suite changes the Kubernetes payment deployment to 20% at startup so the
payment checks remain realistic without making the suite unnecessarily flaky.
You can override the test rate with a decimal value between 0 and 1:

```powershell
.\experiments\comprehensive-test.ps1 -PaymentFailureRate 0.2
```

Run it from PowerShell after deploying Kubernetes:

```powershell
.\experiments\comprehensive-test.ps1
```

The Locust experiment runner also uses a 20% payment failure rate by default.
Use `--payment-failure-rate` to override it. The Compose demo remains at 30%
unless `PAYMENT_FAILURE_RATE` is explicitly set.

It is a smoke and resilience suite, not a full production test. A passing
run means the main demo paths work in the current environment; it does not
prove PostgreSQL failover, network partitions, or long-term payment
reconciliation.

## Admission modes

The admission service supports four modes:

```powershell
$env:ADMISSION_MODE = "fifo"
docker compose up -d --build admission-service
```

The current mode is available at <http://localhost:8004/mode> in Compose.
The default mode is `windowed`.

## Useful commands

```powershell
# Set stock
Invoke-RestMethod -Method POST -Uri "http://localhost:8001/stock/phone/100"

# Check service health
Invoke-RestMethod -Uri "http://localhost:8005/health"

# View Kubernetes pods
kubectl get pods -n dffs

# View gateway logs in Kubernetes
kubectl logs -n dffs deploy/gateway --tail=100
```
