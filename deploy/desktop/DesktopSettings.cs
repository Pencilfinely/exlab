using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace ExperimentManagerDesktop {
    internal static class DesktopSettings {
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern uint GetFinalPathNameByHandle(IntPtr handle,StringBuilder path,uint size,uint flags);
        internal static string PhysicalPath(string path) {
            try {
                using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
                    var actual=new StringBuilder(32768);
                    if(GetFinalPathNameByHandle(stream.SafeFileHandle.DangerousGetHandle(),actual,(uint)actual.Capacity,0)>0)return actual.ToString();
                }
            }catch(IOException){}catch(UnauthorizedAccessException){}
            return Path.GetFullPath(path);
        }
        internal static Dictionary<string,object> Load(string path,bool worker) {
            Dictionary<string,object> settings=null;Exception invalid=null;bool primary=false;
            foreach(string candidate in new[]{path,path+".bak"}) {
                try {
                    var value=App.Read(candidate);
                    if(!File.Exists(candidate))continue;
                    if(value==null)throw new InvalidDataException("桌面设置必须是 JSON 对象。");
                    settings=value;primary=candidate==path;break;
                } catch(ArgumentException ex){invalid=ex;}
                catch(InvalidOperationException ex){invalid=ex;}
                catch(InvalidDataException ex){invalid=ex;}
            }
            var binding=worker?new Dictionary<string,object>():App.Read(Path.Combine(Path.GetDirectoryName(path),"controller-root.json"));
            if(binding==null)throw new InvalidDataException("Center 数据目录记录损坏，请选择原数据目录。");
            if(settings==null) {
                if(invalid!=null&&!binding.ContainsKey("data_root"))throw new InvalidDataException("桌面设置损坏，请恢复 settings.json.bak 或选择原数据目录。",invalid);
                settings=new Dictionary<string,object>();
            }
            if(!worker&&(!primary||!settings.ContainsKey("data_root"))&&binding.ContainsKey("data_root")) {
                if(App.Text(settings,"data_root")!=App.Text(binding,"data_root")){settings.Remove("hub_initialized");settings.Remove("workspace_id");}
                foreach(var item in binding)settings[item.Key]=item.Value;
            }
            if(!worker&&settings.ContainsKey("data_root"))Root(settings);
            return settings;
        }
        internal static string Root(Dictionary<string,object> settings) {
            object raw;
            if(!settings.TryGetValue("data_root",out raw))return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller");
            string root=raw as string;
            if(string.IsNullOrWhiteSpace(root)||!Path.IsPathRooted(root))throw new InvalidDataException("Center 数据目录设置无效，请选择原数据目录。");
            return Path.GetFullPath(root);
        }
        internal static void SelectRoot(Dictionary<string,object> settings,string root) {
            if(string.IsNullOrWhiteSpace(root))throw new InvalidDataException("请选择 Center 原数据目录。");
            string selected=Path.GetFullPath(root);
            if(!string.Equals(Root(settings),selected,StringComparison.OrdinalIgnoreCase)) {
                settings.Remove("hub_initialized");settings.Remove("workspace_id");
            }
            settings["data_root"]=selected;
        }
        internal static void Save(string path,Dictionary<string,object> settings,bool worker) {
            if(!worker&&settings.ContainsKey("data_root"))Root(settings);
            // Keep a complete second copy outside versioned application folders.
            App.Write(path+".bak",settings);App.Write(path,settings);
            if(!worker&&settings.ContainsKey("data_root")) {
                var binding=new Dictionary<string,object>{{"data_root",Root(settings)}};
                foreach(string key in new[]{"hub_initialized","workspace_id"})if(settings.ContainsKey(key))binding[key]=settings[key];
                App.Write(Path.Combine(Path.GetDirectoryName(path),"controller-root.json"),binding);
            }
        }
    }
}
