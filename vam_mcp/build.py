from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from pathlib import Path

from .common import ROOT, GAME, RUNTIME, GameLease, VamError, atomic_json, hidden_options, read_json, sha256

WINDOWS = Path(os.environ.get("WINDIR", r"C:\Windows"))
COMPILER = Path(os.environ.get("VAM_CSC", str(WINDOWS / "Microsoft.NET/Framework64/v4.0.30319/csc.exe")))
ILSPY = Path(os.environ.get("ILSPY_PATH") or shutil.which("ilspycmd") or "")


def references(game: Path = GAME):
    managed = game / "VaM_Data/Managed"
    names = ["mscorlib.dll", "System.dll", "System.Core.dll", "System.Xml.dll", "System.Runtime.Serialization.dll", "Assembly-CSharp.dll", "Assembly-CSharp-firstpass.dll", "UnityEngine.dll", "UnityEngine.UI.dll"]
    return [managed / n for n in names if (managed / n).is_file()] + sorted(managed.glob("UnityEngine.*Module.dll"))


def profile_references(profile: str, game: Path = GAME):
    if profile not in {"mvrscript", "bepinex"}:
        raise VamError("unsupported_profile", f"Profile cannot compile C# sources: {profile}")
    refs = references(game)
    if profile == "bepinex":
        refs += [game / "BepInEx/core/BepInEx.dll", game / "BepInEx/core/0Harmony.dll"]
    return refs


def _resolve_reference_paths(paths, source_root: Path):
    resolved = []
    for value in paths or []:
        path = Path(value)
        if not path.is_absolute():
            candidates = [source_root / path, GAME / path, ROOT / path]
            path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".dll":
            raise VamError("invalid_reference", "Explicit compiler references must be existing DLL files", path=str(path))
        resolved.append(path)
    return resolved


def compile_sources(sources: list[Path], output: Path, *, bridge=False, game: Path = GAME, profile="mvrscript", reference_paths=(), debug=False):
    if not COMPILER.is_file():
        raise VamError("missing_tool", f"C# compiler not found: {COMPILER}")
    output.parent.mkdir(parents=True, exist_ok=True)
    refs = profile_references(profile, game)
    if bridge and profile != "bepinex":
        refs += [game / "BepInEx/core/BepInEx.dll", game / "BepInEx/core/0Harmony.dll"]
    explicit = _resolve_reference_paths(reference_paths, ROOT)
    refs = list(dict.fromkeys(refs + explicit))
    args = [str(COMPILER), "/nologo", "/utf8output", "/codepage:65001", "/noconfig", "/nostdlib+", "/target:library", "/langversion:5", f"/out:{output}"]
    if debug:
        args += ["/debug:full", "/optimize-", f"/pdb:{output.with_suffix('.pdb')}"]
    args += [f"/reference:{p}" for p in refs if p.is_file()] + [str(p) for p in sources]
    proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=120, **hidden_options())
    result = {"ok": proc.returncode == 0, "output": str(output), "diagnostics": proc.stdout + proc.stderr, "sources": [str(p) for p in sources], "references": [str(p) for p in refs if p.is_file()], "runtime": "game-shipped CLR 2.0 references", "language_version": 5, "debug": debug}
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


def _project_paths(entrypoint: str | Path, source_root: str | Path = ""):
    entrypoint = Path(entrypoint)
    if not entrypoint.is_absolute():
        entrypoint = ROOT / entrypoint
    if source_root:
        source_root = Path(source_root)
        if not source_root.is_absolute():
            source_root = ROOT / source_root
    return entrypoint, source_root


def _artifact_manifest(paths, root: Path):
    result = []
    seen = set()
    for value in paths or []:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        path = path.resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(root):
            raise VamError("invalid_path", "Artifact files must be files within source_root", path=str(path))
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        result.append({"path": relative, "source": str(path), "sha256": sha256(path)})
    return result


def _validate_var(path: Path):
    if path.suffix.lower() != ".var" or not zipfile.is_zipfile(path):
        raise VamError("invalid_var", "var_bundle requires a readable .var archive", path=str(path))
    with zipfile.ZipFile(path) as archive:
        members = set()
        for item in archive.infolist():
            name = item.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                raise VamError("invalid_var", "VAR archive contains an unsafe path", member=item.filename)
            members.add(name.casefold())
        if "meta.json" not in members:
            raise VamError("invalid_var", "VAR archive is missing root meta.json")
        try:
            metadata = json.loads(archive.read(next(item for item in archive.namelist() if item.replace("\\", "/").casefold() == "meta.json")))
        except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
            raise VamError("invalid_var", "VAR root meta.json is not valid JSON", detail=str(exc)) from exc
        if not isinstance(metadata, dict):
            raise VamError("invalid_var", "VAR root meta.json must contain a JSON object")
        scripts = sorted(
            name.replace("\\", "/")
            for name in archive.namelist()
            if name.replace("\\", "/").casefold().startswith("custom/scripts/")
            and name.casefold().endswith(".cs")
        )
        return {"archive_members": len(archive.infolist()), "metadata": metadata, "script_members": scripts}


