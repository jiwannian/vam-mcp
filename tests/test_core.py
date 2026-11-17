import asyncio
import json
from pathlib import Path
import socket
import threading
import time

import pytest

from vam_mcp import build
from vam_mcp.client import BridgeClient, normalize
from vam_mcp.common import VamError
from vam_mcp.jobs import HostJobs
from vam_mcp.testing import assert_expectations, dig, resolve_refs


def test_native_scalar_normalization_preserves_strings():
    native = {"ok": "true", "ready": "false", "port": "55000", "name": "123", "scene": "false", "parameters": [{"type": "Float", "value": "0.25", "min": "0", "max": "1"}, {"type": "String", "value": "42"}, {"type": "Bool", "value": "false"}]}
    actual = normalize(native)
    assert actual["ok"] is True and actual["ready"] is False
    assert actual["port"] == 55000 and actual["name"] == "123" and actual["scene"] == "false"
    assert [p["value"] for p in actual["parameters"]] == [.25, "42", False]
    assert normalize({"bounds": {"x": "1.2", "y": "3.4", "width": "97.2", "height": "23.1"}})["bounds"]["width"] == 97.2


@pytest.mark.parametrize("native,expected", [({"control_type": "input", "value": "00012"}, "00012"), ({"control_type": "toggle", "value": "true"}, True), ({"control_type": "slider", "value": "1.25"}, 1.25), ({"uid": "morph", "value": "0.4", "min": "0", "max": "1"}, .4)])
def test_ui_and_morph_scalar_types(native, expected):
    assert normalize(native)["value"] == expected


def test_nested_manifest_resolves_once(tmp_path):
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts/a.cs").write_text("class A {}")
    (tmp_path / "parts/nested.cslist").write_text("a.cs\na.cs\n")
    (tmp_path / "entry.cslist").write_text("// comment\nparts/nested.cslist\n")
    entry, root, files, sources = build.source_manifest(tmp_path / "entry.cslist")
    assert sources == [tmp_path / "parts/a.cs"]
    assert len(files) == 3 and root == tmp_path


def test_build_plugin_resolves_relative_entrypoints_from_project_root(monkeypatch):
    from vam_mcp import build

    source = build.ROOT / "examples/DevelopmentFixture.cslist"
    monkeypatch.setattr(build, "compile_sources", lambda sources, output, **kwargs: {"ok": True, "output": str(output)})
    result = build.build_plugin("examples/DevelopmentFixture.cslist")
    assert result["entrypoint"] == str(source.resolve())


def test_manifest_rejects_cycle(tmp_path):
    (tmp_path / "a.cslist").write_text("b.cslist")
    (tmp_path / "b.cslist").write_text("a.cslist")
    with pytest.raises(VamError, match="Cyclic"):
        build.source_manifest(tmp_path / "a.cslist")


def test_manifest_cannot_escape_declared_source_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "external.cs").write_text("class Outside {}")
    (project / "entry.cslist").write_text("../external.cs")
    with pytest.raises(VamError, match="escapes"):
        build.source_manifest(project / "entry.cslist")
    assert build.source_manifest(project / "entry.cslist", tmp_path)[3] == [tmp_path / "external.cs"]


def test_deployment_preserves_other_files_and_records_backup(tmp_path, monkeypatch):
    source, game, runtime = tmp_path / "src", tmp_path / "game", tmp_path / "runtime"
    source.mkdir()
    (source / "Plugin.cs").write_text("new source")
    target = game / "Custom/Scripts/Example"
    target.mkdir(parents=True)
    (target / "Plugin.cs").write_text("old source")
    (target / "Other.cs").write_text("unrelated")
    monkeypatch.setattr(build, "RUNTIME", runtime)
    monkeypatch.setattr(build, "build_plugin", lambda *args: {"ok": True})
    result = build.deploy_plugin(str(source / "Plugin.cs"), "Example", game=game)
    assert (target / "Plugin.cs").read_text() == "new source"
    assert (target / "Other.cs").read_text() == "unrelated"
    assert (runtime / "deployments" / result["deployment_id"] / "previous/Plugin.cs").read_text() == "old source"


