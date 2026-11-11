@echo off
setlocal

echo.
echo ==========================================
echo   DFFS local demo startup
echo ==========================================
echo.

docker compose up -d --build
if errorlevel 1 (
    echo Failed to build or start Docker Compose.
    exit /b 1
)

echo Waiting for the inventory service...
timeout /t 5 /nobreak >nul
curl.exe -s -X POST http://localhost:8001/stock/phone/100
echo.

echo.
echo Dashboard: http://localhost:5500
echo Gateway:   http://localhost:8005
echo Locust UI: http://localhost:8089
echo.
echo To start Locust:
echo   locust -f .\locust\latency_groups.py --host http://localhost:8005
echo.
echo To run the comprehensive Kubernetes suite:
echo   powershell -ExecutionPolicy Bypass -File .\experiments\comprehensive-test.ps1
echo.
echo Opening the dashboard...
start "" http://localhost:5500

endlocal
