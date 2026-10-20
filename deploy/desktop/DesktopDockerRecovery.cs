using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace ExperimentManagerDesktop {
    internal static class DesktopDockerRecovery {
        // Query the tag without opening the socket: orphan AF_UNIX entries can
        // reject File.Open, File.Delete and even a rename of the socket itself.
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        struct FindData {
            internal uint Attributes;
            internal System.Runtime.InteropServices.ComTypes.FILETIME Created,Accessed,Written;
            internal uint SizeHigh,SizeLow,Tag,Reserved;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=260)] internal string Name;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=14)] internal string ShortName;
        }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern IntPtr FindFirstFile(string path,out FindData data);
        [DllImport("kernel32.dll",SetLastError=true)]
        static extern bool FindClose(IntPtr handle);
        internal static bool IsUnixSocket(string path) {
            FindData data;IntPtr handle=FindFirstFile(path,out data);
            if(handle==new IntPtr(-1)) {
                int error=Marshal.GetLastWin32Error();
                if(error==2||error==3)return false;
                throw new IOException("无法检查 Docker 套接字："+path,new Win32Exception(error));
            }
            try {return (data.Attributes&0x410)==0x400&&data.Tag==0x80000023;}
            finally {FindClose(handle);}
        }
        internal static bool DesktopActive() {
            foreach(string name in new[]{"Docker Desktop","com.docker.backend","com.docker.proxy","com.docker.build"}) {
                var processes=Process.GetProcessesByName(name);
                try {if(processes.Length>0)return true;}
                finally {foreach(var process in processes)process.Dispose();}
            }
            return EngineActive();
        }
        internal static bool EngineActive() {
            foreach(string pipe in Directory.GetFiles(@"\\.\pipe\")) {
                string name=Path.GetFileName(pipe);
                if(name.StartsWith("docker",StringComparison.OrdinalIgnoreCase)&&name.IndexOf("engine",StringComparison.OrdinalIgnoreCase)>=0)return true;
            }
            return false;
        }
        static void NormalAncestors(string path) {
            for(var current=new DirectoryInfo(path);current!=null;current=current.Parent) {
                if(!current.Exists)continue;
                if((current.Attributes&FileAttributes.ReparsePoint)!=0)
                    throw new IOException("Docker 运行目录含重定向，未自动修改："+current.FullName);
            }
        }
        internal static List<string> Prepare(string localRoot,Func<bool> idle=null,Func<string,bool> socket=null) {
            if(idle==null)idle=()=>!DesktopActive();
            if(socket==null)socket=IsUnixSocket;
            var preserved=new List<string>();
            if(!idle())return preserved;
            string root=Path.GetFullPath(localRoot).TrimEnd(Path.DirectorySeparatorChar);
            string[][] candidates={new[]{"docker-secrets-engine","engine.sock","engine.sock.stale"},
                new[]{Path.Combine("Docker","run"),"sailor-ingest.sock","sailor-ingest.sock.stale","dockerInference","dockerInference.stale"}};
            foreach(var candidate in candidates) {
                string source=Path.GetFullPath(Path.Combine(root,candidate[0]));
                if(!source.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new IOException("Docker 运行目录超出范围。");
                NormalAncestors(source);
                if(!Directory.Exists(source))continue;
                bool found=false;
                for(int i=1;i<candidate.Length;i++)found=socket(Path.Combine(source,candidate[i]))||found;
                if(!found)continue;
                string destination=source+".exlab-stale-"+DateTime.UtcNow.ToString("yyyyMMddTHHmmss")+"-"+Guid.NewGuid().ToString("N");
                if(Path.GetDirectoryName(destination)!=Path.GetDirectoryName(source)||Directory.Exists(destination)||File.Exists(destination))
                    throw new IOException("Docker 套接字保留目录无效。");
                // Recheck immediately before each move; never stop Docker or
                // relocate runtime files while another start is in progress.
                if(!idle())throw new IOException("Docker 正在启动，未继续处理残留套接字；请稍后重试。");
                NormalAncestors(source);
                Directory.Move(source,destination);
                try {Directory.CreateDirectory(source);}
                catch {if(!Directory.Exists(source))Directory.Move(destination,source);throw;}
                preserved.Add(destination);
            }
            return preserved;
        }
        internal static string Failure(string localRoot,DateTime since) {
            string path=Path.Combine(localRoot,"Docker","backend.error.json");
            try {
                if(!File.Exists(path)||File.GetLastWriteTimeUtc(path)<since||new FileInfo(path).Length>262144)return "";
                string text=File.ReadAllText(path);
                foreach(string endpoint in new[]{"engine.sock","sailor-ingest.sock","dockerInference"})
                    if(text.IndexOf(endpoint,StringComparison.OrdinalIgnoreCase)>=0&&text.IndexOf("rename",StringComparison.OrdinalIgnoreCase)>=0)
                        return "Docker 启动失败："+endpoint+" 残留套接字无法访问。请退出报错的 Docker Desktop 后重试启用算力；客户端会保留并重建相关运行目录。";
            } catch(IOException) {}catch(UnauthorizedAccessException) {}
            return "";
        }
    }
    internal sealed class DesktopRetrySchedule {
        string intent="";int failures;
        internal DateTime Next {get;private set;}
        internal bool Ready(string current,DateTime now) {
            if(current!=intent){intent=current;failures=0;Next=DateTime.MinValue;}
            return now>=Next;
        }
        internal void Failed(DateTime now) {Next=now.AddSeconds(Math.Min(600,30*Math.Pow(2,Math.Min(failures++,5))));}
        internal void Completed(){failures=0;Next=DateTime.MinValue;}
    }
}
