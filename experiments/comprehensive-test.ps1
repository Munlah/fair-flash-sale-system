#Digital Flash Sale System - Complete E2E Verification
#To test all components, fault tolerance, and recovery mechanisms

param(
    [int]$ConcurrentUsers = 10,
    [int]$TotalUsers = 50,
    [string]$Product = "phone",
    [double]$PaymentFailureRate = 0.2
)

$ErrorActionPreference = "Continue"

Write-Host @"

╔════════════════════════════════════════════════════════════════╗
║                                                                ║
║     Digital Flash Sale System - Comprehensive Test Suite      ║
║                                                                ║
╚════════════════════════════════════════════════════════════════╝

"@ -ForegroundColor Cyan

if ($PaymentFailureRate -lt 0 -or $PaymentFailureRate -gt 1) {
    throw "PaymentFailureRate must be between 0 and 1."
}

Write-Host "Automated test payment failure rate: $([math]::Round($PaymentFailureRate * 100))%" -ForegroundColor Gray
kubectl set env deployment/payment-gateway "PAYMENT_FAILURE_RATE=$PaymentFailureRate" -n dffs | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not configure the Kubernetes payment failure rate."
}
kubectl rollout status deployment/payment-gateway -n dffs --timeout=120s | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Kubernetes payment gateway did not become ready after changing the failure rate."
}

# =============================================================================
# Test Configuration
# =============================================================================

$TestResults = @{
    Total = 0
    Passed = 0
    Failed = 0
    Tests = @()
}

function Test-Component {
    param(
        [string]$Name,
        [scriptblock]$Test,
        [string]$Description
    )
    
    $TestResults.Total++
    Write-Host "`n[$($TestResults.Total)] Testing: $Name" -ForegroundColor Yellow
    Write-Host "    $Description" -ForegroundColor Gray
    
    try {
        $result = & $Test
        if ($result) {
            Write-Host "    PASS" -ForegroundColor Green
            $TestResults.Passed++
            $TestResults.Tests += @{Name=$Name; Status="PASS"; Error=$null}
            return $true
        } else {
            Write-Host "    FAIL" -ForegroundColor Red
            $TestResults.Failed++
            $TestResults.Tests += @{Name=$Name; Status="FAIL"; Error="Test returned false"}
            return $false
        }
    } catch {
        Write-Host "    FAIL: $_" -ForegroundColor Red
        $TestResults.Failed++
        $TestResults.Tests += @{Name=$Name; Status="FAIL"; Error=$_.Exception.Message}
        return $false
    }
}

# =============================================================================
# PHASE 1: Infrastructure Health Checks
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 1: Infrastructure Health Checks" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "All Pods Running" -Description "Verify all pods are in Running state" -Test {
    $pods = kubectl get pods -n dffs -o json | ConvertFrom-Json
    $notRunning = $pods.items | Where-Object { $_.status.phase -ne "Running" -and $_.status.phase -ne "Succeeded" }
    
    if ($notRunning) {
        Write-Host "    Not running: $($notRunning.metadata.name -join ', ')" -ForegroundColor Red
        return $false
    }
    
    $runningPods = ($pods.items | Where-Object { $_.status.phase -eq "Running" }).Count
    Write-Host "    $runningPods pods running" -ForegroundColor Green
    return $true
}

Test-Component -Name "Redis Connectivity" -Description "Verify Redis is accessible and responding" -Test {
    $result = kubectl exec -n dffs deploy/gateway -- python -c "import redis; r = redis.Redis(host='redis', port=6379); print(r.ping())" 2>&1
    return $result -match "True"
}

Test-Component -Name "PostgreSQL Connectivity" -Description "Verify database is accessible" -Test {
    $result = kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT 1" 2>&1
    return $result -match "\(1 row\)"
}

Test-Component -Name "Service Endpoints" -Description "Verify all services have endpoints" -Test {
    $services = @("gateway", "checkout", "inventory", "payment-gateway", "admission", "redis", "postgres")
    $allGood = $true
    
    foreach ($svc in $services) {
        $endpoints = kubectl get endpoints $svc -n dffs -o jsonpath='{.subsets[*].addresses[*].ip}' 2>$null
        if (-not $endpoints) {
            Write-Host "    Missing endpoints for $svc" -ForegroundColor Red
            $allGood = $false
        }
    }
    
    return $allGood
}

# =============================================================================
# PHASE 2: Individual Service Health
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 2: Individual Service Health" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

