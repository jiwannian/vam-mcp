using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using BepInEx;
using SimpleJSON;
using UnityEngine;

namespace VaMMcp
{
    public sealed partial class Bridge
    {
        private IEnumerator Dispatch(Job job)
        {
            JSONClass a = job.Args;
            if (job.Method == "logs") { Finish(job, ReadLogs(a)); yield break; }
            if (job.Method == "status") { Finish(job, Status()); yield break; }
            if (job.Method == "bepinex") { Finish(job, BepInExPlugins(a)); yield break; }
            if (controller == null) throw new BridgeFailure("not_ready", "SuperController has not initialized");
            switch (job.Method)
            {
                case "scene": yield return Scene(job, a); break;
                case "atom": yield return Atoms(job, a); break;
                case "plugin": yield return Plugins(job, a); break;
                case "storable": Finish(job, Storable(a)); break;
                case "morph": Finish(job, Morph(a)); break;
                case "ui_tree": Finish(job, UITree(a)); break;
                case "ui_action": Finish(job, UIAction(a)); break;
                case "file_dialog": Finish(job, FileDialog(a)); break;
                case "capture": yield return Capture(job, a); break;
                case "probe": yield return Probe(job, a); break;
                case "quit": Finish(job, Obj()); Application.Quit(); break;
                default: throw new BridgeFailure("unsupported", "Unknown operation: " + job.Method);
            }
        }

        private JSONClass Status()
        {
            JSONClass value = Obj(); value["bridge_version"] = "0.1.0"; value["protocol"].AsInt = 1;
            value["instance_id"] = instanceId; value["pid"].AsInt = processId; value["game_root"] = gameRoot;
            value["unity_version"] = Application.unityVersion; value["scene_revision"].AsInt = sceneRevision;
            value["ui_revision"].AsInt = uiRevision; value["ready"].AsBool = controller != null && !controller.isLoading;
            value["is_loading"].AsBool = controller != null && controller.isLoading;
            value["game_version"] = controller == null ? "" : controller.GetVersion();
            value["scene"] = controller == null ? "" : controller.LoadedSceneName;
            value["atom_count"].AsInt = controller == null ? 0 : controller.GetAtoms().Count;
            value["screen_width"].AsInt = Screen.width; value["screen_height"].AsInt = Screen.height;
            value["graphics_device"] = SystemInfo.graphicsDeviceName; value["run_in_background"].AsBool = Application.runInBackground;
            return value;
        }

        private static object ReflectedMember(object target, string name)
        {
            if (target == null) return null;
            Type type = target as Type ?? target.GetType();
            object instance = target is Type ? null : target;
            BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
            PropertyInfo property = type.GetProperty(name, flags);
            if (property != null && property.GetIndexParameters().Length == 0) return property.GetValue(instance, null);
            FieldInfo field = type.GetField(name, flags);
            return field == null ? null : field.GetValue(instance);
        }

        private JSONClass BepInExPlugins(JSONClass args)
        {
            Type chainloader = typeof(BaseUnityPlugin).Assembly.GetType("BepInEx.Bootstrap.Chainloader");
            object source = ReflectedMember(chainloader, "PluginInfos");
            if (source == null) source = ReflectedMember(chainloader, "Plugins");
            IEnumerable entries = source as IEnumerable;
            if (entries == null) throw new BridgeFailure("unsupported", "This BepInEx runtime does not expose its loaded plugin registry");
            string requested = Str(args, "plugin_uid");
            JSONArray plugins = new JSONArray();
            foreach (object entry in entries)
            {
                object info = ReflectedMember(entry, "Value") ?? entry;
                object metadata = ReflectedMember(info, "Metadata");
                if (metadata == null) continue;
                string guid = Convert.ToString(ReflectedMember(metadata, "GUID") ?? ReflectedMember(metadata, "Guid"));
                string name = Convert.ToString(ReflectedMember(metadata, "Name"));
                string version = Convert.ToString(ReflectedMember(metadata, "Version"));
                if (requested != "" && !String.Equals(requested, guid, StringComparison.OrdinalIgnoreCase)) continue;
                JSONClass plugin = Obj(); plugin["guid"] = guid ?? ""; plugin["name"] = name ?? ""; plugin["version"] = version ?? "";
                plugin["instance_type"] = info.GetType().FullName;
                plugins.Add(plugin);
            }
            JSONClass result = Obj(); result["plugins"] = plugins; result["total"].AsInt = plugins.Count;
            result["source"] = "BepInEx.Bootstrap.Chainloader";
            return result;
        }

