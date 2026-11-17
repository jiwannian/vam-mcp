using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using SimpleJSON;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.EventSystems;
using uFileBrowser;

namespace VaMMcp
{
    public sealed partial class Bridge
    {
        private readonly Dictionary<string, Transform> handles = new Dictionary<string, Transform>();
        private string Handle(Transform transform)
        {
            string handle = instanceId + ":" + sceneRevision + ":" + uiRevision + ":" + transform.GetInstanceID(); handles[handle] = transform; return handle;
        }
        private Transform RequireHandle(string handle)
        {
            Transform transform;
            if (!handles.TryGetValue(handle, out transform) || transform == null) throw new BridgeFailure("stale_handle", "UI handle expired; query the UI tree again");
            return transform;
        }
        private static string TransformPath(Transform transform)
        {
            string path = transform.name; int depth = 0;
            while (transform.parent != null && depth++ < 32) { transform = transform.parent; path = transform.name + "/" + path; }
            return path;
        }
        private static string Label(Transform t)
        {
            InputField input = t.GetComponent<InputField>();
            if (input != null) { Text placeholder = input.placeholder as Text; if (placeholder != null) return placeholder.text; }
            Text text = t.GetComponent<Text>(); if (text != null) return text.text;
            Text[] labels = t.GetComponentsInChildren<Text>(true); foreach (Text label in labels) if (label.text != "") return label.text;
            return t.name;
        }
        private JSONClass UIInfo(Transform t)
        {
            JSONClass value = Obj(); value["handle"] = Handle(t); value["path"] = TransformPath(t); value["name"] = t.name;
            value["label"] = Label(t); value["active"].AsBool = t.gameObject.activeInHierarchy;
            Selectable selectable = t.GetComponent<Selectable>(); if (selectable != null) value["interactable"].AsBool = selectable.IsInteractable();
            InputField input = t.GetComponent<InputField>(); Toggle toggle = t.GetComponent<Toggle>(); Slider slider = t.GetComponent<Slider>();
            Dropdown dropdown = t.GetComponent<Dropdown>(); UIPopup popup = t.GetComponent<UIPopup>(); ScrollRect scroll = t.GetComponent<ScrollRect>();
            Button button = t.GetComponent<Button>();
            string type = input != null ? "input" : popup != null ? "popup" : dropdown != null ? "dropdown" : toggle != null ? "toggle" : slider != null ? "slider" : button != null ? "button" : scroll != null ? "scroll" : t.GetComponent<Text>() != null ? "text" : "container";
            value["control_type"] = type;
            if (input != null)
            {
                bool secret = input.contentType == InputField.ContentType.Password || input.contentType == InputField.ContentType.Pin;
                value["secret"].AsBool = secret; value["value"] = secret ? "[REDACTED]" : input.text; value["has_value"].AsBool = input.text.Length > 0;
                value["character_limit"].AsInt = input.characterLimit;
            }
            else if (toggle != null) value["value"].AsBool = toggle.isOn;
            else if (slider != null) { value["value"].AsFloat = slider.value; value["min"].AsFloat = slider.minValue; value["max"].AsFloat = slider.maxValue; }
            if (popup != null)
            { value["value"] = popup.currentValue; JSONArray choices = new JSONArray(); if (popup.popupValues != null) foreach (string choice in popup.popupValues) choices.Add(choice); value["choices"] = choices; }
            if (dropdown != null)
            { value["value"].AsInt = dropdown.value; JSONArray choices = new JSONArray(); foreach (Dropdown.OptionData choice in dropdown.options) choices.Add(choice.text); value["choices"] = choices; }
            if (scroll != null) { JSONArray position = new JSONArray(); position.Add(new JSONData(scroll.normalizedPosition.x)); position.Add(new JSONData(scroll.normalizedPosition.y)); value["value"] = position; }
            RectTransform rect = t as RectTransform;
            if (rect != null)
            {
                Vector3[] corners = new Vector3[4]; rect.GetWorldCorners(corners); Canvas canvas = t.GetComponentInParent<Canvas>();
                Camera camera = canvas != null && canvas.renderMode != RenderMode.ScreenSpaceOverlay ? canvas.worldCamera : null;
                Vector2 low = RectTransformUtility.WorldToScreenPoint(camera, corners[0]), high = RectTransformUtility.WorldToScreenPoint(camera, corners[2]);
                JSONClass bounds = Obj(); bounds["x"].AsFloat = low.x; bounds["y"].AsFloat = low.y; bounds["width"].AsFloat = high.x - low.x; bounds["height"].AsFloat = high.y - low.y; value["bounds"] = bounds;
            }
            return value;
        }
        private bool IsUI(Transform t)
        { return t.GetComponent<Selectable>() != null || t.GetComponent<UIPopup>() != null || t.GetComponent<ScrollRect>() != null || t.GetComponent<Text>() != null; }
        private JSONClass UITree(JSONClass a)
        {
            List<Transform> roots = new List<Transform>(); string root = Str(a, "root");
            if (root != "") roots.Add(RequireHandle(root));
            else if (Str(a, "atom_uid") != "" && Str(a, "plugin_uid") != "")
            {
                MVRPluginManager manager = RequireManager(RequireAtom(a));
                MVRPlugin plugin = FindPlugin(manager, Str(a, "plugin_uid"));
                foreach (MVRScriptController sc in plugin.scriptControllers) if (sc.customUI != null) roots.Add(sc.customUI);
            }
            else
            {
                foreach (Canvas canvas in Resources.FindObjectsOfTypeAll<Canvas>())
                    if (canvas.gameObject.scene.IsValid() && canvas.transform.GetComponentInParent<Canvas>() == canvas) roots.Add(canvas.transform);
            }
            int limit = Math.Max(1, Math.Min(1000, Int(a, "limit", 250))), offset = Math.Max(0, Int(a, "offset", 0)), total = 0;
            bool inactive = Bool(a, "include_inactive"); string query = Str(a, "query"), type = Str(a, "control_type");
            JSONArray items = new JSONArray(); HashSet<int> seen = new HashSet<int>();
            foreach (Transform r in roots) foreach (Transform t in r.GetComponentsInChildren<Transform>(true))
            {
                if (!seen.Add(t.GetInstanceID()) || (!inactive && !t.gameObject.activeInHierarchy) || !IsUI(t)) continue;
                if (query != "" && (t.name + " " + Label(t)).IndexOf(query, StringComparison.OrdinalIgnoreCase) < 0) continue;
                JSONClass item = UIInfo(t); if (type != "" && (string)item["control_type"] != type) continue;
                if (total++ >= offset && items.Count < limit) items.Add(item);
            }
            JSONClass result = Obj(); result["items"] = items; result["total"].AsInt = total; result["scene_revision"].AsInt = sceneRevision; result["ui_revision"].AsInt = uiRevision; return result;
        }
        private JSONClass UIAction(JSONClass a)
        {
            Transform t = RequireHandle(Str(a, "handle")); string action = Str(a, "action");
            if (action == "inspect") return UIInfo(t);
            if (!t.gameObject.activeInHierarchy) throw new BridgeFailure("not_interactable", "Control is inactive");
            Selectable selectable = t.GetComponent<Selectable>();
            if (selectable != null && !selectable.IsInteractable()) throw new BridgeFailure("not_interactable", "Control is disabled");
            if (action == "click")
            {
                Button button = t.GetComponent<Button>(); if (button == null) throw new BridgeFailure("unsupported", "Control is not a button"); button.onClick.Invoke();
            }
            else if (action == "set")
            {
                JSONNode value = a["value"]; if (value == null) throw new BridgeFailure("invalid_value", "value is required");
                InputField input = t.GetComponent<InputField>(); Toggle toggle = t.GetComponent<Toggle>(); Slider slider = t.GetComponent<Slider>();
                UIPopup popup = t.GetComponent<UIPopup>(); Dropdown dropdown = t.GetComponent<Dropdown>(); ScrollRect scroll = t.GetComponent<ScrollRect>();
                if (input != null) { input.text = value; if (Bool(a, "submit", true)) input.onEndEdit.Invoke(input.text); }
                else if (toggle != null)
                { if ((string)value != "true" && (string)value != "false") throw new BridgeFailure("invalid_value", "Expected boolean"); toggle.isOn = value.AsBool; }
                else if (slider != null)
                { float f = Number(value); if (f < slider.minValue || f > slider.maxValue) throw new BridgeFailure("invalid_value", "Slider value out of range"); slider.value = f; }
                else if (popup != null)
                { if (popup.popupValues == null || Array.IndexOf(popup.popupValues, (string)value) < 0) throw new BridgeFailure("invalid_value", "Popup choice not available"); popup.currentValue = value; }
                else if (dropdown != null)
                { int index = value.AsInt; if (index < 0 || index >= dropdown.options.Count) throw new BridgeFailure("invalid_value", "Dropdown index out of range"); dropdown.value = index; }
                else if (scroll != null)
                {
                    if (value.AsArray == null || value.Count != 2) throw new BridgeFailure("invalid_value", "Expected [x, y] scroll position");
                    float x = Number(value[0]), y = Number(value[1]); if (x < 0 || x > 1 || y < 0 || y > 1) throw new BridgeFailure("invalid_value", "Scroll components must be 0..1");
                    scroll.normalizedPosition = new Vector2(x, y);
                }
                else throw new BridgeFailure("unsupported", "Control cannot be assigned a value");
            }
            else if (action == "open")
            { UIPopup popup = t.GetComponent<UIPopup>(); if (popup == null) throw new BridgeFailure("unsupported", "Control is not a popup"); if (!popup.visible) popup.Toggle(); }
            else throw new BridgeFailure("unsupported", "Unknown UI action");
            return t == null ? Obj() : UIInfo(t);
        }

