from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .build import build_bridge
from .client import BridgeClient, get_client, instances
from .common import ROOT, RUNTIME, GAME, VamError, GameLease, atomic_json, hidden_options, process_identity, read_json, sha256, terminate_owned


def launch_game_process(args: list[str]) -> int:
    """WMI owns the game so a Windows MCP client job cannot kill it on reconnect."""
    script = r'''
$ErrorActionPreference = 'Stop'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = [string]$request.command
    CurrentDirectory = [string]$request.directory
    ProcessStartupInformation = $startup
}
if ($created.ReturnValue -ne 0) { throw ('Win32_Process.Create failed: ' + $created.ReturnValue) }
[Console]::Out.Write($created.ProcessId)
'''
    request = {"command": subprocess.list2cmdline(args), "directory": str(GAME)}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(request), capture_output=True, encoding="utf-8", errors="replace", timeout=30, **hidden_options())
    if result.returncode or not result.stdout.strip().isdigit():
        raise VamError("launch_failed", "Could not start independent VaM process: " + result.stderr.strip())
    return int(result.stdout.strip())


def install_bridge():
    with GameLease.hold():
        artifact = build_bridge()
        source = Path(artifact["output"])
        target = GAME / "BepInEx/plugins/VaMMcp/VaMMcp.Bridge.dll"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha256(target) == sha256(source):
            return {"ok": True, "path": str(target), "changed": False, "sha256": sha256(source)}
        if instances(include_offline=False):
            raise VamError("bridge_in_use", "Stop bridge-enabled test instances before updating the bridge DLL")
        if target.exists():
            backup = RUNTIME / "backups" / ("bridge_" + uuid.uuid4().hex + ".dll")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        temp = target.with_suffix(".dll.tmp")
        shutil.copy2(source, temp)
        temp.replace(target)
        return {"ok": True, "path": str(target), "changed": True, "sha256": sha256(target)}


def base_scene():
    candidates = [GAME / "Saves/scene/default.json", GAME / "Saves/scene/newScene.json"]
    for path in candidates:
        if not path.exists():
            continue
        data = read_json(path)
        if any(a.get("type") == "Person" for a in data.get("atoms", [])):
            for atom in data["atoms"]:
                atom["storables"] = [s for s in atom.get("storables", []) if s.get("id") not in {"Plugins", "PluginManager"} and not s.get("id", "").startswith("plugin#")]
            return data, str(path)
    return {"atoms": []}, "minimal"


def _start_owned(launch_info: dict, timeout_s: int, cancel_event=None):
    instance_id = launch_info["instance_id"]
    folder, saves = Path(launch_info["instance_folder"]), Path(launch_info["saves_dir"])
    descriptor_path = folder / "bridge.json"
    descriptor_path.unlink(missing_ok=True)
    args = [str(GAME / "VaM.exe"), "-vrmode", "None", "-savesdir", str(saves), "-logFile", str(folder / "unity.log"), "-screen-fullscreen", "0", "-screen-width", "1280", "-screen-height", "800", "-vam-mcp", str(folder), "-vam-mcp-id", instance_id]
    pid = launch_game_process(args)
    identity = process_identity(pid)
    if identity is None:
        raise VamError("launch_failed", "VaM exited immediately")
    launch_info.update(pid=pid, process_identity=identity, launched_at=time.time())
    atomic_json(folder / "launch.json", launch_info)
    deadline = time.monotonic() + min(max(timeout_s, 10), 600)
    client = None
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            terminate_owned(pid, identity)
            raise VamError("cancelled", "Instance launch cancelled")
        if process_identity(pid) != identity:
            raise VamError("launch_failed", "VaM exited before bridge startup", log_path=str(folder / "unity.log"))
        if descriptor_path.exists():
            try:
                client = BridgeClient(read_json(descriptor_path))
                status = client.call("status", timeout_s=5)
                if status["game_version"] and status["ready"]:
                    break
            except (VamError, ValueError, OSError):
                pass
        time.sleep(.5)
    else:
        raise VamError("startup_timeout", "VaM did not become ready; instance was left running for diagnosis", instance_id=instance_id, log_path=str(folder / "unity.log"))
    status = client.call("scene", {"action": "load", "path": str(saves / "scene/default.json")}, timeout_s=min(max(timeout_s, 30), 600), cancel_event=cancel_event)
    atomic_json(folder / "ready.json", status)
    return {"ok": True, **launch_info, "status": status}


def launch(timeout_s: int = 180, cancel_event=None):
    install_bridge()
    with GameLease.hold():
        instance_id = "vam_" + uuid.uuid4().hex[:16]
        folder = RUNTIME / "instances" / instance_id
        saves = GAME / "Saves/VaMMcp" / instance_id
        (saves / "scene").mkdir(parents=True, exist_ok=True)
        folder.mkdir(parents=True)
        scene, origin = base_scene()
        atomic_json(saves / "scene/default.json", scene)
        launch_info = {"instance_id": instance_id, "instance_folder": str(folder), "game_root": str(GAME), "saves_dir": str(saves), "scene_origin": origin, "mode": "shared_game_directory", "launcher": "windows_management", "python": sys.executable}
        return _start_owned(launch_info, timeout_s, cancel_event)


def restart(instance_id: str, timeout_s: int = 180, force: bool = False, cancel_event=None):
    active = instances(include_offline=False)
    selected = next((item for item in active if item["instance_id"] == instance_id), None)
    if not selected:
        raise VamError("not_found", "Specify an owned, live test instance")
    launch_info = read_json(Path(selected["instance_folder"]) / "launch.json")
    stop(instance_id, force=force, cancel_event=cancel_event)
    old_pid, old_identity = launch_info["pid"], launch_info["process_identity"]
    exit_deadline = time.monotonic() + 20
    while process_identity(old_pid) == old_identity and time.monotonic() < exit_deadline:
        time.sleep(.2)
    if process_identity(old_pid) == old_identity:
        raise VamError("shutdown_timeout", "Previous VaM process is still exiting; restart was not launched", instance_id=instance_id)
    if cancel_event is not None and cancel_event.is_set():
        raise VamError("cancelled", "Instance restart cancelled before relaunch")
    with GameLease.hold():
        launch_info["previous_process_identity"] = launch_info.get("process_identity")
        launch_info["restarted_at"] = time.time()
        return _start_owned(launch_info, timeout_s, cancel_event)


def stop(instance_id: str, *, force: bool = False, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise VamError("cancelled", "Instance stop cancelled before shutdown began")
    active = instances(include_offline=False)
    selected = next((i for i in active if i["instance_id"] == instance_id), None) if instance_id else active[0] if len(active) == 1 else None
    if not selected:
        raise VamError("not_found", "Specify an owned, live test instance")
    launch_info = read_json(Path(selected["instance_folder"]) / "launch.json")
    pid, identity = launch_info["pid"], launch_info["process_identity"]
    if force:
        if cancel_event is not None and cancel_event.is_set():
            raise VamError("cancelled", "Instance stop cancelled before force termination")
        terminate_owned(pid, identity)
    else:
        client = get_client(selected["instance_id"])
        if cancel_event is not None and cancel_event.is_set():
            raise VamError("cancelled", "Instance stop cancelled before shutdown request")
        try:
            client.submit("quit", {}, timeout_s=10)
        except VamError as exc:
            if exc.code != "disconnected":
                raise
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process_identity(pid) == identity:
            time.sleep(.2)
        if process_identity(pid) == identity:
            raise VamError("shutdown_timeout", "Graceful shutdown did not finish; force stop is available", instance_id=instance_id)
    return {"ok": True, "instance_id": selected["instance_id"], "stopped": True}
