$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repository 'backend')
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
} finally { Pop-Location }
Push-Location (Join-Path $repository 'frontend')
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
