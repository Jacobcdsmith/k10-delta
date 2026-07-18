# Start Hermes voice profile gateway + K10 bridge
# Usage: pwsh -File C:\Users\jacob\scripts\start_voice_stack.ps1

$ErrorActionPreference = "Continue"
$scripts = "C:\Users\jacob\scripts"
$voiceHome = "$env:LOCALAPPDATA\hermes\profiles\voice"

Write-Host "=== Hermes Voice Stack ===" -ForegroundColor Cyan
Write-Host "Voice profile: $voiceHome"

# 1. Hermes API gateway (voice profile)
Write-Host "`n[1/2] Starting Hermes voice gateway (API :8642)..." -ForegroundColor Yellow
Start-Process -FilePath "hermes" -ArgumentList "-p", "voice", "gateway", "run" -WindowStyle Minimized
Start-Sleep -Seconds 4

# Health check
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8642/health" -TimeoutSec 5
    Write-Host "  API server: $($health.status)" -ForegroundColor Green
} catch {
    Write-Host "  API server not up yet — check: hermes -p voice gateway status" -ForegroundColor Red
}

# 2. K10 bridge
Write-Host "`n[2/2] Starting K10 bridge (WS :8765, HTTP :8766, TCP :5555)..." -ForegroundColor Yellow
$env:VOICE_HERMES_HOME = $voiceHome
$env:HERMES_API_URL = "http://127.0.0.1:8642/v1"
$env:HERMES_API_KEY = "k10-hermes-voice-local"

Set-Location $scripts
python hermes_k10_bridge.py