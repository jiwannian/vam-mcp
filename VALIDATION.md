# Validation

The implementation was exercised against VaM 1.22.0.3, Unity 2018.1.9f1, BepInEx 5.0.1, Python 3.10, and MCP SDK 1.28.1 on Windows.

The final local run passed **44 tests** (29 host-side and 15 live-game tests), with zero failures or skips. Coverage includes the stdio MCP handshake and all 17 tool schemas, compilation against the game's CLR 2.0 assemblies, plugin deployment/reload, parameters and UI callbacks, native file dialogs, screenshots and camera renders, logs, Morph access, C# diagnostics, async cancellation, scene/atom cleanup, multi-instance selection, API indexing, and reconnect after the MCP host exits.

The `examples/smoke.json` workflow was also executed through the MCP server against a live game. It deployed and loaded the fixture plugin, called an action, asserted a parameter, captured the UI, unloaded the plugin, and removed the temporary atom.

To run host tests:

```powershell
python -m pytest tests -q -m "not live"
```

To run the live suite, first install the bridge and start a test instance through `vam_instance`, then run:

```powershell
.\scripts\Test.ps1 -Live -GameRoot "C:\VaM"
```

The game directory is shared with other VaM processes. MCP mutations are serialized among MCP clients but cannot lock out unrelated programs. UI tools invoke Unity callbacks rather than performing desktop mouse hit tests. Synchronous code that blocks Unity's main thread cannot be preempted by coroutine timeouts. These tests validate tool behavior and integration; they do not certify the visual quality of plugins being tested.
