using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Collections;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace ExperimentManagerDesktop {
    internal static class DesktopRuntime {
        internal static string ComputeState(Dictionary<string,object> value) {
            object raw;var availability=value.TryGetValue("availability",out raw)?raw as Dictionary<string,object>:null;
            var receipt=value.TryGetValue("compute_control",out raw)?raw as Dictionary<string,object>:null;
            var policy=receipt!=null&&receipt.TryGetValue("policy",out raw)?raw as Dictionary<string,object>:null;
            bool matches=availability!=null&&policy!=null;
            foreach(string key in new[]{"mode","revision","source","until","reason"})
                matches=matches&&App.Text(availability,key)==App.Text(policy,key);
            string mode=availability==null?"":App.Text(availability,"mode");
            if(!matches)return mode=="stop"||mode=="release"?"等待 Worker 执行并核验；尚未确认停止或释放":mode=="drain"?"已暂停接单；实验继续运行":"允许接单";
            string phase=App.Text(receipt,"phase");
            string label=phase=="released"&&App.Flag(receipt,"resources_released")&&App.Flag(receipt,"experiments_stopped")?(App.Flag(receipt,"runtime_retained")?"训练已停止 · 运行环境仍保留":"资源已释放 · 接单已暂停"):phase=="stopped"&&App.Flag(receipt,"experiments_stopped")?"实验已停止 · 接单已暂停":phase=="stopping"?"正在保存并停止实验":phase=="releasing"?"正在释放资源":phase=="blocked"?"执行未完成 · 需要检查":phase=="admission_paused"?"已暂停接单 · 实验继续":phase=="available"?"允许接单":"等待执行核验";
            return (App.Flag(value,"running")?"":"上次回报：")+label+"；"+App.Text(receipt,"detail");
        }
        internal const string RunKey="Software\\Microsoft\\Windows\\CurrentVersion\\Run";
        internal static bool AutoStart {
            get { using(var run=Registry.CurrentUser.OpenSubKey(RunKey))
                return run!=null&&(run.GetValue("ExLab-"+App.Role)!=null||run.GetValue("ExperimentManager-"+App.Role)!=null); }
            set { using(var run=Registry.CurrentUser.CreateSubKey(RunKey)) {
                run.DeleteValue("ExperimentManager-"+App.Role,false);
                if(value)run.SetValue("ExLab-"+App.Role,App.Quote(Path.Combine(App.Package,App.Executable))+" --background");
                else run.DeleteValue("ExLab-"+App.Role,false);
            } }
        }
        internal static string DockerCli {
            get { string path=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Docker","Docker","resources","bin","docker.exe");
                return File.Exists(path)?path:"docker.exe"; }
        }
        internal static async Task StartDocker() {
            await Task.Run(()=> {
                try { App.RunWithTimeout(120000,DockerCli,"desktop","start");return; }
                catch(Exception) { }
                string executable=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Docker","Docker","Docker Desktop.exe");
                if(!File.Exists(executable))executable=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","DockerDesktop","Docker Desktop.exe");
                if(!File.Exists(executable))throw new IOException("未找到 Docker Desktop。请先安装，并启用所选 Ubuntu 的 WSL 集成。");
                Process.Start(new ProcessStartInfo(executable){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden});
            });
        }
        internal static async Task<bool> EnsureDocker(Func<Task<Dictionary<string,object>>> inspect,
                Func<Dictionary<string,object>,bool> confirmStart,Func<Task> start,Action<string> progress,
                Func<Task> delay=null,TimeSpan? timeout=null) {
            var state=await inspect();
            if(App.Flag(state,"docker_ready"))return true;
            // Manual activation is allowed even when this client originally
            // launched with --background at Windows login.
            if(!confirmStart(state))return false;
            progress("正在启动 Docker Desktop…");await start();var watch=Stopwatch.StartNew();
            while(watch.Elapsed<(timeout??TimeSpan.FromMinutes(2))) {
                if(delay==null)await Task.Delay(2000);else await delay();
                state=await inspect();if(App.Flag(state,"docker_ready"))return true;
            }
            throw new IOException("Docker 尚未就绪。请检查 Docker Desktop 的 Linux containers、所选 Ubuntu 的 WSL 集成，以及本机 Docker context。然后重试。");
        }
        internal static bool CanShutdownAll(string distribution,IEnumerable<string> running) {
            foreach(string name in running)if(name.Length>0&&!name.Equals(distribution,StringComparison.OrdinalIgnoreCase)&&
                    !name.Equals("docker-desktop",StringComparison.OrdinalIgnoreCase)&&!name.Equals("docker-desktop-data",StringComparison.OrdinalIgnoreCase))return false;
            return true;
        }
        static Task<string> Run(int timeout,string executable,string[] arguments) {
            return Task.Run(()=>App.RunWithTimeout(timeout,executable,arguments));
        }
        internal static string[] RunningDistributions(string inventory) {
            return Array.ConvertAll(inventory.Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries),name=>name.Trim());
        }
        internal static Task<string> Release(string distribution,Dictionary<string,object> state,bool closeOtherProcesses=false,IEnumerable<string> stopContainers=null) {
            return Release(distribution,state,closeOtherProcesses,Run,stopContainers);
        }
        internal static async Task<Dictionary<string,object>> PrepareWorkerRelease(string distribution,
                Func<string,Task<Dictionary<string,object>>> workerCommand,Func<bool> confirmStop,
                Action<Dictionary<string,object>> progress,Action stopped,
                Func<int,string,string[],Task<string>> execute=null,Func<Task> delay=null) {
            if(String.IsNullOrWhiteSpace(distribution)||distribution.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("请选择算力端使用的 Ubuntu。");
            if(execute==null)execute=Run;
            string[] running=RunningDistributions(await execute(15000,"wsl.exe",new[]{"--list","--running","--quiet"}));
            Dictionary<string,object> state;
            bool selectedRunning=Array.Exists(running,name=>name.Equals(distribution,StringComparison.OrdinalIgnoreCase));
            if(selectedRunning) {
                var current=await workerCommand("status");
                if((App.Flag(current,"running")||App.Text(current,"status")=="starting"||App.Text(current,"status")=="preparing")&&
                        !confirmStop())return null;
                var requested=await workerCommand("deactivate");
                if(progress!=null)progress(requested);
                await DesktopLifecycle.WaitForStopped(()=>workerCommand("status"),progress,null,delay,finalStatus:"stopped");
                // Mark the client dormant before inspecting resources so its
                // polling and WSL hold cannot keep or restart Ubuntu.
                stopped();
                state=await workerCommand("runtime-release");
            } else {
                // Repeated release must not wake an already stopped Ubuntu.
                stopped();
                state=await InspectStoppedDistribution(execute);
            }
            if(state==null)throw new InvalidDataException("未取得资源检查结果；算力已停用，可重试释放。");
            // Also repair older WSL backends which misclassified ExLab's
            // algorithm registry as an unrelated container.
            if(selectedRunning&&App.Flag(state,"agents_stopped")&&App.Flag(state,"docker_ready")&&!App.Flag(state,"can_stop_docker")) {
                if(App.Text(state,"docker_id").Length==0)throw new InvalidDataException("未取得原 Docker 身份，资源释放未完成；请重试。");
                state=await InspectDocker(state,execute);
            }
            return state;
        }
        internal static Task<Dictionary<string,object>> InspectStoppedDistribution() {
            return InspectStoppedDistribution(Run);
        }
        internal static bool NeedsOtherProcessConfirmation(Dictionary<string,object> state) {
            return App.Flag(state,"agents_stopped")&&!App.Flag(state,"can_terminate_wsl")&&
                (App.Flag(state,"can_stop_docker")||!App.Flag(state,"docker_ready")||ContainerIds(state).Count>0);
        }
        static bool ContainerId(string value) { return Regex.IsMatch(value??"","\\A[0-9a-f]{64}\\z"); }
        internal static List<Dictionary<string,object>> OtherContainers(Dictionary<string,object> state) {
            var items=new List<Dictionary<string,object>>();object raw;
            var values=state.TryGetValue("other_container_details",out raw)?raw as IEnumerable:null;
            if(values!=null)foreach(object value in values) {
                var item=value as Dictionary<string,object>;
                if(item!=null&&ContainerId(App.Text(item,"id")))items.Add(item);
            }
            return items;
        }
        internal static List<string> ContainerIds(Dictionary<string,object> state) {
            var ids=new List<string>();
            if(App.Flag(state,"agents_stopped")&&App.Flag(state,"docker_ready"))
                foreach(var item in OtherContainers(state))ids.Add(App.Text(item,"id"));
            return ids;
        }
        static async Task<List<Dictionary<string,object>>> DockerContainers(Func<int,string,string[],Task<string>> execute) {
            string output=await execute(15000,DockerCli,new[]{"--context","desktop-linux","ps","--no-trunc","--format","{{json .}}"});
            var items=new List<Dictionary<string,object>>();
            foreach(string line in output.Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries)) {
                var item=App.Json.Deserialize<Dictionary<string,object>>(line);
                if(item==null||!ContainerId(App.Text(item,"ID")))throw new InvalidDataException("无法确认运行容器的完整身份。");
                items.Add(item);
            }
            return items;
        }
        internal static async Task<Dictionary<string,object>> InspectDocker(Dictionary<string,object> state,Func<int,string,string[],Task<string>> execute) {
            if(!App.Flag(state,"agents_stopped"))return state;
            string expected=App.Text(state,"docker_id");state["can_stop_docker"]=false;
            string identity=(await execute(15000,DockerCli,new[]{"--context","desktop-linux","info","--format","{{.ID}}"})).Trim();
            if(identity.Length==0||(expected.Length>0&&expected!=identity))
                throw new IOException("Docker 身份发生变化，请重试释放。");
            var live=await DockerContainers(execute);bool stopped=false;
            foreach(var item in live) {
                string name=App.Text(item,"Names"),component=name=="expman-worker-registry"?"worker-registry":name=="expman-harness-registry"?"harness-registry":"";
                if(component.Length==0)continue;
                string id=App.Text(item,"ID");
                var inspected=App.Json.Deserialize<Dictionary<string,object>[]>(await execute(15000,DockerCli,new[]{"--context","desktop-linux","inspect",id}));
                if(inspected==null||inspected.Length!=1||inspected[0]==null)throw new InvalidDataException("无法确认镜像仓库归属。");
                object raw;var config=inspected.Length==1&&inspected[0].TryGetValue("Config",out raw)?raw as Dictionary<string,object>:null;
                var labels=config!=null&&config.TryGetValue("Labels",out raw)?raw as Dictionary<string,object>:null;
                if(inspected.Length!=1||App.Text(inspected[0],"Id")!=id||App.Text(inspected[0],"Name")!="/"+name||labels==null||App.Text(labels,"expman.component")!=component)continue;
                await execute(30000,DockerCli,new[]{"--context","desktop-linux","stop","--time","10",id});stopped=true;
            }
            if(stopped)live=await DockerContainers(execute);
            var others=new List<Dictionary<string,object>>();var names=new List<string>();
            foreach(var item in live) {
                others.Add(new Dictionary<string,object>{{"id",App.Text(item,"ID")},{"name",App.Text(item,"Names")},{"image",App.Text(item,"Image")}});
                names.Add(App.Text(item,"Names"));
            }
            state["docker_ready"]=true;state["docker_id"]=identity;state["can_stop_docker"]=others.Count==0;
            state["other_containers"]=others.Count;state["other_container_details"]=others;
            state["detail"]=others.Count==0?"Docker 已空闲":"资源释放未完成：仍在运行的容器："+String.Join("、",names)+"。可确认停止后继续释放。";
            return state;
        }
        internal static async Task<Dictionary<string,object>> InspectStoppedDistribution(Func<int,string,string[],Task<string>> execute) {
            // The selected Ubuntu is already stopped. Inspect Windows Docker
            // directly so a memory-release request never starts Ubuntu again.
            var state=new Dictionary<string,object>{{"agents_stopped",true},{"docker_ready",false},
                {"can_stop_docker",false},{"can_terminate_wsl",true}};
            try {
                return await InspectDocker(state,execute);
            } catch(Exception) { state["detail"]="Docker 未连接；仅关闭所选 Ubuntu，其他运行环境保留。"; }
            return state;
        }
        internal static async Task<string> Release(string distribution,Dictionary<string,object> state,bool closeOtherProcesses,
                Func<int,string,string[],Task<string>> execute,IEnumerable<string> stopContainers=null) {
            state["resources_released"]=false;
            state["docker_stopped"]=false;state["wsl_stopped"]=false;
            if(String.IsNullOrWhiteSpace(distribution)||distribution.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("请选择算力端使用的 Ubuntu。");
            if(closeOtherProcesses&&!App.Flag(state,"agents_stopped"))
                return "还有算力代理未停止，运行环境已保留；请先完成停用。";
            var approved=stopContainers==null?new HashSet<string>():new HashSet<string>(stopContainers,StringComparer.Ordinal);
            if(approved.Count>0) {
                var shown=new HashSet<string>(ContainerIds(state),StringComparer.Ordinal);
                if(!closeOtherProcesses||!approved.IsSubsetOf(shown))return "未确认这些容器的停止操作，资源释放未完成。";
                string actual=(await execute(15000,DockerCli,new[]{"--context","desktop-linux","info","--format","{{.ID}}"})).Trim();
                if(App.Text(state,"docker_id").Length==0||actual!=App.Text(state,"docker_id"))return "Docker 身份发生变化，资源释放未完成；请重试。";
                var live=await DockerContainers(execute);
                foreach(var item in live)if(!approved.Contains(App.Text(item,"ID")))return "出现未确认的新容器："+App.Text(item,"Names")+"；请重新确认释放。";
                foreach(var item in live)await execute(45000,DockerCli,new[]{"--context","desktop-linux","stop","--time","20",App.Text(item,"ID")});
                string remaining=await execute(15000,DockerCli,new[]{"--context","desktop-linux","ps","-q"});
                if(remaining.Trim().Length>0)return "仍有容器运行，资源释放未完成；请重试。";
                state["can_stop_docker"]=true;
            }
            bool dockerStopped=false;
            if(!App.Flag(state,"can_stop_docker")) {
                if(!closeOtherProcesses||!App.Flag(state,"agents_stopped")||App.Flag(state,"docker_ready"))
                    return App.Text(state,"detail","运行环境仍有其他工作，已保留。");
                // Explicitly closing Ubuntu is allowed only after its agents
                // stopped and the user accepted its other processes. A failed
                // daemon query never authorizes stopping Docker or another distro.
                try {
                    var desktop=App.Json.Deserialize<Dictionary<string,object>>(await execute(15000,DockerCli,new[]{"desktop","status","--format","json"}));
                    dockerStopped=App.Text(desktop,"Status").Equals("stopped",StringComparison.OrdinalIgnoreCase);
                } catch(Exception) { }
            } else {
                try {
                    string expected=App.Text(state,"docker_id");
                    string actual=await execute(15000,DockerCli,new[]{"--context","desktop-linux","info","--format","{{.ID}}"});
                    string containers=await execute(15000,DockerCli,new[]{"--context","desktop-linux","ps","-q"});
                    if(expected.Length==0||actual.Trim()!=expected||containers.Trim().Length!=0)
                        return "Docker 身份或容器状态发生变化，运行环境已保留；请重试释放内存。";
                } catch(Exception) {
                    return "无法复核 Docker 状态，运行环境已保留；请恢复连接后重试。";
                }
                try {
                    await execute(60000,DockerCli,new[]{"desktop","stop"});
                    var desktop=App.Json.Deserialize<Dictionary<string,object>>(await execute(15000,DockerCli,new[]{"desktop","status","--format","json"}));
                    dockerStopped=App.Text(desktop,"Status").Equals("stopped",StringComparison.OrdinalIgnoreCase);
                }
                catch(Exception) { /* An empty, verified Docker may remain running on older CLI versions. */ }
            }
            state["docker_stopped"]=dockerStopped;
            if(!App.Flag(state,"can_terminate_wsl")&&!closeOtherProcesses)
                return "Docker "+(dockerStopped?"已停止":"保持运行")+"；Ubuntu 中另有进程，WSL 内存尚未释放。可点击“停用并释放 Docker / WSL”确认关闭。";
            string[] running=RunningDistributions(await execute(15000,"wsl.exe",new[]{"--list","--running","--quiet"}));
            bool all=dockerStopped&&CanShutdownAll(distribution,running);
            await execute(30000,"wsl.exe",all?new[]{"--shutdown"}:new[]{"--terminate",distribution});
            // A Windows-only recheck cannot wake WSL. Report any immediate
            // restart by another application instead of claiming memory freed.
            string[] after=RunningDistributions(await execute(15000,"wsl.exe",new[]{"--list","--running","--quiet"}));
            foreach(string name in after)if(name.Equals(distribution,StringComparison.OrdinalIgnoreCase))
                return "Ubuntu 被其他程序重新启动，WSL 内存尚未完全释放；请关闭相关会话后重试。";
            state["wsl_stopped"]=after.Length==0;
            if(all&&after.Length==0) {
                state["resources_released"]=true;
                return "WSL 已关闭，内存已释放；Center 保持运行。镜像、数据和实验记录保留。";
            }
            return "算力 Ubuntu 已停止；其他 WSL 或 Docker 环境可能仍在运行并占用内存。Center 保持运行。";
        }
    }
}