        private IEnumerator Scene(Job job, JSONClass a)
        {
            string action = Str(a, "action", "inspect"), path = Str(a, "path");
            if (action == "inspect") { Finish(job, Status()); yield break; }
            long start; lock (logGate) start = logSequence;
            if (controller.isLoading) throw new BridgeFailure("busy", "A scene is already loading");
            if (action == "new") { SceneLoaded(); controller.NewScene(); }
            else if (action == "load")
            {
                if (!File.Exists(path)) throw new BridgeFailure("not_found", "Scene file not found: " + path);
                SceneLoaded(); controller.LoadForEdit(path.Replace('\\', '/'));
            }
            else if (action == "save")
            {
                if (String.IsNullOrEmpty(path)) path = Path.Combine(gameRoot, "Saves/VaMMcp/" + instanceId + "/scene/snapshot.json");
                path = Path.GetFullPath(path); Directory.CreateDirectory(Path.GetDirectoryName(path));
                controller.Save(path.Replace('\\', '/'));
                yield return null;
                string error = SceneErrorAfter(start, true); if (error != null) throw new BridgeFailure("scene_save_failed", error);
                if (!File.Exists(path)) throw new BridgeFailure("scene_save_failed", "Native save did not create a scene file");
                JSONClass saved = Obj(); saved["path"] = path; saved["exists"].AsBool = true; Finish(job, saved); yield break;
            }
            else throw new BridgeFailure("unsupported", "Unknown scene action");
            yield return null; while (controller.isLoading) yield return null;
            string loadError = SceneErrorAfter(start, false); if (loadError != null) throw new BridgeFailure("scene_load_failed", loadError);
            InvalidateUI(); yield return null; Finish(job, Status());
        }

