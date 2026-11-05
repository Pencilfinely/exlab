using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Principal;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    internal static class WorkerUi {
        internal static readonly Color Ink=Color.FromArgb(28,40,57),Muted=Color.FromArgb(96,109,125),Blue=Color.FromArgb(35,99,213);
        internal static FlowLayoutPanel Stack(int width) {return new FlowLayoutPanel {Width=width,AutoSize=true,FlowDirection=FlowDirection.TopDown,WrapContents=false,Margin=new Padding(0,0,0,16)};}
        internal static Label Copy(string text,int width=650,bool heading=false) {return new Label {Text=text,AutoSize=true,MaximumSize=new Size(width,0),ForeColor=heading?Ink:Muted,Font=new Font("Microsoft YaHei UI",heading?14:10,heading?FontStyle.Bold:FontStyle.Regular),Margin=new Padding(0,0,0,12)};}
        internal static void Button(Button button,bool primary=false) {button.FlatStyle=FlatStyle.Flat;button.FlatAppearance.BorderColor=primary?Blue:Color.FromArgb(215,222,231);button.BackColor=primary?Blue:Color.White;button.ForeColor=primary?Color.White:Ink;button.AutoSize=true;button.MinimumSize=new Size(100,36);button.Padding=new Padding(10,3,10,3);button.Margin=new Padding(0,0,10,8);button.Cursor=Cursors.Hand;}
        internal static LinkLabel Link(string text,Action action) {var link=new LinkLabel {Text=text,AutoSize=true,LinkColor=Blue,Margin=new Padding(0,8,16,8)};link.LinkClicked+=(s,e)=>action();return link;}
    }
    internal sealed class WorkerMigrationForm:Form {
        readonly TextBox source=new TextBox {Width=590},folder=new TextBox {Width=590};
        readonly TextBox password=new TextBox {Width=400,UseSystemPasswordChar=true};
        readonly Label status=WorkerUi.Copy("",620);
        readonly CheckBox dedicated=new CheckBox {Text="允许迁移期间关闭原 Ubuntu（请先保存其中的工作）",AutoSize=true};
        readonly Button start=new Button {Text="迁移并开启"},recover=new Button {Text="恢复原节点"},cancel=new Button {Text="取消迁移",Visible=false};
        readonly TextBox details=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Width=620,Height=130,Visible=false,BorderStyle=BorderStyle.FixedSingle};
        readonly ProgressBar progress=new ProgressBar {Width=620,Height=6,Maximum=8,Visible=false};
        readonly FlowLayoutPanel inputs=WorkerUi.Stack(620),advanced=WorkerUi.Stack(620);
        bool working; readonly bool admin;
        internal WorkerMigrationForm(string ownerSid=null,string selectedSource=null,string selectedFolder=null,bool confirmed=false) {
            if(ownerSid!=null&&ownerSid!=WindowsIdentity.GetCurrent().User.Value)throw new IOException("请使用原 Windows 账户授权。");
            Text="Worker · 开机启动";Size=new Size(720,640);MinimumSize=new Size(700,540);StartPosition=FormStartPosition.CenterParent;Font=new Font("Microsoft YaHei UI",10);BackColor=Color.White;ForeColor=WorkerUi.Ink;
            var panel=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,AutoScroll=true,Padding=new Padding(28)};Controls.Add(panel);
            panel.Controls.Add(WorkerUi.Copy("开机运行，无需登录",620,true));
            panel.Controls.Add(WorkerUi.Copy("自动迁移环境并验证显卡，保留原配置和备份。",620));
            source.Text=selectedSource??App.Text(App.ReadSettings(),"distribution");
            if(String.IsNullOrWhiteSpace(source.Text)) {
                var choices=Array.FindAll(App.RegisteredDistributions(),n=>!n.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase));
                if(choices.Length==1)source.Text=choices[0];
            }
            folder.Text=selectedFolder??WorkerMigration.SuggestedFolder();
            panel.Controls.Add(inputs);
            var selection=WorkerUi.Copy("环境："+(source.Text.Length>0?source.Text:"待选择"),620);inputs.Controls.Add(selection);
            inputs.Controls.Add(WorkerUi.Link("更改环境与存储位置",()=>advanced.Visible=!advanced.Visible));
            Add(advanced,"原 Ubuntu",source);Add(advanced,"新环境与备份目录",folder);
            var browse=new Button {Text="选择目录"};WorkerUi.Button(browse);advanced.Controls.Add(browse);
            browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog {SelectedPath=folder.Text})if(dialog.ShowDialog(this)==DialogResult.OK)folder.Text=dialog.SelectedPath;};
            advanced.Visible=String.IsNullOrWhiteSpace(source.Text);inputs.Controls.Add(advanced);
            source.TextChanged+=(s,e)=>selection.Text="环境："+source.Text;
            Add(inputs,"Windows 登录密码（不是 PIN）",password);
            inputs.Controls.Add(WorkerUi.Copy(WindowsIdentity.GetCurrent().Name+" · 密码由 Windows 保存",620));
            dedicated.Checked=confirmed;inputs.Controls.Add(dedicated);
            var actions=new FlowLayoutPanel {AutoSize=true,MaximumSize=new Size(620,0),Margin=new Padding(0,16,0,8)};
            panel.Controls.Add(actions);foreach(var button in new[]{start,recover,cancel}){WorkerUi.Button(button,button==start||button==recover);actions.Controls.Add(button);}
            panel.Controls.Add(progress);panel.Controls.Add(status);
            panel.Controls.Add(WorkerUi.Link("查看详情",()=>details.Visible=!details.Visible));panel.Controls.Add(details);
            var secondary=new FlowLayoutPanel {AutoSize=true,MaximumSize=new Size(620,0)};panel.Controls.Add(secondary);
            secondary.Controls.Add(WorkerUi.Link("已有独立环境",()=>{if(!working)using(var form=new WorkerBootForm())form.ShowDialog(this);}));
            secondary.Controls.Add(WorkerUi.Link("帮助",()=>App.OpenFile(Path.Combine(App.Package,"docs","UNATTENDED-WORKER.zh-CN.md"))));
            admin=new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
            password.Enabled=admin;RefreshMode();
            start.Click+=async(s,e)=>await Execute(async()=>{
                if(WorkerBoot.Managed&&!WorkerMigration.Pending){WorkerBoot.ResumeAfterUpdate();ShowProgress("已请求启动后台。");return;}
                if(!await Elevate())return;
                if(!dedicated.Checked)throw new IOException("请确认允许暂时关闭原 Ubuntu。");
                await WorkerMigration.Migrate(source.Text.Trim(),folder.Text.Trim(),password.Text,new Progress<string>(ShowProgress));
            },true);
            recover.Click+=async(s,e)=>await Execute(async()=>{if(await Elevate())await WorkerMigration.Recover(new Progress<string>(ShowProgress));},false);
            cancel.Click+=(s,e)=>{WorkerMigration.Cancel();cancel.Enabled=false;ShowProgress("正在取消，当前步骤结束后恢复原节点。");};
            FormClosing+=(s,e)=>{if(working){e.Cancel=true;ShowProgress("请先取消迁移，等待当前步骤结束。");}};FormClosed+=(s,e)=>password.Clear();
        }
        void RefreshMode() {
            bool pending=WorkerMigration.Pending,managed=WorkerBoot.Managed;
            recover.Visible=pending;start.Visible=!pending;inputs.Visible=!pending&&!managed;
            start.Text=managed?"启动后台":admin?"迁移并开启":"授权并继续";
            if(status.Text.Length==0)ShowProgress(pending?"上次迁移未完成，请先恢复原节点。":managed?"已配置开机运行。":admin?"保持电源和网络连接，迁移会等待当前实验结束。":"需要同一 Windows 账户的管理员授权。");
        }
        void ShowProgress(string text) {
            details.Text=text.Replace("\r\n","\n").Replace("\n",Environment.NewLine);
            var match=System.Text.RegularExpressions.Regex.Match(text??"","^([1-8])/8\\s*(.*)");
            if(match.Success){progress.Visible=true;progress.Value=Int32.Parse(match.Groups[1].Value);status.Text=match.Groups[2].Value;}
            else {status.Text=text.Length>600?text.Substring(0,600)+"…（查看详情）":text;}
        }
        static void Add(Control parent,string label,Control field){parent.Controls.Add(WorkerUi.Copy(label,620));parent.Controls.Add(field);}
        internal string[] ElevationArguments(){return new[]{"--migration-settings","--owner-sid",WindowsIdentity.GetCurrent().User.Value,"--migration-source",source.Text,"--migration-folder",folder.Text,"--migration-confirmed",dedicated.Checked?"yes":"no"};}
        async Task<bool> Elevate() {
            if(admin)return true;
            password.Clear();
            using(var p=Process.Start(new ProcessStartInfo(Path.Combine(App.Package,App.Executable),App.Arguments(ElevationArguments())){UseShellExecute=true,Verb="runas"}))await Task.Run(()=>p.WaitForExit());
            working=false;Close();return false;
        }
        async Task Execute(Func<Task> action,bool canCancel) {
            if(working)return;working=true;inputs.Enabled=start.Enabled=recover.Enabled=false;cancel.Visible=canCancel;cancel.Enabled=true;
            try {await action();}catch(Exception ex){ShowProgress(ex.Message);}finally {password.Clear();working=false;if(!IsDisposed){inputs.Enabled=start.Enabled=recover.Enabled=true;password.Enabled=admin;cancel.Visible=false;RefreshMode();}}
        }
    }
}
