# Runs one check. Task Scheduler calls this; you can also run it by hand.
#
#   .\deploy\run.ps1              normal run
#   .\deploy\run.ps1 -Preview     show what is new without marking it seen
#
param([switch]$Preview)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
Set-Location $AppDir

# Load .env into this process only, so the tokens never become machine-wide
# environment variables visible to everything else on the box.
$envFile = Join-Path $AppDir ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "no .env found at $envFile -- copy .env.example and fill it in"
}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    # Skip blanks and comments; take only the first '=' so tokens
    # containing '=' survive intact.
    if ($line -and -not $line.StartsWith("#")) {
        $split = $line.IndexOf("=")
        if ($split -gt 0) {
            $name = $line.Substring(0, $split).Trim()
            $value = $line.Substring($split + 1).Trim()
            # Strip a trailing inline comment, but not one inside quotes.
            if ($value -notmatch '^["'']' -and $value.Contains("#")) {
                $value = $value.Substring(0, $value.IndexOf("#")).Trim()
            }
            $value = $value.Trim('"').Trim("'")
            if ($value) {
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
}

$python = Join-Path $AppDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "virtualenv missing -- run .\deploy\install.ps1 first"
}

$arguments = @("-m", "fbmonitor", "--notify")
if ($Preview) { $arguments += "--preview" }

# Append the digest so there is a local history as well as the chat post.
#
# ErrorActionPreference has to drop to Continue around this call. With it
# set to Stop, PowerShell turns every line the program writes to stderr
# into a red ErrorRecord -- so an informational note like "no webhook set"
# is displayed exactly like a crash, and a genuine failure stops standing
# out. The exit code below is what actually says whether the run worked.
$ErrorActionPreference = "Continue"
& $python $arguments 2>&1 |
    ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { "$_" } else { $_ } } |
    Tee-Object -FilePath (Join-Path $AppDir "digest.txt") -Append

$code = $LASTEXITCODE
switch ($code) {
    0 { Write-Host "`nDone -- everything checked." -ForegroundColor Green }
    1 { Write-Host "`nDone, but something could not be checked (see 'Not checked' above)." -ForegroundColor Yellow }
    2 { Write-Host "`nConfig error -- nothing ran. Check accounts.yaml and .env." -ForegroundColor Red }
}

# 0 = clean, 1 = something could not be checked, 2 = config error.
exit $code