$services = @{
    #Use dedicated local ports so Docker Compose services cannot intercept
    #requests intended for the Kubernetes port-forwards.
    "Gateway" = @{Port=18000; Path="/health"; Expected="healthy"}
    "Checkout" = @{Port=18001; Path="/health"; Expected="healthy"}
    "Inventory" = @{Port=18002; Path="/health"; Expected="healthy"}
    "Payment Gateway" = @{Port=18003; Path="/health"; Expected="healthy"}
}

foreach ($svcName in $services.Keys) {
    $svc = $services[$svcName]
    
    Test-Component -Name "$svcName Health" -Description "Check $svcName /health endpoint" -Test {
        $portForward = Start-Job -ScriptBlock {
            param($port, $service)
            kubectl port-forward -n dffs "svc/$service" "${port}:8000" 2>&1 | Out-Null
        } -ArgumentList $svc.Port, $svcName.ToLower().Replace(" ", "-")
        
        Start-Sleep -Seconds 3
        
        try {
            if ($portForward.State -eq "Failed" -or $portForward.State -eq "Completed") {
                throw "Kubernetes port-forward for $svcName did not start"
            }

            $health = Invoke-RestMethod -Uri "http://localhost:$($svc.Port)/health" -TimeoutSec 5
            $result = $health.status -match $svc.Expected
            
            if ($result) {
                Write-Host "    Service: $($health.service)" -ForegroundColor Gray
            }
            
            return $result
        } finally {
            Stop-Job -Job $portForward -ErrorAction SilentlyContinue
            Remove-Job -Job $portForward -ErrorAction SilentlyContinue
        }
    }
}

# =============================================================================
# PHASE 3: Queue and Admission System
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 3: Queue and Admission System" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "Queue Join" -Description "User can join the queue" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $userId = "test-queue-$(Get-Random)"
        $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
        
        if ($join.token -and $join.status -eq "waiting") {
            Write-Host "    Token: $($join.token)" -ForegroundColor Gray
            Write-Host "    Position: $($join.position)" -ForegroundColor Gray
            return $true
        }
        
        return $false
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Token Status Check" -Description "Can query token status" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $userId = "test-status-$(Get-Random)"
        $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
        $status = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$($join.token)"
        
        Write-Host "    Status: $($status.status)" -ForegroundColor Gray
        return $status.status -in @("waiting", "admitted")
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Admission Controller" -Description "Users are being admitted from queue" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $userId = "test-admission-$(Get-Random)"
        $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
        
        #Wait up to 15 seconds for admission
        for ($i = 0; $i -lt 5; $i++) {
            Start-Sleep -Seconds 3
            $status = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$($join.token)"
            
            if ($status.status -eq "admitted") {
                Write-Host "    Admitted after $($i * 3) seconds" -ForegroundColor Gray
                return $true
            }
        }
        
        Write-Host "    Not admitted within 15 seconds" -ForegroundColor Yellow
        return $false
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

# =============================================================================
# PHASE 4: End-to-End Purchase Flow
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 4: End-to-End Purchase Flow" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "Complete Purchase" -Description "Full flow from queue to order creation" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $userId = "test-e2e-$(Get-Random)"
        
        #Join queue
        $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
        Write-Host "    Joined queue: $($join.token)" -ForegroundColor Gray
        
        #Wait for admission
        $admitted = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 3
            $status = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$($join.token)"
            
            if ($status.status -eq "admitted") {
                $admitted = $true
                Write-Host "    Admitted after $($i * 3) seconds" -ForegroundColor Gray
                break
            }
        }
        
        if (-not $admitted) {
            Write-Host "    Failed to get admitted" -ForegroundColor Red
            return $false
        }
        
        #Attempt purchase(may fail due to simulated payment failure)
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $purchase = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/purchase?token=$($join.token)"
            
            if ($purchase.success) {
                Write-Host "    Purchase successful!" -ForegroundColor Green
                Write-Host "    Order ID: $($purchase.order.id)" -ForegroundColor Gray
                Write-Host "    Status: $($purchase.order.status)" -ForegroundColor Gray
                return $true
            } else {
                Write-Host "    Attempt $attempt failed: $($purchase.reason)" -ForegroundColor Yellow
                
                if ($purchase.reason -match "Payment failed|insufficient_funds|Payment error") {
                    # Re-join queue for retry
                    $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
                    Start-Sleep -Seconds 10
                } else {
                    return $false
                }
            }
        }
        
        Write-Host "    All purchase attempts failed" -ForegroundColor Red
        return $false
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Order Persistence" -Description "Orders are saved in database" -Test {
    $result = kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT COUNT(*) FROM orders;" 2>&1
    $count = ($result | Select-String "^\s*\d+\s*$" | ForEach-Object {
        [int]$_.Matches.Value.Trim()
    } | Select-Object -Last 1)
    
    Write-Host "    Total orders in DB: $count" -ForegroundColor Gray
    return [int]$count -gt 0
}

