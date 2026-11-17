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
    monkeypatch.setattr(build, "build_plugin", lambda *args, **kwargs: {"ok": True})
    result = build.deploy_plugin(str(source / "Plugin.cs"), "Example", game=game)
    assert (target / "Plugin.cs").read_text() == "new source"
    assert (target / "Other.cs").read_text() == "unrelated"
    assert (runtime / "deployments" / result["deployment_id"] / "previous/Plugin.cs").read_text() == "old source"


def test_deployment_manifest_copies_assets_and_supports_conflict_checked_rollback(tmp_path, monkeypatch):
    source, game, runtime = tmp_path / "src", tmp_path / "game", tmp_path / "runtime"
    (source / "assets").mkdir(parents=True)
    (source / "Plugin.cs").write_text("new source")
    (source / "assets/settings.json").write_text("{}")
    target = game / "Custom/Scripts/Example"
    target.mkdir(parents=True)
    (target / "Plugin.cs").write_text("old source")
    monkeypatch.setattr(build, "RUNTIME", runtime)
    monkeypatch.setattr(build, "build_plugin", lambda *args, **kwargs: {"ok": True})
    result = build.deploy_plugin(str(source / "Plugin.cs"), "Example", artifact_files=["assets/settings.json"], game=game)
    assert (target / "assets/settings.json").read_text() == "{}"
    assert build.rollback_deployment(result["deployment_id"], game=game)["rolled_back"] is True
    assert (target / "Plugin.cs").read_text() == "old source"
    assert not (target / "assets/settings.json").exists()


def test_rollback_refuses_files_changed_after_deployment(tmp_path, monkeypatch):
    source, game, runtime = tmp_path / "src", tmp_path / "game", tmp_path / "runtime"
    source.mkdir()
    game.mkdir()
    (source / "Plugin.cs").write_text("new source")
    monkeypatch.setattr(build, "RUNTIME", runtime)
    monkeypatch.setattr(build, "build_plugin", lambda *args, **kwargs: {"ok": True})
    result = build.deploy_plugin(str(source / "Plugin.cs"), "Example", game=game)
    target = game / "Custom/Scripts/Example/Plugin.cs"
    target.write_text("changed later")
    with pytest.raises(VamError) as error:
        build.rollback_deployment(result["deployment_id"], game=game)
    assert error.value.code == "deployment_conflict"
    assert target.read_text() == "changed later"


def test_deployment_rejects_target_junction_escape(tmp_path, monkeypatch):
    source, game, outside, runtime = tmp_path / "src", tmp_path / "game", tmp_path / "outside", tmp_path / "runtime"
    source.mkdir()
    outside.mkdir()
    (source / "Plugin.cs").write_text("class Plugin {}")
    (game / "Custom/Scripts").mkdir(parents=True)
    try:
        (game / "Custom/Scripts/Escape").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    monkeypatch.setattr(build, "RUNTIME", runtime)
    monkeypatch.setattr(build, "build_plugin", lambda *args, **kwargs: {"ok": True})
    with pytest.raises(VamError) as error:
        build.deploy_plugin(str(source / "Plugin.cs"), "Escape", game=game)
    assert error.value.code == "invalid_path"


def test_bepinex_profile_deploys_dll_symbols_and_dependencies_with_rollback(tmp_path, monkeypatch):
    source, game, runtime = tmp_path / "src", tmp_path / "game", tmp_path / "runtime"
    (source / "config").mkdir(parents=True)
    game.mkdir()
    (source / "Plugin.cs").write_text("class Plugin {}")
    (source / "config/plugin.json").write_text("{}")
    output = tmp_path / "build/Example.dll"
    symbols = output.with_suffix(".pdb")

    def fake_build(*args, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"dll")
        symbols.write_bytes(b"symbols")
        return {"ok": True, "output": str(output), "symbols": str(symbols)}

    monkeypatch.setattr(build, "RUNTIME", runtime)
    monkeypatch.setattr(build, "build_plugin", fake_build)
    result = build.deploy_plugin(str(source / "Plugin.cs"), "Example", profile="bepinex", debug=True, artifact_files=["config/plugin.json"], game=game)
    target = game / "BepInEx/plugins/Example"
    assert (target / "Example.dll").read_bytes() == b"dll"
    assert (target / "Example.pdb").read_bytes() == b"symbols"
    assert (target / "config/plugin.json").read_text() == "{}"
    assert build.rollback_deployment(result["deployment_id"], game=game)["rolled_back"] is True
    assert not (target / "Example.dll").exists()
    assert not (target / "Example.pdb").exists()
    assert not (target / "config/plugin.json").exists()


