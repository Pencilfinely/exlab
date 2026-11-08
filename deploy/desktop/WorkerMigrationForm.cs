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
        readonly TextBox source=new TextBox {Width=590},folder=new TextBox {Width=590},dataFolder=new TextBox {Width=590};
        readonly TextBox password=new TextBox {Width=400,UseSystemPasswordChar=true};
        readonly Label status=WorkerUi.Copy("",620);
        readonly CheckBox dedicated=new CheckBox {Text="允许关闭 Ubuntu；改盘时重启空闲 Docker / WSL",AutoSize=true};
        readonly Button start=new Button {Text="迁移并开启"},recover=new Button {Text="恢复原节点"},cancel=new Button {Text="取消迁移",Visible=false};
        readonly TextBox details=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Width=620,Height=130,Visible=false,BorderStyle=BorderStyle.FixedSingle};
        readonly ProgressBar progress=new ProgressBar {Width=620,Height=6,Maximum=8,Visible=false};
        readonly FlowLayoutPanel inputs=WorkerUi.Stack(620),advanced=WorkerUi.Stack(620);
        bool working,observing; readonly bool admin;
        readonly Timer monitor=new Timer {Interval=1000};
        readonly DesktopIcons icons=new DesktopIcons();
        NotifyIcon tray;
        internal WorkerMigrationForm(string ownerSid=null,string selectedSource=null,string selectedFolder=null,bool confirmed=false,string selectedDataFolder=null) {
            if(ownerSid!=null&&ownerSid!=WindowsIdentity.GetCurrent().User.Value)throw new IOException("请使用原 Windows 账户授权。");
            Text="Worker · 开机启动";Size=new Size(720,740);MinimumSize=new Size(700,540);StartPosition=FormStartPosition.CenterParent;Font=new Font("Microsoft YaHei UI",10);BackColor=Color.White;ForeColor=WorkerUi.Ink;
            var panel=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,AutoScroll=true,Padding=new Padding(28)};Controls.Add(panel);
            panel.Controls.Add(WorkerUi.Copy("开机运行，无需登录",620,true));
            panel.Controls.Add(WorkerUi.Copy("提交后等待当前实验结束，再自动迁移；可关闭窗口。",620));
            source.Text=selectedSource??App.Text(App.ReadSettings(),"distribution");
            if(String.IsNullOrWhiteSpace(source.Text)) {
                var choices=Array.FindAll(App.RegisteredDistributions(),n=>!n.StartsWith("docker-desktop",StringComparison.OrdinalIgnoreCase));
                if(choices.Length==1)source.Text=choices[0];
            }
            folder.Text=selectedFolder??Path.Combine(WorkerMigration.SuggestedFolder(),"MigrationBackups");
            dataFolder.Text=selectedDataFolder??WorkerDataMove.Suggested(source.Text);
            panel.Controls.Add(inputs);
            var selection=WorkerUi.Copy("环境："+(source.Text.Length>0?source.Text:"待选择"),620);inputs.Controls.Add(selection);
            Add(inputs,"Worker 数据位置",dataFolder);
            var diskActions=new FlowLayoutPanel {AutoSize=true,MaximumSize=new Size(620,0)};
            var chooseData=new Button {Text="选择数据目录"};var checkSpace=new Button {Text="检测空间"};
            foreach(var button in new[]{chooseData,checkSpace}){WorkerUi.Button(button);diskActions.Controls.Add(button);}inputs.Controls.Add(diskActions);
            chooseData.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog {Description="选择空目录或创建新目录，用于 Ubuntu、镜像和实验数据",SelectedPath=dataFolder.Text})if(dialog.ShowDialog(this)==DialogResult.OK)dataFolder.Text=dialog.SelectedPath;};
            checkSpace.Click+=async(s,e)=>await Execute(async()=>{ShowProgress("正在检测数据位置和空间…");ShowProgress(await WorkerMigration.PreviewSpace(source.Text.Trim(),dataFolder.Text.Trim(),folder.Text.Trim()));},false);
            inputs.Controls.Add(WorkerUi.Link("环境与配置备份位置",()=>advanced.Visible=!advanced.Visible));
            Add(advanced,"原 Ubuntu",source);Add(advanced,"配置备份目录",folder);
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
            tray=new NotifyIcon {Icon=icons.TrayIcon,Text="Worker · 迁移队列"};
            var menu=new ContextMenuStrip();menu.Items.Add("查看迁移进度",null,(s,e)=>OpenQueue());
            menu.Items.Add("取消迁移",null,(s,e)=>{if(WorkerMigration.Pending&&WorkerMigration.CanRollback(App.Read(WorkerMigration.ActivePath)))WorkerMigration.Cancel();});
            tray.ContextMenuStrip=menu;tray.DoubleClick+=(s,e)=>OpenQueue();
            monitor.Tick+=(s,e)=>{
                try {
                    bool active=WorkerMigration.Running;
                    if(active&&WorkerMigration.Pending)ShowProgress(WorkerMigration.Status);
                    if(!working) {
                        if(observing&&!active)ShowProgress(App.Text(App.Read(WorkerMigration.ActivePath),"stage","迁移后台已退出，请检查状态。"));
                        observing=active;RefreshMode();
                    }
                }catch(IOException){}catch(UnauthorizedAccessException){}
            };monitor.Start();
            password.Enabled=admin;RefreshMode();
            start.Click+=async(s,e)=>await Execute(async()=>{
                if(WorkerMigration.Pending&&!WorkerMigration.CanRollback(App.Read(WorkerMigration.ActivePath))){if(await Elevate())await WorkerMigration.Finish(new Progress<string>(ShowProgress));return;}
                if(WorkerBoot.Managed&&!WorkerMigration.Pending){WorkerBoot.ResumeAfterUpdate();ShowProgress("已请求启动后台。");return;}
                if(!await Elevate())return;
                if(!dedicated.Checked)throw new IOException("请确认允许暂时关闭原 Ubuntu。");
                await WorkerMigration.Migrate(source.Text.Trim(),folder.Text.Trim(),password.Text,new Progress<string>(ShowProgress),dataFolder.Text.Trim());
            },true);
            recover.Click+=async(s,e)=>await Execute(async()=>{if(await Elevate())await WorkerMigration.Recover(new Progress<string>(ShowProgress));},false);
            cancel.Click+=(s,e)=>{WorkerMigration.Cancel();cancel.Enabled=false;ShowProgress("正在取消，当前步骤结束后恢复原节点。");};
            FormClosing+=(s,e)=>{if(KeepInBackground(working,e.CloseReason)){e.Cancel=true;Hide();tray.Visible=true;}};
            FormClosed+=(s,e)=>{password.Clear();monitor.Dispose();tray.Dispose();icons.Dispose();};
        }
        internal static bool KeepInBackground(bool busy,CloseReason reason){return busy&&reason==CloseReason.UserClosing;}
        void OpenQueue(){Show();WindowState=FormWindowState.Normal;Activate();}
        void RefreshMode() {
            bool pending=WorkerMigration.Pending,managed=WorkerBoot.Managed;
            if(!working&&WorkerMigration.Running) {
                observing=true;inputs.Visible=start.Visible=recover.Visible=false;
                cancel.Visible=pending&&WorkerMigration.CanRollback(App.Read(WorkerMigration.ActivePath));cancel.Enabled=true;
                ShowProgress(WorkerMigration.Status);return;
            }
            bool canRecover=pending&&WorkerMigration.CanRollback(App.Read(WorkerMigration.ActivePath));
            recover.Visible=canRecover;start.Visible=!canRecover;inputs.Visible=!pending&&!managed;
            start.Text=pending?"完成交接并启动":managed?"启动后台":admin?"排队迁移并开启":"授权并继续";
            if(status.Text.Length==0)ShowProgress(pending?(canRecover?"上次迁移未完成，请先恢复原节点。":"环境已验证，点击完成交接。") :managed?"已配置开机运行。":admin?"保持电源和网络连接，迁移会等待当前实验结束。":"需要同一 Windows 账户的管理员授权。");
        }
        void ShowProgress(string text) {
            if(details.Text==text.Replace("\r\n","\n").Replace("\n",Environment.NewLine))return;
            details.Text=text.Replace("\r\n","\n").Replace("\n",Environment.NewLine);
            var match=System.Text.RegularExpressions.Regex.Match(text??"","^([1-8])/8\\s*(.*)");
            if(match.Success){progress.Visible=true;progress.Value=Int32.Parse(match.Groups[1].Value);status.Text=match.Groups[2].Value;}
            else {status.Text=text.Length>600?text.Substring(0,600)+"…（查看详情）":text;}
        }
        static void Add(Control parent,string label,Control field){parent.Controls.Add(WorkerUi.Copy(label,620));parent.Controls.Add(field);}
        internal string[] ElevationArguments(){return new[]{"--migration-settings","--owner-sid",WindowsIdentity.GetCurrent().User.Value,"--migration-source",source.Text,"--migration-folder",folder.Text,"--migration-data-folder",dataFolder.Text,"--migration-confirmed",dedicated.Checked?"yes":"no"};}
        Task<bool> Elevate() {
            if(admin)return Task.FromResult(true);
            password.Clear();
            using(var p=Process.Start(new ProcessStartInfo(Path.Combine(App.Package,App.Executable),App.Arguments(ElevationArguments())){UseShellExecute=true,Verb="runas"})){}
            working=false;Close();return Task.FromResult(false);
        }
        async Task Execute(Func<Task> action,bool canCancel) {
            if(working)return;working=true;inputs.Enabled=start.Enabled=recover.Enabled=false;cancel.Visible=canCancel;cancel.Enabled=true;
            try {await action();}catch(Exception ex){ShowProgress(ex.Message);}finally {password.Clear();working=false;if(!IsDisposed){inputs.Enabled=start.Enabled=recover.Enabled=true;password.Enabled=admin;cancel.Visible=false;RefreshMode();if(!Visible){tray.BalloonTipTitle="Worker 迁移";tray.BalloonTipText="迁移操作已结束，点击托盘图标查看结果。";tray.ShowBalloonTip(5000);}}}
        }
    }
}
