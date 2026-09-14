$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repository 'backend')
try {
    uv run --locked python ../scripts/generate_contracts.py --check
    if ($LASTEXITCODE -ne 0) { throw 'Server contracts are out of date' }
    foreach ($check in @(@('ruff', 'check', 'src', 'tests', '../scripts'), @('ruff', 'format', '--check', 'src', 'tests', '../scripts'), @('mypy', 'src'), @('pytest', '-q'))) {
        uv run --locked @check
        if ($LASTEXITCODE -ne 0) { throw "Backend check failed: $check" }
    }
} finally { Pop-Location }
Push-Location (Join-Path $repository 'frontend')
try {
    foreach ($check in @('lint', 'format:check', 'test', 'build')) {
        npm.cmd run $check
        if ($LASTEXITCODE -ne 0) { throw "Frontend check failed: $check" }
    }
} finally { Pop-Location }
