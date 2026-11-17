param([switch]$RemoveBridge, [string]$GameRoot = $env:VAM_GAME_ROOT)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& python (Join-Path $PSScriptRoot 'Register-Codex.py') --remove
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP removal failed' }
if ($RemoveBridge) {
    if (-not $GameRoot) { throw 'Pass -GameRoot when removing the bridge DLL' }
    $env:VAM_GAME_ROOT = (Resolve-Path -LiteralPath $GameRoot).Path
    Push-Location $projectRoot
    try {
        python -c "from vam_mcp.client import instances; from vam_mcp.common import GAME; active=instances(include_offline=False); assert not active,'Stop MCP test instances first'; p=GAME/'BepInEx/plugins/VaMMcp/VaMMcp.Bridge.dll'; p.unlink(missing_ok=True)"
        if ($LASTEXITCODE -ne 0) { throw 'Bridge removal failed' }
    } finally { Pop-Location }
}
