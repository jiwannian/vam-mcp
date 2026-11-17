"""Verify real stdio startup, reconnect, and two independent scene states."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vam_mcp.client import get_client, instances
from vam_mcp.common import ROOT, RUNTIME, atomic_json


async def main():
    if instances(include_offline=False):
        raise RuntimeError("Stop existing MCP test instances before this lifecycle acceptance")
    report = {"state": "running", "steps": []}
    path = RUNTIME / "acceptance/instance-lifecycle.json"
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")])

    def note(name, result):
        report["steps"].append({"name": name, "result": result})
        atomic_json(path, report)
        print(name, flush=True)
        return result

    async def call(session, name, arguments, *, allow_failure=False):
        response = await session.call_tool(name, arguments)
        value = response.structuredContent or json.loads(response.content[0].text)
        if value.get("ok") is False and "error" in value and not allow_failure:
            raise RuntimeError(value)
        return value

    async def finish(session, value):
        while value["state"] in {"queued", "running"}:
            value = await call(session, "vam_job", {"action": "wait", "job_id": value["job_id"], "timeout_s": 20})
            print("job", value["state"], flush=True)
        if value["state"] != "succeeded":
            raise RuntimeError(value)
        return value["result"]

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                first = note("start_first", await finish(session, await call(session, "vam_instance", {"action": "start"})))
                a = first["instance_id"]
                report["active_instance_id"] = a
        status = get_client(a).call("status")
        assert status["ready"]
        note("game_survives_mcp_host_exit", status)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                connected = note("reconnect", await call(session, "vam_instance", {"action": "connect", "instance_id": a}))
                assert connected["pid"] == first["pid"]
                installed = note("install_while_active", await call(session, "vam_instance", {"action": "install"}))
                assert installed["changed"] is False
                second = note("start_second", await finish(session, await call(session, "vam_instance", {"action": "start"})))
                b = second["instance_id"]
                try:
                    listing = note("both_connected", await call(session, "vam_instance", {"action": "list"}))
                    assert len([i for i in listing["instances"] if i["connected"]]) == 2
                    ambiguous = await call(session, "vam_instance", {"action": "connect"}, allow_failure=True)
                    assert ambiguous["error"]["code"] == "ambiguous_instance"
                    note("explicit_selection_required", ambiguous)
                    marker = "McpInstanceIsolation"
                    await call(session, "vam_atom", {"action": "create", "instance_id": a, "type": "Empty", "atom_uid": marker})
                    try:
                        aa = await call(session, "vam_atom", {"action": "list", "instance_id": a})
                        bb = await call(session, "vam_atom", {"action": "list", "instance_id": b})
                        assert any(i["uid"] == marker for i in aa["atoms"])
                        assert not any(i["uid"] == marker for i in bb["atoms"])
                        note("independent_scene_state", {"first": a, "second": b, "verified": True})
                    finally:
                        await call(session, "vam_atom", {"action": "remove", "instance_id": a, "atom_uid": marker})
                finally:
                    note("stop_second", await finish(session, await call(session, "vam_instance", {"action": "stop", "instance_id": b})))
        assert get_client(a).call("status")["ready"]
        report.update(state="succeeded", reconnect_verified=True)
    except Exception as exc:
        report.update(state="failed", error=str(exc))
        raise
    finally:
        atomic_json(path, report)
    print(json.dumps({"state": "succeeded", "active_instance_id": a, "report": str(path)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
