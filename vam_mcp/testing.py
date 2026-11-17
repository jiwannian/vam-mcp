from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import time
import uuid

from .build import build_plugin, deploy_plugin, compile_sources
from .client import BridgeClient
from .common import GAME, ROOT, RUNTIME, GameLease, VamError, atomic_json, read_json, sha256


def dig(value, path):
    if path in {"", "$"}:
        return value
    current = value
    for part in path.removeprefix("$.").split("."):
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise VamError("missing_path", f"JSON path does not exist: {path}") from None
    return current


def assert_expectations(result, expectations):
    for expectation in expectations:
        path = expectation.get("path", "$")
        try:
            actual = dig(result, path)
        except VamError:
            if expectation.get("exists") is False:
                continue
            raise
        if expectation.get("exists") is False:
            raise VamError("assertion_failed", f"Expected {path} to be absent")
        if "equals" in expectation and actual != expectation["equals"]:
            raise VamError("assertion_failed", f"Unexpected value at {path}", expected=expectation["equals"], actual=actual)
        if "contains" in expectation and expectation["contains"] not in actual:
            raise VamError("assertion_failed", f"Expected containment at {path}")
        if "approx" in expectation and not math.isclose(float(actual), float(expectation["approx"]), abs_tol=float(expectation.get("tolerance", 1e-5)), rel_tol=0):
            raise VamError("assertion_failed", f"Value differs outside tolerance at {path}", actual=actual)
        if "min" in expectation and actual < expectation["min"]:
            raise VamError("assertion_failed", f"Value below minimum at {path}", actual=actual)
        if "max" in expectation and actual > expectation["max"]:
            raise VamError("assertion_failed", f"Value above maximum at {path}", actual=actual)


def resolve_refs(value, context):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return copy.deepcopy(dig(context, value["$ref"]))
        return {k: resolve_refs(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, context) for v in value]
    return value


def wait_for(client: BridgeClient, condition: dict, timeout_s=30, cancel_event=None):
    kind = condition.get("kind", "ready")
    deadline = time.monotonic() + max(1, min(timeout_s, 3600))
    last = None
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise VamError("cancelled", "Wait cancelled")
        if kind == "ready":
            last = client.call("status", timeout_s=10)
            if last.get("ready"):
                return last
        elif kind == "parameter":
            last = client.call("storable", {"action": "get", **{k: condition[k] for k in ("atom_uid", "storable_id", "name")}}, timeout_s=10)
            try:
                assert_expectations(last, [{"path": "value", **condition.get("expect", {"equals": condition.get("equals")})}])
                return last
            except VamError:
                pass
        elif kind == "ui":
            last = client.call("ui_tree", {k: v for k, v in condition.items() if k not in {"kind", "expect", "absent"}}, timeout_s=10)
            if (last["total"] == 0) == bool(condition.get("absent")):
                return last
        elif kind == "file":
            path = Path(condition["path"])
            if path.is_file():
                last = {"path": str(path), "size": path.stat().st_size}
                if condition.get("json_expect"):
                    try:
                        data = read_json(path)
                        assert_expectations(data, condition["json_expect"])
                        return {**last, "json": data}
                    except (VamError, ValueError, OSError):
                        pass
                else:
                    return last
        else:
            raise VamError("unsupported", "Wait kind must be ready, parameter, ui or file")
        if cancel_event is not None:
            cancel_event.wait(.25)
        else:
            time.sleep(.25)
    raise VamError("condition_timeout", "Wait condition was not satisfied", last=last)


def run_probe(client: BridgeClient, code: str, arguments: dict, atom_uid: str = "", timeout_s: int = 60, cancel_event=None):
    if not code.strip():
        raise VamError("invalid_value", "Probe C# body is empty")
    with GameLease.hold(RUNTIME):
        run_id = uuid.uuid4().hex
        folder = RUNTIME / "probes" / run_id
        folder.mkdir(parents=True)
        namespace = "VaMMcpDiagnostic_" + run_id
        template = '''using System; using System.Collections; using System.Collections.Generic; using System.Reflection;
using UnityEngine; using SimpleJSON;
namespace NAMESPACE { public static class Diagnostic {
public static Atom GetAtomById(string uid) { return SuperController.singleton.GetAtomByUid(uid); }
public static IEnumerator Execute(JSONClass args, JSONClass result, Atom containingAtom) {
CODE
yield break;
}}}'''.replace("NAMESPACE", namespace).replace("CODE", code)
        source = folder / "Probe.cs"
        source.write_text(template, encoding="utf-8")
        assembly = folder / ("Probe_" + run_id + ".dll")
        atomic_json(folder / "request.json", {"atom_uid": atom_uid, "arguments": arguments, "code_sha256": sha256(source)})
        compile_sources([source], assembly)
        try:
            result = client.call("probe", {"assembly_path": str(assembly), "type_name": namespace + ".Diagnostic", "arguments": arguments, "atom_uid": atom_uid}, timeout_s, cancel_event)
            response = {"ok": True, "probe_id": run_id, "result": result, "directory": str(folder), "source": str(source), "assembly_lifetime": "until_test_process_exit"}
            atomic_json(folder / "result.json", response)
            return response
        except VamError as error:
            atomic_json(folder / "result.json", error.payload())
            error.details["probe_directory"] = str(folder)
            raise


