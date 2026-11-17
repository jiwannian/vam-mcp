param([switch]$Install, [string]$GameRoot = $env:VAM_GAME_ROOT)
$ErrorActionPreference = 'Stop'
if ($GameRoot) { $env:VAM_GAME_ROOT = (Resolve-Path -LiteralPath $GameRoot).Path }
if ($Install -and -not $env:VAM_GAME_ROOT) { throw 'Set VAM_GAME_ROOT or pass -GameRoot' }
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    if ($Install) { python -c "from vam_mcp.instances import install_bridge; import json; print(json.dumps(install_bridge()))" }
    else { python -c "from vam_mcp.build import build_bridge; import json; print(json.dumps(build_bridge()))" }
    if ($LASTEXITCODE -ne 0) { throw 'Bridge build failed' }
} finally { Pop-Location }
