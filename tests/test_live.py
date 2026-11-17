import asyncio
import json
import os
from pathlib import Path
import shutil
import struct
import threading
import time
import uuid

import pytest

from vam_mcp.build import compile_sources, deploy_plugin
from vam_mcp.client import get_client, instances
from vam_mcp.common import GAME, ROOT, RUNTIME, VamError, read_json
from vam_mcp.jobs import JOBS
from vam_mcp.testing import run_probe, run_test

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live():
    connected = [i for i in instances(include_offline=False) if i["connected"]]
    if not connected:
        pytest.skip("No bridge-enabled test instance; start one with vam_instance")
    c = get_client(connected[-1]["instance_id"])
    uid = "McpLive_" + uuid.uuid4().hex[:8]
    package = "VaMMcpExamples"
    deployed = deploy_plugin(str(ROOT / "examples/DevelopmentFixture.cslist"), package)
    created_person = not any(atom["type"] == "Person" for atom in c.call("atom", {"action": "list"})["atoms"])
    if created_person:
        c.call("atom", {"action": "create", "atom_uid": "Person", "type": "Person"}, 90)
    c.call("atom", {"action": "create", "atom_uid": uid, "type": "Empty"})
    loaded = c.call("plugin", {"action": "load", "atom_uid": uid, "entrypoint": deployed["entrypoint"]})
    value = {"client": c, "atom_uid": uid, "plugin_uid": loaded["plugin_uid"], "storable_id": loaded["scripts"][0]["storable_id"]}
    yield value
    try:
        c.call("atom", {"action": "remove", "atom_uid": uid})
    except VamError:
        pass
    if created_person:
        try:
            c.call("atom", {"action": "remove", "atom_uid": "Person"})
        except VamError:
            pass


def controls(live, **filters):
    return live["client"].call("ui_tree", {"atom_uid": live["atom_uid"], "plugin_uid": live["plugin_uid"], **filters})["items"]


def control(live, kind, label=None):
    items = controls(live, control_type=kind)
    if label is not None:
        names = {"Increment": "FixtureIncrement", "Disabled": "FixtureDisabled", "Open confirmation": "FixtureConfirm", "Select file": "FixtureFile", "Internal error": "FixtureError"}
        items = [i for i in items if i["name"] == names.get(label, label)]
    elif kind == "input":
        items = [i for i in items if i["name"] == "FixtureInput"]
    assert len(items) == 1, [(x["label"], x["path"]) for x in items]
    return items[0]


def param(live, name, value=None, action="get"):
    return live["client"].call("storable", {"action": action, "atom_uid": live["atom_uid"], "storable_id": live["storable_id"], "name": name, **({"value": value} if value is not None else {})})


def test_real_versions_and_parameters(live):
    status = live["client"].call("status")
    assert status["ready"] is True and status["unity_version"] == "2018.1.9f1"
    assert status["game_version"] == "1.22.0.3"
    assert param(live, "level", .75, "set")["value"] == .75
    assert param(live, "option", True, "set")["value"] is True
    assert param(live, "choice", "beta", "set")["value"] == "beta"
    with pytest.raises(VamError) as exc:
        param(live, "level", 12, "set")
    assert exc.value.code == "invalid_value"
    with pytest.raises(VamError):
        param(live, "choice", "absent", "set")


def test_ui_controls_and_input_callbacks(live):
    c = live["client"]
    opened = c.call("plugin", {"action": "open_ui", "atom_uid": live["atom_uid"], "plugin_uid": live["plugin_uid"]})
    assert opened["ui_active"] is True
    before = param(live, "count")["value"]
    c.call("ui_action", {"action": "click", "handle": control(live, "button", "Increment")["handle"]})
    assert param(live, "count")["value"] == before + 1
    c.call("ui_action", {"action": "set", "handle": control(live, "input")["handle"], "value": "Codex 编辑测试", "submit": True})
    assert param(live, "input")["value"] == "Codex 编辑测试"
    assert param(live, "submitted")["value"] == "Codex 编辑测试"
    c.call("ui_action", {"action": "set", "handle": control(live, "popup")["handle"], "value": "gamma"})
    assert param(live, "choice")["value"] == "gamma"
    with pytest.raises(VamError) as exc:
        c.call("ui_action", {"action": "click", "handle": control(live, "button", "Disabled")["handle"]})
    assert exc.value.code == "not_interactable"


def test_confirmation_and_real_ui_screenshot(live):
    c = live["client"]
    c.call("ui_action", {"action": "click", "handle": control(live, "button", "Open confirmation")["handle"]})
    modal = c.call("ui_tree", {"query": "Fixture confirmation"})
    assert modal["total"] > 0
    screenshot = c.call("capture", {"mode": "ui"})
    png = Path(screenshot["path"]).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1280, 800)
    assert len(png) > 20000
    buttons = c.call("ui_tree", {"control_type": "button", "query": "Cancel"})["items"]
    modal_buttons = [b for b in buttons if "alert" in b["path"].lower() or "confirm" in b["path"].lower()]
    if not modal_buttons:
        modal_buttons = [b for b in buttons if b.get("interactable")]
    assert modal_buttons
    c.call("ui_action", {"action": "click", "handle": modal_buttons[-1]["handle"]})
    assert param(live, "submitted")["value"] == "cancelled"


