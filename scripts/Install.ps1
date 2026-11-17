param([string]$GameRoot = $env:VAM_GAME_ROOT)
$ErrorActionPreference = 'Stop'
if (-not $GameRoot) { throw 'Provide -GameRoot <VaM installation directory>' }
$resolvedGameRoot = (Resolve-Path -LiteralPath $GameRoot).Path
$env:VAM_GAME_ROOT = $resolvedGameRoot
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = (Get-Command python -ErrorAction Stop).Source
Push-Location $projectRoot
try {
    & $pythonPath -c "import mcp,pytest; from vam_mcp.instances import install_bridge; import json; print(json.dumps(install_bridge()))"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency check/build failed. See README for local dependency setup.' }
    & $pythonPath (Join-Path $projectRoot 'scripts\Register-Codex.py') --game-root $resolvedGameRoot
    if ($LASTEXITCODE -ne 0) { throw 'Codex MCP registration failed' }
} finally { Pop-Location }
