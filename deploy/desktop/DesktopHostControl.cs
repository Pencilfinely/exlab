using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace ExperimentManagerDesktop {
    // This connection runs in Windows and never needs a running WSL distribution.
    internal sealed class DesktopHostControl {
        readonly string path;
        Dictionary<string,object> link;
        internal Dictionary<string,object> Report { get; private set; }
        internal bool Linked { get { return link!=null; } }
        internal string NodeId { get { return link==null?"":App.Text(link,"node_id"); } }
        internal string Distribution { get { return link==null?"":App.Text(link,"distribution"); } }
        internal DesktopHostControl(string path) {
            this.path=path;
            try {
                var saved=App.Read(path);object raw;
                if(saved.TryGetValue("link",out raw))link=raw as Dictionary<string,object>;
                if(saved.TryGetValue("report",out raw))Report=raw as Dictionary<string,object>;
                if(link!=null)Validate(link);
            } catch(Exception) {link=null;Report=null;}
        }
        static void Validate(Dictionary<string,object> value) {
            Uri uri;
            if(!Uri.TryCreate(App.Text(value,"hub_url"),UriKind.Absolute,out uri)||
                    (uri.Scheme!="http"&&uri.Scheme!="https")||uri.UserInfo.Length>0||uri.Query.Length>0||uri.Fragment.Length>0||
                    App.Text(value,"token").Length<16||App.Text(value,"node_id").Length==0||
                    !Regex.IsMatch(App.Text(value,"host_id"),"\\A[a-f0-9]{32}\\z"))
                throw new InvalidDataException("Windows 控制连接无效，请启用算力后重新连接。");
        }
        void Save(){App.Write(path,new Dictionary<string,object>{{"link",link},{"report",Report}});}
        internal void Disconnect(){link=null;Report=null;Save();}
        internal void Connect(Dictionary<string,object> info,string distribution) {
            var next=new Dictionary<string,object>{{"node_id",App.Text(info,"node_id")},{"hub_url",App.Text(info,"hub_url")},
                {"token",App.Text(info,"token")},{"distribution",distribution},{"host_id",Guid.NewGuid().ToString("N")}};
            Validate(next);link=next;Report=null;Save();
        }
        internal void Observe(string intent,string phase,string detail,Dictionary<string,object> runtime=null) {
            bool released=runtime!=null&&App.Flag(runtime,"resources_released")&&App.Flag(runtime,"docker_stopped")&&App.Flag(runtime,"wsl_stopped");
            if(phase=="released"&&!released)throw new InvalidDataException("尚未确认 Docker / WSL 已释放。");
            Report=new Dictionary<string,object>{{"intent",intent},{"phase",phase},{"detail",detail.Length>1000?detail.Substring(0,1000):detail},
                {"experiments_stopped",runtime!=null&&App.Flag(runtime,"agents_stopped")},
                {"docker_stopped",runtime!=null&&App.Flag(runtime,"docker_stopped")},
                {"wsl_stopped",runtime!=null&&App.Flag(runtime,"wsl_stopped")},{"resources_released",released}};
            Save();
        }
        internal Dictionary<string,object> Sync() {
            if(link==null)throw new InvalidOperationException("尚未连接 Windows 控制代理。");
            var request=(HttpWebRequest)WebRequest.Create(App.Text(link,"hub_url").TrimEnd('/')+"/api/host/sync");
            request.Method="POST";request.ContentType="application/json";request.Timeout=10000;request.ReadWriteTimeout=10000;
            request.Proxy=null;request.AllowAutoRedirect=false;
            request.Headers[HttpRequestHeader.Authorization]="Bearer "+App.Text(link,"token");
            byte[] bytes=Encoding.UTF8.GetBytes(App.Json.Serialize(new Dictionary<string,object>{{"node_id",App.Text(link,"node_id")},
                {"host_id",App.Text(link,"host_id")},{"report",Report}}));
            request.ContentLength=bytes.Length;using(var output=request.GetRequestStream())output.Write(bytes,0,bytes.Length);
            using(var response=request.GetResponse())using(var input=new StreamReader(response.GetResponseStream(),Encoding.UTF8)) {
                var value=App.Json.Deserialize<Dictionary<string,object>>(input.ReadToEnd());object raw;
                var desired=value.TryGetValue("desired",out raw)?raw as Dictionary<string,object>:null;
                if(App.Text(value,"protocol")!="1"||App.Text(value,"node_id")!=App.Text(link,"node_id")||desired==null||
                        !Regex.IsMatch(App.Text(desired,"intent"),"\\A[a-f0-9]{64}\\z")||
                        Array.IndexOf(new[]{"available","drain","stop","release"},App.Text(desired,"mode"))<0)
                    throw new InvalidDataException("主控未提供有效的 Windows 控制请求。");
                return desired;
            }
        }
        internal static async Task<bool> RuntimeClosed() {
            return await RuntimeClosed(()=>Task.Run(()=>App.RunWithTimeout(15000,"wsl.exe","--list","--running","--quiet")),()=>Task.Run(()=>{
                try {
                    using(var pipe=new NamedPipeClientStream(".","dockerDesktopLinuxEngine",PipeDirection.InOut))pipe.Connect(150);
                    return false;
                } catch(TimeoutException){return true;}catch(UnauthorizedAccessException){return false;}catch(IOException){return true;}
            }));
        }
        internal static async Task<bool> RuntimeClosed(Func<Task<string>> readInventory,Func<Task<bool>> dockerStopped) {
            string inventory=await readInventory();
            return DesktopRuntime.RunningDistributions(inventory).Length==0&&await dockerStopped();
        }
    }
}