        private JSONClass FileDialog(JSONClass a)
        {
            List<FileBrowser> open = new List<FileBrowser>();
            foreach (FileBrowser browser in Resources.FindObjectsOfTypeAll<FileBrowser>())
                if (browser.gameObject.scene.IsValid() && browser.gameObject.activeInHierarchy && browser.window != null && browser.window.activeInHierarchy) open.Add(browser);
            JSONClass result = Obj(); JSONArray items = new JSONArray();
            foreach (FileBrowser browser in open)
            {
                JSONClass item = Obj(); item["handle"] = Handle(browser.transform); item["title"] = browser.titleText == null ? "" : browser.titleText.text;
                item["current_path"] = browser.CurrentPath; item["format"] = browser.fileFormat; item["directory"].AsBool = browser.selectDirectory; items.Add(item);
            }
            result["dialogs"] = items; string action = Str(a, "action", "inspect"); if (action == "inspect") return result;
            FileBrowser target = null;
            if (Str(a, "handle") != "") target = RequireHandle(Str(a, "handle")).GetComponent<FileBrowser>();
            else if (open.Count == 1) target = open[0];
            if (target == null || !open.Contains(target)) throw new BridgeFailure(open.Count > 1 ? "ambiguous" : "not_found", "Specify one active file dialog");
            if (action == "cancel") target.CancelButtonClicked();
            else if (action == "select")
            {
                string path = Str(a, "path").Replace('\\', '/');
                if (!(target.selectDirectory ? Directory.Exists(path) : MVR.FileManagement.FileManager.FileExists(path))) throw new BridgeFailure("not_found", "Selected path does not exist");
                if (!target.selectDirectory && target.fileFormat != "" && target.fileFormat != "*")
                {
                    string ext = Path.GetExtension(path).TrimStart('.'); bool match = false;
                    foreach (string allowed in target.fileFormat.Split('|')) if (String.Equals(allowed.TrimStart('*', '.'), ext, StringComparison.OrdinalIgnoreCase)) match = true;
                    if (!match) throw new BridgeFailure("invalid_value", "File extension is not accepted by the dialog");
                }
                string directory = Path.GetDirectoryName(path).Replace('\\', '/');
                if (target.selectDirectory) { target.GotoDirectory(path.Replace('\\', '/')); if (target.fileEntryField != null) target.fileEntryField.text = ""; }
                else { target.GotoDirectory(directory); if (target.fileEntryField == null) throw new BridgeFailure("unsupported", "File dialog has no filename field"); target.fileEntryField.text = Path.GetFileName(path); }
                bool selectOnClick = target.selectOnClick;
                try { target.selectOnClick = false; target.SelectButtonClicked(); }
                finally { target.selectOnClick = selectOnClick; }
            }
            else throw new BridgeFailure("unsupported", "Unknown file dialog action");
            result["action"] = action; return result;
        }

