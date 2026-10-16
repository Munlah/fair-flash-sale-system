# Distributed Fair Flash-Sale System (DFFS)

A fair, distributed flash sale system implementing **windowed-random admission control** to eliminate timing advantages and ensure equitable access during high-demand sales.

![Demo UI](docs/demo-screenshot.png)

## 🎯 Project Overview

This system solves the unfairness problem in traditional flash sales where users with faster networks or automated bots have disproportionate advantages. Our solution groups users into time-based cohorts and randomizes admission within each cohort while maintaining overall FIFO fairness across cohorts.

**Key Innovation**: Windowed-Random Admission Algorithm
- Groups users into 3-second windows based on arrival time
- Randomizes admission order within each window (eliminates sub-second timing advantages)
- Processes older windows first (maintains fairness for those who waited longer)
- Adapts admission rate based on available inventory

## 🏗️ Architecture

### Services

| Service | Port | Purpose |
|---------|------|---------|
| **Gateway** | 8000 | Entry point, manages waiting room, orchestrates purchases |
| **Admission Controller** | 8004 | Implements windowed-random admission algorithm |
| **Inventory** | 8001 | Atomic stock reservations using Redis Lua scripts |
| **Checkout** | 8002 | Durable order persistence with idempotency guarantees |

### Technology Stack

- **Backend**: Python 3.11, FastAPI, Uvicorn
- **State Management**: Redis 7 (queues, reservations, idempotency cache)
- **Persistence**: PostgreSQL 16 (orders)
- **Orchestration**: Docker Compose (local), Kubernetes (production)
- **Load Testing**: Locust

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.11+ (for load testing)

### Run Locally

```bash
# Clone the repository
git clone https://github.com/munlah/dffs.git
cd dffs

# Start all services
docker compose up -d

# Initialize stock
curl -X POST http://localhost:8001/stock/phone/100

# Open the demo UI
# Visit: http://localhost:5500/
```

# Admission strategies and fairness experiments

The admission controller supports four strategies, selected with
`ADMISSION_MODE`:

- `windowed` (default): deterministic random selection from closed time windows.
- `fifo`: admits the oldest waiting tokens first.
- `random`: randomly selects waiting tokens.
- `none`: admits all waiting tokens without rate limiting.

For Docker Compose, select a mode when starting the admission service:

```powershell
$env:ADMISSION_MODE = "fifo"
docker compose up -d --build admission-service
```

The active mode is available at `http://localhost:8004/mode`.

The latency-group load test and experiment runner are under [`locust/`](locust/)
and [`experiments/`](experiments/). Install their dependencies and run the
four-mode experiment with:

```powershell
pip install -r locust/requirements.txt
pip install -r experiments/requirements.txt
python experiments/runner.py
```

Experiment CSV files and generated graphs are written to
`experiments/results/`, which is intentionally ignored by Git.