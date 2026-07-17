param(
    [switch]$Audit
)

$ErrorActionPreference = "Stop"

# Do not write TLS session keys while tools access package indexes or advisories.
$env:SSLKEYLOGFILE = $null
$env:PYTHONUTF8 = "1"

$Python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$Ruff = Join-Path $PSScriptRoot "..\.venv\Scripts\ruff.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing .venv. Follow the setup commands in README.md first."
}
if (-not (Test-Path -LiteralPath $Ruff)) {
    throw "Missing Ruff in .venv. Follow the setup commands in README.md first."
}

function Invoke-Python {
    & $Python @args
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

& $Ruff check app scripts tests
if ($LASTEXITCODE -ne 0) {
    throw "Ruff check failed with exit code $LASTEXITCODE"
}
& $Ruff format --check app scripts tests
if ($LASTEXITCODE -ne 0) {
    throw "Ruff format check failed with exit code $LASTEXITCODE"
}
Invoke-Python scripts/check_secrets.py
Invoke-Python -m pytest --cov=app --cov-report=term-missing

if ($Audit) {
    $AuditCache = Join-Path ([System.IO.Path]::GetTempPath()) "investment-suggestion-tool-pip-audit"
    New-Item -ItemType Directory -Force -Path $AuditCache | Out-Null
    Invoke-Python -m pip_audit --local --skip-editable --progress-spinner off --cache-dir $AuditCache
}
