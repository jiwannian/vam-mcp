param([switch]$Live, [string]$GameRoot = $env:VAM_GAME_ROOT)
$ErrorActionPreference = 'Stop'
if ($GameRoot) { $env:VAM_GAME_ROOT = (Resolve-Path -LiteralPath $GameRoot).Path }
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if ($Live) {
        python -c "from vam_mcp.client import instances; assert any(i['connected'] for i in instances(include_offline=False)), 'Start a bridge-enabled test instance before live tests'"
        if ($LASTEXITCODE -ne 0) { throw 'No connected VaM test instance' }
        python -m pytest tests -q
    }
    else { python -m pytest tests -q -m 'not live' }
    if ($LASTEXITCODE -ne 0) { throw 'VaM MCP tests failed' }
} finally { Pop-Location }