def test_native_file_picker_select_and_cancel(live):
    c = live["client"]
    image = c.call("capture", {"mode": "ui"})["path"]
    button = control(live, "button", "Select file")["handle"]
    c.call("ui_action", {"action": "click", "handle": button})
    dialogs = c.call("file_dialog", {"action": "inspect"})["dialogs"]
    assert len(dialogs) == 1
    c.call("file_dialog", {"action": "select", "path": image, "handle": dialogs[0]["handle"]})
    assert Path(param(live, "chosenFile")["value"]).resolve() == Path(image).resolve()
    c.call("ui_action", {"action": "click", "handle": button})
    with pytest.raises(VamError) as exc:
        c.call("file_dialog", {"action": "select", "path": str(ROOT / "pyproject.toml")})
    assert exc.value.code == "invalid_value"
    c.call("file_dialog", {"action": "cancel"})
    assert param(live, "chosenFile")["value"] == ""


def test_vam_internal_error_channel(live):
    c = live["client"]
    cursor = c.call("logs", {"limit": 1})["latest_sequence"]
    c.call("ui_action", {"action": "click", "handle": control(live, "button", "Internal error")["handle"]})
    logs = c.call("logs", {"after": cursor, "contains": "internal-only error"})["items"]
    assert any(e["source"] == "vam" and e["level"] == "error" for e in logs)
    assert not any(e["source"] == "unity" for e in logs)


def test_source_edit_deploy_reload_and_stale_handles(live, tmp_path):
    c = live["client"]
    c.call("plugin", {"action": "open_ui", "atom_uid": live["atom_uid"], "plugin_uid": live["plugin_uid"]})
    old_handle = control(live, "button", "Increment")["handle"]
    source = (ROOT / "examples/DevelopmentFixture.cs").read_text(encoding="utf-8")
    (tmp_path / "DevelopmentFixture.cs").write_text(source.replace('"fixture-v1"', '"fixture-v2"'), encoding="utf-8")
    (tmp_path / "DevelopmentFixture.cslist").write_text("DevelopmentFixture.cs")
    deploy_plugin(str(tmp_path / "DevelopmentFixture.cslist"), "VaMMcpExamples")
    for _ in range(3):
        c.call("plugin", {"action": "reload", "atom_uid": live["atom_uid"], "plugin_uid": live["plugin_uid"]})
        assert param(live, "version")["value"] == "fixture-v2"
        assert param(live, "count")["value"] == 0
        param(live, "Increment", action="call")
        assert param(live, "count")["value"] == 1
    with pytest.raises(VamError) as exc:
        c.call("ui_action", {"action": "click", "handle": old_handle})
    assert exc.value.code == "stale_handle"
    deploy_plugin(str(ROOT / "examples/DevelopmentFixture.cslist"), "VaMMcpExamples")


def test_reflection_probe_and_timeout_cleanup(live):
    c = live["client"]
    code = 'var f=typeof(SuperController).GetField("errorLog",BindingFlags.Instance|BindingFlags.NonPublic); result["field_found"].AsBool=f!=null; result["atom"]=containingAtom.uid;'
    result = run_probe(c, code, {}, live["atom_uid"])
    assert result["result"]["field_found"] == "true" and result["result"]["atom"] == live["atom_uid"]
    path = RUNTIME / "probe-timeout-cleanup.txt"
    path.unlink(missing_ok=True)
    body = 'try { while(true) { yield return null; } } finally { System.IO.File.WriteAllText(@"' + str(path) + '","disposed"); }'
    with pytest.raises(VamError) as exc:
        run_probe(c, body, {}, timeout_s=1)
    assert exc.value.code == "timeout" and path.read_text() == "disposed"
    assert c.call("status")["ready"]


def test_separate_diagnostic_assembly_identities(live):
    first = run_probe(live["client"], 'result["marker"]="first";', {})
    second = run_probe(live["client"], 'result["marker"]="second";', {})
    assert first["result"]["marker"] == "first" and second["result"]["marker"] == "second"


def test_probe_timeout_during_unity_wait_executes_finally(live):
    path = RUNTIME / "probe-unity-wait-cleanup.txt"
    path.unlink(missing_ok=True)
    body = 'try { yield return new WaitForSeconds(30f); } finally { System.IO.File.WriteAllText(@"' + str(path) + '","disposed"); }'
    started = time.monotonic()
    with pytest.raises(VamError) as error:
        run_probe(live["client"], body, {}, timeout_s=1)
    assert error.value.code == "timeout" and time.monotonic() - started < 8
    assert path.read_text() == "disposed"


