from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .build import COMPILER
from .common import GAME


PLUGIN_PROFILES = {
    "mvrscript": {
        "id": "mvrscript",
        "status": "supported",
        "source_formats": [".cs", ".cslist"],
        "runtime": "VaM Mono / game-shipped CLR references",
        "compiler": {"kind": "VAM_CSC", "language_version": 5, "debug_symbols": True},
        "references": ["VaM_Data/Managed", "UnityEngine modules", "explicit reference_paths"],
        "artifact_formats": ["source files", "configuration", "assets"],
        "deployment": "Custom/Scripts/<package>",
        "lifecycle": "VaM MVRPluginManager load/reload/unload on an atom",
        "validation": ["compile", "native load", "JSONStorable", "uGUI", "capture", "logs", "probe"],
        "ui_support": ["uGUI controls", "screenshots", "C# diagnostics for custom UI"],
    },
    "bepinex": {
        "id": "bepinex",
        "status": "supported",
        "source_formats": [".cs", ".cslist"],
        "runtime": "BepInEx 5 / VaM Mono",
        "compiler": {"kind": "VAM_CSC", "language_version": 5, "debug_symbols": True},
        "references": ["VaM_Data/Managed", "BepInEx/core", "explicit reference_paths"],
        "artifact_formats": [".dll", ".pdb", "dependency DLLs", "configuration", "assets"],
        "deployment": "BepInEx/plugins/<package>",
        "lifecycle": "Loaded at VaM process startup; restart the owned test instance to load updates",
        "validation": ["compile", "deployment manifest", "restart", "Chainloader plugin metadata", "BepInEx logs", "capture", "probe"],
        "ui_support": ["screenshots", "C# diagnostics", "uGUI when exposed by the plugin"],
    },
    "var_bundle": {
        "id": "var_bundle",
        "status": "supported_prebuilt",
        "source_formats": [".var"],
        "runtime": "VaM AddonPackages / packaged plugin entrypoint",
        "compiler": {"kind": "prebuilt_archive", "language_version": None, "debug_symbols": False},
        "references": [],
        "artifact_formats": ["prebuilt .var archive"],
        "deployment": "AddonPackages/<archive>.var",
        "lifecycle": "Load a packaged MVRScript entrypoint through VaM's native plugin manager",
        "validation": ["archive structure", "deployment manifest", "native load", "capture", "logs", "probe"],
        "ui_support": ["uGUI controls", "screenshots", "C# diagnostics for custom UI"],
        "limitations": ["The MCP installs and validates prebuilt archives; it does not create VAR archives."],
    },
}


def plugin_capabilities(profile: str = "", game: Path = GAME, compiler: Path = COMPILER, live_instance_available=False) -> dict:
    if profile:
        if profile not in PLUGIN_PROFILES:
            from .common import VamError

            raise VamError("unsupported_profile", f"Unknown plugin profile: {profile}", available=sorted(PLUGIN_PROFILES))
        selected = {profile: PLUGIN_PROFILES[profile]}
    else:
        selected = PLUGIN_PROFILES
    profiles = deepcopy(selected)
    has_game = (game / "VaM.exe").is_file()
    has_compiler = compiler.is_file()
    has_mvr_refs = (game / "VaM_Data/Managed/Assembly-CSharp.dll").is_file()
    has_bepinex = all((game / path).is_file() for path in ("BepInEx/core/BepInEx.dll", "BepInEx/core/0Harmony.dll"))
    bridge_installed = (game / "BepInEx/plugins/VaMMcp/VaMMcp.Bridge.dll").is_file()
    for key, value in profiles.items():
        missing = []
        if key != "var_bundle":
            if not has_game:
                missing.append("VaM.exe")
            if not has_compiler:
                missing.append("VAM_CSC")
            if not has_mvr_refs:
                missing.append("VaM_Data/Managed/Assembly-CSharp.dll")
        if key == "bepinex" and not has_bepinex:
            missing.append("BepInEx/core/BepInEx.dll and 0Harmony.dll")
        value["available"] = not missing
        value["missing_requirements"] = missing
        value["live_validation_ready"] = has_game and bridge_installed and live_instance_available
    return {"ok": True, "game_root": str(game), "compiler": str(compiler), "bridge_installed": bridge_installed, "live_instance_available": live_instance_available, "profiles": profiles}
