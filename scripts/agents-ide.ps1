param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
$backend = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
Push-Location $backend
try {
    uv run --locked --no-dev agents-ide @Arguments
    exit $LASTEXITCODE
} finally { Pop-Location }