        private IEnumerator Capture(Job job, JSONClass a)
        {
            string mode = Str(a, "mode", "ui"), view = Str(a, "view", "front"); Texture2D image = null; Camera camera = null; RenderTexture render = null;
            string folder = Path.Combine(instanceFolder, "artifacts"); Directory.CreateDirectory(folder);
            string path = Path.Combine(folder, DateTime.UtcNow.ToString("yyyyMMdd_HHmmss_fff") + "_" + Guid.NewGuid().ToString("N").Substring(0, 6) + ".png");
            try
            {
                if (mode == "ui")
                { yield return new WaitForEndOfFrame(); yield return new WaitForEndOfFrame(); image = ScreenCapture.CaptureScreenshotAsTexture(); }
                else if (mode == "render")
                {
                    int width = Math.Max(64, Math.Min(2048, Int(a, "width", 1024))), height = Math.Max(64, Math.Min(2048, Int(a, "height", 1024)));
                    GameObject go = new GameObject("VaMMcpCapture"); camera = go.AddComponent<Camera>(); Camera source = controller.lookCamera != null ? controller.lookCamera : Camera.main;
                    if (source != null) camera.CopyFrom(source); camera.enabled = false; camera.stereoTargetEye = StereoTargetEyeMask.None;
                    render = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32); camera.targetTexture = render;
                    if (Str(a, "atom_uid") != "")
                    {
                        Atom atom = RequireAtom(a); FreeControllerV3 head = atom.GetStorableByID("headControl") as FreeControllerV3;
                        Transform target = head != null ? head.transform : atom.mainController != null ? atom.mainController.transform : atom.transform;
                        Vector3 direction = view == "front" ? target.forward : view == "back" ? -target.forward : view == "left" ? -target.right : view == "right" ? target.right : view == "top" ? target.up : Vector3.zero;
                        if (direction == Vector3.zero) throw new BridgeFailure("invalid_value", "Unknown render view");
                        float distance = a["distance"] == null ? 1.0f : Number(a["distance"]); if (distance < .05f || distance > 100) throw new BridgeFailure("invalid_value", "Camera distance out of range");
                        camera.fieldOfView = 32; camera.nearClipPlane = .01f; camera.transform.position = target.position + direction * distance;
                        camera.transform.rotation = Quaternion.LookRotation(target.position - camera.transform.position, view == "top" ? target.forward : target.up);
                    }
                    camera.enabled = true; yield return new WaitForEndOfFrame(); yield return new WaitForEndOfFrame(); camera.enabled = false; camera.Render();
                    RenderTexture previous = RenderTexture.active;
                    try { RenderTexture.active = render; image = new Texture2D(width, height, TextureFormat.RGB24, false); image.ReadPixels(new Rect(0, 0, width, height), 0, 0); image.Apply(); }
                    finally { RenderTexture.active = previous; }
                }
                else throw new BridgeFailure("unsupported", "Capture mode must be ui or render");
                if (image == null || image.width < 2 || image.height < 2) throw new BridgeFailure("render_unavailable", "No rendered framebuffer is available");
                File.WriteAllBytes(path, image.EncodeToPNG()); JSONClass result = Obj(); result["path"] = path; result["mode"] = mode;
                result["view"] = view; result["width"].AsInt = image.width; result["height"].AsInt = image.height; Finish(job, result);
            }
            finally
            { if (image != null) Destroy(image); if (camera != null) Destroy(camera.gameObject); if (render != null) { render.Release(); Destroy(render); } }
        }
    }
}
