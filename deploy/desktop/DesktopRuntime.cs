using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace ExperimentManagerDesktop {
    internal static class DesktopRuntime {
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
        internal static Task<string> Release(string distribution,Dictionary<string,object> state,bool closeOtherProcesses=false) {
            return Release(distribution,state,closeOtherProcesses,Run);
        }
        internal static Task<Dictionary<string,object>> InspectStoppedDistribution() {
            return InspectStoppedDistribution(Run);
        }
        internal static async Task<Dictionary<string,object>> InspectStoppedDistribution(Func<int,string,string[],Task<string>> execute) {
            // The selected Ubuntu is already stopped. Inspect Windows Docker
            // directly so a memory-release request never starts Ubuntu again.
            var state=new Dictionary<string,object>{{"agents_stopped",true},{"docker_ready",false},
                {"can_stop_docker",false},{"can_terminate_wsl",true}};
            try {
                string identity=await execute(15000,DockerCli,new[]{"--context","desktop-linux","info","--format","{{.ID}}"});
                string live=await execute(15000,DockerCli,new[]{"--context","desktop-linux","ps","-q"});
                state["docker_ready"]=true;state["docker_id"]=identity.Trim();state["can_stop_docker"]=live.Trim().Length==0;
                state["detail"]=live.Trim().Length==0?"Docker 已空闲":"其他 Docker 容器仍在运行，运行环境已保留。";
            } catch(Exception) { state["detail"]="Docker 未连接；仅关闭所选 Ubuntu，其他运行环境保留。"; }
            return state;
        }
        internal static async Task<string> Release(string distribution,Dictionary<string,object> state,bool closeOtherProcesses,
                Func<int,string,string[],Task<string>> execute) {
            if(String.IsNullOrWhiteSpace(distribution)||distribution.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("请选择算力端使用的 Ubuntu。");
            if(closeOtherProcesses&&!App.Flag(state,"agents_stopped"))
                return "还有算力代理未停止，运行环境已保留；请先完成停用。";
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
                try {await execute(60000,DockerCli,new[]{"desktop","stop"});dockerStopped=true;}
                catch(Exception) { /* An empty, verified Docker may remain running on older CLI versions. */ }
            }
            if(!App.Flag(state,"can_terminate_wsl")&&!closeOtherProcesses)
                return "Docker "+(dockerStopped?"已停止":"保持运行")+"；Ubuntu 中另有进程，WSL 已保留。可点击“释放 WSL 内存”确认关闭。";
            string[] running=RunningDistributions(await execute(15000,"wsl.exe",new[]{"--list","--running","--quiet"}));
            bool all=dockerStopped&&CanShutdownAll(distribution,running);
            await execute(30000,"wsl.exe",all?new[]{"--shutdown"}:new[]{"--terminate",distribution});
            // A Windows-only recheck cannot wake WSL. Report any immediate
            // restart by another application instead of claiming memory freed.
            string[] after=RunningDistributions(await execute(15000,"wsl.exe",new[]{"--list","--running","--quiet"}));
            foreach(string name in after)if(name.Equals(distribution,StringComparison.OrdinalIgnoreCase))
                return "Ubuntu 被其他程序重新启动，WSL 内存尚未完全释放；请关闭相关会话后重试。";
            if(all&&after.Length==0) {
                return "WSL 已关闭，内存已释放；Center 保持运行。镜像、数据和实验记录保留。";
            }
            return "算力 Ubuntu 已停止；其他 WSL 或 Docker 环境可能仍在运行并占用内存。Center 保持运行。";
        }
    }
}