def _var_plugin_entrypoints(path: Path, inspection: dict):
    package_id = path.stem
    prefix = "AddonPackages/" + package_id + ":/"
    return [prefix + member.lstrip("/") for member in inspection["script_members"]]


def build_plugin(entrypoint: str, source_root: str = "", *, profile="mvrscript", reference_paths=(), debug=False, artifact_files=()):
    if profile == "var_bundle":
        entry, _ = _project_paths(entrypoint, source_root)
        entry = entry.resolve(strict=True)
        inspection = _validate_var(entry)
        return {"ok": True, "profile": profile, "artifact": str(entry), "artifact_sha256": sha256(entry), **inspection, "plugin_entrypoints": _var_plugin_entrypoints(entry, inspection), "compiler": None}
    if profile not in {"mvrscript", "bepinex"}:
        raise VamError("unsupported_profile", f"Unknown plugin profile: {profile}")
    entrypoint, source_root = _project_paths(entrypoint, source_root)
    entry, root, files, sources = source_manifest(entrypoint, source_root or None)
    output_name = entry.stem + (".dll" if profile == "bepinex" else ".dll")
    key = sha256(entry)[:12] + "_" + uuid.uuid4().hex[:8]
    output = ROOT / "build/plugins" / key / output_name
    resolved_references = _resolve_reference_paths(reference_paths, root)
    result = compile_sources(sources, output, profile=profile, reference_paths=resolved_references, debug=debug)
    result["profile"] = profile
    result["entrypoint"] = str(entry)
    result["manifest"] = [{"path": str(p.relative_to(root)).replace("\\", "/"), "source": str(p), "sha256": sha256(p)} for p in files]
    result["manifest"].extend(_artifact_manifest(artifact_files, root))
    if debug and output.with_suffix(".pdb").is_file():
        result["symbols"] = str(output.with_suffix(".pdb"))
    return result


def _is_reparse_point(path: Path):
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_deploy_path(dest: Path, game: Path, target_root: Path):
    game_root = game.resolve(strict=True)
    allowed_root = (game_root / target_root).resolve(strict=False)
    candidate = dest.absolute()
    try:
        relative = candidate.relative_to(allowed_root)
    except ValueError:
        raise VamError("invalid_path", "Deployment target escapes its profile directory", path=str(candidate)) from None
    current = game_root
    for part in (allowed_root.relative_to(game_root).parts + relative.parts):
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise VamError("invalid_path", "Deployment path contains a symbolic link or reparse point", path=str(current))
        if not current.resolve(strict=False).is_relative_to(game_root):
            raise VamError("invalid_path", "Deployment target resolves outside the game directory", path=str(current))


def _install_files(files, target_root: Path, profile_root: Path, game: Path, backup: Path, run_id: str, records: list):
    seen = set()
    for source, relative in files:
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise VamError("invalid_path", "Artifact destination must remain relative to its package", path=str(relative))
        normalized = relative.as_posix()
        if normalized in seen:
            raise VamError("artifact_conflict", "Multiple artifacts map to the same deployment path", path=normalized)
        seen.add(normalized)
        if not Path(source).is_file():
            raise VamError("not_found", "Deployment artifact does not exist", path=str(source))
        dest = target_root / Path(relative)
        _validate_deploy_path(dest, game, profile_root)
        previous = sha256(dest) if dest.is_file() else None
        if dest.exists():
            saved = backup / "previous" / Path(relative)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, saved)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _validate_deploy_path(dest, game, profile_root)
        temp = dest.with_name(dest.name + "." + run_id + ".tmp")
        try:
            shutil.copy2(source, temp)
            record = {"relative": Path(relative).as_posix(), "target": str(dest), "previous_sha256": previous, "sha256": sha256(temp)}
            records.append(record)
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)
    return records


