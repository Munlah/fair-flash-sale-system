@echo off
setlocal

echo.
echo ==========================================
echo   DFFS Minikube deployment
echo ==========================================
echo.

minikube status >nul 2>&1
if errorlevel 1 minikube start
if errorlevel 1 exit /b 1

for /f "delims=" %%i in ('minikube docker-env --shell cmd') do %%i

docker build -t dffs-gateway:latest .\services\gateway-service
docker build -t dffs-admission:latest .\services\admission-service
docker build -t dffs-inventory:latest .\services\inventory-service
docker build -t dffs-checkout:latest .\services\checkout-service
docker build -t dffs-payment-gateway:latest .\services\payment-gateway
if errorlevel 1 exit /b 1

kubectl apply -f .\kubernetes\
if errorlevel 1 exit /b 1

kubectl rollout status deployment/gateway -n dffs
kubectl get pods -n dffs
echo.
echo Start the Kubernetes gateway and static UI with:
echo   kubectl port-forward -n dffs svc/gateway 8000:8000
echo   python -m http.server 5500 --directory frontend

endlocal