def test_host_probe_cancellation(live):
    c = live["client"]
    path = RUNTIME / "probe-cancel-cleanup.txt"
    path.unlink(missing_ok=True)
    body = 'try { while(true) { yield return null; } } finally { System.IO.File.WriteAllText(@"' + str(path) + '","disposed"); }'
    job = JOBS.start("cancel_probe", lambda event, progress: run_probe(c, body, {}, timeout_s=30, cancel_event=event))
    time.sleep(1)
    JOBS.get(job["job_id"], cancel=True)
    result = JOBS.wait(job["job_id"], 5)
    assert result["state"] == "cancelled" and path.read_text() == "disposed"


def test_native_morph_lookup_and_five_camera_views(live):
    c = live["client"]
    morphs = c.call("morph", {"action": "list", "atom_uid": "Person", "query": "nose", "limit": 10})
    assert morphs["total"] > 0
    morph = next(m for m in morphs["morphs"] if not m["driven"] and not m["disabled"])
    value = morph["value"]
    c.call("morph", {"action": "set", "atom_uid": "Person", "uid": morph["uid"], "value": value})
    for view in ["front", "left", "right", "top", "back"]:
        frame = c.call("capture", {"mode": "render", "atom_uid": "Person", "view": view, "width": 512, "height": 512})
        assert Path(frame["path"]).stat().st_size > 5000


def test_clone_scene_save_and_test_failure_evidence(live):
    c = live["client"]
    clone_uid = "McpClone_" + uuid.uuid4().hex[:8]
    result = c.call("atom", {"action": "clone", "atom_uid": live["atom_uid"], "new_uid": clone_uid}, 60)
    assert result["uid"] == clone_uid
    assert c.call("plugin", {"action": "list", "atom_uid": clone_uid})["plugins"]
    c.call("atom", {"action": "remove", "atom_uid": clone_uid})
    saved = c.call("scene", {"action": "save"})
    assert Path(saved["path"]).is_file()
    temp_uid = "McpCleanup_" + uuid.uuid4().hex[:8]
    spec = {"name": "intentional failure evidence", "steps": [{"operation": "atom", "arguments": {"action": "create", "type": "Empty", "atom_uid": temp_uid}}, {"operation": "status", "expect": [{"path": "ready", "equals": False}]}], "cleanup": [{"operation": "atom", "arguments": {"action": "remove", "atom_uid": temp_uid}}]}
    with pytest.raises(VamError) as exc:
        run_test(c, spec)
    report = read_json(Path(exc.value.details["report"]))
    assert report["state"] == "failed" and Path(report["failure_capture"]["path"]).is_file()
    assert report["cleanup"][0]["state"] == "succeeded"


def test_game_compile_and_initialization_errors(live):
    c = live["client"]
    folder = GAME / "Custom/Scripts/VaMMcpFailures"
    folder.mkdir(exist_ok=True)
    before = {p["plugin_uid"] for p in c.call("plugin", {"action": "list", "atom_uid": live["atom_uid"]})["plugins"]}
    for source, expected in [('using UnityEngine; public class BrokenGame : MVRScript { INVALID C# }', "compile_error"), ('using System; public class BrokenInit : MVRScript { public override void Init(){throw new Exception("VaMMcp deliberate init failure");}}', "initialization_error")]:
        path = folder / ("Broken_" + uuid.uuid4().hex + ".cs")
        path.write_text(source)
        try:
            with pytest.raises(VamError) as exc:
                c.call("plugin", {"action": "load", "atom_uid": live["atom_uid"], "entrypoint": path.relative_to(GAME).as_posix()}, timeout_s=15)
            assert exc.value.code in {expected, "compile_error"}
        finally:
            for p in c.call("plugin", {"action": "list", "atom_uid": live["atom_uid"]})["plugins"]:
                if p["plugin_uid"] not in before:
                    c.call("plugin", {"action": "unload", "atom_uid": live["atom_uid"], "plugin_uid": p["plugin_uid"]})
            path.unlink()


def test_mcp_stdio_handshake_and_image(live):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import sys

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "VaM"
                tools = await session.list_tools()
                assert len(tools.tools) == 17
                status = await session.call_tool("vam_status", {"instance_id": live["client"].instance_id})
                assert not status.isError
                data = status.structuredContent or json.loads(status.content[0].text)
                assert data["ready"] is True
                frame = await session.call_tool("vam_capture", {"instance_id": live["client"].instance_id})
                assert not frame.isError and any(block.type == "image" for block in frame.content)
                api = await session.call_tool("vam_api", {"action": "search", "query": "ReloadPluginWithUID"})
                assert not api.isError and "ReloadPluginWithUID" in api.content[0].text
    asyncio.run(exercise())
