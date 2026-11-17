from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .common import ROOT, GAME, RUNTIME, GameLease, VamError, atomic_json, hidden_options, read_json, sha256

WINDOWS = Path(os.environ.get("WINDIR", r"C:\Windows"))
COMPILER = Path(os.environ.get("VAM_CSC", str(WINDOWS / "Microsoft.NET/Framework64/v4.0.30319/csc.exe")))
ILSPY = Path(os.environ.get("ILSPY_PATH") or shutil.which("ilspycmd") or "")


def references(game: Path = GAME):
    managed = game / "VaM_Data/Managed"
    names = ["mscorlib.dll", "System.dll", "System.Core.dll", "System.Xml.dll", "System.Runtime.Serialization.dll", "Assembly-CSharp.dll", "Assembly-CSharp-firstpass.dll", "UnityEngine.dll", "UnityEngine.UI.dll"]
    return [managed / n for n in names if (managed / n).is_file()] + sorted(managed.glob("UnityEngine.*Module.dll"))


def compile_sources(sources: list[Path], output: Path, *, bridge=False, game: Path = GAME):
    if not COMPILER.is_file():
        raise VamError("missing_tool", f"C# compiler not found: {COMPILER}")
    output.parent.mkdir(parents=True, exist_ok=True)
    refs = references(game)
    if bridge:
        refs += [game / "BepInEx/core/BepInEx.dll", game / "BepInEx/core/0Harmony.dll"]
    args = [str(COMPILER), "/nologo", "/utf8output", "/codepage:65001", "/noconfig", "/nostdlib+", "/target:library", "/langversion:5", f"/out:{output}"]
    args += [f"/reference:{p}" for p in refs] + [str(p) for p in sources]
    proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=120, **hidden_options())
    result = {"ok": proc.returncode == 0, "output": str(output), "diagnostics": proc.stdout + proc.stderr, "sources": [str(p) for p in sources], "runtime": "game-shipped CLR 2.0 references", "language_version": 5}
    if proc.returncode:
        raise VamError("compile_error", result["diagnostics"], output=str(output))
    return result


def build_bridge():
    sources = sorted((ROOT / "bridge").glob("*.cs"))
    output = ROOT / "build/VaMMcp.Bridge.dll"
    manifest = ROOT / "build/bridge-manifest.json"
    inputs = {str(p): sha256(p) for p in sources + references() + [GAME / "BepInEx/core/BepInEx.dll", GAME / "BepInEx/core/0Harmony.dll", COMPILER]}
    if output.is_file() and manifest.is_file():
        previous = read_json(manifest)
        if previous.get("inputs") == inputs and previous.get("output_sha256") == sha256(output):
            return {"ok": True, "output": str(output), "cached": True, "diagnostics": ""}
    result = compile_sources(sources, output, bridge=True)
    atomic_json(manifest, {"inputs": inputs, "output_sha256": sha256(output)})
    return result


def source_manifest(entrypoint: str | Path, source_root: str | Path | None = None):
    entry = Path(entrypoint).resolve(strict=True)
    root = Path(source_root).resolve(strict=True) if source_root else entry.parent
    if entry.suffix.lower() not in {".cs", ".cslist"}:
        raise VamError("unsupported", "Development entrypoint must be .cs or .cslist")
    visited, visiting, files, sources = set(), set(), [], []

    def visit(path: Path):
        path = path.resolve(strict=True)
        if not path.is_relative_to(root):
            raise VamError("invalid_path", "Source dependency escapes source_root; specify the common project root", path=str(path))
        if path in visiting:
            raise VamError("invalid_cslist", "Cyclic .cslist dependency", path=str(path))
        if path in visited:
            return
        if path.suffix.lower() not in {".cs", ".cslist"}:
            raise VamError("invalid_cslist", "Unsupported source dependency", path=str(path))
        visiting.add(path)
        files.append(path)
        if path.suffix.lower() == ".cs":
            sources.append(path)
        else:
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("//"):
                    visit(path.parent / line.replace("\\", "/"))
        visiting.remove(path)
        visited.add(path)

    visit(entry)
    if not sources:
        raise VamError("invalid_cslist", "No C# sources in entrypoint")
    return entry, root, files, sources


def build_plugin(entrypoint: str, source_root: str = ""):
    entrypoint = Path(entrypoint)
    if not entrypoint.is_absolute():
        entrypoint = ROOT / entrypoint
    if source_root:
        source_root = Path(source_root)
        if not source_root.is_absolute():
            source_root = ROOT / source_root
    entry, root, files, sources = source_manifest(entrypoint, source_root or None)
    key = sha256(entry)[:12] + "_" + uuid.uuid4().hex[:8]
    result = compile_sources(sources, ROOT / "build/plugins" / (key + ".dll"))
    result["entrypoint"] = str(entry)
    result["manifest"] = [{"path": str(p.relative_to(root)).replace("\\", "/"), "sha256": sha256(p)} for p in files]
    return result


