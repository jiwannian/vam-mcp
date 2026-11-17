from __future__ import annotations

import asyncio
from functools import wraps
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image

from . import __version__
from .build import api_query, build_plugin, deploy_plugin
from .client import get_client, instances
from .common import GAME, ROOT, RUNTIME, GameLease, VamError, read_json
from .instances import install_bridge, launch, stop
from .jobs import JOBS
from .testing import run_probe, run_test, wait_for

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
mcp = FastMCP("VaM", instructions="Tools for local Virt-A-Mate plugin development and testing. Start a test instance with vam_instance, query real APIs with vam_api, deploy/reload with vam_plugin. Use registered parameters first; use vam_ui_tree and handles for unregistered controls. Handles expire on scene/plugin changes. Capture returns real game pixels. A successful functional test is not a visual quality judgment. All writes target the explicitly selected test instance; game files are shared. Host jobs persist results; use vam_job to poll or cancel.", log_level="WARNING")


def guarded(fn):
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except VamError as exc:
            return exc.payload()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return VamError("invalid_request", str(exc)).payload()
    return wrapper


def game_call(instance_id, operation, arguments, timeout_s=30, write=False):
    def execute():
        client = get_client(instance_id)
        if write:
            with GameLease.hold():
                return {"ok": True, "instance_id": client.instance_id, **client.call(operation, arguments, timeout_s)}
        return {"ok": True, "instance_id": client.instance_id, **client.call(operation, arguments, timeout_s)}
    return execute


@mcp.tool()
@guarded
async def vam_status(instance_id: str = "") -> dict:
    """Inspect installed runtime, game/bridge versions, or a specific live test instance."""
    if instance_id:
        return await asyncio.to_thread(game_call(instance_id, "status", {}))
    return {"ok": True, "version": __version__, "project": str(ROOT), "game_root": str(GAME), "game_exists": (GAME / "VaM.exe").is_file(), "bridge_installed": (GAME / "BepInEx/plugins/VaMMcp/VaMMcp.Bridge.dll").is_file(), "instances": await asyncio.to_thread(instances)}


@mcp.tool()
@guarded
async def vam_instance(action: Literal["list", "start", "connect", "stop", "install"], instance_id: str = "", timeout_s: int = 180, force: bool = False) -> dict:
    """Start an independent hidden desktop VaM using the shared game directory, connect, or stop an owned test instance. Start returns a host job."""
    if action == "list":
        return {"ok": True, "instances": await asyncio.to_thread(instances)}
    if action == "start":
        return JOBS.start("instance_start", lambda event, progress: launch(timeout_s, event))
    if action == "install":
        return await asyncio.to_thread(install_bridge)
    if action == "stop":
        return JOBS.start("instance_stop", lambda event, progress: stop(instance_id, force=force))
    return await asyncio.to_thread(game_call(instance_id, "status", {}))


@mcp.tool()
@guarded
async def vam_api(action: Literal["search", "type"], query: str = "", type_name: str = "", assembly: str = "Assembly-CSharp.dll", offset: int = 0, limit: int = 30) -> dict:
    """Search real local assembly APIs or inspect a type and optional decompiled source. Search is indexed by assembly hash. Offset is item offset for search, source line offset for type."""
    return await asyncio.to_thread(api_query, action, query, type_name, assembly, offset, limit)


@mcp.tool()
@guarded
async def vam_plugin(action: Literal["compile", "deploy", "list", "inspect", "load", "reload", "unload", "open_ui"], instance_id: str = "", atom_uid: str = "", plugin_uid: str = "", entrypoint: str = "", source_root: str = "", package: str = "", script_index: int = 0, timeout_s: int = 30) -> dict:
    """Compile .cs/.cslist with game CLR references, deploy with backups/hash manifest, or use VaM's native plugin lifecycle. Load accepts a game-relative entrypoint. Deploy requires package."""
    if action == "compile":
        return await asyncio.to_thread(build_plugin, entrypoint, source_root)
    if action == "deploy":
        return await asyncio.to_thread(deploy_plugin, entrypoint, package, source_root)
    return await asyncio.to_thread(game_call(instance_id, "plugin", {"action": action, "atom_uid": atom_uid, "plugin_uid": plugin_uid, "entrypoint": entrypoint, "script_index": script_index}, timeout_s, write=action not in {"list", "inspect"}))


