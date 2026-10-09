using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;

namespace ExperimentManagerDesktop {
    internal sealed class UpdateQueueState {
        public UpdateRelease Release;
        public string Stage = "idle", Detail = "", Downloaded;
        public bool InstallRequested, Automatic;
        public int Failures;
        public long Bytes, Total;
        public DateTime NextAttemptUtc;
    }

    // One persisted request owns download/retry/wait/install. Closing its view
    // never cancels it, and a readiness probe never stops experiments.
    internal sealed class DesktopUpdateQueue {
        internal UpdateQueueState State { get; private set; }
        internal bool InFlight { get; private set; }
        internal Action Changed;
        readonly Action<UpdateQueueState> persist;
        CancellationTokenSource cancellation;
        bool cacheValidated;

        internal DesktopUpdateQueue(UpdateQueueState restored, string currentVersion, Action<UpdateQueueState> save) {
            persist = save;
            State = restored ?? new UpdateQueueState();
            if(State.Release != null) {
                if(UpdateService.CompareVersions(currentVersion, State.Release.Version) >= 0)
                    State = new UpdateQueueState { Stage = "completed", Detail = "更新已安装；安装包将自动清理。" };
                else if(State.Stage == "downloading" || State.Stage == "installing" || State.Stage == "handoff") {
                    State.Stage = State.Downloaded == null ? "queued" : "waiting";
                    State.Detail = "已恢复更新队列，将继续下载或等待安装。";
                    State.NextAttemptUtc = DateTime.MinValue;
                }
            }
        }
        internal bool Active { get { return State.Release != null; } }
        internal bool CanCancel { get { return Active && State.Stage != "installing" && State.Stage != "handoff"; } }
        void Save() { persist(State); if(Changed != null) Changed(); }

        internal void Enqueue(UpdateRelease release, bool install, bool automatic) {
            if(release == null) throw new ArgumentNullException("release");
            if(InFlight && State.Release == null) throw new InvalidOperationException("正在取消下载，请稍候。 ");
            if(Active) {
                if(State.Release.Version != release.Version || State.Release.Sha256 != release.Sha256)
                    throw new InvalidOperationException("已有更新排队，请先取消当前队列。 ");
                if(!CanCancel) return;
                State.Automatic = State.InstallRequested ? State.Automatic && automatic : automatic;
                State.InstallRequested |= install; // A manual installation survives disabling automatic updates.
                State.NextAttemptUtc = DateTime.MinValue;
            } else State = new UpdateQueueState { Release = release, InstallRequested = install, Automatic = automatic };
            if(!InFlight)State.Stage = State.Downloaded == null ? "queued" : State.InstallRequested ? "waiting" : "downloaded";
            State.Detail = State.Downloaded == null ? "更新已入队，将自动下载。" : "下载已完成，等待安全安装。";
            Save();
        }
        internal void Cancel() {
            if(!CanCancel) throw new InvalidOperationException("正在交接安装，无法中途取消。 ");
            if(cancellation != null) cancellation.Cancel();
            State = new UpdateQueueState { Detail = "更新队列已取消，已校验的下载保留以便复用。" };
            Save();
        }
        internal async Task Tick(DateTime now, bool clientBusy,
                Func<UpdateRelease, Action<long,long>, CancellationToken, Task<string>> download,
                Func<Task<Dictionary<string,object>>> readiness,
                Func<UpdateRelease,string,Task> install,
                Func<UpdateRelease,string,Task> validate = null) {
            if(InFlight || !Active || State.Stage == "handoff" || now < State.NextAttemptUtc ||
                    (State.Downloaded != null && !State.InstallRequested)) return;
            InFlight = true;
            var request = State;
            cancellation = new CancellationTokenSource();
            try {
                if(request.Downloaded != null && !cacheValidated && validate != null) {
                    try {await validate(request.Release,request.Downloaded);cacheValidated=true;}
                    catch(InvalidDataException) {request.Downloaded=null;}
                    catch(IOException) {request.Downloaded=null;}
                }
                if(request.Downloaded == null) {
                    request.Stage = "downloading"; request.Detail = "正在下载更新…"; Save();
                    var reporter = new Progress<Tuple<long,long>>(value => {
                        if(State != request || request.Stage != "downloading") return;
                        request.Bytes = value.Item1; request.Total = value.Item2;
                        if(Changed != null) Changed();
                    });
                    request.Downloaded = await download(request.Release,
                        (done,total) => ((IProgress<Tuple<long,long>>)reporter).Report(Tuple.Create(done,total)), cancellation.Token);
                    cancellation.Token.ThrowIfCancellationRequested();
                    cacheValidated = true;
                    request.Bytes = request.Total = request.Release.Size;
                    request.Failures = 0; request.Stage = request.InstallRequested ? "waiting" : "downloaded";
                    request.Detail = request.InstallRequested ? "下载已完成，将交接管理服务并安装；Docker 实验继续运行。" : "下载已完成并校验，可排队安装。";
                    Save();
                }
                if(!request.InstallRequested) return;
                request.Stage = "waiting";
                request.NextAttemptUtc = now.AddSeconds(5);
                if(clientBusy) { request.Detail = "等待当前客户端操作完成后自动安装。"; Save(); return; }
                var ready = await readiness();
                object raw;
                if(!ready.TryGetValue("ready_for_update", out raw) || !(raw is bool) || !(bool)raw) {
                    request.Detail = "等待安装：" + (ready.ContainsKey("detail") ? Convert.ToString(ready["detail"]) : "尚未确认安全更新条件。 ");
                    Save(); return;
                }
                cancellation.Token.ThrowIfCancellationRequested();
                request.Stage = "installing"; request.Detail = "正在校验并交接安装…"; Save();
                await install(request.Release, request.Downloaded);
                request.Stage = "handoff"; request.Detail = "安装程序已启动，将自动重启客户端。"; Save();
            } catch(OperationCanceledException) {
                // Cancel already persisted the empty request. Never resurrect it.
            } catch(Exception ex) {
                if(request.Stage == "handoff") {
                    // The installer is already waiting for this client to exit.
                    // A final receipt write failure must not strand that handoff.
                    request.Detail += " 队列记录写入失败：" + ex.Message;
                } else if(State == request) {
                    cacheValidated = false;
                    request.Failures++;
                    request.Stage = "retry"; request.Detail = "更新等待重试：" + ex.Message;
                    request.NextAttemptUtc = now.AddSeconds(Math.Min(300, 30 * Math.Pow(2, Math.Min(4, request.Failures - 1))));
                    Save();
                }
            } finally {
                cancellation.Dispose(); cancellation = null; InFlight = false;
                if(Changed != null) Changed();
            }
        }
    }

