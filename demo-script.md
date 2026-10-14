# Flash Sale Demo Script

## Setup (Before Demo)

```bash
# Start all services
docker compose up -d

# Open logs in separate terminal
docker compose logs -f admission-service

# Open demo UI
start http://localhost:5500/

# Set stock
curl -X POST http://localhost:8001/stock/phone/50