@mcp.tool()
@guarded
async def vam_scene(action: Literal["inspect", "new", "load", "save"], instance_id: str, path: str = "", timeout_s: int = 180) -> dict:
    """Inspect or change the test instance's scene. Save uses an explicit path or the instance snapshot directory."""
    return await asyncio.to_thread(game_call(instance_id, "scene", {"action": action, "path": path}, timeout_s, write=action != "inspect"))


@mcp.tool()
@guarded
async def vam_atom(action: Literal["list", "inspect", "create", "clone", "remove", "transform"], instance_id: str, atom_uid: str = "", type: str = "Person", new_uid: str = "", query: str = "", position: list[float] | None = None, rotation: list[float] | None = None, timeout_s: int = 60) -> dict:
    """Query/create/remove atoms or native scene-merge clones; transform accepts world position and Euler rotation. Cloning includes native serialized plugins and appearance."""
    args = {"action": action, "atom_uid": atom_uid, "type": type, "new_uid": new_uid, "query": query}
    if position is not None:
        args["position"] = position
    if rotation is not None:
        args["rotation"] = rotation
    return await asyncio.to_thread(game_call(instance_id, "atom", args, timeout_s, write=action not in {"list", "inspect"}))


@mcp.tool()
@guarded
async def vam_storable(action: Literal["list", "get", "set", "call"], instance_id: str, atom_uid: str, storable_id: str = "", name: str = "", value: Any = None) -> dict:
    """Enumerate/read/write registered JSONStorable parameters and call Actions. Numeric limits and choices are checked. Color values use [H,S,V], vectors [x,y,z]."""
    args = {"action": action, "atom_uid": atom_uid, "storable_id": storable_id, "name": name}
    if value is not None:
        args["value"] = value
    return await asyncio.to_thread(game_call(instance_id, "storable", args, write=action in {"set", "call"}))


@mcp.tool()
@guarded
async def vam_morph(action: Literal["list", "get", "set"], instance_id: str, atom_uid: str, uid: str = "", query: str = "", value: float | None = None, offset: int = 0, limit: int = 100) -> dict:
    """Discover actual Person morph IDs/ranges; read or assign a non-driven, enabled morph within its native bounds."""
    args = {"action": action, "atom_uid": atom_uid, "uid": uid, "query": query, "offset": offset, "limit": limit}
    if value is not None:
        args["value"] = value
    return await asyncio.to_thread(game_call(instance_id, "morph", args, write=action == "set"))


@mcp.tool()
@guarded
async def vam_ui_tree(instance_id: str, root: str = "", atom_uid: str = "", plugin_uid: str = "", query: str = "", control_type: str = "", include_inactive: bool = False, offset: int = 0, limit: int = 250) -> dict:
    """Read Unity uGUI controls/labels/values/bounds and runtime handles. Scope by root handle or plugin, filter by label/type; password values are redacted."""
    return await asyncio.to_thread(game_call(instance_id, "ui_tree", {"root": root, "atom_uid": atom_uid, "plugin_uid": plugin_uid, "query": query, "control_type": control_type, "include_inactive": include_inactive, "offset": offset, "limit": limit}))


@mcp.tool()
@guarded
async def vam_ui_action(action: Literal["inspect", "click", "set", "open"], instance_id: str, handle: str, value: Any = None, submit: bool = True) -> dict:
    """Operate a currently active/interactable Unity control by a fresh tree handle. Input set emits value-changed and optionally end-edit. Popup values are choice strings."""
    args = {"action": action, "handle": handle, "submit": submit}
    if value is not None:
        args["value"] = value
    return await asyncio.to_thread(game_call(instance_id, "ui_action", args, write=action != "inspect"))