def test_var_bundle_validates_and_deploys_prebuilt_archive(tmp_path, monkeypatch):
    import zipfile
    source, game, runtime = tmp_path / "Fixture.var", tmp_path / "game", tmp_path / "runtime"
    game.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("meta.json", '{"creator":"Example","packageName":"Fixture","version":"1"}')
        archive.writestr("Custom/Scripts/Plugin.cs", "class Plugin {}")
        archive.writestr("Custom/Scripts/Extra/Helper.cs", "class Helper {}")
    monkeypatch.setattr(build, "RUNTIME", runtime)
    result = build.deploy_plugin(str(source), "Fixture.var", profile="var_bundle", game=game)
    installed = game / "AddonPackages/Fixture.var"
    assert installed.read_bytes() == source.read_bytes()
    assert result["compile"]["archive_members"] == 3
    assert result["entrypoint"] == "AddonPackages/Fixture:/Custom/Scripts/Extra/Helper.cs"
    assert len(result["plugin_entrypoints"]) == 2
    assert build.rollback_deployment(result["deployment_id"], game=game)["rolled_back"] is True
    assert not installed.exists()


def test_var_bundle_load_uses_packaged_script_entrypoint(monkeypatch):
    from vam_mcp import server
    calls = []

    def game_call(instance_id, operation, arguments, timeout_s=30, write=False):
        return lambda: calls.append((instance_id, operation, arguments, write)) or {"ok": True}

    monkeypatch.setattr(server, "game_call", game_call)
    result = asyncio.run(server.vam_plugin(action="load", profile="var_bundle", instance_id="fixture", entrypoint="AddonPackages/Fixture:/Custom/Scripts/Plugin.cs"))
    assert result["ok"] is True
    assert calls[0][1] == "plugin" and calls[0][2]["entrypoint"] == "AddonPackages/Fixture:/Custom/Scripts/Plugin.cs"
    assert calls[0][3] is True


def test_compile_profiles_apply_bepinex_references_and_debug_flags(tmp_path, monkeypatch):
    source, game = tmp_path / "Plugin.cs", tmp_path / "game"
    source.write_text("class Plugin {}")
    compiler = tmp_path / "csc.exe"
    compiler.write_bytes(b"compiler")
    dep = tmp_path / "dep.dll"
    dep.write_bytes(b"dep")
    (game / "BepInEx/core").mkdir(parents=True)
    for name in ("BepInEx.dll", "0Harmony.dll"):
        (game / "BepInEx/core" / name).write_bytes(name.encode())
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        Path(next(item[5:] for item in args if item.startswith("/out:"))).write_bytes(b"compiled")
        Path(next(item[5:] for item in args if item.startswith("/pdb:"))).write_bytes(b"symbols")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(build, "COMPILER", compiler)
    monkeypatch.setattr(build.subprocess, "run", fake_run)
    result = build.compile_sources([source], tmp_path / "out.dll", game=game, profile="bepinex", reference_paths=[dep], debug=True)
    assert result["ok"] is True
    assert any(str(game / "BepInEx/core/BepInEx.dll") in arg for arg in captured["args"])
    assert f"/reference:{dep}" in captured["args"]
    assert "/debug:full" in captured["args"] and "/optimize-" in captured["args"]


def test_api_indexer_cache_tracks_compiler_and_cecil_hashes(tmp_path, monkeypatch):
    root, game = tmp_path / "project", tmp_path / "game"
    (root / "tools").mkdir(parents=True)
    (root / "tools/ApiIndex.cs").write_text("source")
    compiler = root / "compiler/csc.exe"
    compiler.parent.mkdir()
    compiler.write_bytes(b"compiler-v1")
    cecil = game / "BepInEx/core/Mono.Cecil.dll"
    cecil.parent.mkdir(parents=True)
    cecil.write_bytes(b"cecil-v1")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        Path(next(item[5:] for item in args if item.startswith("/out:"))).write_bytes(str(len(calls)).encode())
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(build, "GAME", game)
    monkeypatch.setattr(build, "COMPILER", compiler)
    monkeypatch.setattr(build.subprocess, "run", fake_run)
    first = build.build_api_indexer()
    assert build.build_api_indexer() == first and len(calls) == 1
    cecil.write_bytes(b"cecil-v2")
    build.build_api_indexer()
    assert len(calls) == 2
    compiler.write_bytes(b"compiler-v2")
    build.build_api_indexer()
    assert len(calls) == 3


