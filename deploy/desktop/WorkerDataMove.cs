using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;

namespace ExperimentManagerDesktop {
    // WSL owns the move and registration. Never export, unregister, edit BasePath,
    // or delete either disk after an ambiguous failure.
    internal sealed class WorkerDataPlan {
        internal string Distribution,SourceDisk,TargetFolder,TargetDisk,DistributionId;
        internal long DiskBytes,EnvironmentBytes,RequiredBytes,AvailableBytes;
        internal bool Moving;
        internal string Description {
            get {return "数据位置："+TargetFolder+"\n"+Path.GetPathRoot(TargetFolder)+" 可用 "+WorkerStorageForm.Bytes(AvailableBytes)+"，需要约 "+WorkerStorageForm.Bytes(RequiredBytes)+
                (Moving?"（现有虚拟磁盘 "+WorkerStorageForm.Bytes(DiskBytes)+" + 新环境 "+WorkerStorageForm.Bytes(EnvironmentBytes)+"）":"")+
                (AvailableBytes<RequiredBytes?"\n还差约 "+WorkerStorageForm.Bytes(RequiredBytes-AvailableBytes)+"，请先清理或换盘。":"\n空间足够。")+(Moving?"移动后保留原 Ubuntu 名称和节点身份。":"");}
        }
        internal void RequireSpace() {if(AvailableBytes<RequiredBytes)throw new IOException(Description);}
    }

