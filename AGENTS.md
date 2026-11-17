# VaM MCP development

- Use the existing `vam` MCP for game operations; inspect `vam_status` and select an owned test instance first.
- Configure `VAM_GAME_ROOT` to the local VaM installation directory before building, installing, or running live tests.
- Bridge code targets C# 5 and the game's shipped CLR 2.0 assemblies. Build through `vam_mcp.build`; .NET 8 default references are incompatible.
- The installed BepInEx version is 5.0.1; Harmony is `HarmonyLib` from the game's `0Harmony.dll`.
- Stop MCP test instances before updating the bridge DLL. Ordinary VaM instances leave this bridge inactive.
- Start games through `launch_game_process` (Windows WMI/CIM). A Windows MCP client's process job otherwise terminates child games when the MCP connection closes.
- Preserve unrelated Codex configuration when registering/removing `vam`; the Codex CLI can canonicalize other server fields.
- Game files are shared. Serialize mutations with `GameLease`, preserve deployment manifests/backups, and generate uniquely named test resources.
- VaM's native scene/file APIs require game-local paths. Keep test scenes and clone files in `Saves/VaMMcp`.
- The bundled SimpleJSON writes scalar values as strings and is not reliable for re-parsing complex error messages. Keep immutable JSON nodes in the bridge and normalize known public response fields in Python.
- After scene/plugin changes, obtain new UI handles. UI callbacks and actual render screenshots verify different aspects; record both where needed.
- Use `scripts/Test.ps1` for host tests and `scripts/Test.ps1 -Live -GameRoot <VaM directory>` for the complete bridge suite.
- No explanatory text beside controls. Keep help in documentation or a separate dialog.
