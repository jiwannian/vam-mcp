using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using SimpleJSON;

namespace VaMMcp
{
    internal sealed class BridgeFailure : Exception
    {
        internal string Code;
        internal BridgeFailure(string code, string message) : base(message) { Code = code; }
    }

    internal sealed class Job
    {
        internal string Id, Method, State = "queued", Code, Message;
        internal JSONNode Result;
        internal JSONClass Args;
        internal volatile bool Cancel;
        internal float Deadline;
        internal int Timeout;
    }

    public sealed partial class Bridge
    {
        private readonly object gate = new object();
        private readonly Queue<Job> pending = new Queue<Job>();
        private readonly Dictionary<string, Job> jobs = new Dictionary<string, Job>();
        private readonly Dictionary<string, string> requests = new Dictionary<string, string>();
        private readonly Queue<string> jobOrder = new Queue<string>();
        private TcpListener listener;
        private Thread acceptThread;
        private volatile bool stopping;
        private string token, instanceId, instanceFolder, gameRoot;
        private int processId, sceneRevision = 1, uiRevision = 1;
        private Job activeJob;

        private static JSONClass Obj() { return new JSONClass(); }
        private static JSONClass Error(string code, string message)
        {
            JSONClass e = Obj(); e["code"] = code; e["message"] = message; return e;
        }
        private static string Str(JSONClass a, string key, string fallback = "")
        { return a != null && a[key] != null ? (string)a[key] : fallback; }
        private static int Int(JSONClass a, string key, int fallback)
        { return a != null && a[key] != null ? a[key].AsInt : fallback; }
        private static bool Bool(JSONClass a, string key, bool fallback = false)
        { return a != null && a[key] != null ? a[key].AsBool : fallback; }

        private void StartTransport()
        {
            byte[] secret = new byte[32];
            System.Security.Cryptography.RandomNumberGenerator rng = System.Security.Cryptography.RandomNumberGenerator.Create();
            rng.GetBytes(secret);
            rng = null;
            token = Convert.ToBase64String(secret);
            listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
            JSONClass descriptor = Obj(); descriptor["protocol"].AsInt = 1; descriptor["bridge_version"] = "0.1.0";
            descriptor["instance_id"] = instanceId; descriptor["pid"].AsInt = processId;
            descriptor["process_started_utc"] = System.Diagnostics.Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o");
            descriptor["port"].AsInt = ((IPEndPoint)listener.LocalEndpoint).Port;
            descriptor["token"] = token; descriptor["game_root"] = gameRoot;
            descriptor["instance_folder"] = instanceFolder;
            string path = Path.Combine(instanceFolder, "bridge.json");
            File.WriteAllText(path + ".tmp", Serialize(descriptor), new UTF8Encoding(false));
            File.Move(path + ".tmp", path);
            acceptThread = new Thread(AcceptLoop); acceptThread.IsBackground = true; acceptThread.Name = "VaMMcp listener"; acceptThread.Start();
        }

        private void AcceptLoop()
        {
            while (!stopping)
            {
                try { TcpClient client = listener.AcceptTcpClient(); ThreadPool.QueueUserWorkItem(delegate { Serve(client); }); }
                catch (SocketException) { if (stopping) return; }
                catch (ObjectDisposedException) { return; }
            }
        }

