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
        internal static async Task<string> Release(string distribution,Dictionary<string,object> state) {
            if(!App.Flag(state,"can_stop_docker"))return App.Text(state,"detail","运行环境仍有其他工作，已保留。");
            bool dockerStopped=false;
            try {
                string expected=App.Text(state,"docker_id");
                string actual=await Task.Run(()=>App.RunWithTimeout(15000,DockerCli,"--context","desktop-linux","info","--format","{{.ID}}"));
                string containers=await Task.Run(()=>App.RunWithTimeout(15000,DockerCli,"--context","desktop-linux","ps","-q"));
                if(expected.Length>0&&actual.Trim()==expected&&containers.Trim().Length==0) {
                    await Task.Run(()=>App.RunWithTimeout(60000,DockerCli,"desktop","stop"));dockerStopped=true;
                }
            } catch(Exception) {
                // Do not kill Docker Desktop processes: the supported stop CLI
                // is the only shutdown authority, including on older versions.
            }
            if(!App.Flag(state,"can_terminate_wsl"))return "Docker "+(dockerStopped?"已停止":"保持运行")+"；Ubuntu 中另有进程，WSL 已保留。";
            string inventory=await Task.Run(()=>App.RunWithTimeout(15000,"wsl.exe","--list","--running","--quiet"));
            string[] running=inventory.Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries);
            if(dockerStopped&&CanShutdownAll(distribution,Array.ConvertAll(running,name=>name.Trim()))) {
                await Task.Run(()=>App.RunWithTimeout(30000,"wsl.exe","--shutdown"));
                return "Docker 与 WSL 已释放；镜像、数据和实验记录保留。";
            }
            await Task.Run(()=>App.RunWithTimeout(30000,"wsl.exe","--terminate",distribution));
            return "算力 Ubuntu 已停止；其他 WSL 环境保持运行。";
        }
    }
}
