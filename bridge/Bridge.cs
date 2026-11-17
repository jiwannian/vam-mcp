using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;
using SimpleJSON;
using UnityEngine;

namespace VaMMcp
{
    [BepInPlugin("codex.vam.mcp", "VaM MCP", "0.1.0")]
    public sealed partial class Bridge : BaseUnityPlugin
    {
        private static Bridge current;
        private Harmony harmony;
        private BridgeLogListener bepinListener;
        private SuperController controller;
        private readonly object logGate = new object();
        private static readonly object jsonGate = new object();
        private sealed class LogEntry
        {
            internal long Sequence;
            internal string Level, Message;
            internal JSONClass Json;
        }
        private readonly Queue<LogEntry> logEvents = new Queue<LogEntry>();
        private long logSequence;
        private string logPath;

        // The game's bundled SimpleJSON serializer uses shared internal state.
        private static JSONClass ParseObject(string json)
        { lock (jsonGate) return JSON.Parse(json).AsObject; }

        private static string Serialize(JSONNode value)
        { lock (jsonGate) return value.ToString(); }

        private void Awake()
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (args[i] == "-vam-mcp") instanceFolder = Path.GetFullPath(args[i + 1]);
                if (args[i] == "-vam-mcp-id") instanceId = args[i + 1];
            }
            if (String.IsNullOrEmpty(instanceFolder) || String.IsNullOrEmpty(instanceId)) { enabled = false; return; }
            try
            {
                current = this; gameRoot = Directory.GetCurrentDirectory(); processId = System.Diagnostics.Process.GetCurrentProcess().Id;
                Directory.CreateDirectory(instanceFolder); logPath = Path.Combine(instanceFolder, "events.jsonl");
                logSequence = ReadLastLogSequence();
                Application.runInBackground = true;
                Application.logMessageReceivedThreaded += OnUnityLog;
                bepinListener = new BridgeLogListener(this); BepInEx.Logging.Logger.Listeners.Add(bepinListener);
                harmony = new Harmony("codex.vam.mcp");
                harmony.Patch(typeof(SuperController).GetMethod("Error", new Type[] { typeof(string), typeof(bool), typeof(bool) }), new HarmonyMethod(typeof(Bridge).GetMethod("OnVaMError", BindingFlags.Static | BindingFlags.NonPublic)));
                harmony.Patch(typeof(SuperController).GetMethod("Message", new Type[] { typeof(string), typeof(bool), typeof(bool) }), new HarmonyMethod(typeof(Bridge).GetMethod("OnVaMMessage", BindingFlags.Static | BindingFlags.NonPublic)));
                StartTransport(); AddLog("bridge", "info", "Bridge ready", "");
            }
            catch (Exception error) { UnityEngine.Debug.LogError("VaMMcp initialization failed: " + error); enabled = false; }
        }

        private static void OnVaMError(string err) { if (current != null) current.AddLog("vam", "error", err, ""); }
        private static void OnVaMMessage(string msg) { if (current != null) current.AddLog("vam", "info", msg, ""); }
        private void OnUnityLog(string message, string stack, LogType type)
        { AddLog("unity", type == LogType.Error || type == LogType.Exception || type == LogType.Assert ? "error" : type == LogType.Warning ? "warning" : "info", message, stack); }

        private string Redact(string value)
        {
            if (value == null) return "";
            if (token != null) value = value.Replace(token, "[REDACTED]");
            value = Regex.Replace(value, @"(?i)(Bearer\s+)[A-Za-z0-9_+./=:-]+", "$1[REDACTED]");
            return value.Length <= 65536 ? value : value.Substring(0, 65536) + " [truncated]";
        }

        private void AddLog(string source, string level, string message, string stack)
        {
            lock (logGate)
            {
                JSONClass item = Obj(); item["sequence"] = (++logSequence).ToString(System.Globalization.CultureInfo.InvariantCulture);
                item["utc"] = DateTime.UtcNow.ToString("o"); item["instance_id"] = instanceId;
                item["source"] = source; item["level"] = level; item["message"] = Redact(message); item["stack"] = Redact(stack);
                string line = Serialize(item);
                logEvents.Enqueue(new LogEntry { Sequence = logSequence, Level = level, Message = Redact(message), Json = item });
                while (logEvents.Count > 4000) logEvents.Dequeue();
                if (logPath != null) try { File.AppendAllText(logPath, line + "\n", new UTF8Encoding(false)); } catch (IOException) { }
            }
        }

        private long ReadLastLogSequence()
        {
            long latest = 0;
            if (!File.Exists(logPath)) return latest;
            try
            {
                using (StreamReader reader = new StreamReader(logPath))
                {
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        JSONClass entry = ParseObject(line);
                        long value;
                        if (entry != null && Int64.TryParse(Str(entry, "sequence"), out value) && value > latest) latest = value;
                    }
                }
            }
            catch (Exception) { }
            return latest;
        }

        private sealed class BridgeLogListener : ILogListener
        {
            private readonly Bridge owner;
            internal BridgeLogListener(Bridge owner) { this.owner = owner; }
            public void LogEvent(object sender, LogEventArgs args)
            {
                if (args.Source is UnityLogSource) return;
                owner.AddLog("bepinex:" + args.Source.SourceName, (args.Level & (LogLevel.Error | LogLevel.Fatal)) != 0 ? "error" : (args.Level & LogLevel.Warning) != 0 ? "warning" : "info", Convert.ToString(args.Data), "");
            }
            public void Dispose() { }
        }

        private void Update()
        {
            if (current != this || stopping) return;
            if (controller == null && SuperController.singleton != null)
            {
                controller = SuperController.singleton; controller.onSceneLoadedHandlers += SceneLoaded;
                AddLog("bridge", "info", "SuperController available", "");
            }
            Job next = null;
            lock (gate)
            {
                if (activeJob == null && pending.Count > 0)
                {
                    next = pending.Dequeue(); activeJob = next; next.State = "running";
                    next.Deadline = Time.realtimeSinceStartup + next.Timeout;
                }
            }
            if (next != null) StartCoroutine(RunJob(next));
        }

        private void SceneLoaded() { lock (gate) { sceneRevision++; uiRevision++; } handles.Clear(); }
        private void InvalidateUI() { lock (gate) { uiRevision++; } handles.Clear(); }

        private IEnumerator RunJob(Job job)
        {
            Stack<IEnumerator> stack = new Stack<IEnumerator>(); stack.Push(Dispatch(job)); Exception failure = null;
            while (stack.Count > 0)
            {
                if (job.Cancel) { failure = new BridgeFailure("cancelled", "Operation cancelled; inspect state before further writes"); break; }
                if (Time.realtimeSinceStartup > job.Deadline) { failure = new BridgeFailure("timeout", "Operation deadline exceeded; inspect state before retrying"); break; }
                bool more = false; object value = null;
                try { more = stack.Peek().MoveNext(); if (more) value = stack.Peek().Current; }
                catch (Exception error) { failure = error; }
                if (failure != null) break;
                if (!more) { IDisposable done = stack.Pop() as IDisposable; if (done != null) done.Dispose(); continue; }
                IEnumerator child = value as IEnumerator;
                if (child != null) stack.Push(child);
                else if (value == null) yield return null;
                else
                {
                    bool resumed = false;
                    Coroutine wait = StartCoroutine(WaitYield(value, delegate { resumed = true; }));
                    while (!resumed && !job.Cancel && Time.realtimeSinceStartup <= job.Deadline) yield return null;
                    if (!resumed) StopCoroutine(wait);
                }
            }
            while (stack.Count > 0)
            {
                IDisposable done = stack.Pop() as IDisposable;
                if (done != null) try { done.Dispose(); } catch (Exception error) { if (failure == null) failure = error; }
            }
            lock (gate)
            {
                if (failure == null) { job.State = "succeeded"; if (job.Result == null) job.Result = Obj(); }
                else
                {
                    job.Code = failure is BridgeFailure ? ((BridgeFailure)failure).Code : "operation_failed";
                    job.Message = Redact(failure.Message);
                    job.State = job.Code == "cancelled" ? "cancelled" : job.Code == "timeout" ? "timed_out" : "failed";
                }
                activeJob = null;
            }
            if (failure != null && !(failure is BridgeFailure)) AddLog("bridge", "error", failure.Message, failure.ToString());
        }

        private IEnumerator WaitYield(object value, Action resumed)
        { yield return value; resumed(); }

        private JSONClass ReadLogs(JSONClass args)
        {
            long after; Int64.TryParse(Str(args, "after", "0"), out after);
            int limit = Math.Max(1, Math.Min(Int(args, "limit", 200), 1000));
            JSONClass result = Obj(); JSONArray items = new JSONArray(); long cursor = after;
            lock (logGate)
            {
                foreach (LogEntry entry in logEvents)
                {
                    JSONClass item = entry.Json; long seq = entry.Sequence;
                    if (seq <= after) continue;
                    cursor = seq;
                    if ((Str(args, "level") == "" || Str(args, "level") == (string)item["level"]) &&
                        (Str(args, "contains") == "" || ((string)item["message"]).IndexOf(Str(args, "contains"), StringComparison.OrdinalIgnoreCase) >= 0)) items.Add(item);
                    if (items.Count >= limit) break;
                }
                result["latest_sequence"] = logSequence.ToString();
            }
            result["items"] = items; result["cursor"] = cursor.ToString(); return result;
        }

        private void OnDestroy()
        {
            if (current != this) return;
            stopping = true; if (listener != null) listener.Stop();
            Application.logMessageReceivedThreaded -= OnUnityLog;
            if (controller != null) controller.onSceneLoadedHandlers -= SceneLoaded;
            if (bepinListener != null) BepInEx.Logging.Logger.Listeners.Remove(bepinListener);
            if (harmony != null) harmony.UnpatchAll("codex.vam.mcp");
            try { File.Delete(Path.Combine(instanceFolder, "bridge.json")); } catch (IOException) { }
            current = null;
        }
    }
}