        private void Serve(TcpClient client)
        {
            using (client)
            {
                client.ReceiveTimeout = 5000; client.SendTimeout = 5000;
                try
                {
                    NetworkStream stream = client.GetStream();
                    string line;
                    using (MemoryStream frame = new MemoryStream())
                    {
                        int b;
                        while ((b = stream.ReadByte()) != -1 && b != 10)
                        {
                            if (frame.Length >= 2 * 1024 * 1024) throw new BridgeFailure("frame_too_large", "Request exceeds 2 MiB");
                            frame.WriteByte((byte)b);
                        }
                        if (b == -1) return;
                        line = new UTF8Encoding(false, true).GetString(frame.ToArray());
                    }
                    JSONClass request = ParseObject(line);
                    JSONClass response = Obj(); response["id"] = Str(request, "id"); response["instance_id"] = instanceId;
                    try
                    {
                        if (request == null || Str(request, "token") != token) throw new BridgeFailure("unauthenticated", "Invalid bridge token");
                        if (Int(request, "protocol", 0) != 1) throw new BridgeFailure("protocol_mismatch", "Expected protocol 1");
                        response["result"] = TransportCommand(Str(request, "method"), request["params"].AsObject ?? Obj());
                        response["ok"].AsBool = true;
                    }
                    catch (Exception error)
                    { response["ok"].AsBool = false; response["error"] = Error(error is BridgeFailure ? ((BridgeFailure)error).Code : "invalid_request", error.Message); }
                    byte[] data = Encoding.UTF8.GetBytes(Serialize(response) + "\n");
                    stream.Write(data, 0, data.Length);
                }
                catch (Exception) { /* Incomplete/untrusted frames never enter the Unity queue. */ }
            }
        }

        private JSONClass TransportCommand(string method, JSONClass args)
        {
            lock (gate)
            {
                if (method == "ping")
                {
                    JSONClass ping = Obj(); ping["instance_id"] = instanceId; ping["protocol"].AsInt = 1;
                    ping["queued"].AsInt = pending.Count; ping["scene_revision"].AsInt = sceneRevision;
                    ping["active_job"] = activeJob == null ? "" : activeJob.Id; return ping;
                }
                if (method == "submit")
                {
                    string key = Str(args, "request_key"); string known;
                    if (key.Length < 8 || key.Length > 100) throw new BridgeFailure("invalid_request", "request_key is required");
                    if (requests.TryGetValue(key, out known)) return JobJson(jobs[known]);
                    if (pending.Count >= 128) throw new BridgeFailure("busy", "Unity command queue is full");
                    Job job = new Job(); job.Id = "game_" + Guid.NewGuid().ToString("N"); job.Method = Str(args, "operation");
                    job.Args = args["arguments"].AsObject ?? Obj(); job.Timeout = Math.Max(1, Math.Min(Int(args, "timeout_s", 30), 3600));
                    jobs.Add(job.Id, job); requests.Add(key, job.Id); jobOrder.Enqueue(job.Id); pending.Enqueue(job);
                    if (jobOrder.Count > 1024)
                    {
                        string oldest = jobOrder.Peek();
                        if (jobs[oldest].State != "queued" && jobs[oldest].State != "running")
                        {
                            jobOrder.Dequeue(); jobs.Remove(oldest);
                            List<string> remove = new List<string>(); foreach (KeyValuePair<string, string> pair in requests) if (pair.Value == oldest) remove.Add(pair.Key);
                            foreach (string item in remove) requests.Remove(item);
                        }
                    }
                    return JobJson(job);
                }
                Job found;
                if (!jobs.TryGetValue(Str(args, "job_id"), out found)) throw new BridgeFailure("not_found", "Unknown job");
                if (method == "cancel") found.Cancel = true;
                else if (method != "job") throw new BridgeFailure("unsupported", "Unknown transport method");
                return JobJson(found);
            }
        }

        private JSONClass JobJson(Job job)
        {
            JSONClass value = Obj(); value["job_id"] = job.Id; value["state"] = job.State;
            value["instance_id"] = instanceId; value["scene_revision"].AsInt = sceneRevision;
            value["ok"].AsBool = job.State == "succeeded";
            if (job.Result != null) value["result"] = job.Result;
            if (job.Code != null) value["error"] = Error(job.Code, job.Message);
            value["cancel_requested"].AsBool = job.Cancel; return value;
        }

        private void Finish(Job job, JSONNode value)
        { lock (gate) { job.Result = value ?? Obj(); } }
    }
}
