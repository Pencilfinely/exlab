using System;
using System.Collections.Generic;
using System.Collections;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace ExperimentManagerDesktop {
    // Explicit Unicode Shell APIs preserve Chinese names on every Windows locale.
    static class DesktopShortcuts {
        [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IShellLinkW {
            void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int capacity, IntPtr data, uint flags);
            void GetIDList(out IntPtr value);
            void SetIDList(IntPtr value);
            void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int capacity);
            void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
            void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int capacity);
            void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
            void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int capacity);
            void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
            void GetHotkey(out ushort value);
            void SetHotkey(ushort value);
            void GetShowCmd(out int value);
            void SetShowCmd(int value);
            void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int capacity, out int index);
            void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
            void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string value, uint reserved);
            void Resolve(IntPtr window, uint flags);
            void SetPath([MarshalAs(UnmanagedType.LPWStr)] string value);
        }
        static object Create() { return Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046"))); }
        internal static void Save(string destination,string target,string arguments,string description) {
            Directory.CreateDirectory(Path.GetDirectoryName(destination));
            object instance=Create();
            try {
                var link=(IShellLinkW)instance;
                link.SetPath(target);link.SetArguments(arguments);link.SetWorkingDirectory(Path.GetDirectoryName(target));
                link.SetDescription(description);link.SetIconLocation(target,0);
                ((IPersistFile)instance).Save(destination,true);
            } finally { Marshal.ReleaseComObject(instance); }
        }
        internal static string Target(string destination) {
            object instance=Create();
            try {
                ((IPersistFile)instance).Load(destination,0);
                var path=new StringBuilder(32768);
                ((IShellLinkW)instance).GetPath(path,path.Capacity,IntPtr.Zero,4); // Raw target; never resolve or execute it.
                return path.ToString();
            } finally { Marshal.ReleaseComObject(instance); }
        }
    }
    static class App {
        internal static JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 };
        internal static string Package = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        internal static bool Worker;
        internal static string Version {
            get { using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppVersion")) {
                if(stream==null) throw new InvalidOperationException("Application version resource is missing");
                using(var reader=new StreamReader(stream))return reader.ReadToEnd().Trim();
            } }
        }
        internal static string Role { get { return Worker ? "Worker" : "Controller"; } }
        internal static string Title { get { return Worker ? "ExLab Worker" : "ExLab Center"; } }
        internal static string ShortcutName { get { return Title+".lnk"; } }
        internal static string LegacyShortcutName { get { return Worker ? "实验算力.lnk" : "实验台.lnk"; } }
        internal static string Executable { get { return Worker ? "ExLabWorker.exe" : "ExLabCenter.exe"; } }
        internal static string SettingsDirectory=null;
        internal static string SettingsRoot { get { return SettingsDirectory??Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ExperimentManager", "desktop", Role); } }
        internal static string SettingsFile { get { return Path.Combine(SettingsRoot, "settings.json"); } }
        internal static string InstanceKey {get{return "Local\\ExperimentManager-"+Role+"-"+(Environment.UserDomainName+"-"+Environment.UserName).Replace('\\','-');}}
        internal static string Arg(string[] args, string key) { for (int i=0; i<args.Length-1; i++) if(args[i]==key) return args[i+1]; return null; }
        internal static bool Has(string[] args, string key) { return Array.IndexOf(args, key)>=0; }
        internal static string Text(Dictionary<string,object> obj, string key, string fallback="") { return obj.ContainsKey(key) && obj[key]!=null ? Convert.ToString(obj[key]) : fallback; }
        internal static bool Flag(Dictionary<string,object> obj, string key) { return obj.ContainsKey(key) && obj[key] is bool && (bool)obj[key]; }
        internal static Dictionary<string,object> Read(string path) {
            if(!File.Exists(path)) return new Dictionary<string,object>();
            return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path, Encoding.UTF8));
        }
        internal static void Write(string path, object value) {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string tmp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
            File.WriteAllText(tmp, Json.Serialize(value), new UTF8Encoding(false));
            if(File.Exists(path)) File.Replace(tmp,path,null); else File.Move(tmp,path);
        }
        internal static void UpdateReport(string stage,string status,string detail="") {
            // A failure after the old client exits must remain diagnosable.
            // Logging must not prevent installation or hide its original error.
            try { Write(Path.Combine(SettingsRoot,"last-update.json"),new {
                time=DateTime.UtcNow.ToString("o"),role=Role,version=Version,stage=stage,status=status,detail=detail }); }
            catch(IOException) { } catch(UnauthorizedAccessException) { }
        }
        internal static bool CleanupInstalledPackage() {
            string receipt=Path.Combine(SettingsRoot,"installer-cleanup.json");
            using(var guard=new Mutex(false,InstanceKey+"-InstallerCleanup")) {
                bool acquired;
                try {acquired=guard.WaitOne(0);}catch(AbandonedMutexException){acquired=true;}
                if(!acquired)return false;
                try {
                    if(!File.Exists(receipt))return true;
                    var request=Json.Deserialize<InstallerCleanupRequest>(File.ReadAllText(receipt,Encoding.UTF8));
                    if(!InstallerCleanup.TryDelete(request,Assembly.GetExecutingAssembly().Location,InstallerCleanup.ProcessAlive))return false;
                    File.Delete(receipt);UpdateReport("installer_cleanup","completed");return true;
                } finally {guard.ReleaseMutex();}
            }
        }
        internal static void ScheduleInstallerCleanup(string destination) {
            try {
                using(var self=Process.GetCurrentProcess()) {
                    var request=InstallerCleanup.Record(Assembly.GetExecutingAssembly().Location,Version,Worker,
                        self.Id,self.StartTime.ToUniversalTime().Ticks);
                    InstallerCleanup.Validate(request,Path.Combine(destination,Executable));
                    Write(Path.Combine(SettingsRoot,"installer-cleanup.json"),request);
                }
                // The installed client can delete the original EXE only after
                // Windows releases the installer's executable mapping.
                var cleanup=Process.Start(new ProcessStartInfo(Path.Combine(destination,Executable),"--cleanup-installer") {
                    WorkingDirectory=destination,UseShellExecute=false,CreateNoWindow=true });
                if(cleanup!=null)cleanup.Dispose();
            } catch(Exception ex) {UpdateReport("installer_cleanup","pending",ex.Message);}
        }
        internal static string Quote(string value) {
            // WSL parses leading options itself: leave simple flags unquoted.
            if(value.Length>0&&value.IndexOfAny(new[]{' ','\t','\r','\n','"'})<0)return value;
            StringBuilder b=new StringBuilder("\""); int slashes=0;
            foreach(char c in value) { if(c=='\\') { slashes++; continue; } if(c=='"') { b.Append('\\',slashes*2+1); b.Append(c); } else { b.Append('\\',slashes); b.Append(c); } slashes=0; }
            b.Append('\\',slashes*2); return b.Append('"').ToString();
        }
        internal static string Arguments(params string[] args) { return string.Join(" ",Array.ConvertAll(args,Quote)); }
        internal static Icon LoadIcon(Size size) {
            using(var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppIcon")) {
                if(resource==null) throw new InvalidOperationException("Application icon resource is missing");
                // Select a frame from the multi-size ICO, then detach it from the
                // resource stream. Each caller owns and disposes its returned icon.
                using(var icon=new Icon(resource,size)) return (Icon)icon.Clone();
            }
        }
        internal static string Run(string executable, params string[] args) {
            return RunWithTimeout(120000,executable,args);
        }
        internal static string RunWithTimeout(int timeoutMs,string executable, params string[] args) {
            var info=new ProcessStartInfo(executable,Arguments(args)) { WorkingDirectory=Package, UseShellExecute=false, CreateNoWindow=true,
                RedirectStandardOutput=true, RedirectStandardError=true, StandardOutputEncoding=Encoding.UTF8, StandardErrorEncoding=Encoding.UTF8 };
            if(Path.GetFileName(executable).Equals("wsl.exe",StringComparison.OrdinalIgnoreCase)&&Array.IndexOf(args,"--list")>=0) info.StandardOutputEncoding=Encoding.Unicode;
            info.EnvironmentVariables["PYTHONUTF8"]="1"; info.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";
            StringBuilder output=new StringBuilder(),error=new StringBuilder();
            using(var p=new Process { StartInfo=info }) {
                p.OutputDataReceived += (s,e)=>{ if(e.Data!=null) lock(output) output.AppendLine(e.Data); };
                p.ErrorDataReceived += (s,e)=>{ if(e.Data!=null) lock(error) error.AppendLine(e.Data); };
                p.Start(); p.BeginOutputReadLine(); p.BeginErrorReadLine();
                if(!p.WaitForExit(timeoutMs)) throw new Exception("操作仍在执行，请稍后查看状态或日志。后台安装可能需要下载依赖。");
                p.WaitForExit();
                string value=output.ToString().Replace("\0", "").Trim();
                if(p.ExitCode!=0 && !value.StartsWith("{")) throw new Exception((value+"\n"+error.ToString()).Trim());
                return value;
            }
        }
        internal static Dictionary<string,object> Command(string executable, params string[] args) {
            return CommandWithTimeout(120000,executable,args);
        }
        internal static Dictionary<string,object> CommandWithTimeout(int timeoutMs,string executable, params string[] args) {
            string text=RunWithTimeout(timeoutMs,executable,args);
            var value=Json.Deserialize<Dictionary<string,object>>(text);
            if(Text(value,"status")=="error") throw new Exception(Text(value,"detail",text));
            return value;
        }
        internal static void OpenFile(string path) { if(File.Exists(path)||Directory.Exists(path)) Process.Start(new ProcessStartInfo(path){UseShellExecute=true}); }
        internal static string[] RegisteredDistributions() {
            // WSL and Windows Terminal enumerate this per-user registration
            // store; it remains readable when wsl.exe has not been installed.
            var names=new List<string>();
            using(var registry=Registry.CurrentUser.OpenSubKey("Software\\Microsoft\\Windows\\CurrentVersion\\Lxss")) {
                if(registry==null)return names.ToArray();
                foreach(string child in registry.GetSubKeyNames())using(var distribution=registry.OpenSubKey(child)) {
                    string name=distribution==null?null:distribution.GetValue("DistributionName") as string;
                    if(String.IsNullOrWhiteSpace(name))throw new IOException("无法确认 WSL 发行版状态，请检查原算力环境后重试退出。");
                    names.Add(name);
                }
            }
            return names.ToArray();
        }
        internal static void Shortcut(string destination, string target, string arguments) {
            DesktopShortcuts.Save(destination,target,arguments,Title);
        }
        internal static bool OwnsShortcutTarget(string target,string installRoot) {
            try {
                string path=Path.GetFullPath(target),root=Path.GetFullPath(installRoot).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
                string name=Path.GetFileName(path);
                return path.StartsWith(root,StringComparison.OrdinalIgnoreCase)&&
                    (name.Equals(Executable,StringComparison.OrdinalIgnoreCase)||name.Equals("Experiment"+Role+".exe",StringComparison.OrdinalIgnoreCase));
            } catch { return false; }
        }
        internal static bool OwnedShortcut(string link,string installRoot) {
            if(!File.Exists(link))return false;
            try {
                return OwnsShortcutTarget(DesktopShortcuts.Target(link),installRoot);
            } catch { return false; }
        }
        internal static void InstallShortcuts(string desktopFolder,string menuFolder,string installRoot,string target,bool makeDesktop) {
            string menu=Path.Combine(menuFolder,"ExLab",ShortcutName),desktop=Path.Combine(desktopFolder,ShortcutName);
            if(File.Exists(menu)&&!OwnedShortcut(menu,installRoot))throw new IOException("同名开始菜单快捷方式不属于 ExLab，请先为其改名。");
            if(makeDesktop&&File.Exists(desktop)&&!OwnedShortcut(desktop,installRoot))throw new IOException("同名桌面快捷方式不属于 ExLab，请先为其改名。");
            Shortcut(menu,target,"");if(makeDesktop)Shortcut(desktop,target,"");
            else if(OwnedShortcut(desktop,installRoot))File.Delete(desktop);
            foreach(string old in new[]{Path.Combine(desktopFolder,LegacyShortcutName),
                Path.Combine(menuFolder,"ExLab",(Worker?"Worker":"Center")+".lnk"),
                Path.Combine(menuFolder,"Experiment Manager",Role+".lnk")})
                if(OwnedShortcut(old,installRoot))File.Delete(old);
        }
        internal static void WaitForPreviousClient(string[] args) {
            string raw=Arg(args,"--wait-pid");if(raw==null)return;
            int pid;long started;
            if(!int.TryParse(raw,out pid)||pid<=0||pid==Process.GetCurrentProcess().Id||!long.TryParse(Arg(args,"--wait-start"),out started))
                throw new Exception("更新交接参数无效，请重新打开安装程序。");
            try { using(var previous=Process.GetProcessById(pid)) {
                if(previous.StartTime.ToUniversalTime().Ticks==started&&!previous.WaitForExit(60000))
                    throw new Exception("旧客户端尚未退出，请从托盘退出后重新打开安装程序。");
            } } catch(ArgumentException) { /* The old client has already exited. */ }
        }
        [STAThread] static void Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                using(Stream role=Assembly.GetExecutingAssembly().GetManifestResourceStream("Role")) {
                    if(role!=null) using(var reader=new StreamReader(role)) Worker=reader.ReadToEnd().Trim()=="worker";
                    else Worker=File.ReadAllText(Path.Combine(Package,"release-role.json")).Contains("worker");
                }
                if(Has(args,"--cleanup-installer")) {
                    var until=DateTime.UtcNow.AddMinutes(5);
                    while(DateTime.UtcNow<until) {
                        try {if(CleanupInstalledPackage())return;}
                        catch(IOException) { } catch(UnauthorizedAccessException) { }
                        Thread.Sleep(1000);
                    }
                    UpdateReport("installer_cleanup","pending","安装包仍被占用，下次启动客户端后继续清理。 ");return;
                }
                if(Has(args,"--self-test")) {
                    // .NET Framework selects at most 128 px from this multi-size
                    // ICO; the 256 px frame remains available to Windows Explorer.
                    int[] runtimeIconSizes={16,20,24,28,32,36,40,44,48,56,64,72,80,88,96,112,128};
                    foreach(int size in runtimeIconSizes) using(var icon=LoadIcon(new Size(size,size))) using(var bitmap=icon.ToBitmap()) {
                        if(bitmap.Width!=size||bitmap.Height!=size) throw new Exception("Application icon frame is missing: "+size);
                    }
                    string iconHash;
                    using(var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppIcon")) using(var hash=SHA256.Create())
                        iconHash=BitConverter.ToString(hash.ComputeHash(resource)).Replace("-","").ToLowerInvariant();
                    int payloadFiles=0;
                    using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) if(payload!=null) using(var zip=new ZipArchive(payload,ZipArchiveMode.Read)) {
                        if(zip.GetEntry(Executable)==null||zip.GetEntry("release-role.json")==null) throw new Exception("Incomplete application payload");
                        foreach(var item in zip.Entries) { using(var input=item.Open()) {byte[] buffer=new byte[65536];while(input.Read(buffer,0,buffer.Length)>0){} }payloadFiles++; }
                    }
                    File.WriteAllText(Arg(args,"--report")??Path.Combine(Path.GetTempPath(),"expman-desktop-test.json"), Json.Serialize(new { role=Role, version=Version, executable=Executable, status="passed",payload_files=payloadFiles,icon_sha256=iconHash,runtime_icon_sizes=runtimeIconSizes })); return;
                }
                using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) if(payload!=null) {
                    if(Has(args,"--apply-update"))UpdateReport("waiting_for_previous_client","running");
                    WaitForPreviousClient(args);
                    if(Has(args,"--install")) {
                        string dataRoot=Arg(args,"--data-root")??Text(Read(SettingsFile),"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller"));
                        bool startup=Has(args,"--startup");
                        startup=startup||DesktopRuntime.AutoStart;
                        string installed=InstallerForm.Install(dataRoot,Arg(args,"--pairing"),Has(args,"--desktop"),startup);
                        Write(Arg(args,"--report")??Path.Combine(SettingsRoot,"install-report.json"),new {status="installed",role=Role,path=installed});
                        ScheduleInstallerCleanup(installed);
                    } else using(var form=new InstallerForm(args)) Application.Run(form);
                    return;
                }
                string key=InstanceKey; bool created;
                using(var mutex=new Mutex(true,key,out created)) using(var show=new EventWaitHandle(false,EventResetMode.AutoReset,key+"-Open")) {
                    if(!created) { show.Set(); return; }
                    using(var form=new ClientForm(args)) {
                        var handle=form.Handle;
                        var monitor=new Thread(()=> { while(show.WaitOne()) { if(form.IsDisposed) break; try { form.BeginInvoke((Action)(()=>form.OpenFromTray())); } catch(InvalidOperationException) { break; } } });
                        monitor.IsBackground=true; monitor.Start(); Application.Run(form); show.Set();
                    }
                }
            } catch(Exception ex) { if(Has(args,"--cleanup-installer")){UpdateReport("installer_cleanup","pending",ex.Message);Environment.ExitCode=1;return;} if(Has(args,"--apply-update")||Has(args,"--after-update"))UpdateReport(Has(args,"--after-update")?"client_start":"installer_start","failed",ex.Message); if(Has(args,"--self-test")||Has(args,"--install")) {Write(Arg(args,"--report")??Path.Combine(Path.GetTempPath(),"expman-desktop-error.json"),new{status="failed",detail=ex.Message});Environment.ExitCode=1;}else MessageBox.Show(ex.Message,Title,MessageBoxButtons.OK,MessageBoxIcon.Error); }
        }
    }

    sealed class InstallerForm : IconForm {
        bool installing;
        Button install=new Button { Text="安装并启动 / Install", AutoSize=true };
        CheckBox desktop=new CheckBox { Text="创建桌面快捷方式", Checked=true, AutoSize=true };
        CheckBox startup=new CheckBox { Text="登录 Windows 后后台启动", Checked=false, AutoSize=true };
        TextBox data=new TextBox { Dock=DockStyle.Fill };
        TextBox pairing=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        Label status=new Label { AutoSize=true,MaximumSize=new Size(530,0) };
        internal InstallerForm(string[] args) {
            Text="安装 "+App.Title; Size=new Size(610,460); MinimumSize=Size; StartPosition=FormStartPosition.CenterScreen;
            startup.Checked=DesktopRuntime.AutoStart;
            var saved=App.Read(App.SettingsFile);
            if(App.Has(args,"--apply-update"))desktop.Checked=saved.ContainsKey("desktop_shortcut")?App.Flag(saved,"desktop_shortcut"):
                (File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),App.ShortcutName))||
                 File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),App.LegacyShortcutName)));
            if(App.Worker)pairing.Text=App.Text(saved,"pairing_file");
            Font=new Font("Microsoft YaHei UI",10); BackColor=Color.White;
            var panel=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=8 };
            panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            panel.Controls.Add(new Label { Text=App.Title,Font=new Font(Font.FontFamily,19,FontStyle.Bold),AutoSize=true });
            panel.Controls.Add(new Label { Text="安装到当前用户，不需要管理员权限。运行环境和实验数据分别保存。",AutoSize=true,MaximumSize=new Size(530,0) });
            panel.Controls.Add(new Label { Text=App.Worker?"导入主控提供的配对凭证（已有节点可直接复用）":"主控数据目录（升级时可选择原实验台目录）",AutoSize=true });
            var row=new TableLayoutPanel { Dock=DockStyle.Top,ColumnCount=2,AutoSize=true };
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); row.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            var browse=new Button { Text="选择…", AutoSize=true };
            if(App.Worker) { row.Controls.Add(pairing,0,0); browse.Click+=(s,e)=>{ using(var f=new OpenFileDialog { Filter="节点凭证 (*.json)|*.json" }) if(f.ShowDialog()==DialogResult.OK) pairing.Text=f.FileName; }; }
            else { data.Text=App.Arg(args,"--data-root")??App.Text(App.Read(App.SettingsFile),"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller")); row.Controls.Add(data,0,0); browse.Click+=(s,e)=>{ using(var f=new FolderBrowserDialog { Description="选择实验台数据目录",SelectedPath=data.Text }) if(f.ShowDialog()==DialogResult.OK) data.Text=f.SelectedPath; }; }
            row.Controls.Add(browse,1,0); panel.Controls.Add(row); panel.Controls.Add(desktop); panel.Controls.Add(startup); panel.Controls.Add(status); panel.Controls.Add(install); Controls.Add(panel);
            install.Click+=async (s,e)=> {
                if(installing)return;installing=true;install.Enabled=false; status.Text="正在安装…";
                string stage="installing";
                try {
                    App.UpdateReport(stage,"running");
                    string dataPath=data.Text,credential=pairing.Text; bool makeDesktop=desktop.Checked,autoStart=startup.Checked;
                    string destination=await Task.Run(()=>Install(dataPath,credential,makeDesktop,autoStart));
                    stage="starting_client";
                    App.UpdateReport(stage,"running",destination);
                    string startArgs="--after-update"+(App.Worker&&App.Has(args,"--resume-service")?" --background --resume-service":"");
                    var launched=Process.Start(new ProcessStartInfo(Path.Combine(destination,App.Executable),startArgs){WorkingDirectory=destination,UseShellExecute=true});
                    if(launched==null)throw new Exception("新客户端未能启动，请从开始菜单打开客户端。");
                    launched.Dispose();
                    await Task.Run(()=>App.ScheduleInstallerCleanup(destination));
                    installing=false;Close();
                } catch(Exception ex) { App.UpdateReport(stage,"failed",ex.Message);status.Text=ex.Message; installing=false;install.Enabled=true; }
            };
            FormClosing+=(s,e)=>{if(installing){e.Cancel=true;status.Text="正在安装，请等待完成后再关闭。";}};
            if(App.Has(args,"--apply-update"))Shown+=(s,e)=>install.PerformClick();
            ResumeLayout(true);
        }
        internal static string Install(string dataPath,string credential,bool makeDesktop,bool autoStart) {
            Mutex active;
            if(Mutex.TryOpenExisting(App.InstanceKey,out active)) {active.Dispose();throw new Exception("此客户端已经运行。升级前请在原客户端中停止主控或代理，再从托盘菜单退出客户端，然后点击安装。已有 Docker 实验不会因此停止。");}
            using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) using(var zip=new ZipArchive(payload,ZipArchiveMode.Read)) {
                var role=zip.GetEntry("release-role.json"); string version;
                using(var reader=new StreamReader(role.Open())) version=App.Text(App.Json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd()),"version");
                foreach(char c in version) if(!(char.IsLetterOrDigit(c)||c=='.'||c=='-')) throw new Exception("安装包版本无效");
                if(string.IsNullOrEmpty(version)) throw new Exception("安装包版本无效");
                string destination=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","ExperimentManager",App.Role,version);
                string ancestor=destination;
                while(ancestor!=null) {if(Directory.Exists(ancestor)&&(File.GetAttributes(ancestor)&FileAttributes.ReparsePoint)!=0)throw new Exception("安装目录不能包含目录链接");ancestor=Path.GetDirectoryName(ancestor);}
                Directory.CreateDirectory(destination);
                string prefix=Path.GetFullPath(destination)+Path.DirectorySeparatorChar;
                foreach(var entry in zip.Entries) {
                    if(string.IsNullOrEmpty(entry.Name)) continue;
                    string path=Path.GetFullPath(Path.Combine(destination,entry.FullName));
                    if(!path.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)||entry.FullName.Contains(":")) throw new Exception("安装包路径不安全");
                    string current=Path.GetDirectoryName(path);
                    while(current!=null && current.StartsWith(destination,StringComparison.OrdinalIgnoreCase)) {
                        if(Directory.Exists(current)&&(File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0) throw new Exception("安装目录不能包含目录链接");
                        current=Path.GetDirectoryName(current);
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using(var input=entry.Open()) using(var memory=new MemoryStream()) {
                        input.CopyTo(memory); byte[] bytes=memory.ToArray();
                        if(File.Exists(path)) { if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)throw new Exception("安装文件不能是链接");using(var hash=SHA256.Create()) if(Convert.ToBase64String(hash.ComputeHash(File.ReadAllBytes(path)))!=Convert.ToBase64String(hash.ComputeHash(bytes))) throw new Exception("同版本安装文件已被修改，请选择新的版本安装包："+entry.Name); }
                        else File.WriteAllBytes(path,bytes);
                    }
                }
                var settings=App.Read(App.SettingsFile);
                var stopped=VerifyBackendStopped(destination,dataPath,settings);
                if(App.Worker&&stopped!=null) {
                    string distribution=App.Text(settings,"distribution");
                    string package=App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",destination).Trim();
                    CompleteWorkerInstallation(package,version,stopped,arguments=> {
                        var command=new List<string>{"-d",distribution,"--exec","bash"};
                        command.AddRange(arguments);
                        return App.Command("wsl.exe",command.ToArray());
                    });
                }
                if(!App.Worker) settings["data_root"]=Path.GetFullPath(dataPath);
                if(App.Worker&&!string.IsNullOrWhiteSpace(credential)) settings["pairing_file"]=Path.GetFullPath(credential);
                settings["installed_version"]=version;settings["desktop_shortcut"]=makeDesktop; App.Write(App.SettingsFile,settings);
                string target=Path.Combine(destination,App.Executable);
                App.InstallShortcuts(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),"Programs"),
                    Directory.GetParent(destination).FullName,target,makeDesktop);
                using(var run=Registry.CurrentUser.OpenSubKey("Software\\Microsoft\\Windows\\CurrentVersion\\Run",true)) {
                    run.DeleteValue("ExperimentManager-"+App.Role,false);
                    if(autoStart) run.SetValue("ExLab-"+App.Role,App.Quote(target)+" --background");
                    else run.DeleteValue("ExLab-"+App.Role,false);
                }
                return destination;
            }
        }
        internal static Dictionary<string,object> VerifyBackendStopped(string destination,string dataPath,Dictionary<string,object> settings) {
            Dictionary<string,object> state;
            if(App.Worker) {
                string distribution=App.Text(settings,"distribution");
                // A fresh worker installation has no selected Linux service yet.
                if(string.IsNullOrEmpty(distribution))return null;
                string package=App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",destination).Trim();
                state=App.Command("wsl.exe","-d",distribution,"--exec","bash",package+"/Client-Worker.sh","install-status");
            } else state=App.Command(Path.Combine(destination,"runtime","python.exe"),"-m","expman.desktop",
                "controller-install-status","--root",Path.GetFullPath(dataPath));
            DesktopLifecycle.RequireInstallReady(state,App.Worker);
            return state;
        }
        internal static void CompleteWorkerInstallation(string package,string version,Dictionary<string,object> stopped,
                Func<string[],Dictionary<string,object>> invoke) {
            DesktopLifecycle.RequireInstallReady(stopped,true);
            string config=App.Text(stopped,"config");
            if(string.IsNullOrEmpty(config))return; // First pairing remains an explicit setup action.
            string backend=App.Text(stopped,"backend","detached");
            if(backend!="detached"&&backend!="systemd")throw new InvalidOperationException("无法确认原算力后台启动方式，尚未切换代码。");
            var arguments=new List<string>{package+"/Client-Worker.sh","install","--no-start","--backend",backend,"--config",config};
            string serviceRoot=App.Text(stopped,"service_root");
            if(!string.IsNullOrEmpty(serviceRoot)){arguments.Add("--service-root");arguments.Add(serviceRoot);}
            // Deploy WSL code even when the worker was stopped before the update.
            // Starting it is a separate choice carried by --resume-service.
            var result=invoke(arguments.ToArray());
            if(result==null)throw new InvalidOperationException("算力后台安装未返回有效状态，请重试安装。");
            object running;
            string status=App.Text(result,"status");
            if(!result.TryGetValue("running",out running)||!(running is bool)||(bool)running||status!="stopped"||
                    !UpdateService.BackendMatchesRelease(App.Text(result,"installed_version"),version)||
                    App.Text(result,"config")!=config||App.Text(result,"node_id")!=App.Text(stopped,"node_id"))
                throw new InvalidOperationException("算力后台代码更新尚未确认完成："+App.Text(result,"detail",status)+"。原节点配置与实验数据保留，请重试安装。");
        }
    }

    sealed class ClientForm : IconForm {
        readonly DesktopIcons trayIcons=new DesktopIcons();
        NotifyIcon tray; bool exiting,busy,background; Dictionary<string,object> settings; string lastLog=""; Process workerHold; string heldDistribution="";
        UpdateForm updateDialog;
        BrowserAppWindow browserWindowIcons;
        bool resumeWorkerAfterUpdate;
        bool startingAfterUpdate;
        bool deactivated;
        CheckBox startup=new CheckBox { Text="登录 Windows 后启动",AutoSize=true };
        CheckBox autoEnableCompute=new CheckBox { Text="启动客户端后自动启用算力",AutoSize=true };
        CheckBox autoUpdate=new CheckBox { Text="自动检查、下载并排队安装更新",AutoSize=true };
        CheckBox releaseResources=new CheckBox { Text="退出客户端时自动释放空闲的 Docker 与 WSL",AutoSize=true };
        Label updateStatus=new Label { AutoSize=true,MaximumSize=new Size(810,0) };
        internal DesktopUpdateQueue UpdateQueue;
        internal UpdateRelease AvailableUpdate;
        internal event Action UpdatesChanged;
        bool checkingUpdates,pumpingUpdates;
        DateTime nextAutoCheck=DateTime.MinValue,nextCleanup=DateTime.MinValue;
        internal string UpdateDetail="";
        Label summary=new Label { AutoSize=true, MaximumSize=new Size(810,0), Text="正在检查状态…" };
        TextBox log=new TextBox { Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Both,Dock=DockStyle.Fill,WordWrap=false,Font=new Font("Consolas",9) };
        TextBox data=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        TextBox pairing=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        ComboBox distro=new ComboBox { Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList };
        ComboBox existing=new ComboBox { Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList };
        List<string> configs=new List<string>();
        System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer { Interval=5000 };
        internal ClientForm(string[] args) {
            settings=App.Read(App.SettingsFile); background=App.Has(args,"--background");
            UpdateQueueState restored=null;
            string queueFile=Path.Combine(App.SettingsRoot,"update-queue.json");
            try {if(File.Exists(queueFile))restored=App.Json.Deserialize<UpdateQueueState>(File.ReadAllText(queueFile,Encoding.UTF8));}
            catch(Exception ex) {UpdateDetail="更新队列读取失败："+ex.Message;}
            try {
                UpdateQueue=new DesktopUpdateQueue(restored,App.Version,state=>App.Write(queueFile,state));
                if(restored!=null)App.Write(queueFile,UpdateQueue.State);
            } catch(Exception ex) {UpdateDetail="更新队列读取失败："+ex.Message;UpdateQueue=new DesktopUpdateQueue(null,App.Version,state=>App.Write(queueFile,state));}
            UpdateQueue.Changed+=NotifyUpdates;
            deactivated=App.Flag(settings,"deactivated");
            startingAfterUpdate=App.Has(args,"--after-update");
            string supplied=App.Arg(args,"--data-root"); if(supplied!=null) { settings["data_root"]=Path.GetFullPath(supplied); App.Write(App.SettingsFile,settings); }
            Text=App.Title; Size=new Size(900,650); MinimumSize=new Size(700,510); StartPosition=FormStartPosition.CenterScreen;
            Font=new Font("Microsoft YaHei UI",10); BackColor=Color.FromArgb(246,248,251);
            var layout=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=7 };
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(new Label { Text=App.Title,Font=new Font(Font.FontFamily,21,FontStyle.Bold),AutoSize=true });
            layout.Controls.Add(summary);
            var controls=new TableLayoutPanel { Dock=DockStyle.Top,AutoSize=true,ColumnCount=3 };
            controls.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); controls.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); controls.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            if(App.Worker) {
                controls.Controls.Add(new Label {Text="Ubuntu",AutoSize=true},0,0); controls.Controls.Add(distro,1,0);
                controls.Controls.Add(new Label {Text="节点凭证",AutoSize=true},0,1); controls.Controls.Add(pairing,1,1);
                pairing.Text=App.Text(settings,"pairing_file"); var choose=new Button {Text="选择文件…",AutoSize=true};
                choose.Click+=(s,e)=>{ using(var picker=new OpenFileDialog {Filter="主控配对凭证 (*.json)|*.json"}) if(picker.ShowDialog()==DialogResult.OK) { pairing.Text=picker.FileName; settings["pairing_file"]=pairing.Text; App.Write(App.SettingsFile,settings); } }; controls.Controls.Add(choose,2,1);
                controls.Controls.Add(new Label {Text="已有配置",AutoSize=true},0,2); controls.Controls.Add(existing,1,2);
                configs.Add(""); existing.Items.Add("自动选择 / 新节点使用凭证"); existing.SelectedIndex=0;
                distro.SelectedIndexChanged+=(s,e)=>{settings["distribution"]=Convert.ToString(distro.SelectedItem);App.Write(App.SettingsFile,settings);};
                var paste=new Button {Text="粘贴连接凭证",AutoSize=true};paste.Click+=(s,e)=>ImportClipboard();controls.Controls.Add(paste,2,2);
            } else {
                data.Text=App.Text(settings,"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller"));
                controls.Controls.Add(new Label {Text="数据目录",AutoSize=true},0,0); controls.Controls.Add(data,1,0);
                var choose=new Button {Text="选择目录…",AutoSize=true}; choose.Click+=(s,e)=>{using(var picker=new FolderBrowserDialog {SelectedPath=data.Text,Description="选择已有或新建的主控数据目录"}) if(picker.ShowDialog()==DialogResult.OK) {data.Text=picker.SelectedPath;settings["data_root"]=data.Text;App.Write(App.SettingsFile,settings);RefreshState();}};controls.Controls.Add(choose,2,0);
            }
            layout.Controls.Add(controls);
            var actions=new FlowLayoutPanel {Dock=DockStyle.Top,AutoSize=true};
            AddButton(actions,App.Worker?"启用算力":"打开工作空间",()=> { if(App.Worker) StartWorker(); else OpenController(); });
            AddButton(actions,"停用并释放资源",()=>Stop());
            if(App.Worker) AddButton(actions,"显卡设置",()=>OpenGpuSettings());
            AddButton(actions,"刷新状态",()=>RefreshState()); AddButton(actions,"打开日志",()=>App.OpenFile(lastLog));
            AddButton(actions,"检查更新 · "+App.Version,()=>CheckUpdates());
            AddButton(actions,"转入后台",()=>Hide()); layout.Controls.Add(actions);
            var preferences=new FlowLayoutPanel {Dock=DockStyle.Top,AutoSize=true};
            startup.Checked=DesktopRuntime.AutoStart;startup.CheckedChanged+=(s,e)=>{try{DesktopRuntime.AutoStart=startup.Checked;}catch(Exception ex){summary.Text=ex.Message;}};
            autoEnableCompute.Checked=App.Flag(settings,"auto_enable_compute");
            autoEnableCompute.CheckedChanged+=(s,e)=>SavePreference("auto_enable_compute",autoEnableCompute.Checked);
            autoUpdate.Checked=App.Flag(settings,"auto_update");
            autoUpdate.CheckedChanged+=(s,e)=>{
                SavePreference("auto_update",autoUpdate.Checked);nextAutoCheck=DateTime.MinValue;
                if(autoUpdate.Checked&&UpdateQueue.Active&&!UpdateQueue.State.InstallRequested)
                    UpdateQueue.Enqueue(UpdateQueue.State.Release,true,true);
                if(!autoUpdate.Checked&&UpdateQueue.State.Automatic&&UpdateQueue.CanCancel)UpdateQueue.Cancel();
            };
            releaseResources.Checked=!settings.ContainsKey("release_resources")||App.Flag(settings,"release_resources");
            releaseResources.CheckedChanged+=(s,e)=>{settings["release_resources"]=releaseResources.Checked;App.Write(App.SettingsFile,settings);};
            preferences.Controls.Add(startup);if(App.Worker)preferences.Controls.Add(autoEnableCompute);
            preferences.Controls.Add(autoUpdate);if(App.Worker)preferences.Controls.Add(releaseResources);
            layout.Controls.Add(preferences);
            layout.Controls.Add(updateStatus);layout.Controls.Add(log); layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            Controls.Add(layout);
            tray=new NotifyIcon { Icon=trayIcons.TrayIcon,Text=App.Title,Visible=true };
            var menu=new ContextMenuStrip(); menu.Items.Add(App.Worker?"打开算力客户端":"打开实验台",null,(s,e)=>OpenFromTray());
            menu.Items.Add("停用并释放资源",null,(s,e)=>Stop());
            menu.Items.Add("检查更新…",null,(s,e)=>CheckUpdates());
            menu.Items.Add("状态与日志",null,(s,e)=>ShowStatus()); menu.Items.Add("退出",null,(s,e)=>ExitFromTray()); tray.ContextMenuStrip=menu;tray.DoubleClick+=(s,e)=>OpenFromTray();
            FormClosing+=(s,e)=>{if(!exiting){e.Cancel=true;Hide();}else{timer.Stop();tray.Visible=false;}};
            timer.Tick+=async(s,e)=>{tray.Icon=trayIcons.TrayIcon;await PumpUpdates();if(!exiting)RefreshState();};
            Shown+=async (s,e)=> {
                if(startingAfterUpdate)App.UpdateReport("client_start","launched");
                if(App.Worker) {
                    await Execute(async()=>{
                        string inventory=await Task.Run(()=>App.Run("wsl.exe","--list","--quiet"));
                        foreach(string name in inventory.Split(new[]{'\n','\r'},StringSplitOptions.RemoveEmptyEntries)) if(!name.Trim().StartsWith("docker-desktop")) distro.Items.Add(name.Trim());
                        if(distro.Items.Count==0) throw new Exception("请先安装 WSL2 和 Ubuntu，并在 Ubuntu 中创建普通用户。");
                        string chosen=App.Text(settings,"distribution"); distro.SelectedItem=chosen; if(distro.SelectedIndex<0) distro.SelectedIndex=0;
                    });
                    if(DesktopLifecycle.ShouldEnableCompute(settings,App.Worker,App.Has(args,"--resume-service")))await StartWorkerAsync(autoEnableCompute.Checked);
                    else RefreshState();
                    if(background)Hide();
                } else if(deactivated)DisplayInactive();else if(background) { await Execute(async()=>{await Controller("controller-start");Hide();}); } else OpenController();
                timer.Start();NotifyUpdates();await PumpUpdates();
            };
            ResumeLayout(true);
        }
        protected override void Dispose(bool disposing) {
            if(disposing) {
                timer.Dispose();
                if(browserWindowIcons!=null)browserWindowIcons.Dispose();
                if(tray!=null) { tray.Visible=false; tray.Dispose(); tray=null; }
            }
            base.Dispose(disposing);
            if(disposing) trayIcons.Dispose();
        }
        static void AddButton(Control parent,string text,Action action) {var button=new Button {Text=text,AutoSize=true,Margin=new Padding(0,8,12,8)};button.Click+=(s,e)=>action();parent.Controls.Add(button);}
        async Task Execute(Func<Task> action) { if(busy)return;busy=true;try {await action();}catch(Exception ex){summary.Text=ex.Message;if(startingAfterUpdate)App.UpdateReport("backend_start","failed",ex.Message);try{App.Write(Path.Combine(App.SettingsRoot,"last-error.json"),new{time=DateTime.UtcNow.ToString("o"),detail=ex.Message});}catch(IOException){}catch(UnauthorizedAccessException){}}finally{busy=false;} }
        internal void ShowStatus(){Show();WindowState=FormWindowState.Normal;Activate();RefreshState();}
        async void ExitFromTray() {
            if(busy){Show();WindowState=FormWindowState.Normal;Activate();return;}
            busy=true;timer.Stop();Enabled=false;tray.ContextMenuStrip.Enabled=false;
            Show();WindowState=FormWindowState.Normal;
            summary.Text=App.Worker?"正在保存并停止实验，完成后退出算力客户端…":"正在停止管理服务并退出实验台…";
            try {
                if(deactivated){exiting=true;Close();return;}
                if(App.Worker&&distro.SelectedItem==null&&String.IsNullOrWhiteSpace(App.Text(settings,"distribution"))&&
                        DesktopLifecycle.CanExitUnconfiguredWorker(App.Text(settings,"distribution"),App.RegisteredDistributions())) {
                    exiting=true;Close();return;
                }
                var requested=App.Worker?await WorkerCommand("shutdown"):await Controller("controller-stop");
                Display(requested);
                if(App.Text(requested,"status")=="exit_failed")throw new Exception(App.Text(requested,"detail","后台退出失败，请重试。"));
                await DesktopLifecycle.WaitForStopped(
                    ()=>App.Worker?WorkerCommand("status"):Controller("controller-status"),Display,
                    App.Worker?(TimeSpan?)null:TimeSpan.FromSeconds(30),finalStatus:App.Worker?"stopped":null);
                if(!App.Worker&&browserWindowIcons!=null)await Task.Run(()=>browserWindowIcons.CloseOwnedWindows());
                deactivated=true;settings["deactivated"]=true;App.Write(App.SettingsFile,settings);
                if(App.Worker)await ReleaseWorkerResources();
                exiting=true;Close();
            } catch(Exception ex) {
                summary.Text="退出未完成："+ex.Message+"\n客户端已保留，可从托盘重试退出。";
                try {App.Write(Path.Combine(App.SettingsRoot,"last-error.json"),new{time=DateTime.UtcNow.ToString("o"),detail=ex.Message});}catch(IOException){}catch(UnauthorizedAccessException){}
                Show();WindowState=FormWindowState.Normal;Activate();
            } finally {
                if(!exiting){busy=false;Enabled=true;tray.ContextMenuStrip.Enabled=true;timer.Start();}
            }
        }
        void CheckUpdates(){
            if(updateDialog!=null){updateDialog.Activate();return;}
            try {using(var dialog=new UpdateForm(this)){updateDialog=dialog;dialog.ShowDialog(this);}}
            finally {updateDialog=null;}
        }
        void SavePreference(string name,bool value) {
            try {settings[name]=value;App.Write(App.SettingsFile,settings);}
            catch(Exception ex) {summary.Text="设置保存失败："+ex.Message;}
        }
        void NotifyUpdates() {
            if(IsDisposed)return;
            if(InvokeRequired){BeginInvoke((Action)NotifyUpdates);return;}
            updateStatus.Text=UpdateQueue.Active||String.IsNullOrEmpty(UpdateDetail)?UpdateQueue.State.Detail:UpdateDetail;
            if(UpdateQueue.State.Stage=="downloading")updateStatus.Text+=string.Format(" {0:0.0} / {1:0.0} MiB",UpdateQueue.State.Bytes/1048576.0,UpdateQueue.State.Total/1048576.0);
            if(UpdateQueue.State.Stage=="retry")updateStatus.Text+=" 下次重试："+UpdateQueue.State.NextAttemptUtc.ToLocalTime().ToString("HH:mm:ss");
            if(UpdatesChanged!=null)UpdatesChanged();
        }
        internal async Task CheckForUpdates(bool automatic=false) {
            if(checkingUpdates)return;checkingUpdates=true;UpdateDetail="正在检查更新…";NotifyUpdates();
            try {
                AvailableUpdate=await Task.Run(()=>UpdateService.Check(App.Version,App.Worker));
                UpdateDetail=AvailableUpdate==null?"已是当前渠道的最新版本。":"发现新版本："+AvailableUpdate.Version;
                if(automatic&&autoUpdate.Checked&&AvailableUpdate!=null&&!UpdateQueue.Active)UpdateQueue.Enqueue(AvailableUpdate,true,true);
                nextAutoCheck=DateTime.UtcNow.AddHours(1);
            } catch(Exception ex) {UpdateDetail="检查更新失败，将稍后重试："+ex.Message;nextAutoCheck=DateTime.UtcNow.AddMinutes(5);}
            finally {checkingUpdates=false;NotifyUpdates();}
        }
        internal void QueueUpdate(bool install) {
            UpdateQueue.Enqueue(UpdateQueue.State.Release??AvailableUpdate,install,false);NotifyUpdates();
        }
        async Task PumpUpdates() {
            if(pumpingUpdates||exiting)return;pumpingUpdates=true;
            try {
                if(DateTime.UtcNow>=nextCleanup) {
                    nextCleanup=DateTime.UtcNow.AddSeconds(30);
                    try {await Task.Run(()=>App.CleanupInstalledPackage());}
                    catch(Exception ex) {UpdateDetail="安装包清理待重试："+ex.Message;NotifyUpdates();}
                }
                if(autoUpdate.Checked&&UpdateQueue.Active&&!UpdateQueue.State.InstallRequested)
                    UpdateQueue.Enqueue(UpdateQueue.State.Release,true,true);
                if(autoUpdate.Checked&&!UpdateQueue.Active&&DateTime.UtcNow>=nextAutoCheck)await CheckForUpdates(true);
                await UpdateQueue.Tick(DateTime.UtcNow,busy,
                    (release,progress,token)=>Task.Run(()=>UpdateService.Download(release,Path.Combine(App.SettingsRoot,"updates"),progress,token)),
                    ()=>App.Worker?WorkerCommand("update-status"):Controller("controller-update-status"),InstallUpdate,
                    (release,path)=>Task.Run(()=>UpdateService.ValidateDownloaded(release,path)));
                if(UpdateQueue.State.Stage=="handoff")FinishUpdateExit();
            } catch(Exception ex) {UpdateDetail="更新队列等待重试："+ex.Message;NotifyUpdates();}
            finally {pumpingUpdates=false;}
        }
        internal async Task InstallUpdate(UpdateRelease release,string installer) {
            if(busy)throw new Exception("当前操作尚未完成，请稍后安装。");
            busy=true;timer.Stop();
            bool wasRunning=false,stopRequested=false;
            Exception failure=null;
            try {
                await Task.Run(()=>UpdateService.ValidateDownloaded(release,installer));
                string report=Path.Combine(App.SettingsRoot,"updates","verify-"+Guid.NewGuid().ToString("N")+".json");
                try {
                    await Task.Run(()=>App.Run(installer,"--self-test","--report",report));
                    var result=App.Read(report);
                    if(App.Text(result,"status")!="passed"||App.Text(result,"role")!=App.Role||App.Text(result,"version")!=release.Version||
                        !result.ContainsKey("payload_files")||Convert.ToInt32(result["payload_files"])<=0)
                        throw new Exception("安装包版本或角色不匹配，请重新下载。");
                } finally {if(File.Exists(report))File.Delete(report);}
                var ready=App.Worker?await WorkerCommand("update-status"):await Controller("controller-update-status");
                if(!App.Flag(ready,"ready_for_update"))throw new Exception(App.Text(ready,"detail","实验或文件回传尚未完成。"));
                wasRunning=App.Flag(ready,"running");
                resumeWorkerAfterUpdate=resumeWorkerAfterUpdate||(App.Worker&&wasRunning);
                stopRequested=true;
                var stopped=App.Worker?await WorkerCommand("stop-for-update"):await Controller("controller-stop-for-update");
                if(!App.Flag(stopped,"ready_for_update"))throw new Exception(App.Text(stopped,"detail","暂时无法安全停止服务。"));
                await DesktopLifecycle.WaitForStopped(
                    ()=>App.Worker?WorkerCommand("status"):Controller("controller-status"),null,TimeSpan.FromSeconds(30));
                ready=App.Worker?await WorkerCommand("update-status"):await Controller("controller-update-status");
                if(!App.Flag(ready,"ready_for_update"))throw new Exception(App.Text(ready,"detail","停止后检查未通过。"));
                await Task.Run(()=>UpdateService.ValidateDownloaded(release,installer));
                using(var self=Process.GetCurrentProcess()) {
                    var arguments=new List<string>{"--apply-update","--wait-pid",self.Id.ToString(),"--wait-start",self.StartTime.ToUniversalTime().Ticks.ToString()};
                    if(resumeWorkerAfterUpdate)arguments.Add("--resume-service");
                    App.UpdateReport("handoff","running",release.Version);
                    var next=Process.Start(new ProcessStartInfo(installer,App.Arguments(arguments.ToArray())){UseShellExecute=false,CreateNoWindow=true});
                    if(next==null)throw new Exception("无法启动更新安装程序。");
                    next.Dispose();
                }
            } catch(Exception ex) {failure=ex;}
            if(failure!=null) {
                App.UpdateReport("preparing_install","failed",failure.Message);
                string recovery="";
                if(stopRequested&&wasRunning) {
                    try {
                        var current=App.Worker?await WorkerCommand("status"):await Controller("controller-status");
                        if(!App.Flag(current,"running")) {
                            var restored=App.Worker?await WorkerCommand("start"):await Controller("controller-start");
                            if(!App.Flag(restored,"running")&&App.Text(restored,"status")!="starting")
                                throw new Exception(App.Text(restored,"detail","服务未能启动。"));
                            recovery=" 原服务已重新启动。";
                        }
                    } catch(Exception restoreError) {recovery=" 原服务恢复失败，请在客户端重新启动："+restoreError.Message;}
                }
                busy=false;timer.Start();throw new Exception(failure.Message+recovery,failure);
            }
        }
        internal void FinishUpdateExit(){exiting=true;if(updateDialog!=null)updateDialog.Close();Close();}
        internal void OpenFromTray(){if(App.Worker)ShowStatus();else OpenController();}
        async Task<Dictionary<string,object>> Controller(string action) {
            string root=data.Text; var value=await Task.Run(()=>App.Command(Path.Combine(App.Package,"runtime","python.exe"),"-m","expman.desktop",action,"--root",root));
            Display(value);return value;
        }
        string SelectedDistribution {get{if(distro.SelectedItem==null)throw new Exception("选择原节点使用的 Ubuntu。");return Convert.ToString(distro.SelectedItem);}}
        async Task<Dictionary<string,object>> WorkerCommand(string action,params string[] options) {
            string distribution=SelectedDistribution,credential=pairing.Text;
            string selected=existing.SelectedIndex>0?configs[existing.SelectedIndex]:"";
            return await Task.Run(()=> {
                string package=App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",App.Package).Trim();
                var argv=new List<string>{"-d",distribution,"--exec","bash",package+"/Client-Worker.sh",action};
                argv.AddRange(options);
                if(action=="start"||action=="install") {
                    if(action=="install") {argv.Add("--backend");argv.Add("detached");}
                    if(!string.IsNullOrEmpty(selected)) {argv.Add("--config");argv.Add(selected);}
                    else if(!string.IsNullOrEmpty(credential)&&File.Exists(credential)) {argv.Add("--pairing");argv.Add(App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",credential).Trim());}
                }
                var result=App.CommandWithTimeout(action=="gpu-enable"?300000:120000,"wsl.exe",argv.ToArray());
                if(action=="update-status")return result;
                if(action!="stop"&&action!="stop-for-update"&&(App.Flag(result,"running")||App.Text(result,"status")=="starting"||App.Text(result,"status")=="preparing"||App.Text(result,"status")=="shutting_down")) {
                    if(heldDistribution!=distribution) {
                        if(workerHold!=null)workerHold.Dispose();
                        workerHold=Process.Start(new ProcessStartInfo("wsl.exe",App.Arguments("-d",distribution,"--exec","bash",package+"/Client-Worker.sh","_hold")){UseShellExecute=false,CreateNoWindow=true});
                        heldDistribution=distribution;
                    }
                } else heldDistribution="";
                return result;
            });
        }
        async void RefreshState(){if(deactivated){DisplayInactive();return;}await Execute(async()=>{var result=App.Worker?await WorkerCommand("status"):await Controller("controller-status");Display(result);if(App.Worker){var logs=await WorkerCommand("logs");object lines;if(logs.TryGetValue("lines",out lines)&&lines is IList){var shown=new List<string>();foreach(var line in (IList)lines)shown.Add(Convert.ToString(line));log.Lines=shown.ToArray();}}});}
        async void StartWorker(){await StartWorkerAsync(false);}
        async Task StartWorkerAsync(bool automatic){await Execute(async()=>{if(!await EnsureDocker(automatic))return;deactivated=false;settings["deactivated"]=false;App.Write(App.SettingsFile,settings);summary.Text="正在准备算力…";Display(await WorkerCommand("install"));});}
        async void OpenGpuSettings(){await Execute(async()=>{
            using(var form=new WorkerGpuForm((action,options)=>WorkerCommand(action,options))) form.ShowDialog(this);
            Display(await WorkerCommand("status"));
        });}
        async void Stop(){if(!App.Worker&&deactivated)return;await Execute(async()=>{
            timer.Stop();bool workerStopped=false;try {
                if(App.Worker) {
                    var state=await DesktopRuntime.PrepareWorkerRelease(SelectedDistribution,action=>WorkerCommand(action),
                        ()=>MessageBox.Show(this,"停用将请求本机实验保存并停止，然后释放空闲的 Docker 与 WSL 内存。\n未配置续训的算法会记为中断。Center 保持运行。继续吗？",
                            App.Title,MessageBoxButtons.OKCancel,MessageBoxIcon.Question)==DialogResult.OK,
                        Display,()=>{
                            workerStopped=true;deactivated=true;settings["deactivated"]=true;
                            settings["release_detail"]="算力已停用；正在检查 Docker 与 WSL 的释放状态。";
                            ReleaseWorkerHold();App.Write(App.SettingsFile,settings);
                        });
                    if(state==null)return;
                    await ReleaseWorkerResources(true,state);
                } else await StopBackend();
                DisplayInactive();Show();
            }catch(Exception ex){
                if(workerStopped) {
                    settings["release_detail"]="算力已停用；资源释放未完成："+ex.Message+"。可再次点击“停用并释放资源”重试。";
                    App.Write(App.SettingsFile,settings);DisplayInactive();Show();
                    // Execute's general error display would hide that compute
                    // already stopped and resource release remains retryable.
                    return;
                }
                throw;
            }finally{timer.Start();}
        });}
        async Task StopBackend(){
            Display(App.Worker?await WorkerCommand("deactivate"):await Controller("controller-stop"));
            await DesktopLifecycle.WaitForStopped(()=>App.Worker?WorkerCommand("status"):Controller("controller-status"),Display,App.Worker?(TimeSpan?)null:TimeSpan.FromSeconds(30),finalStatus:App.Worker?"stopped":null);
            deactivated=true;settings["deactivated"]=true;App.Write(App.SettingsFile,settings);
            if(!App.Worker&&browserWindowIcons!=null)await Task.Run(()=>browserWindowIcons.CloseOwnedWindows());
        }
        async void OpenController(){await Execute(async()=>{deactivated=false;settings["deactivated"]=false;App.Write(App.SettingsFile,settings);var result=await Controller("controller-open");string url=App.Text(result,"url");if(!string.IsNullOrEmpty(url)){OpenAppWindow(url);Hide();}});}
        void DisplayInactive(){summary.Text="已停用 · 点击“"+(App.Worker?"启用算力":"打开工作空间")+"”恢复\n"+App.Text(settings,"release_detail","实验记录与连接身份已保留。");}
        void ImportClipboard(){
            try {
                string text=Clipboard.GetText().Trim();if(text.Length==0||text.Length>16384)throw new Exception("请先复制主控提供的连接凭证（小于 16 KiB）。");
                if(text.StartsWith("exlab://connect/",StringComparison.Ordinal)) {string raw=text.Substring("exlab://connect/".Length).Replace('-','+').Replace('_','/');text=Encoding.UTF8.GetString(Convert.FromBase64String(raw.PadRight((raw.Length+3)/4*4,'=')));}
                var value=App.Json.Deserialize<Dictionary<string,object>>(text);
                if(App.Text(value,"node_id").Length==0||App.Text(value,"token").Length<20||App.Text(value,"hub_url").Length==0)throw new Exception("这是算力端，请复制 Worker 连接凭证。");
                string path=Path.Combine(App.SettingsRoot,"clipboard.pairing.json");App.Write(path,value);pairing.Text=path;settings["pairing_file"]=path;App.Write(App.SettingsFile,settings);summary.Text="连接凭证已导入，点击“启用算力”。";
            }catch(Exception ex){summary.Text="粘贴失败："+ex.Message;}
        }
        async Task<bool> EnsureDocker(bool automatic=false){
            var state=await WorkerCommand("runtime-status");if(App.Flag(state,"docker_ready"))return true;
            if(background&&!automatic){summary.Text="Docker 未就绪。打开算力客户端并点击“启用算力”以启动 Docker。";Show();return false;}
            if(!automatic&&MessageBox.Show(this,App.Text(state,"detail")+"\n启动 Docker Desktop 并等待连接吗？",App.Title,MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes)return false;
            summary.Text="正在启动 Docker Desktop…";await DesktopRuntime.StartDocker();var watch=Stopwatch.StartNew();
            while(watch.Elapsed<TimeSpan.FromMinutes(2)){await Task.Delay(2000);state=await WorkerCommand("runtime-status");if(App.Flag(state,"docker_ready"))return true;}
            throw new Exception("Docker 尚未就绪。请检查 Docker Desktop 的 Linux containers、所选 Ubuntu 的 WSL 集成，以及本机 Docker context。然后重试。");
        }
        void ReleaseWorkerHold(){
            if(workerHold!=null){try{if(!workerHold.HasExited)workerHold.Kill();}catch(InvalidOperationException){}workerHold.Dispose();workerHold=null;}heldDistribution="";
        }
        async Task ReleaseWorkerResources(bool explicitRelease=false,Dictionary<string,object> state=null){
            ReleaseWorkerHold();
            if(!explicitRelease&&!releaseResources.Checked){settings["release_detail"]="算力已停用；退出时自动释放选项未开启，Docker 与 WSL 保留。";App.Write(App.SettingsFile,settings);return;}
            if(state==null)state=await WorkerCommand("runtime-release");
            bool closeOtherProcesses=explicitRelease&&App.Flag(state,"can_terminate_wsl");
            bool confirm=explicitRelease&&DesktopRuntime.NeedsOtherProcessConfirmation(state);
            if(confirm) {
                var names=new List<string>();object details;
                if(state.TryGetValue("other_process_details",out details)&&details is IList)foreach(object item in (IList)details) {
                    var process=item as Dictionary<string,object>;if(process!=null)names.Add(App.Text(process,"name")+"（PID "+App.Text(process,"pid")+"）");
                }
                string message="算力已停用。关闭 "+SelectedDistribution+" 将结束其中的其他会话和程序。\n";
                if(names.Count>0)message+="检测到："+String.Join("、",names)+"\n";
                if(!App.Flag(state,"docker_ready"))message+="Docker 未连接，无法核查其容器；可能仍有其他任务。\n";
                message+="请先保存这些程序中的工作。Center 保持运行，实验文件和连接凭证保留。\n确认关闭并释放 WSL 内存吗？";
                if(MessageBox.Show(this,message,App.Title,MessageBoxButtons.YesNo,MessageBoxIcon.Warning)!=DialogResult.Yes) {
                    settings["release_detail"]="算力已停用；已保留其他会话，WSL 内存尚未释放。可再次点击“停用并释放资源”重试。";App.Write(App.SettingsFile,settings);return;
                }
                closeOtherProcesses=true;
            }
            summary.Text="正在释放 WSL 内存…";
            settings["release_detail"]=await DesktopRuntime.Release(SelectedDistribution,state,closeOtherProcesses);App.Write(App.SettingsFile,settings);
        }
        void Display(Dictionary<string,object> value) {
            string state=App.Text(value,"status");
            string stateLabel=state=="shutting_down"?"正在保存并停止实验":state=="exit_failed"?"退出未完成":state=="online"?"已连接":state=="offline"?"主控离线":state=="preparing"?"准备中":state=="starting"?"启动中":state=="stopped"?"已停止":state=="running"?"运行中":state=="failed"?"启动失败":state;
            summary.Text=App.Text(value,"node_id",App.Worker?"算力客户端":"实验台")+" · "+stateLabel+"\n"+App.Text(value,"detail");
            string backendVersion=App.Text(value,"version");
            if(App.Worker) {
                summary.Text+="\n客户端版本："+App.Version;
                string installedVersion=App.Text(value,"installed_version");
                if(App.Flag(value,"running"))summary.Text+=" · 运行中后台版本："+(string.IsNullOrEmpty(backendVersion)?"无法确认":backendVersion);
                else if(!string.IsNullOrEmpty(installedVersion))summary.Text+=" · 已安装后台版本："+installedVersion+"（当前未运行）";
                string lastVersion=App.Text(value,"last_run_version");
                if(!App.Flag(value,"running")&&!string.IsNullOrEmpty(lastVersion))summary.Text+="\n上次运行版本："+lastVersion;
                if(startingAfterUpdate&&state=="stopped"&&UpdateService.BackendMatchesRelease(installedVersion,App.Version)) {
                    App.UpdateReport("backend_install","completed",installedVersion+"；后台保持停止");startingAfterUpdate=false;
                }
            } else if(!string.IsNullOrEmpty(backendVersion))summary.Text+="\n客户端版本："+App.Version+" · 后台版本："+backendVersion;
            if(startingAfterUpdate&&App.Flag(value,"running")&&(App.Worker||App.Flag(value,"responsive"))&&UpdateService.BackendMatchesRelease(backendVersion,App.Version)) {
                App.UpdateReport("backend_start","completed",backendVersion);startingAfterUpdate=false;
            }
            lastLog=App.Text(value,"log_path");
            if(App.Worker) {
                object raw;if(value.TryGetValue("candidates",out raw)&&raw is IList) foreach(object item in (IList)raw) {
                    var candidate=item as Dictionary<string,object>;if(candidate==null)continue;string path=App.Text(candidate,"path");
                    if(!configs.Contains(path)){configs.Add(path);existing.Items.Add(App.Text(candidate,"node_id")+" · "+path);}
                }
                if(lastLog.StartsWith("/"))lastLog="\\\\wsl.localhost\\"+SelectedDistribution+lastLog.Replace('/','\\');
            } else if(File.Exists(lastLog)) { using(var stream=new FileStream(lastLog,FileMode.Open,FileAccess.Read,FileShare.ReadWrite)) {if(stream.Length>48000)stream.Seek(-48000,SeekOrigin.End);using(var reader=new StreamReader(stream)){string text=reader.ReadToEnd();log.Text=text.Length>12000?text.Substring(text.Length-12000):text;}} }
        }
        void OpenAppWindow(string url) {
            string edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),"Microsoft","Edge","Application","msedge.exe");
            if(!File.Exists(edge))edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Microsoft","Edge","Application","msedge.exe");
            if(!File.Exists(edge)){Process.Start(new ProcessStartInfo(url){UseShellExecute=true});return;}
            string profile=Path.Combine(App.SettingsRoot,"web-profile");
            if(browserWindowIcons==null)browserWindowIcons=new BrowserAppWindow(edge,profile);
            browserWindowIcons.Start();
            Process.Start(new ProcessStartInfo(edge,App.Arguments("--app="+url,"--user-data-dir="+profile,"--no-first-run","--no-default-browser-check")){UseShellExecute=false,CreateNoWindow=true});
        }
    }
}
