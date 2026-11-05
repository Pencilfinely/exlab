using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace ExperimentManagerDesktop {
    internal static class WorkerMigration {
        static volatile bool cancelRequested;
        internal static string ActivePath {get{return Path.Combine(App.SettingsRoot,"migration-active.json");}}
        internal static bool Pending {get{var j=App.Read(ActivePath);return j.Count>0&&!App.Flag(j,"finished");}}
        internal static bool CanRollback(Dictionary<string,object> job){return !App.Flag(job,"committed")&&!App.Flag(job,"finished")&&!File.Exists(Path.Combine(App.Text(job,"path"),"committed.json"));}
        internal static void CheckOwner(Dictionary<string,object> job) {
            if(App.Text(job,"owner")!=WindowsIdentity.GetCurrent().User.Value)throw new IOException("请使用创建迁移的同一 Windows 账户。");
            string id=App.Text(job,"id"),path=App.Text(job,"path"),target=App.Text(job,"target");
            bool inplace=App.Text(job,"mode")=="in_place";
            if(!System.Text.RegularExpressions.Regex.IsMatch(id,"\\A[a-f0-9]{32}\\z")||(inplace?target!=App.Text(job,"source")||String.IsNullOrEmpty(target)||target.StartsWith("-")||target.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase):target!="ExLab-Worker-"+id)||Path.GetFileName(path)!=id||!Path.IsPathRooted(path))throw new IOException("迁移记录无效，拒绝修改运行环境。");
        }
        static void Save(Dictionary<string,object> job,string stage,IProgress<string> progress) {
            job["stage"]=stage;App.Write(ActivePath,job);
            File.AppendAllText(Path.Combine(App.Text(job,"path"),"progress.log"),DateTime.UtcNow.ToString("o")+" "+stage+Environment.NewLine,Encoding.UTF8);
            if(progress!=null)progress.Report(stage);
        }
        internal static string SuggestedFolder() {
            DriveInfo chosen=null;
            foreach(var drive in DriveInfo.GetDrives())try {if(drive.DriveType==DriveType.Fixed&&drive.IsReady&&(chosen==null||drive.AvailableFreeSpace>chosen.AvailableFreeSpace))chosen=drive;}catch(IOException){}
            return chosen==null?Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExLabWorker"):Path.Combine(chosen.RootDirectory.FullName,"ExLabWorker");
        }
        internal static void CheckSpace(string folder,long required) {
            string full=Path.GetFullPath(folder);var drive=new DriveInfo(Path.GetPathRoot(full));
            if(full.StartsWith(@"\\")||drive.DriveType!=DriveType.Fixed||!drive.IsReady||drive.DriveFormat!="NTFS")throw new IOException("请选择本机 NTFS 固定磁盘。网络盘和移动盘不能保证未登录启动。");
            if(drive.AvailableFreeSpace<required)throw new IOException("空间不足：此磁盘还需约 "+Math.Ceiling(required/1073741824.0)+" GB 可用空间。请先清理可回收项。");
        }
        internal static string DistributionPath(string distribution) {
            using(var root=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Lxss"))if(root!=null)foreach(var name in root.GetSubKeyNames())using(var key=root.OpenSubKey(name))if(String.Equals(Convert.ToString(key.GetValue("DistributionName")),distribution,StringComparison.OrdinalIgnoreCase)) {
                string path=Convert.ToString(key.GetValue("BasePath"));if(path.StartsWith(@"\\?\"))path=path.Substring(4);return Path.GetFullPath(path);
            }
            throw new IOException("找不到此 Ubuntu 的磁盘位置。");
        }
        static void PrivateDirectory(string path) {
            Directory.CreateDirectory(path);
            var acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);
            foreach(var sid in new[]{WindowsIdentity.GetCurrent().User,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)})
                acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            new DirectoryInfo(path).SetAccessControl(acl);
        }
        static string Run(int timeout,params string[] args) {
            var info=new ProcessStartInfo("wsl.exe",App.Arguments(args)){UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8};
            using(var process=new Process {StartInfo=info}) {
                // Bound apt/export diagnostics; never put command output containing
                // node configuration or registry credentials into the UI/log.
                var output=new StringBuilder();object gate=new object();
                process.OutputDataReceived+=(s,e)=>{if(e.Data!=null)lock(gate){if(output.Length>4*1024*1024)output.Remove(0,output.Length-1024*1024);output.AppendLine(e.Data);}};
                process.ErrorDataReceived+=(s,e)=>{};
                process.Start();
                if(Pending) {var job=App.Read(ActivePath);job["child_pid"]=process.Id;job["child_start"]=process.StartTime.ToUniversalTime().Ticks.ToString();App.Write(ActivePath,job);}
                process.BeginOutputReadLine();process.BeginErrorReadLine();
                // Closing wsl.exe does not reliably stop Linux apt/docker. Never
                // roll back while a detached operation can still mutate data.
                if(!process.WaitForExit(timeout))process.WaitForExit();
                process.WaitForExit();string value=output.ToString().Trim();
                if(process.ExitCode!=0) {
                    if(value.StartsWith("{")) {try {throw new IOException(App.Text(App.Json.Deserialize<Dictionary<string,object>>(value),"detail","迁移操作失败。"));}catch(ArgumentException){}}
                    throw new IOException("WSL 迁移步骤失败（退出码 "+process.ExitCode+"）。原数据保留；请检查网络、WSL 和磁盘空间后重试。");
                }
                return value;
            }
        }
        static string LinuxPath(string distribution,string path) {return Run(30000,"-d",distribution,"--exec","wslpath","-a",path).Trim();}
        static Dictionary<string,object> Helper(string distribution,string user,string action,string jobPath=null) {
            string package=LinuxPath(distribution,App.Package);
            var args=new List<string>{"-d",distribution};if(!String.IsNullOrEmpty(user)){args.Add("-u");args.Add(user);}args.AddRange(new[]{"--exec","bash",package+"/Migrate-Worker.sh",action});
            if(jobPath!=null){args.Add("--job");args.Add(LinuxPath(distribution,jobPath));}
            var result=App.Json.Deserialize<Dictionary<string,object>>(Run(action=="prepare"?90000000:43200000,args.ToArray()));
            if(!App.Flag(result,"ready"))throw new IOException(App.Text(result,"detail","迁移步骤未通过验证。"));return result;
        }
        static void CheckCancelled(Dictionary<string,object> job) {if(cancelRequested||File.Exists(Path.Combine(App.Text(job,"path"),"cancel")))throw new OperationCanceledException("已取消迁移。");}
        internal static void Cancel() {cancelRequested=true;var j=App.Read(ActivePath);if(j.Count>0&&!App.Flag(j,"committed")&&!App.Flag(j,"finished"))File.WriteAllText(Path.Combine(App.Text(j,"path"),"cancel"),"");}
        static void BackupWindows(string folder) {
            var snapshots=new Dictionary<string,object>();
            foreach(string name in new[]{"boot-config.json","host-control.json","settings.json","boot-request.json"}) {
                string file=Path.Combine(App.SettingsRoot,name);snapshots[name]=File.Exists(file)?Convert.ToBase64String(File.ReadAllBytes(file)):null;
            }
            App.Write(Path.Combine(folder,"windows-before.json"),snapshots);
        }
        static void RestoreWindows(string folder) {
            foreach(var pair in App.Read(Path.Combine(folder,"windows-before.json"))) {
                if(Array.IndexOf(new[]{"boot-config.json","host-control.json","settings.json","boot-request.json"},pair.Key)<0)throw new IOException("Windows 备份文件名无效。");
                string file=Path.Combine(App.SettingsRoot,pair.Key);
                if(pair.Value==null){if(File.Exists(file))File.Delete(file);}else App.Write(file,App.Json.DeserializeObject(Encoding.UTF8.GetString(Convert.FromBase64String((string)pair.Value))));
            }
        }
        internal static bool OwnsImportedPath(string actual,string expected) {
            if(actual.StartsWith(@"\\?\"))actual=actual.Substring(4);
            return String.Equals(Path.GetFullPath(actual).TrimEnd('\\'),Path.GetFullPath(expected).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase);
        }
        static void RemoveIncompleteClone(Dictionary<string,object> job) {
            if(App.Text(job,"mode")=="in_place")return;
            string target=App.Text(job,"target"),expected=Path.Combine(App.Text(job,"path"),"distribution");
            using(var root=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Lxss")) {
                if(root==null)return;
                foreach(var name in root.GetSubKeyNames())using(var key=root.OpenSubKey(name)) {
                    if(!String.Equals(Convert.ToString(key.GetValue("DistributionName")),target,StringComparison.OrdinalIgnoreCase))continue;
                    if(!OwnsImportedPath(Convert.ToString(key.GetValue("BasePath")),expected))throw new IOException("目标 WSL 路径与本次新建目录不一致，已拒绝删除。");
                    Run(60000,"--terminate",target);
                    // Only our random, uncommitted clone can be unregistered.
                    // Source, Desktop, and successful target distributions never enter here.
                    Run(120000,"--unregister",target);return;
                }
            }
        }
        static FileStream OwnOperation() {Directory.CreateDirectory(App.SettingsRoot);return new FileStream(Path.Combine(App.SettingsRoot,"migration-owner.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);}
        internal static Dictionary<string,string> BackupArchives(Dictionary<string,object> job) {
            CheckOwner(job);var result=new Dictionary<string,string>();
            if(!App.Flag(job,"committed")||!App.Flag(job,"finished")||App.Text(job,"mode")=="in_place")return result;
            string path=WorkerDisk.DirectPath(App.Text(job,"path"));
            var checksums=App.Read(Path.Combine(path,"checksums.json"));
            var allowed=new HashSet<string>(new[]{"images.tar","distribution.tar"});
            object registries;var manifest=App.Read(Path.Combine(path,"manifest.json"));
            if(manifest.TryGetValue("registries",out registries)&&registries is IList)foreach(Dictionary<string,object> item in (IList)registries) {
                string volume=App.Text(item,"volume");if(System.Text.RegularExpressions.Regex.IsMatch(volume,"\\Aexpman-[a-z0-9-]+\\z"))allowed.Add(volume+".tar");
            }
            checksums["distribution.tar"]=App.Text(App.Read(Path.Combine(path,"distribution-sha256.json")),"sha256");
            foreach(var pair in checksums) {
                string hash=Convert.ToString(pair.Value);
                if(!allowed.Contains(pair.Key)||!System.Text.RegularExpressions.Regex.IsMatch(hash??"","\\A[a-f0-9]{64}\\z"))continue;
                string file=WorkerDisk.DirectPath(Path.Combine(path,pair.Key));if(File.Exists(file))result[file]=hash;
            }
            return result;
        }
        internal static long CleanBackups() {
            using(OwnOperation()) {
                var job=App.Read(ActivePath);var files=BackupArchives(job);if(files.Count==0)return 0;
                var config=WorkerBoot.Load();WorkerBoot.Validate(config);
                var verified=App.Read(Path.Combine(App.Text(job,"path"),"verified.json"));
                if(!config.Enabled||config.Distribution!=App.Text(job,"target")||config.EngineId!=App.Text(verified,"engine_id")||config.NodeId!=App.Text(verified,"node_id")||!verified.ContainsKey("cuda"))throw new IOException("请先完成迁移验收，备份已保留。");
                var drive=new DriveInfo(Path.GetPathRoot(App.Text(job,"path")));long before=drive.AvailableFreeSpace;
                foreach(var pair in files) {
                    WorkerDisk.DeleteVerified(pair.Key,pair.Value);
                }
                long freed=Math.Max(0,drive.AvailableFreeSpace-before);job["backup_cleanup_bytes"]=freed;App.Write(ActivePath,job);return freed;
            }
        }
        internal static async Task Recover(IProgress<string> progress) {using(OwnOperation()){await RecoverCore(progress);}}
        internal static async Task Finish(IProgress<string> progress) {
            using(OwnOperation()) {
                var job=App.Read(ActivePath);CheckOwner(job);
                if(CanRollback(job))throw new IOException("尚未交接，请先恢复原节点。");
                var config=WorkerBoot.Load();WorkerBoot.Validate(config);
                var verified=App.Read(Path.Combine(App.Text(job,"path"),"verified.json"));
                if(!config.Enabled||config.Distribution!=App.Text(job,"target")||config.EngineId!=App.Text(verified,"engine_id")||config.NodeId!=App.Text(verified,"node_id"))throw new IOException("开机任务与已验证的环境不一致，请检查配置。");
                await WorkerBoot.SuspendForUpdate();
                await Task.Run(()=>Run(120000,"-d",config.Distribution,"-u","root","--exec","systemctl","start",WorkerBoot.ServiceName(config)));
                await Task.Run(()=>Helper(config.Distribution,config.LinuxUser,"activate",App.Text(job,"path")));
                WorkerBoot.Request("available");job["committed"]=true;job["finished"]=true;Save(job,"交接完成，正在启动后台。",progress);
                WorkerBoot.ResumeAfterUpdate();
            }
        }
        static async Task RecoverCore(IProgress<string> progress) {
            var job=App.Read(ActivePath);CheckOwner(job);
            if(!CanRollback(job))throw new IOException("迁移已交接，目标可能已接收新实验。不能回退到旧数据；请使用“启动迁移后的后台”。");
            Cancel();
            int pid;long ticks;
            if(Int32.TryParse(App.Text(job,"child_pid"),out pid)&&Int64.TryParse(App.Text(job,"child_start"),out ticks)) {
                try {using(var child=Process.GetProcessById(pid))if(!child.HasExited&&child.StartTime.ToUniversalTime().Ticks==ticks)throw new IOException("上次迁移的 WSL 操作仍在运行。已请求取消，请等待该步骤结束后重试恢复。");}
                catch(ArgumentException){}
            }
            await WorkerBoot.SuspendForUpdate();
            Save(job,"正在恢复原节点，保留备份…",progress);
            await Task.Run(()=>Helper(App.Text(job,"source"),App.Text(job,"user"),"quiescent",App.Text(job,"path")));
            if(App.Text(job,"mode")=="in_place"&&File.Exists(Path.Combine(App.Text(job,"path"),"engine-before.json"))) {
                string source=App.Text(job,"source"),path=App.Text(job,"path");
                await Task.Run(()=>Run(600000,"-d",source,"-u","root","--exec","bash",LinuxPath(source,App.Package)+"/Prepare-Isolated-Engine.sh",App.Text(job,"id"),LinuxPath(source,path),"rollback"));
            }
            if(App.Flag(job,"registering")) {
                try {WorkerBoot.DisableAutostart();}catch(System.Runtime.InteropServices.COMException){}
            }
            await Task.Run(()=>RemoveIncompleteClone(job));
            RestoreWindows(App.Text(job,"path"));
            var restored=await Task.Run(()=>Helper(App.Text(job,"source"),App.Text(job,"user"),"rollback",App.Text(job,"path")));
            job["finished"]=true;Save(job,"已恢复原节点的配置和接单策略；备份保留。",progress);
            WorkerBoot.ResumeAfterUpdate();
            if(App.Flag(restored,"was_running")) {
                var host=new DesktopHostControl(Path.Combine(App.SettingsRoot,"host-control.json"));
                if(host.Linked) {
                    try {
                        var desired=await Task.Run(()=>host.Sync());
                        if(App.Text(desired,"mode")!="release") {
                            var m=App.Read(Path.Combine(App.Text(job,"path"),"manifest.json"));
                            var original=new WorkerBootConfig {Distribution=App.Text(job,"source"),LinuxUser=App.Text(job,"user"),ServiceRoot=App.Text(m,"service_root")};
                            await Task.Run(()=>WorkerBoot.Command(original,"start"));
                        }
                    }catch(System.Net.WebException){progress.Report("原配置已恢复；主控暂不可达，恢复连接后可重新启用算力。");}
                }
            }
        }
        internal static async Task Migrate(string source,string folder,string password,IProgress<string> progress) {using(OwnOperation()){await MigrateCore(source,folder,password,progress);}}
        static async Task MigrateCore(string source,string folder,string password,IProgress<string> progress) {
            cancelRequested=false;
            if(Pending)throw new IOException("上次迁移尚未结束，请先恢复原节点或启动迁移后的后台。");
            if(WorkerBoot.Managed)throw new IOException("此节点已配置无人值守模式，无需重复迁移。");
            if(String.IsNullOrWhiteSpace(source)||source.StartsWith("-")||source.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase))throw new IOException("请选择原 Worker 的 Ubuntu。");
            if(String.IsNullOrEmpty(password))throw new IOException("请输入 Windows 账户密码（不是 PIN）。");
            if(!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))throw new IOException("请先使用同一账户授权管理员权限。");
            string installed=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","ExperimentManager","Worker",App.Version);
            if(!String.Equals(Path.GetFullPath(installed),Path.GetFullPath(App.Package),StringComparison.OrdinalIgnoreCase))throw new IOException("请先安装当前版本，再打开一键迁移。");
            WorkerBoot.CheckCredentials(password);
            progress.Report("正在检测原节点、镜像、数据位置与磁盘空间…");
            var manifest=await Task.Run(()=>Helper(source,null,"inspect"));
            if(cancelRequested)throw new OperationCanceledException("已取消检测，原环境保持原状。");
            CheckSpace(folder,64*1024*1024);CheckSpace(DistributionPath(source),Convert.ToInt64(manifest["required_bytes"]));
            foreach(object drive in (IEnumerable)manifest["asset_drives"]) {
                var disk=new DriveInfo(Convert.ToString(drive)+@":\");
                if(!disk.IsReady||disk.DriveType!=DriveType.Fixed)throw new IOException("数据集使用的磁盘不支持未登录运行："+disk.Name);
            }
            string id=Guid.NewGuid().ToString("N"),path=Path.Combine(Path.GetFullPath(folder),id),target=source;
            PrivateDirectory(path);manifest["job_id"]=id;App.Write(Path.Combine(path,"manifest.json"),manifest);BackupWindows(path);
            var job=new Dictionary<string,object>{{"id",id},{"path",path},{"source",source},{"target",target},{"mode","in_place"},{"user",App.Text(manifest,"linux_user")},{"owner",WindowsIdentity.GetCurrent().User.Value},{"committed",false},{"finished",false}};
            Save(job,"迁移已开始，备份目录："+path,progress);
            Exception failure=null;
            try {
                CheckCancelled(job);
                await WorkerBoot.SuspendForUpdate();
                Save(job,"1/8 等待当前实验结束…",progress);
                await Task.Run(()=>Helper(source,App.Text(job,"user"),"prepare",path));CheckCancelled(job);
                manifest=App.Read(Path.Combine(path,"manifest.json"));CheckSpace(folder,64*1024*1024);CheckSpace(DistributionPath(source),Convert.ToInt64(manifest["required_bytes"]));
                Save(job,"2/8 保存配置…",progress);
                await Task.Run(()=>Helper(source,App.Text(job,"user"),"pre-export",path));CheckCancelled(job);
                Save(job,"3/8 准备独立引擎…",progress);
                await Task.Run(()=>Run(7200000,"-d",target,"-u","root","--exec","bash",LinuxPath(target,App.Package)+"/Prepare-Isolated-Engine.sh",id,LinuxPath(target,path)));
                CheckCancelled(job);
                Save(job,"4/8 启动独立引擎…",progress);
                await Task.Run(()=>Run(60000,"--terminate",target));
                await Task.Run(()=>Run(120000,"-d",target,"-u","root","--exec","systemctl","start","exlab-docker.service"));CheckCancelled(job);
                Save(job,"5/8 转移并校验镜像…",progress);
                await Task.Run(()=>Helper(target,App.Text(job,"user"),"transfer",path));CheckCancelled(job);
                Save(job,"6/8 验证显卡和数据…",progress);
                var verified=await Task.Run(()=>Helper(target,App.Text(job,"user"),"verify",path));CheckCancelled(job);
                var config=new WorkerBootConfig {Distribution=target,LinuxUser=App.Text(job,"user"),ServiceRoot=App.Text(manifest,"service_root"),NodeId=App.Text(manifest,"node_id"),EngineId=App.Text(verified,"engine_id"),EngineService="exlab-docker.service",OwnerSid=WindowsIdentity.GetCurrent().User.Value,DedicatedDistribution=true};
                var info=await Task.Run(()=>WorkerBoot.Command(config,"host-info"));
                if(App.Text(info,"node_id")!=config.NodeId)throw new IOException("目标节点身份不匹配。");
                Save(job,"7/8 开启自启动…",progress);
                job["registering"]=true;App.Write(ActivePath,job);
                WorkerBoot.Register(config,password);password=null;
                new DesktopHostControl(Path.Combine(App.SettingsRoot,"host-control.json")).Connect(info,target);
                var settings=App.ReadSettings();settings["distribution"]=target;App.SaveSettings(settings);
                job["committed"]=true;App.Write(ActivePath,job);
                await Task.Run(()=>Helper(target,config.LinuxUser,"activate",path));
                WorkerBoot.Request("available");
                // This is the no-return boundary: after it the target may accept
                // new work. Crash recovery must never resurrect the old database.
                job["committed"]=true;job["finished"]=true;Save(job,"迁移及 CUDA 验证完成，开机任务已登记。",progress);
                Save(job,"8/8 回收旧引擎副本…",progress);
                string cleanup="";
                try {var result=await Task.Run(()=>Helper(target,config.LinuxUser,"cleanup-source",path));object skipped;if(result.TryGetValue("skipped",out skipped)&&skipped is ICollection&&((ICollection)skipped).Count>0)cleanup="部分共享资源保留，可在详情中查看。";}
                catch(Exception ex){cleanup="旧引擎清理待重试："+ex.Message;}
                job["cleanup_detail"]=cleanup;Save(job,"迁移完成。"+cleanup,progress);
                WorkerBoot.ResumeAfterUpdate();
                progress.Report("已完成。实验数据原位保留。"+cleanup+"\n请重启并保持未登录，从主控验证一次 GPU 任务。\n配置备份："+path);
            } catch(Exception original) {failure=original;} finally {password=null;}
            if(failure!=null) {
                if(App.Flag(job,"committed"))throw new IOException("迁移已完成交接；后台启动需重试，不能恢复旧数据库。"+failure.Message,failure);
                try {await RecoverCore(progress);}catch(Exception recovery){throw new IOException(failure.Message+"\n自动恢复尚未完成："+recovery.Message+"\n请打开本向导点击“恢复原节点”。备份目录："+path,failure);}
                throw new IOException(failure.Message+"\n已恢复原节点配置，备份保留。",failure);
            }
        }
    }
}