        private Atom RequireAtom(JSONClass a)
        {
            string uid = Str(a, "atom_uid"); Atom atom = controller.GetAtomByUid(uid);
            if (atom == null) throw new BridgeFailure("not_found", "Atom not found: " + uid); return atom;
        }
        private static JSONArray Vector(Vector3 v)
        { JSONArray a = new JSONArray(); a.Add(new JSONData(v.x)); a.Add(new JSONData(v.y)); a.Add(new JSONData(v.z)); return a; }
        private static float Number(JSONNode node)
        {
            float value;
            if (node == null || !Single.TryParse((string)node, NumberStyles.Float, CultureInfo.InvariantCulture, out value) || Single.IsNaN(value) || Single.IsInfinity(value))
                throw new BridgeFailure("invalid_value", "Expected a finite number");
            return value;
        }
        private static Vector3 ReadVector(JSONNode value)
        {
            if (value == null || value.AsArray == null || value.Count != 3) throw new BridgeFailure("invalid_value", "Expected three numeric components");
            return new Vector3(Number(value[0]), Number(value[1]), Number(value[2]));
        }
        private JSONClass AtomInfo(Atom atom)
        {
            JSONClass v = Obj(); v["uid"] = atom.uid; v["type"] = atom.type; v["active"].AsBool = atom.gameObject.activeInHierarchy;
            if (atom.mainController != null) { v["position"] = Vector(atom.mainController.transform.position); v["rotation"] = Vector(atom.mainController.transform.eulerAngles); }
            return v;
        }
        private IEnumerator Atoms(Job job, JSONClass a)
        {
            string action = Str(a, "action", "list");
            if (action == "list")
            {
                JSONArray list = new JSONArray(); string query = Str(a, "query");
                foreach (Atom item in controller.GetAtoms()) if (query == "" || (item.uid + " " + item.type).IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0) list.Add(AtomInfo(item));
                JSONClass v = Obj(); v["atoms"] = list; Finish(job, v); yield break;
            }
            if (action == "create")
            {
                string uid = Str(a, "atom_uid"); if (uid == "") uid = "Mcp_" + Guid.NewGuid().ToString("N").Substring(0, 8);
                if (controller.GetAtomByUid(uid) != null) throw new BridgeFailure("conflict", "Atom uid already exists");
                yield return controller.AddAtomByType(Str(a, "type", "Person"), uid, false, false, false);
                Atom created = controller.GetAtomByUid(uid); if (created == null) throw new BridgeFailure("not_found", "Atom type could not be created");
                Finish(job, AtomInfo(created)); yield break;
            }
            Atom atom = RequireAtom(a);
            if (action == "inspect")
            {
                JSONClass v = AtomInfo(atom); JSONArray ids = new JSONArray(); foreach (string id in atom.GetStorableIDs()) ids.Add(id);
                v["storables"] = ids; Finish(job, v);
            }
            else if (action == "remove") { controller.RemoveAtom(atom); InvalidateUI(); Finish(job, Obj()); }
            else if (action == "transform")
            {
                if (atom.mainController == null) throw new BridgeFailure("unsupported", "Atom has no main controller");
                if (a["position"] != null) atom.mainController.transform.position = ReadVector(a["position"]);
                if (a["rotation"] != null) atom.mainController.transform.eulerAngles = ReadVector(a["rotation"]);
                Finish(job, AtomInfo(atom));
            }
            else if (action == "clone")
            {
                string uid = Str(a, "new_uid"); if (uid == "") uid = "McpClone_" + Guid.NewGuid().ToString("N").Substring(0, 8);
                if (controller.GetAtomByUid(uid) != null) throw new BridgeFailure("conflict", "Clone uid already exists");
                JSONArray stored = new JSONArray(); atom.Store(stored); JSONClass copy = stored[0].AsObject; copy["id"] = uid;
                JSONClass scene = Obj(); scene["atoms"] = stored;
                string path = Path.Combine(gameRoot, "Saves/VaMMcp/" + instanceId + "/scene/clone_" + Guid.NewGuid().ToString("N") + ".json");
                Directory.CreateDirectory(Path.GetDirectoryName(path)); File.WriteAllText(path, Serialize(scene));
                controller.LoadMerge(path.Replace('\\', '/')); yield return null; while (controller.isLoading) yield return null;
                Atom clone = controller.GetAtomByUid(uid); if (clone == null) throw new BridgeFailure("operation_failed", "Native clone merge failed");
                Finish(job, AtomInfo(clone));
            }
            else throw new BridgeFailure("unsupported", "Unknown atom action");
        }

