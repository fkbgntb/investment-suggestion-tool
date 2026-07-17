$ErrorActionPreference = "Stop"

# Do not write TLS session keys while the application calls external HTTPS APIs.
$env:SSLKEYLOGFILE = $null
$env:PYTHONUTF8 = "1"

$Python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing .venv. Follow the setup commands in README.md first."
}

& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
