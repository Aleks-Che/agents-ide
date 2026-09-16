$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $repository 'frontend/dist/index.html'))) {
    throw 'Built UI is missing. Use the release archive or build frontend first.'
}
Push-Location (Join-Path $repository 'backend')
try {
    uv sync --locked --no-dev
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    uv run --locked --no-dev agents-ide migrate
    if ($LASTEXITCODE -ne 0) { throw 'Migration failed' }
    Write-Output 'Installed. Run scripts/agents-ide.ps1 start, then auth pair-code.'
} finally { Pop-Location }