def test_api_query_allows_installed_plugin_assemblies_but_rejects_outside_paths(tmp_path, monkeypatch):
    game = tmp_path / "game"
    allowed = game / "BepInEx/plugins/Example/Dependency.dll"
    outside = tmp_path / "outside.dll"
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b"assembly")
    outside.write_bytes(b"assembly")
    monkeypatch.setattr(build, "GAME", game)
    with pytest.raises(VamError) as error:
        build.api_query("search", "anything", assembly_path=str(outside))
    assert error.value.code == "invalid_path"
    indexer = tmp_path / "ApiIndex.exe"
    indexer.write_bytes(b"indexer")
    monkeypatch.setattr(build, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(build, "build_api_indexer", lambda: indexer)
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": '{"types":[{"name":"Example.Dependency","kind":"class","methods":[],"properties":[],"fields":[]}]}', "stderr": ""})())
    result = build.api_query("search", "Dependency", assembly_path=str(allowed))
    assert result["total"] == 1 and result["matches"][0]["type"] == "Example.Dependency"


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


def test_stop_honors_cancellation_before_shutdown():
    from vam_mcp.instances import stop
    event = threading.Event()
    event.set()
    with pytest.raises(VamError) as error:
        stop("vam_fixture", cancel_event=event)
    assert error.value.code == "cancelled"


def test_stop_honors_cancellation_before_shutdown():
    from vam_mcp.instances import stop
    event = threading.Event()
    event.set()
    with pytest.raises(VamError) as error:
        stop("vam_fixture", cancel_event=event)
    assert error.value.code == "cancelled"


def test_restart_reuses_instance_identity_and_save_directory(monkeypatch, tmp_path):
    import contextlib
    import vam_mcp.instances as instance_module

    launch = {"instance_id": "vam_fixture", "pid": 123, "instance_folder": str(tmp_path / "instance"), "saves_dir": str(tmp_path / "saves"), "process_identity": 10}
    (tmp_path / "instance").mkdir()
    monkeypatch.setattr(instance_module, "instances", lambda **kwargs: [{"instance_id": "vam_fixture", "instance_folder": launch["instance_folder"]}])
    monkeypatch.setattr(instance_module, "read_json", lambda path: dict(launch))
    stopped = []
    monkeypatch.setattr(instance_module, "stop", lambda instance_id, **kwargs: stopped.append(instance_id))
    monkeypatch.setattr(instance_module, "process_identity", lambda pid: None)
    monkeypatch.setattr(instance_module.GameLease, "hold", staticmethod(lambda: contextlib.nullcontext()))
    result = {}

    def start(info, timeout_s, cancel_event):
        result.update(info)
        return {"ok": True, **info}

    monkeypatch.setattr(instance_module, "_start_owned", start)
    value = instance_module.restart("vam_fixture", timeout_s=30)
    assert stopped == ["vam_fixture"]
    assert value["instance_id"] == "vam_fixture"
    assert value["saves_dir"] == str(tmp_path / "saves")
    assert value["previous_process_identity"] == 10


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
    if not (build.GAME / "VaM_Data/Managed/Assembly-CSharp.dll").is_file():
        pytest.skip("VaM_GAME_ROOT is not configured to a VaM installation")
    result = build.api_query("search", "GetAtomByUid")
    assert any("GetAtomByUid(System.String uid)" in m.get("signature", "") for m in result["matches"])


def test_all_mcp_tools_have_json_schemas():
    from vam_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 18
    assert {"vam_capture", "vam_test", "vam_probe", "vam_ui_action", "vam_capabilities"}.issubset({t.name for t in tools})
    assert all(t.inputSchema["type"] == "object" for t in tools)


def test_plugin_profiles_describe_build_lifecycle_and_validation():
    from vam_mcp.profiles import plugin_capabilities
    profiles = plugin_capabilities()["profiles"]
    assert set(profiles) == {"mvrscript", "bepinex", "var_bundle"}
    assert profiles["bepinex"]["deployment"] == "BepInEx/plugins/<package>"
    assert "restart" in profiles["bepinex"]["validation"]
    assert profiles["var_bundle"]["compiler"]["kind"] == "prebuilt_archive"
    with pytest.raises(VamError, match="Unknown plugin profile"):
        plugin_capabilities("unknown")


def test_plugin_capabilities_report_missing_local_prerequisites(tmp_path):
    from vam_mcp.profiles import plugin_capabilities
    result = plugin_capabilities(game=tmp_path, compiler=tmp_path / "csc.exe")
    assert result["profiles"]["mvrscript"]["available"] is False
    assert "VaM.exe" in result["profiles"]["mvrscript"]["missing_requirements"]
    assert result["profiles"]["var_bundle"]["available"] is True
    assert result["profiles"]["var_bundle"]["live_validation_ready"] is False


def test_plugin_capabilities_require_an_active_bridge_for_live_readiness(tmp_path):
    from vam_mcp.profiles import plugin_capabilities
    (tmp_path / "VaM.exe").write_bytes(b"game")
    bridge = tmp_path / "BepInEx/plugins/VaMMcp/VaMMcp.Bridge.dll"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes(b"bridge")
    cold = plugin_capabilities(game=tmp_path, compiler=tmp_path / "csc.exe")
    live = plugin_capabilities(game=tmp_path, compiler=tmp_path / "csc.exe", live_instance_available=True)
    assert cold["bridge_installed"] is True and cold["profiles"]["mvrscript"]["live_validation_ready"] is False
    assert live["profiles"]["mvrscript"]["live_validation_ready"] is True


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
