<#
.SYNOPSIS
  Start MBMeister. Creates a virtual environment (.venv) next to this script on
  first run and installs the Python dependencies from requirements.txt into it.

.EXAMPLE
  .\start_mbmeister.ps1
  .\start_mbmeister.ps1 register.csv --host 192.168.0.10 --port 502 --unit 1

.NOTES
  If script execution is blocked, run it with:
  powershell -ExecutionPolicy Bypass -File .\start_mbmeister.ps1
#>
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv   = Join-Path $PSScriptRoot ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"

function Fail($msg) {
    Write-Host "Error: $msg" -ForegroundColor Red
    exit 1
}

# --- find a Python interpreter (py launcher preferred, then python) -------- #
$pyExe = $null
$pyPre = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyExe = "py"; $pyPre = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyExe = "python"
} else {
    Fail "Python not found. Install Python 3.9 or newer from https://www.python.org/downloads/ (tick 'tcl/tk')."
}

& $pyExe @pyPre -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "Python 3.9 or newer is required." }

& $pyExe @pyPre -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "Python's tkinter module is missing. Re-run the Python installer and enable 'tcl/tk and IDLE'." }

# --- virtual environment --------------------------------------------------- #
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment in .venv ..."
    & $pyExe @pyPre -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
}

# --- dependencies (only when requirements.txt changed) --------------------- #
$stampFile = Join-Path $venv ".requirements.sha256"
$current   = (Get-FileHash (Join-Path $PSScriptRoot "requirements.txt") -Algorithm SHA256).Hash
$installed = if (Test-Path $stampFile) { (Get-Content $stampFile -Raw).Trim() } else { "" }
if ($installed -ne $current) {
    Write-Host "Installing dependencies ..."
    & $venvPy -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Fail "Installing dependencies failed." }
    Set-Content -Path $stampFile -Value $current
}

# --- start ----------------------------------------------------------------- #
$scriptArgs = @($args)
if ($scriptArgs.Count -eq 0 -and (Test-Path (Join-Path $PSScriptRoot "register.csv"))) {
    $scriptArgs = @("register.csv")
}
& $venvPy (Join-Path $PSScriptRoot "mbmeister.py") @scriptArgs
exit $LASTEXITCODE
