$ErrorActionPreference = "Stop"

# Do not inherit TLS session-key logging into project tooling or HTTP clients.
$env:SSLKEYLOGFILE = $null
$env:PYTHONUTF8 = "1"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Invoke-Python {
    & $Python @args
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}

Invoke-Python -m ensurepip --upgrade --default-pip
Invoke-Python -m pip install --upgrade pip
Invoke-Python -m pip install -e "$ProjectRoot[dev]"
