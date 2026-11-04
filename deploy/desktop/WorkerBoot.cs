using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;
using System.Xml;

namespace ExperimentManagerDesktop {
    internal sealed class WorkerBootConfig {
        public bool Enabled {get;set;}
        public string Distribution {get;set;}
        public string LinuxUser {get;set;}
        public string ServiceRoot {get;set;}
        public string NodeId {get;set;}
        public string EngineId {get;set;}
        public string OwnerSid {get;set;}
        public bool DedicatedDistribution {get;set;}
    }
    internal static class WorkerBoot {
        internal static string FilePath {get{return Path.Combine(App.SettingsRoot,"boot-config.json");}}
        internal static string StatusPath {get{return Path.Combine(App.SettingsRoot,"boot-status.json");}}
        internal static string TaskName {get{return "ExLab-Worker-Boot-"+WindowsIdentity.GetCurrent().User.Value;}}
        internal static string SuspendPath {get{return Path.Combine(App.SettingsRoot,"boot-suspend");}}
        internal static WorkerBootConfig Load() {
            return App.Json.ConvertToType<WorkerBootConfig>(App.Read(FilePath));
        }
        internal static bool Managed {get{return App.Worker&&Load().Enabled;}}
        internal static void Validate(WorkerBootConfig c) {
            if(c==null||String.IsNullOrWhiteSpace(c.Distribution)||c.Distribution.StartsWith("-")||c.Distribution.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase)||
                !System.Text.RegularExpressions.Regex.IsMatch(c.LinuxUser??"","\\A[a-z_][a-z0-9_-]*\\z")||c.LinuxUser=="root"||
                String.IsNullOrEmpty(c.ServiceRoot)||!c.ServiceRoot.StartsWith("/")||String.IsNullOrEmpty(c.NodeId)||String.IsNullOrEmpty(c.EngineId)||
                c.OwnerSid!=WindowsIdentity.GetCurrent().User.Value)
                throw new InvalidDataException("无人值守配置不完整或不属于当前 Windows 账户，请重新检测环境。");
        }
        static string[] Args(WorkerBootConfig c,params string[] tail) {
            var args=new List<string>{"-d",c.Distribution,"-u",c.LinuxUser,"--exec"};args.AddRange(tail);return args.ToArray();
        }
        internal static string[] InstalledCommand(string distribution,string package,params string[] tail) {
            var c=Load();var args=new List<string>{"-d",distribution};
            if(c.Enabled){Validate(c);if(c.Distribution!=distribution)throw new IOException("无人值守发行版与安装目标不一致。");args.Add("-u");args.Add(c.LinuxUser);}
            args.Add("--exec");args.Add("bash");args.Add(package+"/Client-Worker.sh");args.AddRange(tail);
            if(c.Enabled&&Array.IndexOf(tail,"--service-root")<0){args.Add("--service-root");args.Add(c.ServiceRoot);}return args.ToArray();
        }
        internal static string DisplayStatus() {
            var state=App.Read(StatusPath);DateTime timestamp;
            bool fresh=DateTime.TryParse(App.Text(state,"time"),out timestamp)&&(DateTime.UtcNow-timestamp.ToUniversalTime()).TotalSeconds<90;
            return "无人值守后台 · "+(fresh?App.Text(state,"status"):"状态已过期，点击启用可重试")+"\n"+App.Text(state,"detail");
        }
        internal static bool CanAutoUpdate(Dictionary<string,object> state) {
            DateTime timestamp;string status=App.Text(state,"status");
            return (status=="online"||status=="offline")&&DateTime.TryParse(App.Text(state,"time"),out timestamp)&&
                (DateTime.UtcNow-timestamp.ToUniversalTime()).TotalSeconds<90;
        }
        internal static Dictionary<string,object> Command(WorkerBootConfig c,string action,params string[] options) {
            string package=App.RunWithTimeout(20000,"wsl.exe",Args(c,"wslpath","-a",App.Package)).Trim();
            var tail=new List<string>{"bash",package+"/Client-Worker.sh",action,"--service-root",c.ServiceRoot};tail.AddRange(options);
            return App.CommandWithTimeout(action=="gpu-enable"?300000:120000,"wsl.exe",Args(c,tail.ToArray()));
        }
        internal static Dictionary<string,object> OriginalStatus(string distribution) {
            string package=App.RunWithTimeout(20000,"wsl.exe","-d",distribution,"--exec","wslpath","-a",App.Package).Trim();
            return App.CommandWithTimeout(60000,"wsl.exe","-d",distribution,"--exec","bash",package+"/Client-Worker.sh","install-status");
        }
        internal static Dictionary<string,object> Inspect(WorkerBootConfig c) {
            string package=App.RunWithTimeout(20000,"wsl.exe",Args(c,"wslpath","-a",App.Package)).Trim();
            return App.CommandWithTimeout(60000,"wsl.exe",Args(c,"bash",package+"/Check-Unattended-Worker.sh",c.ServiceRoot??"",c.EngineId??""));
        }
        internal static string TaskXml(string launcher,string account) {
            // No credentials enter XML, command lines, files or diagnostics.
            var doc=new XmlDocument();doc.LoadXml("<Task version='1.2' xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task'><RegistrationInfo><Description>ExLab unattended Worker</Description></RegistrationInfo><Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers><Principals><Principal id='Owner'><UserId/><LogonType>Password</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure></Settings><Actions Context='Owner'><Exec><Command/><Arguments/></Exec></Actions></Task>");
            var ns=new XmlNamespaceManager(doc.NameTable);ns.AddNamespace("t",doc.DocumentElement.NamespaceURI);
            doc.SelectSingleNode("//t:UserId",ns).InnerText=account;
            doc.SelectSingleNode("//t:Command",ns).InnerText=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell","v1.0","powershell.exe");
            doc.SelectSingleNode("//t:Arguments",ns).InnerText=App.Arguments("-NoProfile","-NonInteractive","-WindowStyle","Hidden","-ExecutionPolicy","Bypass","-File",launcher);
            return doc.OuterXml;
        }
        internal static void Register(WorkerBootConfig c,string password) {
            Validate(c);
            string installed=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","ExperimentManager","Worker",App.Version);
            if(!String.Equals(Path.GetFullPath(App.Package),Path.GetFullPath(installed),StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("请先安装此版本，再配置开机任务。");
            if(String.IsNullOrEmpty(password))throw new ArgumentException("请输入 Windows 账户密码（不是 PIN），由任务计划程序保存。");
            if(!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))throw new UnauthorizedAccessException("创建开机任务需要管理员权限。");
            string launcher=Path.Combine(App.SettingsRoot,"boot-launcher.ps1");
            // Stable launcher follows the installed version after updates.
            string script="$ErrorActionPreference='Stop'\n$root=Split-Path -Parent $PSCommandPath\n$s=Get-Content -LiteralPath (Join-Path $root 'settings.json') -Raw | ConvertFrom-Json\n$v=[string]$s.installed_version\nif($v -notmatch '^\\d+\\.\\d+\\.\\d+([-.][A-Za-z0-9.]+)?$'){throw 'Invalid installed version'}\n$exe=Join-Path $env:LOCALAPPDATA ('Programs\\ExperimentManager\\Worker\\'+$v+'\\ExLabWorker.exe')\n$p=Start-Process -FilePath $exe -ArgumentList '--boot-run' -WindowStyle Hidden -PassThru -Wait\nexit $p.ExitCode\n";
            Directory.CreateDirectory(App.SettingsRoot);File.WriteAllText(launcher,script,new System.Text.UTF8Encoding(true));
            dynamic scheduler=Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service"));scheduler.Connect();
            dynamic folder=scheduler.GetFolder("\\");
            string account=WindowsIdentity.GetCurrent().Name;
            folder.RegisterTask(TaskName,TaskXml(launcher,account),6,account,password,1,null);
            c.Enabled=true;App.Write(FilePath,c);
        }
        internal static void DisableAutostart() {
            dynamic scheduler=Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service"));scheduler.Connect();
            scheduler.GetFolder("\\").GetTask(TaskName).Enabled=false;
        }
        internal static void Request(string mode) {
            if(mode!="available"&&mode!="release")throw new ArgumentException("Unsupported local request");
            App.Write(Path.Combine(App.SettingsRoot,"boot-request.json"),new{mode=mode});
        }
        internal static void EnsureHost() {
            if(!Managed||File.Exists(SuspendPath))return;
            try {using(new FileStream(Path.Combine(App.SettingsRoot,"boot-owner.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None)){} }
            catch(IOException){return;}
            string version=App.Text(App.ReadSettings(),"installed_version");
            if(!System.Text.RegularExpressions.Regex.IsMatch(version,"\\A[0-9]+\\.[0-9]+\\.[0-9]+([-.][A-Za-z0-9.]+)?\\z"))throw new IOException("已安装版本记录无效，请重新安装 Worker。");
            string executable=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","ExperimentManager","Worker",version,App.Executable);
            Process.Start(new ProcessStartInfo(executable,"--boot-run"){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden});
        }
        internal static async Task SuspendForUpdate() {
            App.Write(SuspendPath,new{time=DateTime.UtcNow.ToString("o")});
            var until=DateTime.UtcNow.AddSeconds(90);
            while(true) {
                try {using(new FileStream(Path.Combine(App.SettingsRoot,"boot-owner.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None))return;}
                catch(IOException) {if(DateTime.UtcNow>=until)throw new IOException("无人值守后台仍在操作，请稍后重试更新。");}
                await Task.Delay(500);
            }
        }
        internal static void ResumeAfterUpdate(){if(File.Exists(SuspendPath))File.Delete(SuspendPath);EnsureHost();}
        internal static string Desired(string local,Dictionary<string,object> desired,bool known) {
            if(local=="release")return "release";
            if(!known)return "wait";
            string mode=App.Text(desired,"mode");
            return mode=="release"?"release":"available";
        }
        static Process EnsureHold(WorkerBootConfig c,Process hold) {
            if(hold!=null&&!hold.HasExited)return hold;
            if(hold!=null)hold.Dispose();
            string package=App.RunWithTimeout(20000,"wsl.exe",Args(c,"wslpath","-a",App.Package)).Trim();
            return Process.Start(new ProcessStartInfo("wsl.exe",App.Arguments(Args(c,"bash",package+"/Client-Worker.sh","_hold","--service-root",c.ServiceRoot))){UseShellExecute=false,CreateNoWindow=true});
        }
        internal static int Run() {
            Directory.CreateDirectory(App.SettingsRoot);
            FileStream owner;
            try {owner=new FileStream(Path.Combine(App.SettingsRoot,"boot-owner.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);}
            catch(IOException){return 0;}
            using(owner) {
                var c=Load();Validate(c);if(!c.Enabled)return 0;
                var host=new DesktopHostControl(Path.Combine(App.SettingsRoot,"host-control.json"));
                var retry=new DesktopRetrySchedule();Process hold=null;
                Dictionary<string,object> desired=null;bool known=false;
                try {while(Load().Enabled) {
                    try {
                        if(File.Exists(SuspendPath)){WriteStatus("suspended","等待软件更新交接。");return 0;}
                        if(host.Linked) {
                            if(host.NodeId!=c.NodeId||host.Distribution!=c.Distribution)throw new IOException("Windows 控制连接与无人值守节点不一致，请重新检测并配置。");
                            try {desired=host.Sync();known=true;}catch(System.Net.WebException){ /* Retain last confirmed policy during an outage. */ }
                        }
                        string local=App.Text(App.Read(Path.Combine(App.SettingsRoot,"boot-request.json")),"mode","available");
                        string mode=Desired(local,desired,known),intent=mode+"/"+(desired==null?"":App.Text(desired,"intent"));
                        if(mode=="wait"){WriteStatus("waiting","等待主控策略确认，尚未启用算力。");Thread.Sleep(5000);continue;}
                        if(!retry.Ready(intent,DateTime.UtcNow)){Thread.Sleep(1000);continue;}
                        if(mode=="release") {
                            {
                                var running=DesktopRuntime.RunningDistributions(App.RunWithTimeout(15000,"wsl.exe","--list","--running","--quiet"));
                                if(Array.Exists(running,n=>n.Equals(c.Distribution,StringComparison.OrdinalIgnoreCase))) {
                                    Command(c,"deactivate");
                                    DesktopLifecycle.WaitForStopped(()=>Task.FromResult(Command(c,"status")),s=>{
                                        WriteStatus("stopping",App.Text(s,"detail"));
                                        if(host.Linked) {try{desired=host.Sync();}catch(System.Net.WebException){}}
                                    },TimeSpan.FromMinutes(10),finalStatus:"stopped").GetAwaiter().GetResult();
                                    if(local!="release"&&desired!=null&&App.Text(desired,"mode")!="release")continue;
                                    var state=Command(c,"runtime-release");
                                    if(!App.Flag(state,"can_stop_docker")||!App.Flag(state,"can_terminate_wsl"))throw new IOException(App.Text(state,"detail","运行环境仍有其他工作，已保留。"));
                                    if(!c.DedicatedDistribution)throw new IOException("算力已停止；此 Ubuntu 未授权作为专用环境关闭，WSL 保留。");
                                    if(hold!=null){if(!hold.HasExited)hold.Kill();hold.Dispose();hold=null;}
                                    App.RunWithTimeout(30000,"wsl.exe","-d",c.Distribution,"-u","root","--exec","systemctl","stop","docker.service","docker.socket");
                                    App.RunWithTimeout(30000,"wsl.exe","--terminate",c.Distribution);
                                    var remaining=DesktopRuntime.RunningDistributions(App.RunWithTimeout(15000,"wsl.exe","--list","--running","--quiet"));
                                    if(Array.Exists(remaining,n=>n.Equals(c.Distribution,StringComparison.OrdinalIgnoreCase)))throw new IOException("Ubuntu 仍在运行，尚未确认释放。");
                                }
                            }
                            WriteStatus("released","算力已停用，指定 Ubuntu 已关闭；Windows 控制连接仍在线。");
                            if(desired!=null&&App.Text(desired,"mode")=="release")host.Observe(App.Text(desired,"intent"),"released","无人值守运行环境已释放。",new Dictionary<string,object>{{"resources_released",true},{"docker_stopped",true},{"wsl_stopped",true},{"agents_stopped",true}});
                        } else {
                            if(desired!=null&&App.Flag(desired,"require_ac")&&System.Windows.Forms.SystemInformation.PowerStatus.PowerLineStatus!=System.Windows.Forms.PowerLineStatus.Online)
                                throw new IOException("主控要求接通电源，尚未确认供电；保持等待。");
                            var current=Command(c,"status");
                            if(App.Flag(current,"running")&&(App.Text(current,"status")=="online"||App.Text(current,"status")=="offline")) {
                                hold=EnsureHold(c,hold);
                                WriteStatus(App.Text(current,"status"),App.Text(current,"detail"));retry.Completed();Thread.Sleep(5000);continue;
                            }
                            WriteStatus("starting","正在检查独立 Docker 与 GPU…");
                            App.RunWithTimeout(45000,"wsl.exe","-d",c.Distribution,"-u","root","--exec","systemctl","start","docker.service");
                            var check=Inspect(c);if(!App.Flag(check,"ready")||App.Text(check,"node_id")!=c.NodeId)throw new IOException(App.Text(check,"detail","节点检查未通过。"));
                            var state=Command(c,"start");
                            hold=EnsureHold(c,hold);
                            WriteStatus(App.Text(state,"status"),App.Text(state,"detail"));
                            if(App.Text(state,"status")=="failed"||App.Text(state,"status")=="exit_failed")throw new IOException(App.Text(state,"detail"));
                        }
                        retry.Completed();Thread.Sleep(5000);
                    } catch(Exception ex){WriteStatus("failed",ex.Message);if(host.Linked&&desired!=null){try{host.Observe(App.Text(desired,"intent"),"blocked",ex.Message);}catch(Exception){}}retry.Failed(DateTime.UtcNow);Thread.Sleep(1000);}
                }} finally {if(hold!=null)hold.Dispose();}
            }
            return 0;
        }
        static void WriteStatus(string status,string detail) {App.Write(StatusPath,new{time=DateTime.UtcNow.ToString("o"),status=status,detail=detail,pid=Process.GetCurrentProcess().Id});}
    }
}
