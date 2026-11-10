# Flash Sale Kubernetes Demo Script

## Setup (Before Demo)

Run these commands from PowerShell in the repository root.

### Terminal 1: deploy and expose the Kubernetes gateway

```powershell
.\deploy-kubernetes.bat
kubectl port-forward -n dffs svc/gateway 8000:8000
```

Leave the port-forward running.

### Terminal 2: start the dashboard

```powershell
python -m http.server 5501 --directory frontend
Start-Process "http://localhost:5501"
```

### Terminal 3: initialize stock

```powershell
$gateway = "http://localhost:8000"

Invoke-RestMethod -Method POST `
  -Uri "$gateway/stock/phone/100"

Invoke-RestMethod "$gateway/health"
```