        private JSONClass Storable(JSONClass a)
        {
            Atom atom = RequireAtom(a); string id = Str(a, "storable_id"), action = Str(a, "action", "list");
            JSONClass result = Obj();
            if (id == "") { JSONArray ids = new JSONArray(); foreach (string item in atom.GetStorableIDs()) ids.Add(item); result["storables"] = ids; return result; }
            JSONStorable st = atom.GetStorableByID(id); if (st == null) throw new BridgeFailure("not_found", "Storable not found: " + id);
            if (action == "list")
            {
                JSONArray list = new JSONArray(); foreach (string name in st.GetAllParamAndActionNames()) list.Add(ParamInfo(st, name)); result["parameters"] = list; return result;
            }
            string param = Str(a, "name"); JSONStorable.Type type = st.GetParamOrActionType(param);
            if (type == JSONStorable.Type.None) throw new BridgeFailure("not_found", "Parameter/action not found: " + param);
            if (action == "call")
            {
                if (!st.IsAction(param)) throw new BridgeFailure("invalid_value", "Parameter is not an action");
                st.CallAction(param); result["called"] = param; return result;
            }
            if (action == "set")
            {
                JSONNode value = a["value"]; if (value == null) throw new BridgeFailure("invalid_value", "value is required");
                if (type == JSONStorable.Type.Float)
                {
                    float f = Number(value); JSONStorableFloat p = st.GetFloatJSONParam(param);
                    if (f < p.min || f > p.max) throw new BridgeFailure("invalid_value", "Value is outside parameter limits"); st.SetFloatParamValue(param, f);
                }
                else if (type == JSONStorable.Type.Bool)
                {
                    if ((string)value != "true" && (string)value != "false") throw new BridgeFailure("invalid_value", "Expected a boolean"); st.SetBoolParamValue(param, value.AsBool);
                }
                else if (type == JSONStorable.Type.StringChooser)
                {
                    List<string> choices = st.GetStringChooserJSONParamChoices(param);
                    if (choices != null && !choices.Contains((string)value)) throw new BridgeFailure("invalid_value", "Value is not an available choice"); st.SetStringChooserParamValue(param, value);
                }
                else if (type == JSONStorable.Type.Url) st.SetUrlParamValue(param, value);
                else if (type == JSONStorable.Type.String) st.SetStringParamValue(param, value);
                else if (type == JSONStorable.Type.Vector3) st.SetVector3ParamValue(param, ReadVector(value));
                else if (type == JSONStorable.Type.Color)
                {
                    Vector3 hsv = ReadVector(value); if (hsv.x < 0 || hsv.x > 1 || hsv.y < 0 || hsv.y > 1 || hsv.z < 0 || hsv.z > 1) throw new BridgeFailure("invalid_value", "HSV components must be between 0 and 1");
                    HSVColor color = new HSVColor(); color.H = hsv.x; color.S = hsv.y; color.V = hsv.z; st.SetColorParamValue(param, color);
                }
                else throw new BridgeFailure("unsupported", "Unsupported parameter type: " + type.ToString());
            }
            else if (action != "get") throw new BridgeFailure("unsupported", "Unknown storable action");
            return ParamInfo(st, param);
        }

        private JSONClass ParamInfo(JSONStorable st, string name)
        {
            JSONClass v = Obj(); v["name"] = name; JSONStorable.Type type = st.GetParamOrActionType(name); v["type"] = type.ToString();
            if (type == JSONStorable.Type.Float) { JSONStorableFloat p = st.GetFloatJSONParam(name); v["value"].AsFloat = p.val; v["min"].AsFloat = p.min; v["max"].AsFloat = p.max; }
            else if (type == JSONStorable.Type.Bool) v["value"].AsBool = st.GetBoolParamValue(name);
            else if (type == JSONStorable.Type.StringChooser)
            { v["value"] = st.GetStringChooserParamValue(name); JSONArray c = new JSONArray(); List<string> choices = st.GetStringChooserJSONParamChoices(name); if (choices != null) foreach (string x in choices) c.Add(x); v["choices"] = c; }
            else if (type == JSONStorable.Type.Url) v["value"] = st.GetUrlParamValue(name);
            else if (type == JSONStorable.Type.String) v["value"] = st.GetStringParamValue(name);
            else if (type == JSONStorable.Type.Vector3) v["value"] = Vector(st.GetVector3ParamValue(name));
            else if (type == JSONStorable.Type.Color) { HSVColor c = st.GetColorParamValue(name); v["value"] = Vector(new Vector3(c.H, c.S, c.V)); }
            return v;
        }