@pytest.mark.parametrize("package", ["../outside", "a/b", "..", "name with spaces"])
def test_invalid_deployment_package(package):
    with pytest.raises(VamError) as exc:
        build.deploy_plugin("unused.cs", package)
    assert exc.value.code == "invalid_path"


def test_result_assertions_check_numbers_absence_and_lists():
    result = {"a": [{"value": .250001}], "names": ["x", "y"]}
    assert_expectations(result, [{"path": "a.0.value", "approx": .25, "tolerance": .00001}, {"path": "names", "contains": "y"}, {"path": "missing", "exists": False}])
    with pytest.raises(VamError, match="Unexpected value"):
        assert_expectations(result, [{"path": "a.0.value", "equals": 1}])


def test_refs_support_values_without_string_coercion():
    context = {"steps": {"load": {"scripts": [{"id": "plugin#0"}], "enabled": False}}}
    assert resolve_refs({"id": {"$ref": "steps.load.scripts.0.id"}, "enabled": {"$ref": "steps.load.enabled"}}, context) == {"id": "plugin#0", "enabled": False}
    with pytest.raises(VamError):
        dig(context, "steps.missing")


def test_host_job_cancel_and_persistence(tmp_path):
    jobs = HostJobs(tmp_path)
    entered = threading.Event()

    def task(event, progress):
        entered.set()
        event.wait(5)
        raise VamError("cancelled", "cancelled fixture")

    job = jobs.start("fixture", task)
    assert entered.wait(2)
    jobs.get(job["job_id"], cancel=True)
    done = jobs.wait(job["job_id"], 3)
    assert done["state"] == "cancelled"
    assert HostJobs(tmp_path).get(job["job_id"])["state"] == "cancelled"


def test_host_restart_marks_unfinished_job_interrupted(tmp_path):
    from vam_mcp.common import atomic_json
    job_id = "host_" + "a" * 32
    atomic_json(tmp_path / "jobs" / (job_id + ".json"), {"job_id": job_id, "state": "running"})
    assert HostJobs(tmp_path).get(job_id)["state"] == "interrupted"


def test_transport_handles_native_false_and_does_not_retry():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []

    def serve():
        with listener:
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as stream:
                request = json.loads(stream.readline())
                received.append(request)
                response = {"id": request["id"], "instance_id": "fixture", "ok": "false", "error": {"code": "not_found", "message": "missing"}}
                connection.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    client = BridgeClient({"instance_id": "fixture", "protocol": "1", "port": listener.getsockname()[1], "token": "local-fixture"})
    with pytest.raises(VamError) as exc:
        client.request("ping")
    thread.join(2)
    assert exc.value.code == "not_found" and len(received) == 1


def test_compile_diagnostics(tmp_path):
    source = tmp_path / "Broken.cs"
    source.write_text("class Broken { this is invalid C# }")
    with pytest.raises(VamError) as exc:
        build.build_plugin(str(source))
    assert exc.value.code == "compile_error"


@pytest.mark.live
def test_real_api_index():
    result = build.api_query("search", "GetAtomByUid")
    assert any("GetAtomByUid(System.String uid)" in m.get("signature", "") for m in result["matches"])


def test_all_mcp_tools_have_json_schemas():
    from vam_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 17
    assert {"vam_capture", "vam_test", "vam_probe", "vam_ui_action"}.issubset({t.name for t in tools})
    assert all(t.inputSchema["type"] == "object" for t in tools)


def test_test_scene_does_not_autoload_source_plugins():
    from vam_mcp.instances import base_scene
    data, origin = base_scene()
    assert not any(s.get("id") in {"PluginManager", "Plugins"} or s.get("id", "").startswith("plugin#") for a in data["atoms"] for s in a.get("storables", []))


def test_multiple_instances_require_explicit_selection(monkeypatch):
    import vam_mcp.client as client
    monkeypatch.setattr(client, "instances", lambda **kwargs: [{"instance_id": "first"}, {"instance_id": "second"}])
    with pytest.raises(VamError) as exc:
        client.get_client()
    assert exc.value.code == "ambiguous_instance"