def _profile_target(profile: str, package: str, entry: Path, game: Path):
    if profile == "mvrscript":
        return game / "Custom/Scripts" / package, Path("Custom/Scripts") / package
    if profile == "bepinex":
        return game / "BepInEx/plugins" / package, Path("BepInEx/plugins") / package
    if profile == "var_bundle":
        filename = package or entry.name
        if Path(filename).name != filename or not filename.lower().endswith(".var"):
            raise VamError("invalid_path", "var_bundle package must be a simple .var filename")
        return game / "AddonPackages" / filename, Path("AddonPackages") / filename
    raise VamError("unsupported_profile", f"Unknown plugin profile: {profile}")


def deploy_plugin(entrypoint: str, package: str = "", source_root: str = "", *, game: Path = GAME, profile="mvrscript", reference_paths=(), debug=False, artifact_files=()):
    if profile != "var_bundle" and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", package) or ".." in package):
        raise VamError("invalid_path", "package must be a simple directory name")
    entrypoint, source_root = _project_paths(entrypoint, source_root)
    if profile == "var_bundle":
        entry = entrypoint.resolve(strict=True)
        inspection = _validate_var(entry)
        root = entry.parent
        source_files = []
        extra_files = []
        compiled = build_plugin(str(entry), profile=profile)
    else:
        entry, root, source_files, _ = source_manifest(entrypoint, source_root or None)
        extra_files = _artifact_manifest(artifact_files, root)
        compiled = build_plugin(str(entry), str(root), profile=profile, reference_paths=reference_paths, debug=debug, artifact_files=artifact_files)
    target, profile_root = _profile_target(profile, package, entry, game)
    with GameLease.hold(RUNTIME):
        run_id = uuid.uuid4().hex
        backup = RUNTIME / "deployments" / run_id
        backup.mkdir(parents=True, exist_ok=True)
        records = []
        try:
            if profile == "mvrscript":
                deploy_files = [(path, path.relative_to(root)) for path in source_files]
                deploy_files.extend((Path(item["source"]), Path(item["path"])) for item in extra_files)
            elif profile == "bepinex":
                dll = Path(compiled["output"])
                deploy_files = [(dll, Path(dll.name))]
                symbols = Path(compiled["symbols"]) if compiled.get("symbols") else None
                if symbols and symbols.is_file():
                    deploy_files.append((symbols, Path(symbols.name)))
                deploy_files.extend((Path(item["source"]), Path(item["path"])) for item in extra_files)
            else:
                deploy_files = [(entry, Path(target.name))]
            _install_files(deploy_files, target.parent if profile == "var_bundle" else target, profile_root, game, backup, run_id, records)
        except Exception:
            for record in reversed(locals().get("records", [])):
                dest = Path(record["target"])
                if dest.is_file() and sha256(dest) == record["sha256"]:
                    prior = backup / "previous" / record["relative"]
                    if prior.exists():
                        shutil.copy2(prior, dest)
                    else:
                        dest.unlink()
            raise
        if profile == "mvrscript":
            installed_entry = "Custom/Scripts/" + package + "/" + entry.relative_to(root).as_posix()
        elif profile == "bepinex":
            installed_entry = "BepInEx/plugins/" + package + "/" + Path(compiled["output"]).name
        else:
            installed_entry = "AddonPackages/" + target.name
        report = {"ok": True, "profile": profile, "deployment_id": run_id, "entrypoint": installed_entry, "target": str(target), "files": records, "compile": compiled}
        if profile == "var_bundle":
            report["entrypoint"] = compiled["plugin_entrypoints"][0] if compiled["plugin_entrypoints"] else ""
            report["plugin_entrypoints"] = compiled["plugin_entrypoints"]
        atomic_json(backup / "manifest.json", report)
        return report


