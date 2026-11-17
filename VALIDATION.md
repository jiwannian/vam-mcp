# Validation

The bridge and BepInEx fixture were compiled locally against VaM's CLR references and BepInEx. A real test instance loaded the fixture after restart; the Chainloader registry exposed its GUID and the bridge captured its startup log. The fixture deployment was rolled back and the test instance stopped. The API indexer also queried the installed `BepInEx.dll` successfully.

Current host-only validation passed **43 tests**, with 17 live tests deselected. With the VaM root configured, the host/API run passed **44 tests** and skipped 16 live tests when no bridge-enabled instance is active. The live suite now includes the successfully exercised BepInEx DLL/PDB deployment, restart, Chainloader GUID and startup-log verification, rollback, and cleanup workflow. Other coverage includes MCP tool schemas, profile prerequisite discovery, source and asset deployment, prebuilt VAR inspection and load routing, path escape rejection, conflict-checked rollback, compiler reference flags, restart identity handling, API indexer hash invalidation, and installed assembly path validation.

To run host tests:

```powershell
python -m pytest tests -q -m "not live"
```

To run the live suite, first install the bridge and start a test instance through `vam_instance`, then run:

```powershell
.\scripts\Test.ps1 -Live -GameRoot "C:\VaM"
```

The game directory is shared with other VaM processes. MCP mutations are serialized among MCP clients but cannot lock out unrelated programs. UI tools invoke Unity callbacks rather than performing desktop mouse hit tests. Synchronous code that blocks Unity's main thread cannot be preempted by coroutine timeouts. These tests validate tool behavior and integration; they do not certify the visual quality of plugins being tested.
