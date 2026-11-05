using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    internal sealed class ComputePauseForm : Form {
        readonly RadioButton[] modes=new RadioButton[3];
        readonly ComboBox duration;
        readonly DateTimePicker until;
        readonly Label preview,feedback;
        readonly Button apply,restore;
        readonly CheckBox ac;
        readonly Func<string[],Task<Dictionary<string,object>>> command;
        bool powerChanged;

        internal ComputePauseForm(Dictionary<string,object> policy,Dictionary<string,object> status,
                                  Func<string[],Task<Dictionary<string,object>>> command) {
            this.command=command;Text="Worker · 暂停接单";Size=new Size(560,640);MinimumSize=new Size(520,560);
            StartPosition=FormStartPosition.CenterParent;AutoScaleMode=AutoScaleMode.Dpi;Font=new Font("Microsoft YaHei UI",9);BackColor=Color.White;
            var panel=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,Padding=new Padding(20),AutoScroll=true};
            panel.Controls.Add(Copy("当前："+DesktopRuntime.ComputeState(status),false));
            panel.Controls.Add(Copy("暂停方式",true));
            string[] titles={"只暂停接单","停止当前实验","释放训练资源"};
            string[] descriptions={"当前实验继续运行。","保存并停止，之后手动续训。","停止并释放训练占用；可续训任务可转移。"};
            for(int i=0;i<modes.Length;i++) {
                modes[i]=new RadioButton {Text=titles[i]+"\n"+descriptions[i],AutoSize=false,Width=470,Height=58,Margin=new Padding(0,4,0,4)};
                modes[i].CheckedChanged+=(s,e)=>UpdatePreview();panel.Controls.Add(modes[i]);
            }
            panel.Controls.Add(Copy("恢复接单",true));
            duration=new ComboBox {DropDownStyle=ComboBoxStyle.DropDownList,Width=450};
            duration.Items.AddRange(new object[]{"由我手动恢复","1 小时后恢复接单","2 小时后恢复接单","4 小时后恢复接单","指定恢复时间"});duration.SelectedIndex=0;
            until=new DateTimePicker {Format=DateTimePickerFormat.Custom,CustomFormat="yyyy-MM-dd HH:mm",Value=DateTime.Now.AddHours(2),MaxDate=DateTime.Now.AddYears(1),Width=450,Visible=false};
            duration.SelectedIndexChanged+=(s,e)=>UpdatePreview();panel.Controls.Add(duration);panel.Controls.Add(until);
            preview=Copy("",false);panel.Controls.Add(preview);
            var advanced=new Button {Text="电源条件…",AutoSize=true,FlatStyle=FlatStyle.Flat};
            ac=new CheckBox {Text="仅接通电源时提供算力",Checked=App.Flag(policy,"require_ac"),AutoSize=true,Visible=false};
            ac.CheckedChanged+=(s,e)=>powerChanged=true;advanced.Click+=(s,e)=>{ac.Visible=!ac.Visible;};panel.Controls.Add(advanced);panel.Controls.Add(ac);
            var actions=new FlowLayoutPanel {AutoSize=true,Width=470,WrapContents=true,Margin=new Padding(0,14,0,0)};
            apply=new Button {AutoSize=true,MinimumSize=new Size(160,36)};
            restore=new Button {Text="恢复接单",AutoSize=true,MinimumSize=new Size(120,36)};
            var close=new Button {Text="关闭",AutoSize=true,MinimumSize=new Size(70,36)};close.Click+=(s,e)=>Close();CancelButton=close;
            apply.Click+=async(s,e)=>await Save(false);restore.Click+=async(s,e)=>await Save(true);
            WorkerUi.Button(apply,true);WorkerUi.Button(restore);WorkerUi.Button(close);
            actions.Controls.Add(apply);actions.Controls.Add(restore);actions.Controls.Add(close);panel.Controls.Add(actions);
            feedback=Copy("恢复接单不等于续训；主控限制仍然有效。",false);panel.Controls.Add(feedback);Controls.Add(panel);
            string mode=App.Text(policy,"mode");object expiry;
            bool expired=policy.TryGetValue("until",out expiry)&&expiry!=null&&Convert.ToDouble(expiry,CultureInfo.InvariantCulture)<=(DateTime.UtcNow-new DateTime(1970,1,1)).TotalSeconds;
            modes[!expired&&mode=="release"?2:!expired&&mode=="stop"?1:0].Checked=true;
            UpdatePreview();
        }
        static Label Copy(string text,bool heading) {
            return new Label {Text=text,AutoSize=true,MaximumSize=new Size(470,0),Margin=new Padding(0,heading?12:5,0,8),
                              Font=new Font("Microsoft YaHei UI",9,heading?FontStyle.Bold:FontStyle.Regular)};
        }
        int Selected {get{return modes[2].Checked?2:modes[1].Checked?1:0;}}
        void UpdatePreview() {
            if(duration==null||preview==null||apply==null)return;
            until.Visible=duration.SelectedIndex==4;
            apply.Text=Selected==2?"停止并释放训练资源":Selected==1?"保存并停止实验":"确认暂停接单";
            preview.Text=(Selected==2?"运行环境仍保留；完全释放请用主窗口“停用并释放”。":Selected==1?"实验停止后保留在本机，等待手动续训。":"当前实验继续，新任务暂停。")+
                (duration.SelectedIndex==0?" 直到你手动恢复。":" 到期只恢复接单，已停止的实验仍需手动续训。");
            if(feedback!=null)feedback.Text="恢复接单不等于续训；主控限制仍然有效。";
        }
        async Task Save(bool resume) {
            apply.Enabled=restore.Enabled=false;
            try {
                var options=new List<string>{"--compute-mode",resume?"available":Selected==2?"release":Selected==1?"stop":"drain"};
                if(!resume&&duration.SelectedIndex>0) {
                    double minutes=duration.SelectedIndex==4?(until.Value-DateTime.Now).TotalMinutes:duration.SelectedIndex==3?240:duration.SelectedIndex==2?120:60;
                    if(minutes<=0){feedback.Text="请选择晚于现在的恢复时间。";return;}
                    options.Add("--minutes");options.Add(minutes.ToString(CultureInfo.InvariantCulture));
                }
                if(powerChanged)options.Add(ac.Checked?"--require-ac":"--allow-battery");
                var result=await command(options.ToArray());
                if(App.Text(result,"status")=="failed"||result.ContainsKey("error"))feedback.Text=App.Text(result,"error",App.Text(result,"detail","请求未成功，请重试。"));
                else feedback.Text=resume?"已请求恢复接单，等待主控确认。":"请求已发送，可在概览查看执行状态。";
            } catch(Exception error){feedback.Text="未确认成功："+error.Message;} finally {apply.Enabled=restore.Enabled=true;}
        }
    }
}
