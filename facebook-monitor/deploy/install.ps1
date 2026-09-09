# Install fbmonitor on a Windows box (the Ecomm NUC).
#
# Creates a virtualenv, installs dependencies, and registers a Scheduled
# Task that runs every 15 minutes. Safe to re-run.
#
# Open PowerShell AS ADMINISTRATOR, then:
#   cd C:\path\to\facebook-monitor
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\deploy\install.ps1
#
param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName = "FacebookMonitor"
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot

Write-Host "==> installing into $AppDir"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python not found. Install it from python.org and tick 'Add Python to PATH'."
}
Write-Host "==> found $(& python --version)"

& python -m venv (Join-Path $AppDir ".venv")
$venvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $AppDir "requirements.txt")
Write-Host "==> dependencies installed"

foreach ($required in @("accounts.yaml", ".env")) {
    if (-not (Test-Path (Join-Path $AppDir $required))) {
        Write-Warning "$required is missing -- copy $required.example and fill it in"
    }
}

# .env holds Page tokens that never expire. Restrict it to this user, so
# another account on the machine cannot read them.
$envFile = Join-Path $AppDir ".env"
if (Test-Path $envFile) {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    & icacls $envFile /inheritance:r /grant:r "${me}:(R,W)" | Out-Null
    Write-Host "==> locked down .env to $me"
}

# Register the task.
#
# -WindowStyle Hidden alone is not enough: a task registered against the
# interactive user still flashes a console window on screen every run,
# which on a machine someone actually uses is fifteen-minute visual noise
# forever. The principal below is what actually fixes it -- S4U means "run
# whether the user is logged on or not" without storing a password, so the
# task runs in a non-interactive session and nothing appears at all. It
# also means checks continue while the NUC sits at the login screen.
$runner = Join-Path $AppDir "deploy\run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $AppDir

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "==> replaced the existing task"
}

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "Check Facebook for new ad comments and comments" | Out-Null
    Write-Host "==> scheduled every $IntervalMinutes minutes (runs hidden)"
} catch {
    # S4U needs "Log on as a batch job" rights, which a locked-down domain
    # policy can withhold. Falling back keeps the monitor working; it just
    # shows a window each run.
    Write-Warning "could not register a hidden task ($($_.Exception.Message))"
    Write-Warning "falling back to an interactive task -- a window will appear each run"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Limited `
        -Description "Check Facebook for new ad comments and comments" | Out-Null
    Write-Host "==> scheduled every $IntervalMinutes minutes (visible)"
}
Write-Host ""
Write-Host "Run it now:     Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it:       Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Latest digest:  Get-Content .\digest.txt -Tail 40"
Write-Host "By hand:        .\deploy\run.ps1"
