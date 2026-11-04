using System;
using System.Diagnostics;
using System.Drawing;
using System.Security.Principal;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    internal sealed class WorkerBootForm:Form {
        readonly TextBox distribution=new TextBox {Width=340},linuxUser=new TextBox {Width=340},serviceRoot=new TextBox {Width=580};
        readonly TextBox password=new TextBox {Width=340,UseSystemPasswordChar=true};
        readonly Label status=new Label {AutoSize=true,MaximumSize=new Size(640,0)};
        readonly Button enable=new Button {Text="启用未登录开机启动",AutoSize=true,Enabled=false};
        readonly CheckBox dedicated=new CheckBox {Text="此 Ubuntu 专供 Worker 使用，释放资源时允许关闭它",AutoSize=true};
        WorkerBootConfig checkedConfig;bool working;
        internal WorkerBootForm(string ownerSid=null) {
            if(ownerSid!=null&&ownerSid!=WindowsIdentity.GetCurrent().User.Value)throw new InvalidOperationException("请使用拥有 WSL 的同一 Windows 账户提升权限，不能切换管理员账户。");
            Text="Worker · 无人值守启动";Size=new Size(740,650);MinimumSize=Size;Font=new Font("Microsoft YaHei UI",10);StartPosition=FormStartPosition.CenterParent;
            var layout=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,AutoScroll=true,Padding=new Padding(20)};Controls.Add(layout);
            layout.Controls.Add(new Label {Text="开机运行算力后台，无需登录 Windows",AutoSize=true,Font=new Font(Font.FontFamily,15,FontStyle.Bold)});
            layout.Controls.Add(new Label {Text="需要独立 Docker Engine、NVIDIA 容器支持及已配置的 Worker。\n先准备环境并停止原 Worker，再检测。密码交给 Windows 任务计划程序保存。",AutoSize=true});
            Add(layout,"WSL 发行版",distribution);Add(layout,"Linux 普通用户名",linuxUser);Add(layout,"Worker 服务目录（可留空）",serviceRoot);
            Add(layout,"Windows 账户 "+WindowsIdentity.GetCurrent().Name+" 的密码（不是 PIN）",password);
            layout.Controls.Add(dedicated);
            var saved=WorkerBoot.Load();distribution.Text=saved.Distribution??App.Text(App.ReadSettings(),"distribution");linuxUser.Text=saved.LinuxUser??"";serviceRoot.Text=saved.ServiceRoot??"";
            dedicated.Checked=saved.DedicatedDistribution;
            password.Enabled=new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
            EventHandler changed=(s,e)=>{checkedConfig=null;enable.Enabled=false;};distribution.TextChanged+=changed;linuxUser.TextChanged+=changed;serviceRoot.TextChanged+=changed;
            var actions=new FlowLayoutPanel {AutoSize=true,MaximumSize=new Size(660,0)};layout.Controls.Add(actions);
            var check=new Button {Text="检测独立环境",AutoSize=true};actions.Controls.Add(check);check.Click+=async(s,e)=>await Execute(async()=>{
                var c=new WorkerBootConfig {Distribution=distribution.Text.Trim(),LinuxUser=linuxUser.Text.Trim(),ServiceRoot=serviceRoot.Text.Trim(),OwnerSid=WindowsIdentity.GetCurrent().User.Value};
                if(String.IsNullOrWhiteSpace(c.Distribution)||c.Distribution.StartsWith("-")||!System.Text.RegularExpressions.Regex.IsMatch(c.LinuxUser,"\\A[a-z_][a-z0-9_-]*\\z")||c.LinuxUser=="root")throw new ArgumentException("请填写发行版和原节点的普通 Linux 用户名。");
                status.Text="正在检测 Docker、GPU 和节点身份…";
                var result=await Task.Run(()=>WorkerBoot.Inspect(c));
                if(!App.Flag(result,"ready"))throw new InvalidOperationException(App.Text(result,"detail"));
                c.ServiceRoot=App.Text(result,"service_root");c.NodeId=App.Text(result,"node_id");c.EngineId=App.Text(result,"engine_id");WorkerBoot.Validate(c);
                checkedConfig=c;enable.Enabled=true;status.Text=App.Text(result,"detail")+"\n节点："+c.NodeId+"。未登录重启验收尚未完成。";
            });
            actions.Controls.Add(enable);enable.Click+=async(s,e)=>await Execute(async()=>{
                if(checkedConfig==null)throw new InvalidOperationException("请先检测环境。");
                if(!await EnsureElevation())return;
                var settings=App.ReadSettings();string oldDistro=App.Text(settings,"distribution");
                if(oldDistro.Length>0) {
                    // Query the old distro's default user; never guess it from the new environment.
                    var previous=await Task.Run(()=>WorkerBoot.OriginalStatus(oldDistro));
                    if(!App.Flag(previous,"ready_for_install"))throw new InvalidOperationException("原节点仍在运行，请先完成实验并停用原 Worker。");
                    if(App.Text(previous,"node_id").Length>0&&App.Text(previous,"node_id")!=checkedConfig.NodeId)throw new InvalidOperationException("请选择原节点迁移后的配置；不能用其他节点覆盖此客户端。");
                }
                var checkedAgain=await Task.Run(()=>WorkerBoot.Inspect(checkedConfig));
                if(!App.Flag(checkedAgain,"ready"))throw new InvalidOperationException(App.Text(checkedAgain,"detail"));
                var target=await Task.Run(()=>WorkerBoot.Command(checkedConfig,"install-status"));
                if(!App.Flag(target,"ready_for_install"))throw new InvalidOperationException("目标环境的 Worker 尚未停止，请先停止原管理进程再登记。");
                var info=await Task.Run(()=>WorkerBoot.Command(checkedConfig,"host-info"));
                if(App.Text(info,"node_id")!=checkedConfig.NodeId)throw new InvalidOperationException("节点身份已变化。");
                checkedConfig.DedicatedDistribution=dedicated.Checked;
                try {
                    await WorkerBoot.SuspendForUpdate();
                    WorkerBoot.Register(checkedConfig,password.Text);password.Clear();
                    new DesktopHostControl(System.IO.Path.Combine(App.SettingsRoot,"host-control.json")).Connect(info,checkedConfig.Distribution);
                    settings["distribution"]=checkedConfig.Distribution;App.SaveSettings(settings);
                    WorkerBoot.Request("available");
                } finally {WorkerBoot.ResumeAfterUpdate();}
                status.Text="开机任务已登记，后台正在启动。需重启并保持未登录，从另一设备验收 GPU 任务。";
            });
            var disable=new Button {Text="关闭开机启动",AutoSize=true};actions.Controls.Add(disable);disable.Click+=async(s,e)=>await Execute(async()=>{
                if(!await EnsureElevation())return;
                WorkerBoot.DisableAutostart();status.Text="已关闭下次开机启动；当前运行和节点数据保留。";
            });
            var help=new Button {Text="环境准备与迁移说明",AutoSize=true};actions.Controls.Add(help);help.Click+=(s,e)=>App.OpenFile(System.IO.Path.Combine(App.Package,"docs","UNATTENDED-WORKER.zh-CN.md"));
            layout.Controls.Add(status);status.Text=saved.Enabled?"已配置无人值守模式；可重新检测、更新账户密码或关闭开机启动。":"尚未登记任务。环境检测通过后才能开启。";FormClosed+=(s,e)=>password.Clear();
            FormClosing+=(s,e)=>{if(working){e.Cancel=true;status.Text="正在完成当前操作，请稍候。";}};
        }
        async Task<bool> EnsureElevation() {
            if(new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))return true;
            password.Clear();
            using(var elevated=Process.Start(new ProcessStartInfo(System.IO.Path.Combine(App.Package,App.Executable),App.Arguments("--boot-settings","--owner-sid",WindowsIdentity.GetCurrent().User.Value)){UseShellExecute=true,Verb="runas"}))await Task.Run(()=>elevated.WaitForExit());
            working=false;Close();return false;
        }
        static void Add(Control parent,string text,Control input){parent.Controls.Add(new Label {Text=text,AutoSize=true,Margin=new Padding(0,9,0,3)});parent.Controls.Add(input);}
        async Task Execute(Func<Task> action){if(working)return;working=true;Controls[0].Enabled=false;try{await action();}catch(Exception ex){status.Text=ex.Message;password.Clear();}finally{working=false;if(!IsDisposed){Controls[0].Enabled=true;enable.Enabled=checkedConfig!=null;}}}
    }
}