    internal sealed class InstallerCleanupRequest {
        public string Path, Sha256, Version;
        public long Size, ProcessStartTicks;
        public int ProcessId;
        public bool Worker;
    }

    internal static class InstallerCleanup {
        internal static void Validate(InstallerCleanupRequest request, string currentExecutable) {
            if(request == null || request.ProcessId <= 0 || request.ProcessStartTicks <= 0 || request.Size < 2 ||
                    request.Size > UpdateService.MaxInstallerBytes || request.Sha256 == null || request.Sha256.Length != 64)
                throw new InvalidDataException("安装包清理记录无效。 ");
            string name = System.IO.Path.GetFileName(request.Path), suffix = request.Version + "-windows-" +
                (request.Worker ? "worker" : "controller") + "-x64-Setup.exe";
            UpdateService.CompareVersions(request.Version, request.Version);
            if(name != "ExLab-" + suffix && name != "ExperimentManager-" + suffix)
                throw new InvalidDataException("清理目标不是本次安装包。 ");
            if(!System.IO.Path.IsPathRooted(request.Path) || !String.Equals(System.IO.Path.GetFullPath(request.Path), request.Path, StringComparison.OrdinalIgnoreCase) ||
                    String.Equals(request.Path, System.IO.Path.GetFullPath(currentExecutable), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("安装包清理路径无效。 ");
            string ancestor = request.Path;
            while(ancestor != null) {
                if((File.Exists(ancestor) || Directory.Exists(ancestor)) && (File.GetAttributes(ancestor) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidDataException("安装包清理目标不能包含目录或文件链接。 ");
                ancestor = System.IO.Path.GetDirectoryName(ancestor);
            }
        }
        internal static InstallerCleanupRequest Record(string path, string version, bool worker, int pid, long started) {
            var request = new InstallerCleanupRequest { Path = System.IO.Path.GetFullPath(path), Version = version, Worker = worker,
                ProcessId = pid, ProcessStartTicks = started, Size = new FileInfo(path).Length };
            using(var sha = SHA256.Create()) using(var stream = File.OpenRead(path))
                request.Sha256 = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            return request;
        }
        internal static bool ProcessAlive(int pid, long started) {
            try { using(var process = Process.GetProcessById(pid)) return process.StartTime.ToUniversalTime().Ticks == started && !process.HasExited; }
            catch(ArgumentException) { return false; }
        }
        internal static bool TryDelete(InstallerCleanupRequest request, string currentExecutable, Func<int,long,bool> processAlive) {
            Validate(request, currentExecutable);
            if(processAlive(request.ProcessId, request.ProcessStartTicks)) return false;
            if(!File.Exists(request.Path)) return true;
            using(var stream = new FileStream(request.Path, FileMode.Open, FileAccess.Read, FileShare.Read)) {
                if(stream.Length != request.Size || stream.ReadByte() != 'M' || stream.ReadByte() != 'Z')
                    throw new InvalidDataException("安装包已发生变化，保留文件。 ");
                stream.Position = 0;
                using(var sha = SHA256.Create())
                    if(BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != request.Sha256)
                        throw new InvalidDataException("安装包哈希改变，保留文件。 ");
            }
            File.Delete(request.Path);
            return true;
        }
    }
}
