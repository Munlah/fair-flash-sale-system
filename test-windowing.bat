@echo off
echo ========================================
echo Testing Windowed-Random Admission
echo ========================================
echo.

REM Reset stock
echo [1/4] Resetting stock to 50...
curl -s -X POST http://localhost:8001/stock/phone/50
echo.
echo.

REM Join Wave 1 (Window 1) - 5 users in quick succession
echo [2/4] Wave 1: Joining 5 users in Window 1...
for /L %%i in (1,1,5) do (
    curl -s -X POST "http://localhost:8000/queue/join?user_id=wave1_user%%i&product_id=phone"
    echo Joined wave1_user%%i
)
echo Wave 1 complete - all in same 3-second window
echo.

REM Wait 4 seconds (ensure Window 1 closes)
echo [3/4] Waiting 4 seconds for Window 1 to close...
timeout /t 4 /nobreak >nul
echo.

REM Join Wave 2 (Window 2) - 5 users in a NEW window
echo [4/4] Wave 2: Joining 5 users in Window 2...
for /L %%i in (1,1,5) do (
    curl -s -X POST "http://localhost:8000/queue/join?user_id=wave2_user%%i&product_id=phone"
    echo Joined wave2_user%%i
)
echo Wave 2 complete - all in different 3-second window
echo.

echo ========================================
echo Test complete!
echo.
echo Watch admission-service logs to see:
echo 1. Wave 1 admitted first (older window)
echo 2. Wave 1 order is RANDOMIZED (not FIFO)
echo 3. Wave 2 admitted only after Wave 1 complete
echo ========================================
pause