        private JSONClass Morph(JSONClass a)
        {
            DAZCharacterSelector geometry = RequireAtom(a).GetStorableByID("geometry") as DAZCharacterSelector;
            if (geometry == null) throw new BridgeFailure("unsupported", "Atom has no Person geometry");
            string action = Str(a, "action", "list"), uid = Str(a, "uid"), query = Str(a, "query");
            int offset = Math.Max(0, Int(a, "offset", 0)), limit = Math.Max(1, Math.Min(500, Int(a, "limit", 100))), count = 0;
            JSONArray list = new JSONArray(); HashSet<string> seen = new HashSet<string>();
            foreach (DAZMorphBank bank in new DAZMorphBank[] { geometry.morphBank1, geometry.morphBank2, geometry.morphBank3 })
            {
                if (bank == null || bank.morphs == null) continue;
                foreach (DAZMorph morph in bank.morphs)
                {
                    if (!seen.Add(morph.uid)) continue;
                    if (action != "list" && morph.uid != uid) continue;
                    if (query != "" && (morph.uid + " " + morph.resolvedDisplayName + " " + morph.resolvedRegionName).IndexOf(query, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (action == "set")
                    {
                        float value = Number(a["value"]);
                        if (morph.disable || morph.isDriven || value < morph.min || value > morph.max) throw new BridgeFailure("invalid_value", "Morph is driven, disabled or value exceeds its range");
                        morph.morphValue = value;
                    }
                    JSONClass v = Obj(); v["uid"] = morph.uid; v["name"] = morph.resolvedDisplayName; v["region"] = morph.resolvedRegionName;
                    v["value"].AsFloat = morph.morphValue; v["min"].AsFloat = morph.min; v["max"].AsFloat = morph.max;
                    v["disabled"].AsBool = morph.disable; v["driven"].AsBool = morph.isDriven;
                    if (action != "list") return v;
                    if (count++ >= offset && list.Count < limit) list.Add(v);
                }
            }
            if (action != "list") throw new BridgeFailure("not_found", "Morph not found");
            JSONClass result = Obj(); result["morphs"] = list; result["total"].AsInt = count; return result;
        }

        private MVRPluginManager RequireManager(Atom atom)
        {
            MVRPluginManager manager = atom.GetStorableByID("PluginManager") as MVRPluginManager;
            if (manager == null) manager = atom.GetStorableByID("Plugins") as MVRPluginManager;
            if (manager == null) throw new BridgeFailure("unsupported", "Atom has no native plugin manager");
            return manager;
        }
        private List<MVRPlugin> PluginList(MVRPluginManager manager)
        { return (List<MVRPlugin>)typeof(MVRPluginManager).GetField("plugins", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(manager); }
        private MVRPlugin FindPlugin(MVRPluginManager manager, string uid)
        { foreach (MVRPlugin p in PluginList(manager)) if (p.uid == uid) return p; throw new BridgeFailure("not_found", "Plugin slot not found: " + uid); }
        private JSONClass PluginInfo(MVRPlugin p)
        {
            JSONClass v = Obj(); v["plugin_uid"] = p.uid; v["entrypoint"] = p.pluginURLJSON.val; JSONArray scripts = new JSONArray();
            foreach (MVRScriptController sc in p.scriptControllers)
            {
                if (sc.script == null) continue; JSONClass s = Obj(); s["storable_id"] = sc.script.storeId;
                s["type"] = sc.script.GetType().FullName; s["enabled"].AsBool = sc.script.enabled; s["ui_available"].AsBool = sc.customUI != null; scripts.Add(s);
            }
            v["scripts"] = scripts; return v;
        }
        private string ErrorsAfter(long sequence)
        {
            lock (logGate) foreach (LogEntry entry in logEvents)
            {
                if (entry.Sequence > sequence && entry.Level == "error") return entry.Message;
            }
            return null;
        }
        private string SceneErrorAfter(long sequence, bool saving)
        {
            lock (logGate) foreach (LogEntry entry in logEvents)
            {
                string message = entry.Message;
                if (entry.Sequence <= sequence || entry.Level != "error") continue;
                if (message.IndexOf(saving ? "Exception during Save" : "Exception during load", StringComparison.OrdinalIgnoreCase) >= 0) return message;
            }
            return null;
        }
        private IEnumerator Plugins(Job job, JSONClass a)
        {
            Atom atom = RequireAtom(a); MVRPluginManager manager = RequireManager(atom);
            string action = Str(a, "action", "list");
            if (action == "list")
            { JSONClass v = Obj(); JSONArray list = new JSONArray(); foreach (MVRPlugin p in PluginList(manager)) list.Add(PluginInfo(p)); v["plugins"] = list; Finish(job, v); yield break; }
            MVRPlugin plugin;
            long start; lock (logGate) start = logSequence;
            if (action == "load")
            {
                string path = Str(a, "entrypoint"); if (path == "") throw new BridgeFailure("invalid_value", "entrypoint is required");
                plugin = manager.CreatePlugin(); InvalidateUI(); plugin.pluginURLJSON.val = path.Replace('\\', '/');
            }
            else
            {
                plugin = FindPlugin(manager, Str(a, "plugin_uid"));
                if (action == "inspect") { Finish(job, PluginInfo(plugin)); yield break; }
                if (action == "unload") { manager.RemovePluginWithUID(plugin.uid); InvalidateUI(); yield return null; Finish(job, Obj()); yield break; }
                if (action == "open_ui")
                {
                    controller.ShowMainHUDMonitor();
                    if (atom.mainController != null) controller.SelectController(atom.mainController, false, true, true, true);
                    // Native UI prefabs apply startingTabName in Start on the next frame.
                    yield return null;
                    if (manager.scriptUIParent != null)
                    {
                        Transform ancestor = manager.scriptUIParent;
                        while (ancestor != null)
                        {
                            UITabSelector selector = ancestor.GetComponent<UITabSelector>();
                            if (selector != null && selector.HasTab("Plugins")) { selector.SetActiveTab("Plugins"); break; }
                            ancestor = ancestor.parent;
                        }
                    }
                    int index = Int(a, "script_index", 0);
                    if (index < 0 || index >= plugin.scriptControllers.Count) throw new BridgeFailure("not_found", "Script controller index not found");
                    plugin.scriptControllers[index].OpenUI(); yield return null;
                    JSONClass v = PluginInfo(plugin); Transform root = plugin.scriptControllers[index].customUI;
                    v["ui_root"] = root == null ? "" : Handle(root); v["ui_active"].AsBool = root != null && root.gameObject.activeInHierarchy;
                    Finish(job, v); yield break;
                }
                if (action != "reload") throw new BridgeFailure("unsupported", "Unknown plugin action");
                InvalidateUI(); manager.ReloadPluginWithUID(plugin.uid);
            }
            yield return null;
            while (plugin.scriptControllers.Count == 0)
            {
                string error = ErrorsAfter(start); if (error != null) throw new BridgeFailure("compile_error", error);
                yield return null;
            }
            yield return null; yield return null;
            string initialization = ErrorsAfter(start); if (initialization != null) throw new BridgeFailure("initialization_error", initialization);
            Finish(job, PluginInfo(plugin));
        }

        private IEnumerator Probe(Job job, JSONClass a)
        {
            string path = Path.GetFullPath(Str(a, "assembly_path"));
            if (!File.Exists(path)) throw new BridgeFailure("not_found", "Diagnostic assembly does not exist");
            Assembly assembly = Assembly.Load(File.ReadAllBytes(path));
            Type type = assembly.GetType(Str(a, "type_name"), true);
            MethodInfo method = type.GetMethod("Execute", BindingFlags.Static | BindingFlags.Public);
            if (method == null) throw new BridgeFailure("invalid_probe", "Diagnostic must expose Execute(args, result, containingAtom)");
            JSONClass result = Obj(); Atom atom = Str(a, "atom_uid") == "" ? null : RequireAtom(a);
            IEnumerator routine = method.Invoke(null, new object[] { a["arguments"].AsObject ?? Obj(), result, atom }) as IEnumerator;
            if (routine == null) throw new BridgeFailure("invalid_probe", "Execute must return IEnumerator");
            yield return routine;
            Finish(job, result);
        }
    }
}