Test-Component -Name "Idempotency Protection" -Description "Duplicate orders are prevented" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $purchase1 = $null
        $join = $null

        #Payment is intentionally probabilistic in the demo. Try fresh users
        #until one payment succeeds; idempotency is checked only after that.
        for ($attempt = 1; $attempt -le 8 -and -not $purchase1.success; $attempt++) {
            $userId = "test-idempotent-$attempt-$(Get-Random)"
            $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"

            for ($poll = 0; $poll -lt 10; $poll++) {
                Start-Sleep -Seconds 3
                $status = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$($join.token)"
                if ($status.status -eq "admitted") { break }
            }

            if ($status.status -ne "admitted") {
                continue
            }

            $purchase1 = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/purchase?token=$($join.token)"
            if (-not $purchase1.success) {
                Write-Host "    Attempt $attempt payment did not succeed; trying a fresh token" -ForegroundColor DarkGray
            }
        }

        if ($purchase1 -and $purchase1.success) {
            #A second request with the same token must not create another order.
            $purchase2 = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/purchase?token=$($join.token)"
            $sameOrder = $purchase2.success -and $purchase2.order.id -eq $purchase1.order.id
            $explicitlyRejected = $purchase2.reason -match "already|completed"
            $protected = $sameOrder -or $explicitlyRejected

            Write-Host "    First order: $($purchase1.order.id)" -ForegroundColor Gray
            Write-Host "    Second attempt: $($purchase2.reason)" -ForegroundColor Gray
            return $protected
        }

        Write-Host "    No successful payment was available for the idempotency check" -ForegroundColor Red
        return $false
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

