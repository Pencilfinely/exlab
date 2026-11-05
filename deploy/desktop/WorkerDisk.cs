using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Threading.Tasks;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace ExperimentManagerDesktop {
    // Only a registered, stopped, dedicated Worker disk is eligible. Never
    // shutdown all WSL distributions or infer Docker Desktop's disk location.
    internal static class WorkerDisk {
        [StructLayout(LayoutKind.Sequential)] struct StorageType {public uint DeviceId;public Guid VendorId;}
        [DllImport("virtdisk.dll",CharSet=CharSet.Unicode)] static extern uint OpenVirtualDisk(ref StorageType type,string path,uint access,uint flags,IntPtr parameters,out SafeFileHandle handle);
        [DllImport("virtdisk.dll")] static extern uint CompactVirtualDisk(SafeFileHandle handle,uint flags,IntPtr parameters,IntPtr overlapped);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string name,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
        [StructLayout(LayoutKind.Sequential)] struct Disposition {[MarshalAs(UnmanagedType.U1)]public bool Delete;}
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetFileInformationByHandle(SafeFileHandle handle,int type,ref Disposition info,uint size);
        internal static void DeleteVerified(string path,string expected) {
            path=DirectPath(path);
            using(var handle=CreateFile(path,0x80010000,0,IntPtr.Zero,3,0x08200000,IntPtr.Zero)) {
                if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());
                using(var stream=new FileStream(handle,FileAccess.Read))using(var sha=SHA256.Create()) {
                    string hash=BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
                    if(hash!=expected)throw new IOException("迁移备份校验失败，已保留："+Path.GetFileName(path));
                    // Delete exactly the handle we hashed, with rename/write excluded.
                    var info=new Disposition {Delete=true};if(!SetFileInformationByHandle(handle,4,ref info,1))throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            }
        }
        internal static string DirectPath(string path) {
            string full=Path.GetFullPath(path);
            if(full.StartsWith(@"\\"))throw new IOException("磁盘必须位于本机。");
            for(string current=full;!String.IsNullOrEmpty(current);current=Path.GetDirectoryName(current))
                if((File.Exists(current)||Directory.Exists(current))&&(File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)throw new IOException("磁盘路径被重定向，已保留。");
            return full;
        }
        internal static string RegisteredDisk(string distribution) {
            using(var root=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Lxss"))if(root!=null)foreach(string name in root.GetSubKeyNames())using(var key=root.OpenSubKey(name)) {
                if(!String.Equals(Convert.ToString(key.GetValue("DistributionName")),distribution,StringComparison.OrdinalIgnoreCase))continue;
                string folder=WorkerMigration.DistributionPath(distribution),file=Convert.ToString(key.GetValue("VhdFileName","ext4.vhdx"));
                if(String.IsNullOrWhiteSpace(file)||Path.GetFileName(file)!=file||Path.GetExtension(file)!=".vhdx")throw new IOException("无法确认 Worker 虚拟磁盘。");
                string path=DirectPath(Path.Combine(folder,file));if(!File.Exists(path))throw new IOException("Worker 虚拟磁盘不存在。");return path;
            }
            throw new IOException("找不到已登记的 Worker 虚拟磁盘。");
        }
        static void RequireStopped(string distribution) {
            string running=App.RunWithTimeout(20000,"wsl.exe","--list","--running","--quiet").Replace("\0","");
            foreach(string name in running.Split(new[]{'\r','\n'},StringSplitOptions.RemoveEmptyEntries))if(String.Equals(name.Trim(),distribution,StringComparison.OrdinalIgnoreCase))throw new IOException("请先在 Worker 主界面“停用并释放”，再回收磁盘空间。");
        }
        internal static void CompactFile(string path) {
            path=DirectPath(path);
            var type=new StorageType {DeviceId=3,VendorId=new Guid("EC984AEC-A0F9-47e9-901F-71415A66345B")};
            SafeFileHandle handle;uint error=OpenVirtualDisk(ref type,path,0x00200000,0,IntPtr.Zero,out handle);
            using(handle) {if(error!=0)throw new Win32Exception((int)error,"无法打开已停止的虚拟磁盘："+new Win32Exception((int)error).Message);
                error=CompactVirtualDisk(handle,0,IntPtr.Zero,IntPtr.Zero);if(error!=0)throw new Win32Exception((int)error,"磁盘回收未完成："+new Win32Exception((int)error).Message);}
        }
        internal static async Task RunElevated(string owner,string id) {
            if(owner!=WindowsIdentity.GetCurrent().User.Value||!System.Text.RegularExpressions.Regex.IsMatch(id??"","\\A[a-f0-9]{32}\\z"))throw new IOException("磁盘维护请求归属不符。");
            string resultPath=Path.Combine(App.SettingsRoot,"compact-"+id+".json");
            FileStream ownerLock=null;
            try {
                ownerLock=new FileStream(Path.Combine(App.SettingsRoot,"compact-owner.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);
                if(!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))throw new IOException("回收虚拟磁盘需要管理员授权。");
                var config=WorkerBoot.Load();WorkerBoot.Validate(config);
                if(!config.Enabled||!config.DedicatedDistribution||WorkerMigration.Pending)throw new IOException("请先完成独立 Worker 环境配置。");
                if(App.Text(App.Read(Path.Combine(App.SettingsRoot,"boot-request.json")),"mode")!="release")throw new IOException("请先停用并释放算力。");
                string disk=RegisteredDisk(config.Distribution);RequireStopped(config.Distribution);
                // Keep the host from reopening WSL during offline maintenance.
                if(File.Exists(WorkerBoot.SuspendPath))throw new IOException("更新或迁移正在进行，请稍后重试。");
                try {
                    await WorkerBoot.SuspendForUpdate();
                    RequireStopped(config.Distribution);
                    var drive=new DriveInfo(Path.GetPathRoot(disk));long before=drive.AvailableFreeSpace;
                    await Task.Run(()=>{try {App.RunWithTimeout(600000,"wsl.exe","-d",config.Distribution,"-u","root","--exec","fstrim","-v","/");}
                        finally {App.RunWithTimeout(60000,"wsl.exe","--terminate",config.Distribution);}});
                    RequireStopped(config.Distribution);
                    if(RegisteredDisk(config.Distribution)!=disk)throw new IOException("磁盘登记发生变化，已停止回收。");
                    await Task.Run(()=>CompactFile(disk));
                    App.Write(resultPath,new{id=id,status="completed",windows_reclaimed_bytes=Math.Max(0,drive.AvailableFreeSpace-before)});
                } finally {WorkerBoot.ResumeAfterUpdate();}
            } catch(Exception ex) {App.Write(resultPath,new{id=id,status="failed",detail=ex.Message});}
            finally {if(ownerLock!=null)ownerLock.Dispose();}
        }
        internal static async Task<Dictionary<string,object>> Request() {
            string id=Guid.NewGuid().ToString("N"),resultPath=Path.Combine(App.SettingsRoot,"compact-"+id+".json");
            using(var process=Process.Start(new ProcessStartInfo(Path.Combine(App.Package,App.Executable),App.Arguments("--compact-worker","--owner-sid",WindowsIdentity.GetCurrent().User.Value,"--compact-id",id)){UseShellExecute=true,Verb="runas",WindowStyle=ProcessWindowStyle.Hidden}))await Task.Run(()=>process.WaitForExit());
            var result=App.Read(resultPath);if(App.Text(result,"id")!=id)throw new IOException("磁盘维护未返回结果。");File.Delete(resultPath);
            if(App.Text(result,"status")!="completed")throw new IOException(App.Text(result,"detail","磁盘回收未完成。"));return result;
        }
    }
}
