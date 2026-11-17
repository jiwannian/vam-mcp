from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

ROOT = Path(__file__).resolve().parent.parent
GAME = Path(os.environ.get("VAM_GAME_ROOT", str(ROOT)))
RUNTIME = ROOT / "runtime"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


class VamError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details

    def payload(self):
        return {"ok": False, "error": {"code": self.code, "message": str(self), **self.details}}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def hidden_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def process_identity(pid: int) -> int | None:
    """Return the Windows creation FILETIME; PID reuse must never select another game."""
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def terminate_owned(pid: int, identity: int):
    if process_identity(pid) != identity:
        raise VamError("instance_changed", "Process identity no longer matches the launched test instance")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000 | 1, False, pid)
    if not handle:
        raise VamError("process_unavailable", "Cannot open the owned test process")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)) or (times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) != identity:
            raise VamError("instance_changed", "Opened process identity does not match the owned test instance")
        if not kernel.TerminateProcess(handle, 1):
            raise VamError("process_unavailable", "Cannot terminate the owned test process")
    finally:
        kernel.CloseHandle(handle)


class GameLease:
    """A reentrant, cross-process lease for mutations in the shared game directory."""
    _local = threading.local()

    @classmethod
    @contextlib.contextmanager
    def hold(cls, runtime: Path = RUNTIME):
        depth = getattr(cls._local, "depth", 0)
        if depth:
            cls._local.depth = depth + 1
            try:
                yield
            finally:
                cls._local.depth -= 1
            return
        import msvcrt
        runtime.mkdir(parents=True, exist_ok=True)
        f = (runtime / "game-write.lock").open("a+b")
        f.seek(0, 2)
        if not f.tell():
            f.write(b"\0")
            f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            f.close()
            raise VamError("busy", "Another VaM MCP session owns the shared game write lease") from exc
        cls._local.depth = 1
        try:
            yield
        finally:
            cls._local.depth = 0
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            f.close()