ALLOWED_TEST_OPERATIONS = {"status", "scene", "atom", "plugin", "bepinex", "storable", "morph", "ui_tree", "ui_action", "file_dialog", "capture", "logs"}


def run_test(client: BridgeClient, spec: dict, timeout_s=600, cancel_event=None, progress=None):
    if not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise VamError("invalid_test", "Test requires a non-empty steps list")
    if len(spec["steps"]) > 200:
        raise VamError("invalid_test", "A test may contain at most 200 steps")
    ids = [s.get("id", str(i)) for i, s in enumerate(spec["steps"])]
    if len(ids) != len(set(ids)):
        raise VamError("invalid_test", "Step ids must be unique")
    folder = RUNTIME / "runs" / ("run_" + uuid.uuid4().hex)
    report = {"name": spec.get("name", "VaM test"), "instance_id": client.instance_id, "state": "running", "directory": str(folder), "steps": [], "started_at": time.time()}
    context = {"instance_id": client.instance_id, "run_directory": str(folder), "steps": {}}
    deadline = time.monotonic() + max(1, min(timeout_s, 3600))
    with GameLease.hold(RUNTIME):
        atomic_json(folder / "spec.json", spec)
        log_start = client.call("logs", {"limit": 1})["latest_sequence"]
        failure = None
        try:
            for index, step in enumerate(spec["steps"]):
                if cancel_event is not None and cancel_event.is_set():
                    raise VamError("cancelled", "Test cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VamError("test_timeout", "Overall test deadline exceeded")
                if progress:
                    progress(step_id=ids[index], step_index=index, total_steps=len(ids), report=str(folder / "report.json"))
                operation = step["operation"]
                arguments = resolve_refs(step.get("arguments", {}), context)
                step_timeout = max(1, min(int(step.get("timeout_s", 30)), int(remaining)))
                entry = {"id": ids[index], "operation": operation, "started_at": time.time()}
                report["steps"].append(entry)
                try:
                    if operation in ALLOWED_TEST_OPERATIONS:
                        value = client.call(operation, arguments, step_timeout, cancel_event)
                    elif operation == "wait":
                        value = wait_for(client, arguments, step_timeout, cancel_event)
                    elif operation == "compile":
                        value = build_plugin(**arguments)
                    elif operation == "deploy":
                        value = deploy_plugin(**arguments)
                    elif operation == "probe":
                        value = run_probe(client, **arguments, timeout_s=step_timeout, cancel_event=cancel_event)
                    else:
                        raise VamError("unsupported", "Unsupported test operation: " + operation)
                    assert_expectations(value, step.get("expect", []))
                    entry.update(state="succeeded", result=value, completed_at=time.time())
                    context["steps"][ids[index]] = value
                except Exception as error:
                    exc = error if isinstance(error, VamError) else VamError("test_execution_error", str(error))
                    entry.update(state="failed", error=exc.payload()["error"], completed_at=time.time())
                    raise exc
                finally:
                    atomic_json(folder / "report.json", report)
            report["state"] = "succeeded"
        except Exception as error:
            exc = error if isinstance(error, VamError) else VamError("test_execution_error", str(error))
            failure = exc
            report.update(state="cancelled" if exc.code == "cancelled" else "failed", error=exc.payload()["error"])
            try:
                report["failure_capture"] = client.call("capture", {"mode": "ui"}, 10)
            except VamError as capture_error:
                report["capture_error"] = capture_error.payload()["error"]
        finally:
            report["cleanup"] = []
            for cleanup in spec.get("cleanup", []):
                try:
                    operation = cleanup["operation"]
                    if operation not in ALLOWED_TEST_OPERATIONS:
                        raise VamError("unsupported", "Cleanup supports native game operations only")
                    arguments = resolve_refs(cleanup.get("arguments", {}), context)
                    value = client.call(operation, arguments, min(30, int(cleanup.get("timeout_s", 15))))
                    report["cleanup"].append({"operation": operation, "state": "succeeded", "result": value})
                except Exception as cleanup_error:
                    payload = cleanup_error.payload()["error"] if isinstance(cleanup_error, VamError) else {"code": "invalid_cleanup", "message": str(cleanup_error)}
                    report["cleanup"].append({"operation": cleanup.get("operation", ""), "state": "failed", "error": payload})
                    if failure is None:
                        failure = VamError("cleanup_failed", "Test cleanup failed; inspect the recorded state")
                        report.update(state="failed", error=failure.payload()["error"])
            try:
                report["logs"] = client.call("logs", {"after": log_start, "limit": 1000}, timeout_s=10)
            except VamError as log_error:
                report["log_error"] = log_error.payload()["error"]
            report["completed_at"] = time.time()
            atomic_json(folder / "report.json", report)
        if failure:
            failure.details["report"] = str(folder / "report.json")
            raise failure
        return {"ok": True, **report}