def test_disconnect_is_reported_without_replaying_request():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received = []

    def serve():
        with listener:
            connection, _ = listener.accept()
            with connection, connection.makefile("rb") as stream:
                received.append(json.loads(stream.readline()))

    thread = threading.Thread(target=serve)
    thread.start()
    client = BridgeClient({"instance_id": "fixture", "protocol": 1, "port": listener.getsockname()[1], "token": "fixture"})
    with pytest.raises(VamError) as exc:
        client.request("submit", {"request_key": "unique-key"})
    thread.join(2)
    assert exc.value.code in {"disconnected", "invalid_response"} and len(received) == 1


def test_job_poll_retries_only_a_mismatched_read_response():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    requests = []

    def serve():
        with listener:
            for index in range(2):
                connection, _ = listener.accept()
                with connection, connection.makefile("rb") as stream:
                    request = json.loads(stream.readline())
                    requests.append(request)
                    response = {"id": request["id"], "instance_id": "other" if index == 0 else "fixture", "ok": True, "result": {"state": "succeeded"}}
                    connection.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    client = BridgeClient({"instance_id": "fixture", "protocol": 1, "port": listener.getsockname()[1], "token": "fixture"})
    assert client.job("game_example")["state"] == "succeeded"
    thread.join(2)
    assert len(requests) == 2 and all(request["method"] == "job" for request in requests)


def test_file_error_finalizes_report_and_cleanup_continues(tmp_path, monkeypatch):
    import vam_mcp.testing as testing
    from vam_mcp.common import read_json

    class Client:
        instance_id = "fixture"
        calls = []

        def call(self, operation, arguments=None, *args, **kwargs):
            self.calls.append((operation, arguments))
            if operation == "logs":
                return {"latest_sequence": "0", "items": []}
            return {}

    monkeypatch.setattr(testing, "RUNTIME", tmp_path)
    client = Client()
    spec = {
        "steps": [{"id": "compile", "operation": "compile", "arguments": {"entrypoint": str(tmp_path / "missing.cs")}}],
        "cleanup": [{"operation": "status", "timeout_s": "invalid"}, {"operation": "atom", "arguments": {"action": "remove", "atom_uid": "fixture"}}],
    }
    with pytest.raises(VamError) as error:
        testing.run_test(client, spec)
    assert error.value.code == "test_execution_error"
    report = read_json(Path(error.value.details["report"]))
    assert report["state"] == "failed" and report["steps"][0]["state"] == "failed"
    assert "completed_at" in report and "failure_capture" in report
    assert [entry["state"] for entry in report["cleanup"]] == ["failed", "succeeded"]
    assert ("atom", {"action": "remove", "atom_uid": "fixture"}) in client.calls


@pytest.mark.parametrize("existing_vam", [False, True])
def test_registration_preserves_unrelated_settings_and_is_repeatable(existing_vam):
    import runpy
    import tomli
    from vam_mcp.common import ROOT

    functions = runpy.run_path(str(ROOT / "scripts/Register-Codex.py"))
    register = functions["updated_config"]
    original = '# preserve this comment\nmodel = "example"\n[mcp_servers.other]\nenabled = false\nargs = []\n'
    if existing_vam:
        original += '[mcp_servers.vam]\ncommand = "old"\n[mcp_servers.vam.env]\nOLD = "value"\n'
    original += '[projects."D:/Project"]\ntrust_level = "trusted"\n'
    game_root = r"C:\VaM"
    updated = register(original, ROOT, r"C:\Program Files\Python310\python.exe", game_root)
    data = tomli.loads(updated)
    assert data["mcp_servers"]["other"] == {"enabled": False, "args": []}
    assert data["projects"]["D:/Project"]["trust_level"] == "trusted"
    assert '# preserve this comment' in updated
    assert data["mcp_servers"]["vam"]["env"] == {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "VAM_GAME_ROOT": game_root}
    assert register(updated, ROOT, r"C:\Program Files\Python310\python.exe", game_root) == updated
    removed = tomli.loads(functions["unregistered_config"](updated))
    expected = tomli.loads(original)
    expected["mcp_servers"].pop("vam", None)
    assert removed == expected


def test_force_stop_checks_creation_identity_before_termination():
    import subprocess
    import sys
    from vam_mcp.common import hidden_options, process_identity, terminate_owned

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **hidden_options())
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        with pytest.raises(VamError) as error:
            terminate_owned(process.pid, identity + 1)
        assert error.value.code == "instance_changed" and process.poll() is None
        terminate_owned(process.pid, identity)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