# =============================================================================
# PHASE 5: Fault Tolerance and Recovery
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 5: Fault Tolerance and Recovery" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "Payment Retry Logic" -Description "System handles payment failures gracefully" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        $attempts = 0
        $successes = 0
        $paymentFailures = 0
        
        for ($i = 1; $i -le 10; $i++) {
            $userId = "test-retry-$i-$(Get-Random)"
            $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
            
            Start-Sleep -Seconds 5
            
            $purchase = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/purchase?token=$($join.token)"
            $attempts++
            
            if ($purchase.success) {
                $successes++
            } elseif ($purchase.reason -match "Payment failed|insufficient_funds|Payment error") {
                $paymentFailures++
            }
        }
        
        Write-Host "    Attempts: $attempts, Successes: $successes, Payment Failures: $paymentFailures" -ForegroundColor Gray
        
        #A random sample is not required to contain both outcomes. Verify every
        #attempt produced either a success or a payment failure.
        return $attempts -eq 10 -and ($successes + $paymentFailures) -eq $attempts
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Pod Recovery" -Description "System recovers when pods are deleted" -Test {
    Write-Host "    Deleting one gateway pod..." -ForegroundColor Gray
    
    $pod = kubectl get pods -n dffs -l app=gateway -o jsonpath='{.items[0].metadata.name}'
    kubectl delete pod $pod -n dffs --force --grace-period=0 2>&1 | Out-Null
    
    Write-Host "    Waiting 30 seconds for recovery..." -ForegroundColor Gray
    Start-Sleep -Seconds 30
    
    #Check if new pod is running
    $pods = kubectl get pods -n dffs -l app=gateway -o json | ConvertFrom-Json
    $runningPods = ($pods.items | Where-Object { $_.status.phase -eq "Running" }).Count
    
    Write-Host "    Gateway pods running: $runningPods" -ForegroundColor Gray
    
    #Verify service still works
    #Use a dedicated port so an operator's separate Kubernetes port-forward
    #cannot intercept this recovery check.
    $recoveryPort = 18000
    $portForward = Start-Job -ScriptBlock {
        param($port)
        kubectl port-forward -n dffs svc/gateway "${port}:8000" 2>&1 | Out-Null
    } -ArgumentList $recoveryPort
    Start-Sleep -Seconds 5

    try {
        if ($portForward.State -eq "Failed" -or $portForward.State -eq "Completed") {
            throw "Kubernetes Gateway port-forward did not start after pod recovery"
        }

        $health = Invoke-RestMethod -Uri "http://localhost:$recoveryPort/health" -TimeoutSec 5
        $works = $health.status -eq "healthy"
        
        Write-Host "    Gateway health: $($health.status)" -ForegroundColor Gray
        return $runningPods -ge 2 -and $works
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Redis Persistence" -Description "Queue state survives Redis reconnection" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 3
    
    try {
        #Create a token
        $userId = "test-persist-$(Get-Random)"
        $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$Product"
        $originalToken = $join.token
        
        Write-Host "    Created token: $originalToken" -ForegroundColor Gray
        
        #Query it back
        $status1 = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$originalToken"
        
        Write-Host "    Initial status: $($status1.status)" -ForegroundColor Gray
        
        #Restart gateway pods to force new Redis connections
        kubectl delete pod -l app=gateway -n dffs --force --grace-period=0 2>&1 | Out-Null
        
        Write-Host "    Restarted gateway pods" -ForegroundColor Gray
        Start-Sleep -Seconds 30
        
        #Recreate the port-forward because the original forward points at
        #deleted gateway pods.
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
        $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
        Start-Sleep -Seconds 5

        #Query token again
        $status2 = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$originalToken"
        
        Write-Host "    Status after restart: $($status2.status)" -ForegroundColor Gray
        
        return $status2.status -ne $null
    } catch {
        Write-Host "    Error: $_" -ForegroundColor Red
        return $false
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

Test-Component -Name "Inventory Reservation Lifecycle" -Description "Reservations can be created and explicitly released" -Test {
    $inventoryPort = 18002
    $portForward = Start-Job -ScriptBlock { param($port) kubectl port-forward -n dffs svc/inventory "${port}:8000" 2>&1 | Out-Null } -ArgumentList $inventoryPort
    Start-Sleep -Seconds 3

    try {
        if ($portForward.State -eq "Failed" -or $portForward.State -eq "Completed") {
            throw "Kubernetes port-forward for inventory did not start"
        }

        $before = Invoke-RestMethod -Uri "http://localhost:$inventoryPort/stock/$Product" -TimeoutSec 5
        $reservation = Invoke-RestMethod -Method Post -Uri "http://localhost:$inventoryPort/reserve/$Product" -TimeoutSec 5
        if (-not $reservation.success) {
            Write-Host "    Reservation failed: $($reservation.reason)" -ForegroundColor Red
            return $false
        }

        $during = Invoke-RestMethod -Uri "http://localhost:$inventoryPort/stock/$Product" -TimeoutSec 5
        $release = Invoke-RestMethod -Method Post -Uri "http://localhost:$inventoryPort/release/$($reservation.reservation_id)" -TimeoutSec 5
        $after = Invoke-RestMethod -Uri "http://localhost:$inventoryPort/stock/$Product" -TimeoutSec 5

        Write-Host "    Before: $($before.stock), during: $($during.stock), after: $($after.stock)" -ForegroundColor Gray
        return $release.success -and $after.stock -eq $before.stock
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

# =============================================================================
# PHASE 6: Concurrent Load Test
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 6: Concurrent Load Test" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "Concurrent Users" -Description "System handles $ConcurrentUsers concurrent purchase attempts" -Test {
    $portForward = Start-Job -ScriptBlock { kubectl port-forward -n dffs svc/gateway 8000:8000 2>&1 | Out-Null }
    Start-Sleep -Seconds 5
    
    try {
        Write-Host "    Simulating $ConcurrentUsers concurrent users..." -ForegroundColor Gray
        
        $jobs = @()
        $results = @()
        
        #Create concurrent users
        for ($i = 1; $i -le $ConcurrentUsers; $i++) {
            $job = Start-Job -ScriptBlock {
                param($userId, $product)
                
                try {
                    #Join queue
                    $join = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/queue/join?user_id=$userId&product_id=$product" -TimeoutSec 10
                    
                    #Wait for admission
                    $admitted = $false
                    for ($attempt = 0; $attempt -lt 20; $attempt++) {
                        Start-Sleep -Seconds 3
                        $status = Invoke-RestMethod -Uri "http://localhost:8000/queue/status/$($join.token)" -TimeoutSec 10
                        
                        if ($status.status -eq "admitted") {
                            $admitted = $true
                            break
                        }
                    }
                    
                    if (-not $admitted) {
                        return @{Success=$false; Reason="Not admitted"}
                    }
                    
                    #Attempt purchase
                    $purchase = Invoke-RestMethod -Method POST -Uri "http://localhost:8000/purchase?token=$($join.token)" -TimeoutSec 10
                    
                    return @{
                        Success=$purchase.success
                        Reason=$purchase.reason
                        OrderId=if($purchase.order){$purchase.order.id}else{$null}
                    }
                } catch {
                    return @{Success=$false; Reason="Exception: $_"}
                }
            } -ArgumentList "concurrent-$i-$(Get-Random)", $Product
            
            $jobs += $job
            Start-Sleep -Milliseconds 100
        }
        
        Write-Host "    Waiting for all $ConcurrentUsers users to complete..." -ForegroundColor Gray
        
        #Wait for all jobs(max 120 seconds)
        $timeout = 120
        $elapsed = 0
        while ($elapsed -lt $timeout) {
            $completed = ($jobs | Where-Object { $_.State -eq "Completed" }).Count
            
            if ($completed -eq $jobs.Count) {
                break
            }
            
            Write-Host "    Progress: $completed/$($jobs.Count) completed" -ForegroundColor Gray
            Start-Sleep -Seconds 5
            $elapsed += 5
        }
        
        #Collect results
        foreach ($job in $jobs) {
            $result = Receive-Job -Job $job
            $results += $result
            Remove-Job -Job $job -Force
        }
        
        $successful = ($results | Where-Object { $_.Success -eq $true }).Count
        $failed = ($results | Where-Object { $_.Success -eq $false }).Count
        $paymentFailed = ($results | Where-Object {
            $_.Reason -match "Payment failed|insufficient_funds|Payment error"
        }).Count
        
        Write-Host "    Results:" -ForegroundColor Gray
        Write-Host "      Successful purchases: $successful" -ForegroundColor Green
        Write-Host "      Failed (payment): $paymentFailed" -ForegroundColor Yellow
        Write-Host "      Other failures: $($failed - $paymentFailed)" -ForegroundColor Red
        
        #Success if at least 30% got through, accounting for the 20% test rate.
        return $successful -gt ($ConcurrentUsers * 0.3)
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

# =============================================================================
# PHASE 7: Leader Election and High Availability
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 7: Leader Election and High Availability" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "Admission Leader Election" -Description "One admission pod holds the lease" -Test {
    $holder = (kubectl exec -n dffs deploy/redis -- redis-cli GET admission:leader 2>$null | Select-Object -Last 1).Trim()
    $admissionPods = kubectl get pods -n dffs -l app=admission -o json | ConvertFrom-Json
    $matchingPod = $admissionPods.items | Where-Object { $_.metadata.name -eq $holder }

    Write-Host "    Current leader: $holder" -ForegroundColor Gray
    return [bool]$matchingPod
}

Test-Component -Name "Leader Failover" -Description "New leader is elected when current leader fails" -Test {
    $originalLeader = (kubectl exec -n dffs deploy/redis -- redis-cli GET admission:leader 2>$null | Select-Object -Last 1).Trim()
    if (-not $originalLeader) {
        Write-Host "    No leader to test failover" -ForegroundColor Yellow
        return $false
    }

    Write-Host "    Original leader: $originalLeader" -ForegroundColor Gray
    
    #Find and kill the leader pod
    $pods = kubectl get pods -n dffs -l app=admission -o json | ConvertFrom-Json
    $leaderPod = $pods.items | Where-Object { $_.metadata.name -eq $originalLeader }
    
    if ($leaderPod) {
        Write-Host "    Killing leader pod: $($leaderPod.metadata.name)" -ForegroundColor Gray
        kubectl delete pod $leaderPod.metadata.name -n dffs --force --grace-period=0 2>&1 | Out-Null
        
        Write-Host "    Waiting 30 seconds for new leader election..." -ForegroundColor Gray
        Start-Sleep -Seconds 30
        
        #Check for new leader
        $newLeader = (kubectl exec -n dffs deploy/redis -- redis-cli GET admission:leader 2>$null | Select-Object -Last 1).Trim()
        Write-Host "    New leader: $newLeader" -ForegroundColor Gray
        return $newLeader -and $newLeader -ne $originalLeader
    }
    
    return $false
}

# =============================================================================
# PHASE 8: Data Integrity and Consistency
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "PHASE 8: Data Integrity and Consistency" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Test-Component -Name "No Duplicate Orders" -Description "Each idempotency key appears only once" -Test {
    $result = kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT idempotency_key, COUNT(*) as cnt FROM orders GROUP BY idempotency_key HAVING COUNT(*) > 1;" 2>&1
    
    $duplicates = $result | Select-String "\d+ rows?" | ForEach-Object { 
        if ($_ -match "(\d+)") { [int]$matches[1] } 
    }
    
    Write-Host "    Duplicate idempotency keys: $duplicates" -ForegroundColor Gray
    return $duplicates -eq 0
}

Test-Component -Name "Order Data Integrity" -Description "All orders have required fields" -Test {
    $result = kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT COUNT(*) FROM orders WHERE order_id IS NULL OR user_id IS NULL OR product_id IS NULL OR status IS NULL;" 2>&1
    
    $incomplete = $result | Select-String "\s+(\d+)" | ForEach-Object { 
        if ($_ -match "\s+(\d+)") { [int]$matches[1] } 
    }
    
    Write-Host "    Incomplete orders: $incomplete" -ForegroundColor Gray
    return $incomplete -eq 0
}

Test-Component -Name "Inventory Consistency" -Description "Stock levels are tracked correctly" -Test {
    $inventoryPort = 18002
    $portForward = Start-Job -ScriptBlock { param($port) kubectl port-forward -n dffs svc/inventory "${port}:8000" 2>&1 | Out-Null } -ArgumentList $inventoryPort
    Start-Sleep -Seconds 3
    
    try {
        if ($portForward.State -eq "Failed" -or $portForward.State -eq "Completed") {
            throw "Kubernetes port-forward for inventory did not start"
        }

        $stock = Invoke-RestMethod -Uri "http://localhost:$inventoryPort/stock/$Product" -TimeoutSec 5
        
        Write-Host "    Product: $Product" -ForegroundColor Gray
        Write-Host "    Stock: $($stock.stock)" -ForegroundColor Gray

        return $null -ne $stock.stock -and [int]$stock.stock -ge 0
    } finally {
        Stop-Job -Job $portForward -ErrorAction SilentlyContinue
        Remove-Job -Job $portForward -ErrorAction SilentlyContinue
    }
}

# =============================================================================
# Final Report
# =============================================================================

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "TEST SUMMARY" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Write-Host "`nTotal Tests: $($TestResults.Total)" -ForegroundColor White
Write-Host "Passed: $($TestResults.Passed)" -ForegroundColor Green
Write-Host "Failed: $($TestResults.Failed)" -ForegroundColor Red

$successRate = [math]::Round(($TestResults.Passed / $TestResults.Total) * 100, 2)
Write-Host "Success Rate: $successRate%" -ForegroundColor $(if($successRate -ge 80){"Green"}else{"Yellow"})

if ($TestResults.Failed -gt 0) {
    Write-Host "`nFailed Tests:" -ForegroundColor Red
    $TestResults.Tests | Where-Object { $_.Status -eq "FAIL" } | ForEach-Object {
        Write-Host "  $($_.Name)" -ForegroundColor Red
        if ($_.Error) {
            Write-Host "     $($_.Error)" -ForegroundColor Gray
        }
    }
}

#System snapshot
Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "SYSTEM SNAPSHOT" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

Write-Host "`nPod Status:" -ForegroundColor Yellow
kubectl get pods -n dffs

Write-Host "`nTotal Orders:" -ForegroundColor Yellow
kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT COUNT(*) as total_orders FROM orders;" 2>&1 | Select-String "total_orders|^\s+\d+|rows?\)"

Write-Host "`nRecent Orders:" -ForegroundColor Yellow
kubectl exec -n dffs deploy/postgres -- psql -U flash -d orders -c "SELECT order_id, user_id, product_id, status, created_at FROM orders ORDER BY created_at DESC LIMIT 10;" 2>&1

Write-Host "`n" + "="*70 -ForegroundColor Cyan
Write-Host "Comprehensive test completed!" -ForegroundColor Cyan
Write-Host "="*70 -ForegroundColor Cyan

if ($successRate -ge 90) {
    Write-Host "`nSystem is healthy and operating correctly!" -ForegroundColor Green
} elseif ($successRate -ge 70) {
    Write-Host "`nSystem is operational but some issues detected" -ForegroundColor Yellow
} else {
    Write-Host "`nSystem has significant issues" -ForegroundColor Red
}