def rollback_deployment(deployment_id: str, *, game: Path = GAME):
    if not re.fullmatch(r"[0-9a-f]{32}", deployment_id):
        raise VamError("invalid_deployment", "deployment_id must be a deployment manifest id")
    folder = RUNTIME / "deployments" / deployment_id
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise VamError("not_found", "Deployment manifest not found", deployment_id=deployment_id)
    manifest = read_json(manifest_path)
    profile = manifest.get("profile")
    target = Path(manifest.get("target", ""))
    expected_roots = {"mvrscript": Path("Custom/Scripts"), "bepinex": Path("BepInEx/plugins"), "var_bundle": Path("AddonPackages")}
    if profile not in expected_roots or not target.is_absolute():
        raise VamError("invalid_deployment", "Deployment manifest has an unsupported target")
    game_root = game.resolve(strict=True)
    try:
        target_relative = target.absolute().relative_to(game_root / expected_roots[profile])
    except ValueError:
        raise VamError("invalid_deployment", "Deployment manifest target escapes its profile directory") from None
    if not target_relative.parts:
        raise VamError("invalid_deployment", "Deployment manifest target is not a package")
    package_root = target if profile != "var_bundle" else target.parent
    with GameLease.hold(RUNTIME):
        preflight = []
        for record in manifest.get("files", []):
            dest = Path(record["target"])
            expected_dest = target if profile == "var_bundle" else target / record["relative"]
            if dest.absolute() != expected_dest.absolute():
                raise VamError("invalid_deployment", "Manifest file does not match the deployment target", path=str(dest))
            _validate_deploy_path(dest, game, package_root.relative_to(game_root))
            if not dest.is_file() or sha256(dest) != record["sha256"]:
                raise VamError("deployment_conflict", "A deployed file changed since installation; rollback stopped", path=str(dest))
            previous = folder / "previous" / record["relative"]
            expected_previous = record.get("previous_sha256")
            if expected_previous and (not previous.is_file() or sha256(previous) != expected_previous):
                raise VamError("backup_invalid", "A deployment backup is missing or has changed", path=str(previous))
            preflight.append((dest, previous if expected_previous else None))
        for dest, previous in reversed(preflight):
            if previous:
                temp = dest.with_name(dest.name + ".rollback.tmp")
                shutil.copy2(previous, temp)
                temp.replace(dest)
            else:
                dest.unlink()
    return {"ok": True, "deployment_id": deployment_id, "rolled_back": True, "files": [str(path) for path, _ in preflight]}


def build_api_indexer():
    output = ROOT / "build/ApiIndex.exe"
    manifest_path = ROOT / "build/ApiIndex-manifest.json"
    source = ROOT / "tools/ApiIndex.cs"
    cecil = GAME / "BepInEx/core/Mono.Cecil.dll"
    system_web = COMPILER.parent / "System.Web.Extensions.dll"
    if not COMPILER.is_file() or not cecil.is_file():
        raise VamError("missing_tool", "ApiIndex requires the configured C# compiler and BepInEx Mono.Cecil.dll")
    inputs = {str(path): sha256(path) for path in (source, COMPILER, cecil) if path.is_file()}
    if system_web.is_file():
        inputs[str(system_web)] = sha256(system_web)
    if output.is_file() and manifest_path.is_file():
        previous = read_json(manifest_path)
        if previous.get("inputs") == inputs and previous.get("output_sha256") == sha256(output):
            return output
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_name(output.stem + "." + uuid.uuid4().hex + output.suffix)
    args = [str(COMPILER), "/nologo", "/target:exe", f"/out:{temp_output}", f"/reference:{system_web if system_web.is_file() else 'System.Web.Extensions.dll'}", f"/reference:{cecil}", str(source)]
    proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=60, **hidden_options())
    if proc.returncode:
        temp_output.unlink(missing_ok=True)
        raise VamError("compile_error", proc.stdout + proc.stderr)
    temp_output.replace(output)
    atomic_json(manifest_path, {"inputs": inputs, "output_sha256": sha256(output)})
    return output


def api_query(action: str, query: str = "", type_name: str = "", assembly: str = "Assembly-CSharp.dll", offset: int = 0, limit: int = 30, assembly_path: str = ""):
    if assembly_path:
        path = Path(assembly_path).resolve(strict=True)
        allowed = [GAME / "VaM_Data/Managed", GAME / "BepInEx/core", GAME / "BepInEx/plugins", GAME / "Custom/Scripts"]
        allowed = [root.resolve(strict=False) for root in allowed]
        if not path.is_file() or path.suffix.lower() != ".dll" or not any(path.is_relative_to(root) for root in allowed):
            raise VamError("invalid_path", "assembly_path must identify a DLL under VaM managed, BepInEx, or Custom/Scripts directories")
    else:
        if Path(assembly).name != assembly:
            raise VamError("invalid_path", "assembly must be a filename")
        path = GAME / "VaM_Data/Managed" / assembly
        if not path.is_file():
            path = GAME / "BepInEx/core" / assembly
        if not path.is_file():
            raise VamError("not_found", "Assembly not found")
    digest = sha256(path)
    indexer = build_api_indexer()
    indexer_digest = sha256(indexer)
    cache = RUNTIME / "api" / digest / indexer_digest
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "index.json"
    if not index_path.exists():
        proc = subprocess.run([str(indexer), str(path), str(GAME / "BepInEx/core/Mono.Cecil.dll")], capture_output=True, encoding="utf-8", errors="replace", timeout=120, **hidden_options())
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
