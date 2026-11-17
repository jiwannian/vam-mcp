from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from .common import RUNTIME, TERMINAL, VamError, atomic_json, read_json


class HostJobs:
    def __init__(self, runtime: Path = RUNTIME):
        self.root = runtime / "jobs"
        self.jobs, self.events = {}, {}
        self.lock = threading.RLock()

    def start(self, kind, function):
        job_id = "host_" + uuid.uuid4().hex
        event = threading.Event()
        with self.lock:
            self.jobs[job_id] = {"job_id": job_id, "kind": kind, "state": "queued", "created_at": time.time()}
            self.events[job_id] = event
            self._save(job_id)

        def run():
            self.update(job_id, state="running")
            try:
                result = function(event, lambda **fields: self.update(job_id, **fields))
                self.update(job_id, state="succeeded", result=result, completed_at=time.time())
            except VamError as exc:
                self.update(job_id, state="cancelled" if exc.code == "cancelled" else "timed_out" if "timeout" in exc.code else "failed", error=exc.payload()["error"], completed_at=time.time())
            except Exception as exc:
                self.update(job_id, state="failed", error={"code": "host_error", "message": str(exc)}, completed_at=time.time())

        threading.Thread(target=run, name=kind + ":" + job_id, daemon=True).start()
        return self.get(job_id)

    def _save(self, job_id):
        atomic_json(self.root / (job_id + ".json"), self.jobs[job_id])

    def update(self, job_id, **fields):
        with self.lock:
            self.jobs[job_id].update(fields)
            self._save(job_id)

    def get(self, job_id, cancel=False):
        if not job_id.startswith("host_") or any(c not in "host_0123456789abcdef" for c in job_id):
            raise VamError("not_found", "Unknown host job")
        with self.lock:
            if job_id not in self.jobs:
                path = self.root / (job_id + ".json")
                if not path.exists():
                    raise VamError("not_found", "Unknown host job")
                value = read_json(path)
                if value["state"] not in TERMINAL:
                    value.update(state="interrupted", error={"code": "host_restarted", "message": "Owning MCP host stopped; inspect the game before retrying"})
                return value
            if cancel and self.jobs[job_id]["state"] not in TERMINAL:
                self.events[job_id].set()
                self.jobs[job_id]["cancel_requested"] = True
                self._save(job_id)
            return dict(self.jobs[job_id])

    def wait(self, job_id, timeout_s=20):
        deadline = time.monotonic() + max(0, min(timeout_s, 45))
        while True:
            value = self.get(job_id)
            if value["state"] in TERMINAL or time.monotonic() >= deadline:
                return value
            time.sleep(.1)


JOBS = HostJobs()