@mcp.tool()
@guarded
async def vam_file_dialog(action: Literal["inspect", "select", "cancel"], instance_id: str, path: str = "", handle: str = "") -> dict:
    """Inspect or operate an open VaM native file dialog using its callback, without desktop clicking. Multiple open dialogs require a handle."""
    return await asyncio.to_thread(game_call(instance_id, "file_dialog", {"action": action, "path": path, "handle": handle}, write=action != "inspect"))


@mcp.tool(structured_output=False)
@guarded
async def vam_capture(instance_id: str, mode: Literal["ui", "render"] = "ui", atom_uid: str = "", view: Literal["front", "left", "right", "top", "back"] = "front", width: int = 1024, height: int = 1024, distance: float = 1.0):
    """Return real game framebuffer (ui) or camera render as an MCP image and local PNG. Render can frame an atom's head from five views."""
    result = await asyncio.to_thread(game_call(instance_id, "capture", {"mode": mode, "atom_uid": atom_uid, "view": view, "width": width, "height": height, "distance": distance}, 30))
    return [json.dumps(result, ensure_ascii=False), Image(path=result["path"])]


@mcp.tool()
@guarded
async def vam_logs(instance_id: str, after: str = "0", level: Literal["", "info", "warning", "error"] = "", contains: str = "", limit: int = 200) -> dict:
    """Read cursor-based Unity, BepInEx and VaM internal log events. Save cursor for the next read; full events also persist in the instance directory."""
    return await asyncio.to_thread(game_call(instance_id, "logs", {"after": after, "level": level, "contains": contains, "limit": limit}))


@mcp.tool()
@guarded
async def vam_wait(instance_id: str, condition: dict, timeout_s: int = 30) -> dict:
    """Wait for ready/parameter/ui/file condition. Returns a host job; parameter supports equals or expect, file supports json_expect assertions."""
    client = await asyncio.to_thread(get_client, instance_id)
    return JOBS.start("wait", lambda event, progress: wait_for(client, condition, timeout_s, event))


@mcp.tool()
@guarded
async def vam_probe(instance_id: str, code: str, arguments: dict | None = None, atom_uid: str = "", timeout_s: int = 60) -> dict:
    """Run a C# IEnumerator diagnostic body on the Unity main thread with args/result JSONClass and optional containingAtom. Supports reflection/yield and finally cleanup. Assembly remains until process exit; returns a host job."""
    client = await asyncio.to_thread(get_client, instance_id)
    return JOBS.start("probe", lambda event, progress: run_probe(client, code, arguments or {}, atom_uid, timeout_s, event))


@mcp.tool()
@guarded
async def vam_test(instance_id: str, spec: dict | None = None, spec_path: str = "", timeout_s: int = 600) -> dict:
    """Run serial test steps, $ref substitutions, waits and result assertions under the shared-game lease. Reports, logs and failure screenshot are saved. Returns a host job."""
    client = await asyncio.to_thread(get_client, instance_id)
    if spec is None:
        if not spec_path:
            raise VamError("invalid_test", "Supply spec or spec_path")
        spec = await asyncio.to_thread(read_json, Path(spec_path))
    return JOBS.start("test", lambda event, progress: run_test(client, spec, timeout_s, event, progress))


@mcp.tool()
@guarded
async def vam_job(action: Literal["get", "wait", "cancel"], job_id: str, instance_id: str = "", timeout_s: int = 20) -> dict:
    """Inspect/wait/cancel host_ jobs or game_ bridge jobs. Waiting is bounded to 45 seconds; cancellation preserves already-completed effects."""
    if job_id.startswith("host_"):
        if action == "wait":
            return await asyncio.to_thread(JOBS.wait, job_id, timeout_s)
        return JOBS.get(job_id, cancel=action == "cancel")
    client = await asyncio.to_thread(get_client, instance_id)
    if action == "wait":
        deadline = time.monotonic() + max(0, min(45, timeout_s))
        while True:
            value = await asyncio.to_thread(client.job, job_id)
            if value["state"] not in {"queued", "running"} or time.monotonic() >= deadline:
                return value
            await asyncio.sleep(.1)
    return await asyncio.to_thread(client.job, job_id, action == "cancel")


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