    internal static class WorkerDataMove {
        internal static bool Same(string a,string b){return String.Equals(Path.GetFullPath(a).TrimEnd('\\'),Path.GetFullPath(b).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase);}
        static bool Inside(string path,string parent){return Same(path,parent)||Path.GetFullPath(path).StartsWith(Path.GetFullPath(parent).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase);}
        internal static long SpaceRequired(string source,string target,long diskBytes,long environmentBytes) {
            if(diskBytes<0||environmentBytes<0)throw new IOException("磁盘空间信息无效。");
            return checked(environmentBytes+64L*1024*1024+(Same(Path.GetPathRoot(source),Path.GetPathRoot(target))?0:diskBytes));
        }
        internal static string Suggested(string distribution) {
            string current=null;
            try {current=WorkerMigration.DistributionPath(distribution);}catch(IOException){}
            string system=Path.GetPathRoot(Environment.GetFolderPath(Environment.SpecialFolder.Windows));
            if(current!=null&&!Same(Path.GetPathRoot(current),system))return current;
            DriveInfo best=null;
            foreach(var drive in DriveInfo.GetDrives())try {
                if(drive.IsReady&&drive.DriveType==DriveType.Fixed&&drive.DriveFormat=="NTFS"&&!Same(drive.Name,system)&&(best==null||drive.AvailableFreeSpace>best.AvailableFreeSpace))best=drive;
            }catch(IOException){}
            if(best==null&&current!=null)return current;
            string name=Regex.Replace(distribution??"Ubuntu","[^A-Za-z0-9._-]","_");
            return Path.Combine(best==null?WorkerMigration.SuggestedFolder():Path.Combine(best.Name,"ExLabWorker"),name+"-Data");
        }
        internal static void ValidateTarget(string sourceDisk,string targetFolder,string backupFolder) {
            sourceDisk=WorkerDisk.DirectPath(sourceDisk);targetFolder=WorkerDisk.DirectPath(targetFolder);
            if(Same(Path.GetDirectoryName(sourceDisk),targetFolder))return;
            if(Same(targetFolder,Path.GetPathRoot(targetFolder))||Inside(targetFolder,Path.GetDirectoryName(sourceDisk))||Inside(sourceDisk,targetFolder))throw new IOException("请选择独立的新数据目录，不能是磁盘根目录或原 Ubuntu 的上下级目录。");
            if(!String.IsNullOrEmpty(backupFolder)&&(Inside(backupFolder,targetFolder)||Inside(targetFolder,backupFolder)))throw new IOException("数据目录和配置备份目录需要分开。");
            if(File.Exists(targetFolder)||Directory.Exists(targetFolder)&&Directory.GetFileSystemEntries(targetFolder).Length!=0)throw new IOException("目标数据目录已有文件，请选择空目录或新目录。");
        }
        internal static WorkerDataPlan Plan(string distribution,string targetFolder,string backupFolder,long environmentBytes) {
            string disk=WorkerDisk.RegisteredDisk(distribution);
            targetFolder=String.IsNullOrWhiteSpace(targetFolder)?Path.GetDirectoryName(disk):Path.GetFullPath(targetFolder);
            ValidateTarget(disk,targetFolder,backupFolder);
            WorkerMigration.CheckSpace(targetFolder,0);
            bool moving=!Same(Path.GetDirectoryName(disk),targetFolder);
            if(moving)RequireSupport(WorkerMigration.Management("--version"),WorkerMigration.Management("--help"));
            long bytes=new FileInfo(disk).Length;
            return new WorkerDataPlan {Distribution=distribution,SourceDisk=disk,TargetFolder=targetFolder,
                TargetDisk=Path.Combine(targetFolder,Path.GetFileName(disk)),DistributionId=WorkerMigration.DistributionId(distribution),
                DiskBytes=bytes,EnvironmentBytes=environmentBytes,Moving=moving,
                RequiredBytes=SpaceRequired(disk,targetFolder,bytes,environmentBytes),AvailableBytes=new DriveInfo(Path.GetPathRoot(targetFolder)).AvailableFreeSpace};
        }
        internal static void RequireSupport(string versionText,string help) {
            var match=Regex.Match((versionText??"").Replace("\0",""),@"\b\d+\.\d+\.\d+(?:\.\d+)?\b");Version version;
            if(!match.Success||!Version.TryParse(match.Value,out version)||version<new Version(2,7,14)||!(help??"").Replace("\0","").Contains("--move"))
                throw new IOException("改盘需要 WSL 2.7.14 或更新版本。请先运行 wsl --update，再重试；原数据尚未移动。");
        }
        internal static void RequireWindow(string source,string running,bool beforeStop) {
            var blocked=new List<string>();
            foreach(string raw in (running??"").Replace("\0","").Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries)) {
                string name=raw.Trim();
                if(beforeStop&&(String.Equals(name,source,StringComparison.OrdinalIgnoreCase)||String.Equals(name,"docker-desktop",StringComparison.OrdinalIgnoreCase)))continue;
                blocked.Add(name);
            }
            if(blocked.Count>0)throw new IOException("改盘需要释放 WSL 磁盘。请先结束这些环境中的工作并关闭它们："+String.Join("、",blocked)+"。原环境保留。");
        }
        internal static void PauseDesktop(string source,string engine,Dictionary<string,object> job,Action save,Func<string[],string> wsl,Func<string[],string> docker) {
            RequireWindow(source,wsl(new[]{"--list","--running","--quiet"}),true);
            if(docker(new[]{"--context","desktop-linux","info","--format","{{.ID}}"}).Trim()!=engine)throw new IOException("Windows 与 Ubuntu 的 Docker 引擎不一致，已停止改盘。");
            if(!String.IsNullOrWhiteSpace(docker(new[]{"--context","desktop-linux","ps","-q"})))throw new IOException("改盘需要短暂重启 Docker；仍有容器运行，请结束后重试。");
            // Persist before stopping Desktop so recovery can restart it even
            // after a crash or a partially successful CLI stop.
            job["desktop_paused"]=true;save();
            docker(new[]{"desktop","stop","--timeout","120"});
            wsl(new[]{"--terminate",source});
            RequireWindow(source,wsl(new[]{"--list","--running","--quiet"}),false);
            // WSL 2.7 keeps stopped distro disks attached to its utility VM.
            // Only an empty VM may be shut down; never stop another distro.
            wsl(new[]{"--shutdown"});
        }
        internal static void ResumeDesktop(string engine,Dictionary<string,object> job,Action save,Func<string[],string> docker) {
            if(!App.Flag(job,"desktop_paused"))return;
            docker(new[]{"desktop","start","--timeout","120"});
            if(docker(new[]{"--context","desktop-linux","info","--format","{{.ID}}"}).Trim()!=engine)throw new IOException("Docker 重启后的引擎身份不符，原数据保留。");
            job["desktop_paused"]=false;save();
        }
        internal static bool Matches(string path,Dictionary<string,object> job,Func<string,string> fingerprint) {
            return File.Exists(path)&&(App.Flag(job,"data_moved")||new FileInfo(WorkerDisk.DirectPath(path)).Length==Convert.ToInt64(job["move_bytes"]))&&fingerprint(path)==App.Text(job,"move_fingerprint");
        }
        internal static bool VerifyLocation(Dictionary<string,object> job,Func<string,string> disk,Func<string,string> identity,Func<string,string> fingerprint=null) {
            fingerprint=fingerprint??WorkerDisk.Identifier;
            if(!App.Flag(job,"move_started"))return false;
            string distro=App.Text(job,"source"),current=disk(distro),before=App.Text(job,"move_from"),after=App.Text(job,"move_to");
            if(identity(distro)!=App.Text(job,"move_distribution_id"))throw new IOException("Ubuntu 登记身份发生变化，磁盘文件已保留。");
            if(Same(current,after)&&Matches(after,job,fingerprint))return true;
            if(Same(current,before)&&Matches(before,job,fingerprint))return false;
            throw new IOException("无法确认 WSL 改盘结果，已停止后续操作并保留文件。请检查磁盘登记；不要删除或注销 Ubuntu。\n原位置："+before+"\n目标位置："+after);
        }
        internal static void Move(WorkerDataPlan plan,Dictionary<string,object> job,Action save,Func<string[],string> run,Func<string,string> disk,Func<string,string> identity,Func<string,string> fingerprint=null) {
            fingerprint=fingerprint??WorkerDisk.Identifier;
            if(!plan.Moving)return;
            ValidateTarget(plan.SourceDisk,plan.TargetFolder,App.Text(job,"path"));plan.RequireSpace();
            if(!Same(disk(plan.Distribution),plan.SourceDisk)||identity(plan.Distribution)!=plan.DistributionId)throw new IOException("Ubuntu 磁盘登记已变化，请重新检测。");
            run(new[]{"--terminate",plan.Distribution});
            foreach(string name in run(new[]{"--list","--running","--quiet"}).Replace("\0","").Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries))
                if(String.Equals(name.Trim(),plan.Distribution,StringComparison.OrdinalIgnoreCase))throw new IOException("Ubuntu 尚未退出，请稍后重试。");
            job["move_from"]=plan.SourceDisk;job["move_to"]=plan.TargetDisk;job["move_distribution_id"]=plan.DistributionId;
            plan.DiskBytes=new FileInfo(plan.SourceDisk).Length;plan.RequiredBytes=SpaceRequired(plan.SourceDisk,plan.TargetFolder,plan.DiskBytes,plan.EnvironmentBytes);
            plan.AvailableBytes=new DriveInfo(Path.GetPathRoot(plan.TargetFolder)).AvailableFreeSpace;plan.RequireSpace();
            job["move_bytes"]=plan.DiskBytes;job["move_fingerprint"]=fingerprint(plan.SourceDisk);job["move_started"]=true;save();
            Exception failure=null;
            try {run(new[]{"--manage",plan.Distribution,"--move",plan.TargetFolder});}catch(Exception error){failure=error;}
            bool moved=VerifyLocation(job,disk,identity,fingerprint);job["data_moved"]=moved;save();
            if(!moved)throw new IOException("WSL 未完成改盘，原环境保留。"+(failure==null?"":failure.Message),failure);
        }
    }
}
