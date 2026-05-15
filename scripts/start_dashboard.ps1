$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$DashboardUrl = "http://127.0.0.1:8787"

Set-Location $ProjectRoot

Write-Host ""
Write-Host "Starting Local Lead Dashboard"
Write-Host "Project: $ProjectRoot"
Write-Host "URL: $DashboardUrl"
Write-Host ""
Write-Host "If the browser does not open, open this URL manually:"
Write-Host $DashboardUrl
Write-Host ""
Write-Host "Leave this window open while you use the dashboard."
Write-Host "Press Ctrl+C in this window to stop the dashboard."
Write-Host ""

$ActivateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $ActivateScript) {
    . $ActivateScript
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Write-Host "Using local virtual environment."
}

if (-not $PythonExe) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonExe = "python"
    }
}

if (-not $PythonExe) {
    $PyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PyCommand) {
        $PythonExe = "py"
    }
}

if (-not $PythonExe) {
    Write-Error "Python was not found. Create .venv or install Python, then run this file again."
    exit 1
}

Start-Process $DashboardUrl

& $PythonExe -m src.dashboard_app --host 127.0.0.1 --port 8787

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dashboard stopped with an error. Check the message above."
    Read-Host "Press Enter to close"
}
