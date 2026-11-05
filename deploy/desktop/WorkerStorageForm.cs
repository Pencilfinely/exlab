using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    sealed class WorkerStorageForm:IconForm {
        readonly Func<string,string[],Task<Dictionary<string,object>>> command;
        readonly ListView spaces=new ListView {Dock=DockStyle.Fill,View=View.Details,FullRowSelect=true};
        readonly Label status=WorkerUi.Copy("扫描后显示空间明细。",700);
        readonly Button scan=new Button {Text="扫描空间"},clean=new Button {Text="清理可回收项",Enabled=false};
        readonly Button compact=new Button {Text="回收磁盘空间"},backups=new Button {Text="清理迁移备份"};
        readonly Button cancel=new Button {Text="取消",Visible=false};
        readonly CheckBox automatic=new CheckBox {AutoSize=true,Text="自动回收已完成实验的冗余副本"};
        readonly TextBox details=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Bottom,Height=110,Visible=false};
        readonly Timer timer=new Timer {Interval=2000};
        bool busy,loading,pending,polling,operationRunning;
        internal WorkerStorageForm(Func<string,string[],Task<Dictionary<string,object>>> command) {
            this.command=command;Text="Worker · 存储与环境";Size=new Size(800,590);MinimumSize=new Size(720,500);
            StartPosition=FormStartPosition.CenterParent;Font=new Font("Microsoft YaHei UI",10);BackColor=Color.White;
            var layout=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=6};
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));for(int i=0;i<3;i++)layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(WorkerUi.Copy("存储与环境",700,true));
            layout.Controls.Add(WorkerUi.Copy("结果和权重保留。代码按版本归档，已结束的容器和重复副本可回收。",700));
            spaces.Columns.Add("内容",430);spaces.Columns.Add("占用",190);layout.Controls.Add(spaces);
            var actions=new FlowLayoutPanel {AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0,14,0,0)};
            WorkerUi.Button(scan);WorkerUi.Button(clean,true);actions.Controls.Add(scan);actions.Controls.Add(clean);
            WorkerUi.Button(compact);compact.Visible=WorkerBoot.Managed;actions.Controls.Add(compact);
            WorkerUi.Button(backups);backups.Visible=System.IO.File.Exists(WorkerMigration.ActivePath);actions.Controls.Add(backups);
            WorkerUi.Button(cancel);actions.Controls.Add(cancel);
            actions.Controls.Add(WorkerUi.Link("详情",()=>details.Visible=!details.Visible));layout.Controls.Add(actions);
            layout.Controls.Add(automatic);layout.Controls.Add(status);Controls.Add(layout);Controls.Add(details);
            scan.Click+=async(s,e)=>await Send("storage-scan");clean.Click+=async(s,e)=>await Send("storage-clean");
            compact.Click+=async(s,e)=>await WindowsAction(async()=>{var value=await WorkerDisk.Request();return "Windows 可用空间增加 "+Bytes(Number(value,"windows_reclaimed_bytes"))+"。算力保持停用。";});
            backups.Click+=async(s,e)=>await WindowsAction(async()=>{long freed=await Task.Run(()=>WorkerMigration.CleanBackups());return "迁移备份已检查 · Windows 可用空间增加 "+Bytes(freed);});
            automatic.CheckedChanged+=async(s,e)=>{if(!loading)await Send("storage-status",new[]{"--automatic-cleanup",automatic.Checked?"yes":"no"});};
            cancel.Click+=async(s,e)=>{cancel.Enabled=false;try{await command("storage-cancel",new string[0]);status.Text="当前项目处理完后取消…";}catch(Exception ex){status.Text=ex.Message;cancel.Enabled=true;}};
            timer.Tick+=async(s,e)=>{if((pending||operationRunning)&&!polling){polling=true;try{var value=await command("storage-status",new string[0]);if(!IsDisposed){Display(value);if(busy)scan.Enabled=clean.Enabled=automatic.Enabled=false;}}catch(Exception ex){if(!IsDisposed)status.Text=ex.Message;}finally{polling=false;}}};
            Shown+=async(s,e)=>{timer.Start();if(WorkerBoot.Managed&&App.Text(App.Read(System.IO.Path.Combine(App.SettingsRoot,"boot-request.json")),"mode")=="release")status.Text="算力已停用。可回收 Windows 磁盘空间；扫描文件会唤醒 Ubuntu。";else await Send("storage-status");};
            FormClosed+=(s,e)=>timer.Dispose();
            FormClosing+=(s,e)=>{if(busy)e.Cancel=true;};
        }
        internal static string Bytes(long value) {return value>=1073741824?String.Format("{0:0.0} GB",value/1073741824.0):String.Format("{0:0.0} MB",value/1048576.0);}
        static long Number(Dictionary<string,object> value,string key){object raw;return value.TryGetValue(key,out raw)?Convert.ToInt64(raw):0;}
        internal void Display(Dictionary<string,object> value) {
            string phase=App.Text(value,"status");pending=phase=="queued"||phase=="running";
            cancel.Visible=pending||operationRunning;cancel.Enabled=true;
            object rows;if(value.TryGetValue("groups",out rows)&&rows is IList){spaces.Items.Clear();foreach(Dictionary<string,object> row in (IList)rows)spaces.Items.Add(new ListViewItem(new[]{App.Text(row,"label"),Bytes(Number(row,"bytes"))}));}
            loading=true;try {if(value.ContainsKey("automatic"))automatic.Checked=App.Flag(value,"automatic");}finally{loading=false;}
            status.Text=App.Text(value,"detail");
            if(phase=="ready")status.Text="可回收约 "+Bytes(Number(value,"reclaimable_bytes"))+" · 文件系统可用 "+Bytes(Number(value,"free_bytes"));
            if(phase=="completed")status.Text="已清理 "+Number(value,"completed_items")+" 项 · 文件系统增加 "+Bytes(Number(value,"filesystem_reclaimed_bytes"))+" 可用空间。\nWSL 虚拟磁盘可能仍需压缩，Windows 可用空间以系统显示为准。";
            clean.Enabled=!pending&&phase=="ready"&&value.TryGetValue("items",out rows)&&rows is IList&&((IList)rows).Count>0;
            var text=new StringBuilder();foreach(string key in new[]{"warnings","skipped"})if(value.TryGetValue(key,out rows)&&rows is IList)foreach(object row in (IList)rows){var item=row as Dictionary<string,object>;text.AppendLine(item==null?Convert.ToString(row):App.Text(item,"job_id")+" · "+App.Text(item,"detail"));}
            details.Text=text.ToString();scan.Enabled=automatic.Enabled=!pending;
        }
        async Task Send(string action,string[] options=null) {
            if(busy)return;busy=true;scan.Enabled=clean.Enabled=automatic.Enabled=false;UseWaitCursor=true;
            operationRunning=action=="storage-scan"||action=="storage-clean";cancel.Visible=operationRunning;
            try {var value=await command(action,options??new string[0]);if(!IsDisposed)Display(value);}
            catch(Exception ex){if(!IsDisposed){status.Text=ex.Message;scan.Enabled=automatic.Enabled=true;pending=false;}}
            finally {busy=false;operationRunning=false;if(!IsDisposed){UseWaitCursor=false;cancel.Visible=pending;}}
        }
        async Task WindowsAction(Func<Task<string>> action) {
            if(busy||pending)return;busy=true;scan.Enabled=clean.Enabled=automatic.Enabled=compact.Enabled=backups.Enabled=false;
            status.Text="正在回收空间…";UseWaitCursor=true;
            try {status.Text=await action();}catch(Exception ex){status.Text=ex.Message;}
            finally {busy=false;UseWaitCursor=false;scan.Enabled=automatic.Enabled=compact.Enabled=backups.Enabled=true;}
        }
    }
}