def deploy_plugin(entrypoint: str, package: str, source_root: str = "", *, game: Path = GAME):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", package) or ".." in package:
        raise VamError("invalid_path", "package must be a simple directory name")
    entry, root, files, _ = source_manifest(entrypoint, source_root or None)
    compiled = build_plugin(str(entry), str(root))
    with GameLease.hold(RUNTIME):
        run_id = uuid.uuid4().hex
        backup = RUNTIME / "deployments" / run_id
        target = game / "Custom/Scripts" / package
        records = []
        backup.mkdir(parents=True, exist_ok=True)
        try:
            for p in files:
                relative = p.relative_to(root)
                dest = target / relative
                previous = sha256(dest) if dest.is_file() else None
                if dest.exists():
                    saved = backup / "previous" / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, saved)
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.with_name(dest.name + "." + run_id + ".tmp")
                shutil.copy2(p, temp)
                record = {"relative": relative.as_posix(), "target": str(dest), "previous_sha256": previous, "sha256": sha256(temp)}
                records.append(record)
                temp.replace(dest)
        except Exception:
            for record in reversed(records):
                dest = Path(record["target"])
                if dest.is_file() and sha256(dest) == record["sha256"]:
                    prior = backup / "previous" / record["relative"]
                    if prior.exists():
                        shutil.copy2(prior, dest)
                    else:
                        dest.unlink()
            raise
        report = {"ok": True, "deployment_id": run_id, "entrypoint": "Custom/Scripts/" + package + "/" + entry.relative_to(root).as_posix(), "files": records, "compile": compiled}
        atomic_json(backup / "manifest.json", report)
        return report


def build_api_indexer():
    output = ROOT / "build/ApiIndex.exe"
    source = ROOT / "tools/ApiIndex.cs"
    if output.exists() and output.stat().st_mtime >= source.stat().st_mtime:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [str(COMPILER), "/nologo", "/target:exe", f"/out:{output}", "/reference:System.Web.Extensions.dll", f"/reference:{GAME / 'BepInEx/core/Mono.Cecil.dll'}", str(source)]
    proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=60, **hidden_options())
    if proc.returncode:
        raise VamError("compile_error", proc.stdout + proc.stderr)
    return output


def api_query(action: str, query: str = "", type_name: str = "", assembly: str = "Assembly-CSharp.dll", offset: int = 0, limit: int = 30):
    if Path(assembly).name != assembly:
        raise VamError("invalid_path", "assembly must be a filename")
    path = GAME / "VaM_Data/Managed" / assembly
    if not path.is_file():
        path = GAME / "BepInEx/core" / assembly
    if not path.is_file():
        raise VamError("not_found", "Assembly not found")
    digest = sha256(path)
    cache = RUNTIME / "api" / digest
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "index.json"
    if not index_path.exists():
        proc = subprocess.run([str(build_api_indexer()), str(path), str(GAME / "BepInEx/core/Mono.Cecil.dll")], capture_output=True, encoding="utf-8", errors="replace", timeout=120, **hidden_options())
        if proc.returncode:
            raise VamError("api_index_failed", proc.stderr + proc.stdout[:2000])
        atomic_json(index_path, json.loads(proc.stdout))
    index = read_json(index_path)
    limit, offset = max(1, min(limit, 100)), max(offset, 0)
    if action == "search":
        needle = query.casefold()
        matches = []
        for t in index["types"]:
            if needle in t["name"].casefold():
                matches.append({"type": t["name"], "kind": t["kind"]})
            for category in ("methods", "properties", "fields"):
                for member in t[category]:
                    if needle in member.casefold():
                        matches.append({"type": t["name"], "kind": category, "signature": member})
        return {"ok": True, "assembly_sha256": digest, "total": len(matches), "matches": matches[offset:offset + limit]}
    if action != "type":
        raise VamError("unsupported", "API action must be search or type")
    found = next((t for t in index["types"] if t["name"].replace("/", "+") == type_name.replace("/", "+")), None)
    if not found:
        raise VamError("not_found", "Type not found in assembly index")
    result = {"ok": True, "assembly_sha256": digest, **found}
    if ILSPY.is_file():
        source_cache = cache / (hashlib_name(type_name) + ".cs")
        if not source_cache.exists():
            proc = subprocess.run([str(ILSPY), "-t", type_name, str(path)], capture_output=True, encoding="utf-8", errors="replace", timeout=120, **hidden_options())
            if not proc.returncode:
                source_cache.write_text(proc.stdout, encoding="utf-8")
        if source_cache.exists():
            lines = source_cache.read_text(encoding="utf-8").splitlines()
            start = offset
            result.update({"source_lines": lines[start:start + limit * 10], "source_start_line": start + 1, "source_total_lines": len(lines), "source_cache": str(source_cache)})
    return result


def hashlib_name(value: str):
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:24]
