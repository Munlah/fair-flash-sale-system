# Flash Sale Demo Script

## Setup (Before Demo)

```bash
#start all services
docker compose up -d

#open logs in separate terminal
docker compose logs -f admission-service

#open demo UI
start http://localhost:5500/

#set stock
curl -X POST http://localhost:8001/stock/phone/50