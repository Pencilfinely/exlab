using System;
using System.Drawing;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    sealed class UpdateForm : IconForm {
        readonly ClientForm client;
        readonly Label versions=new Label { AutoSize=true };
        readonly Label status=new Label { AutoSize=true,MaximumSize=new Size(560,0) };
        readonly TextBox notes=new TextBox { Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Fill,BackColor=Color.White };
        readonly ProgressBar progress=new ProgressBar { Dock=DockStyle.Top,Height=18 };
        readonly Button check=new Button { Text="检查更新",AutoSize=true };
        readonly Button download=new Button { Text="下载更新",AutoSize=true };
        readonly Button install=new Button { Text="下载并排队安装",AutoSize=true };
        readonly Button cancel=new Button { Text="取消队列",AutoSize=true };
        readonly LinkLabel releaseLink=new LinkLabel { Text="查看 GitHub 发布页",AutoSize=true };
        bool checking;

        internal UpdateForm(ClientForm owner) {
            client=owner;Text="软件更新 · "+App.Title;
            Size=new Size(650,520);MinimumSize=new Size(570,440);StartPosition=FormStartPosition.CenterParent;
            Font=new Font("Microsoft YaHei UI",10);
            var layout=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=7 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            for(int i=0;i<4;i++)layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(versions);
            layout.Controls.Add(new Label { Text="排队一次即可：自动下载并交接管理服务，Docker 实验继续运行，队列和回传在重启后接续。关闭此窗口仍会继续；安装成功后自动删除安装包。",AutoSize=true,MaximumSize=new Size(560,0),Margin=new Padding(0,12,0,12) });
            layout.Controls.Add(notes);layout.Controls.Add(progress);layout.Controls.Add(status);
            var buttons=new FlowLayoutPanel { AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0,12,0,8) };
            buttons.Controls.Add(check);buttons.Controls.Add(download);buttons.Controls.Add(install);buttons.Controls.Add(cancel);
            layout.Controls.Add(buttons);layout.Controls.Add(releaseLink);Controls.Add(layout);
            check.Click+=async(s,e)=>await Check();
            download.Click+=(s,e)=>Enqueue(false);
            install.Click+=(s,e)=>Enqueue(true);
            cancel.Click+=(s,e)=>{try{client.UpdateQueue.Cancel();}catch(Exception ex){status.Text=ex.Message;}};
            releaseLink.LinkClicked+=(s,e)=>{
                var release=client.UpdateQueue.State.Release??client.AvailableUpdate;
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(
                    release==null?UpdateService.Repository+"/releases":release.ReleaseUrl){UseShellExecute=true});
            };
            client.UpdatesChanged+=RefreshQueue;
            Shown+=async(s,e)=>{RefreshQueue();if(!client.UpdateQueue.Active)await Check();};
            ActiveControl=check;RefreshQueue();ResumeLayout(true);
        }
        async Task Check() {
            if(checking)return;checking=true;RefreshQueue();
            try {await client.CheckForUpdates();}
            finally {checking=false;RefreshQueue();}
        }
        void Enqueue(bool installing) {
            try {client.QueueUpdate(installing);RefreshQueue();}
            catch(Exception ex) {status.Text=ex.Message;}
        }
        void RefreshQueue() {
            if(IsDisposed)return;
            var queue=client.UpdateQueue;var state=queue.State;var release=state.Release??client.AvailableUpdate;
            versions.Text="当前版本："+App.Version+(release==null?"":"    新版本："+release.Version);
            string text=release==null?"当前渠道暂无可用的新版本。":release.Notes??"";
            text=text.Replace("\r\n","\n").Replace('\r','\n').Replace("\n","\r\n");
            if(notes.Text!=text)notes.Text=text;
            status.Text=queue.Active?state.Detail:client.UpdateDetail;
            if(state.Stage=="downloading")status.Text+=string.Format(" {0:0.0} / {1:0.0} MiB",state.Bytes/1048576.0,state.Total/1048576.0);
            if(state.Stage=="retry")status.Text+=" 下次重试："+state.NextAttemptUtc.ToLocalTime().ToString("HH:mm:ss");
            progress.Style=checking?ProgressBarStyle.Marquee:ProgressBarStyle.Blocks;
            progress.Value=state.Total>0?(int)Math.Min(100,state.Bytes*100/state.Total):0;
            check.Enabled=!checking;
            download.Enabled=release!=null&&!queue.Active;
            install.Enabled=release!=null&&(!queue.Active||(!state.InstallRequested&&queue.CanCancel));
            install.Text=state.Downloaded==null?"下载并排队安装":"排队安装";
            cancel.Enabled=queue.CanCancel;
        }
        protected override void Dispose(bool disposing) {
            if(disposing)client.UpdatesChanged-=RefreshQueue;
            base.Dispose(disposing);
        }
    }
}
