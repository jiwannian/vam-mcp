from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path

from .common import RUNTIME, VamError, atomic_json, process_identity, read_json


BOOL_KEYS = {"ok", "ready", "is_loading", "run_in_background", "cancel_requested", "active", "interactable", "secret", "has_value", "enabled", "ui_available", "ui_active", "disabled", "driven", "directory", "exists"}
INT_KEYS = {"protocol", "pid", "port", "queued", "scene_revision", "ui_revision", "atom_count", "screen_width", "screen_height", "character_limit", "width", "height", "total"}
FLOAT_KEYS = {"min", "max", "x", "y", "distance"}


def normalize(value, key="", parent=None):
    """VaM's bundled SimpleJSON serializes every scalar as a JSON string."""
    if isinstance(value, dict):
        return {k: normalize(v, k, value) for k, v in value.items()}
    if isinstance(value, list):
        if key in {"position", "rotation", "bounds"} or (key == "value" and parent and parent.get("type") in {"Vector3", "Color"}):
            return [float(x) for x in value]
        return [normalize(v) for v in value]
    if key in BOOL_KEYS:
        return value is True or str(value).lower() == "true"
    if key in {"width", "height"} and parent and "x" in parent and "y" in parent:
        return float(value)
    if key in INT_KEYS:
        return int(value)
    if key in FLOAT_KEYS:
        return float(value)
    if key == "value" and parent:
        if parent.get("type") == "Bool" or parent.get("control_type") == "toggle":
            return value is True or str(value).lower() == "true"
        if parent.get("type") == "Float" or parent.get("control_type") == "slider" or ("min" in parent and "max" in parent):
            return float(value)
        if parent.get("control_type") == "dropdown":
            return int(value)
    return value


class BridgeClient:
    def __init__(self, descriptor: dict):
        descriptor = normalize(descriptor)
        self.descriptor = descriptor
        self.instance_id = descriptor["instance_id"]
        if descriptor.get("protocol") != 1 or not 1 <= int(descriptor["port"]) <= 65535:
            raise VamError("protocol_mismatch", "Invalid bridge descriptor")

    def request(self, method: str, params: dict | None = None):
        request = {"protocol": 1, "id": uuid.uuid4().hex, "token": self.descriptor["token"], "method": method, "params": params or {}}
        data = json.dumps(request, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > 2 * 1024 * 1024:
            raise VamError("frame_too_large", "Bridge request exceeds 2 MiB")
        try:
            with socket.create_connection(("127.0.0.1", self.descriptor["port"]), timeout=5) as sock:
                sock.settimeout(8)
                sock.sendall(data)
                with sock.makefile("rb") as stream:
                    line = stream.readline(32 * 1024 * 1024 + 1)
                    if not line.endswith(b"\n"):
                        raise VamError("invalid_response", "Incomplete or oversized bridge response")
                    try:
                        response = normalize(json.loads(line))
                    except (ValueError, TypeError) as exc:
                        raise VamError("invalid_response", "Invalid bridge JSON or scalar type") from exc
        except OSError as exc:
            raise VamError("disconnected", "Bridge connection interrupted; no operation was replayed", transport_method=method, instance_id=self.instance_id) from exc
        if response.get("instance_id") != self.instance_id or response.get("id") != request["id"]:
            raise VamError("instance_changed", "Bridge response identity does not match", expected_instance_id=self.instance_id, response_instance_id=response.get("instance_id"), expected_request_id=request["id"], response_request_id=response.get("id"))
        if not response.get("ok"):
            e = response.get("error", {})
            raise VamError(e.get("code", "bridge_error"), e.get("message", "Bridge request failed"))
        if "result" not in response:
            raise VamError("invalid_response", "Bridge response is missing its result", response_keys=sorted(response))
        return response["result"]

    def submit(self, operation: str, arguments: dict, timeout_s: int = 30, request_key: str | None = None):
        key = request_key or uuid.uuid4().hex
        request_path = RUNTIME / "requests" / (key + ".json")
        atomic_json(request_path, {"instance_id": self.instance_id, "operation": operation, "request_key": key, "state": "submitting"})
        try:
            result = self.request("submit", {"request_key": key, "operation": operation, "arguments": arguments, "timeout_s": timeout_s})
        except VamError as exc:
            exc.details["request_key"] = key
            raise
        atomic_json(request_path, {"instance_id": self.instance_id, "operation": operation, "request_key": key, "job_id": result["job_id"], "state": result["state"]})
        return result

    def job(self, job_id: str, cancel: bool = False):
        method = "cancel" if cancel else "job"
        for attempt in range(3):
            try:
                return self.request(method, {"job_id": job_id})
            except VamError as exc:
                if exc.code != "instance_changed" or attempt == 2:
                    raise
                time.sleep(.02 * (attempt + 1))

    def call(self, operation: str, arguments: dict | None = None, timeout_s: int = 30, cancel_event=None, on_submit=None):
        result = self.submit(operation, arguments or {}, timeout_s)
        job_id = result["job_id"]
        if on_submit:
            on_submit(job_id)
        deadline = time.monotonic() + timeout_s + 15
        cancel_sent = False
        while result["state"] in {"queued", "running"}:
            if cancel_event is not None and cancel_event.is_set() and not cancel_sent:
                self.job(job_id, cancel=True)
                cancel_sent = True
            if time.monotonic() > deadline:
                raise VamError("host_timeout", "Bridge job is still pending; inspect it before further writes", job_id=job_id, instance_id=self.instance_id)
            time.sleep(.1)
            result = self.job(job_id)
        if result["state"] != "succeeded":
            error = result.get("error", {})
            raise VamError(error.get("code", result["state"]), error.get("message", "Game job failed"), job_id=job_id, instance_id=self.instance_id)
        return result.get("result", {})


def instances(runtime: Path = RUNTIME, *, include_offline: bool = True):
    result = []
    for folder in sorted((runtime / "instances").glob("*")):
        launch_path, descriptor_path = folder / "launch.json", folder / "bridge.json"
        if not launch_path.exists():
            continue
        try:
            launch = read_json(launch_path)
            alive = process_identity(launch["pid"]) == launch["process_identity"]
            item = {k: v for k, v in launch.items() if k != "token"}
            item.update({"alive": alive, "connected": False})
            if alive and descriptor_path.exists():
                descriptor = normalize(read_json(descriptor_path))
                if descriptor["pid"] == launch["pid"] and descriptor["instance_id"] == launch["instance_id"]:
                    try:
                        item["bridge"] = BridgeClient(descriptor).request("ping")
                        item["connected"] = True
                    except VamError as exc:
                        item["connection_error"] = exc.code
            if include_offline or alive:
                result.append(item)
        except (OSError, ValueError, KeyError):
            continue
    return result


def get_client(instance_id: str = ""):
    active = instances(include_offline=False)
    if instance_id:
        match = next((i for i in active if i["instance_id"] == instance_id), None)
    elif len(active) == 1:
        match = active[0]
    else:
        raise VamError("ambiguous_instance" if active else "not_connected", "Specify instance_id" if active else "Start a bridge-enabled test instance first")
    if not match:
        raise VamError("not_found", "Requested instance is not alive")
    if not match["connected"]:
        raise VamError("not_connected", "Requested instance has no live bridge")
    descriptor = read_json(Path(match["instance_folder"]) / "bridge.json")
    return BridgeClient(descriptor)
