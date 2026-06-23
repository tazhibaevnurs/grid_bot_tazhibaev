# Start grid bot + dashboard (run from project root or via .vscode terminal profile).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$venvActivate = Join-Path $Root ".venv\Scripts\Activate.ps1"
$port = 8003
if ($env:GRID_BOT_PORT) {
    $port = [int]$env:GRID_BOT_PORT
}

if (-not (Test-Path $venvPython)) {
    Write-Host "[grid-bot] .venv not found. Run: python -m venv .venv" -ForegroundColor Red
    return
}

& $venvActivate

function Test-PortListening {
    param([int]$p)
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

function Test-BotRunning {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    if (-not $procs) {
        return $false
    }
    foreach ($proc in @($procs)) {
        if ($proc.CommandLine -and ($proc.CommandLine -like "*grid_bot.main*")) {
            return $true
        }
    }
    return $false
}

if (-not (Test-BotRunning)) {
    $botCmd = "Set-Location '$Root'; & '$venvActivate'; & '$venvPython' -m grid_bot.main"
    Start-Process powershell.exe -ArgumentList @("-NoExit", "-Command", $botCmd) | Out-Null
    Write-Host "[grid-bot] Bot started in a new window." -ForegroundColor Green
}
else {
    Write-Host "[grid-bot] Bot is already running." -ForegroundColor Yellow
}

if (Test-PortListening -p $port) {
    Write-Host "[grid-bot] Dashboard: http://127.0.0.1:$port (already running)" -ForegroundColor Cyan
    return
}

Write-Host "[grid-bot] Dashboard: http://127.0.0.1:$port" -ForegroundColor Cyan
& $venvPython -m uvicorn web.app:app --port $port --host 127.0.0.1
