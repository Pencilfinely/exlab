using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Principal;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    internal sealed class WorkerMigrationForm:Form {
        readonly TextBox source=new TextBox {Width=610},folder=new TextBox {Width=610};
        readonly TextBox password=new TextBox {Width=400,UseSystemPasswordChar=true};
        readonly Label status=new Label {AutoSize=true,MaximumSize=new Size(650,0)};
        readonly CheckBox dedicated=new CheckBox {Text="原 Ubuntu 专供 Worker 使用；迁移期间允许关闭并复制它",AutoSize=true};
        readonly Button start=new Button {Text="一键迁移并启用",AutoSize=true},recover=new Button {Text="恢复原节点",AutoSize=true},cancel=new Button {Text="取消迁移",AutoSize=true,Enabled=false};
        bool working;
        internal WorkerMigrationForm(string ownerSid=null) {
            if(ownerSid!=null&&ownerSid!=WindowsIdentity.GetCurrent().User.Value)throw new IOException("请使用拥有原 WSL 的同一账户提升权限。");
            Text="Worker · 一键迁移与无人值守启动";Size=new Size(760,700);MinimumSize=Size;StartPosition=FormStartPosition.CenterParent;Font=new Font("Microsoft YaHei UI",10);
            var panel=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,AutoScroll=true,Padding=new Padding(22)};Controls.Add(panel);
            panel.Controls.Add(new Label {Text="自动迁移，开机无需登录",AutoSize=true,Font=new Font(Font.FontFamily,16,FontStyle.Bold)});
            panel.Controls.Add(new Label {Text="自动等待实验结束、备份、安装独立 Docker、迁移镜像、验证 GPU 并登记开机任务。\n保留原环境；中途失败自动恢复。下载与复制可能需要较长时间和额外磁盘空间。\n请保持电源、网络和 Docker Desktop 运行。迁移完成后可验收并自行卸载 Desktop。",AutoSize=true,MaximumSize=new Size(650,0)});
            Add(panel,"原 Worker 的 WSL 发行版",source);source.Text=App.Text(App.ReadSettings(),"distribution");
            Add(panel,"新环境及备份目录（默认选择空间最大的固定磁盘）",folder);folder.Text=WorkerMigration.SuggestedFolder();
            var browse=new Button {Text="选择磁盘目录…",AutoSize=true};panel.Controls.Add(browse);browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog {SelectedPath=folder.Text})if(dialog.ShowDialog(this)==DialogResult.OK)folder.Text=dialog.SelectedPath;};
            Add(panel,"Windows 账户 "+WindowsIdentity.GetCurrent().Name+" 的密码（不是 PIN）",password);
            panel.Controls.Add(new Label {Text="密码仅交给 Windows 任务计划程序，不保存到迁移记录。需要同一账户的管理员授权。",AutoSize=true,MaximumSize=new Size(650,0)});
            panel.Controls.Add(dedicated);
            var actions=new FlowLayoutPanel {AutoSize=true};panel.Controls.Add(actions);actions.Controls.Add(start);actions.Controls.Add(recover);actions.Controls.Add(cancel);
            var resume=new Button {Text="启动迁移后的后台",AutoSize=true};actions.Controls.Add(resume);resume.Click+=(s,e)=>{try {if(!WorkerBoot.Managed)throw new IOException("尚未完成迁移交接。");if(WorkerMigration.Pending)throw new IOException("迁移未结束，请先恢复原节点。");WorkerBoot.ResumeAfterUpdate();status.Text="已请求启动后台；主控的释放或暂停策略仍然有效。";}catch(Exception ex){status.Text=ex.Message;}};
            panel.Controls.Add(status);status.Text=WorkerMigration.Pending?"上次迁移未完成，请先点击“恢复原节点”。":"填写账户密码并确认专用环境后，点击“一键迁移并启用”。";
            bool admin=new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);password.Enabled=admin;
            if(!admin){start.Text="授权并打开一键迁移";status.Text="先使用同一 Windows 账户授权管理员权限，再输入密码。";}
            start.Click+=async(s,e)=>await Execute(async()=>{
                if(!await Elevate())return;
                if(!dedicated.Checked)throw new IOException("请先确认原 Ubuntu 专供 Worker 使用。");
                await WorkerMigration.Migrate(source.Text.Trim(),folder.Text.Trim(),password.Text,new Progress<string>(text=>status.Text=text));password.Clear();
            });
            recover.Click+=async(s,e)=>await Execute(async()=>{if(await Elevate())await WorkerMigration.Recover(new Progress<string>(text=>status.Text=text));});
            cancel.Click+=(s,e)=>{WorkerMigration.Cancel();cancel.Enabled=false;status.Text="已请求取消；等待当前复制或安装步骤安全结束后自动恢复原节点。";};
            FormClosing+=(s,e)=>{if(working){e.Cancel=true;status.Text="迁移仍在进行。可点击“取消迁移”，等待当前步骤结束并恢复。";}};FormClosed+=(s,e)=>password.Clear();
        }
        static void Add(Control parent,string label,Control field){parent.Controls.Add(new Label {Text=label,AutoSize=true,Margin=new Padding(0,10,0,3)});parent.Controls.Add(field);}
        async Task<bool> Elevate() {
            if(new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))return true;
            password.Clear();
            using(var p=Process.Start(new ProcessStartInfo(Path.Combine(App.Package,App.Executable),App.Arguments("--migration-settings","--owner-sid",WindowsIdentity.GetCurrent().User.Value)){UseShellExecute=true,Verb="runas"}))await Task.Run(()=>p.WaitForExit());
            working=false;Close();return false;
        }
        async Task Execute(Func<Task> action) {
            if(working)return;working=true;start.Enabled=recover.Enabled=source.Enabled=folder.Enabled=password.Enabled=dedicated.Enabled=false;cancel.Enabled=true;
            try {await action();}catch(Exception ex){status.Text=ex.Message;}finally {password.Clear();working=false;if(!IsDisposed){start.Enabled=recover.Enabled=source.Enabled=folder.Enabled=dedicated.Enabled=true;password.Enabled=new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);cancel.Enabled=false;}}
        }
    }
}
