using BepInEx;

[BepInPlugin("codex.vam.mcp.fixture", "VaM MCP Fixture", "1.0.0")]
public sealed class BepInExFixture : BaseUnityPlugin
{
    private void Awake()
    {
        Logger.LogInfo("VaM MCP BepInEx fixture loaded");
    }
}
