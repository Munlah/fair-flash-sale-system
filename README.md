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