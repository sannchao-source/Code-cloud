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
    # Twice a day. Meta blocks an app that calls too often, and a
    # blocked app reports every source as failing -- so the schedule is
    # a correctness constraint, not just a preference. Shortening this
    # means lowering MAX_COMMENT_CALLS to match; see README.md.
    [int]$IntervalMinutes = 720,
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

# Register-ScheduledTask reports failure as a NON-TERMINATING error, so a
# plain try/catch never fires and the script sails on announcing success --
# while the old task has already been removed, leaving nothing scheduled at
# all. -ErrorAction Stop is what makes the failure catchable.
$registered = $false
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -ErrorAction Stop `
        -Description "Check Facebook for new ad comments and comments" | Out-Null
    $registered = $true
    Write-Host "==> scheduled every $IntervalMinutes minutes (runs hidden)" -ForegroundColor Green
} catch {
    # S4U needs "Log on as a batch job" rights, which a locked-down policy
    # can withhold. Falling back keeps the monitor running; it just shows a
    # window each run.
    Write-Warning "hidden task refused ($($_.Exception.Message.Trim()))"
    Write-Warning "falling back to a visible task -- a window will appear each run"
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -RunLevel Limited -ErrorAction Stop `
            -Description "Check Facebook for new ad comments and comments" | Out-Null
        $registered = $true
        Write-Host "==> scheduled every $IntervalMinutes minutes (visible)" -ForegroundColor Yellow
    } catch {
        Write-Warning "visible task also refused ($($_.Exception.Message.Trim()))"
    }
}

# Never claim success without checking. The install removes the previous
# task first, so a silent failure here leaves the monitor switched off --
# and a monitor that is off looks exactly like a monitor with nothing to
# report.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "NOTHING IS SCHEDULED. The monitor will not run." -ForegroundColor Red
    Write-Host "Re-run this in an ADMIN PowerShell. If it still fails, the" -ForegroundColor Red
    Write-Host "account may lack 'Log on as a batch job' rights." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Run it now:     Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it:       Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Latest digest:  Get-Content .\digest.txt -Tail 40"
Write-Host "By hand:        .\deploy\run.ps1"
