param($body)

$logPath = "C:\Users\jacob\scripts\k10_webhook.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$bridgeUrl = $env:K10_BRIDGE_URL
if (-not $bridgeUrl) {
    $bridgeUrl = "http://127.0.0.1:8766/button"
}

$payload = $body
if ($body -is [string] -and $body.Trim().StartsWith("{")) {
    try { $payload = $body | ConvertFrom-Json } catch { $payload = @{ text = $body } }
} elseif ($body -isnot [hashtable] -and $body -isnot [pscustomobject]) {
    $payload = @{ text = [string]$body }
}

$jsonBody = if ($payload -is [string]) { $payload } else { ($payload | ConvertTo-Json -Compress -Depth 5) }
"[$timestamp] Received: $jsonBody" | Out-File -FilePath $logPath -Append

try {
    $response = Invoke-RestMethod -Uri $bridgeUrl -Method POST -Body $jsonBody -ContentType "application/json" -TimeoutSec 10
    "[$timestamp] Bridge OK: $($response | ConvertTo-Json -Compress)" | Out-File -FilePath $logPath -Append
} catch {
    "[$timestamp] Bridge FAIL: $_" | Out-File -FilePath $logPath -Append
}