/* Availability, execution control and planning never write final result envelopes. */
(function(root){
  'use strict';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const defaults=()=>({revision:0,kind:'shared',mode:'available',until:null,reason:'',require_ac:false,reliability:1,maintenance:[],risk_windows:[]});
  const modeLabels={available:'允许接单',drain:'暂停接单',stop:'停止实验并暂停接单',release:'停止实验、暂停接单并释放训练资源'};
  const phaseLabels={available:'允许接单',admission_paused:'已暂停接单 · 实验继续',stopping:'正在保存并停止实验',stopped:'实验已停止 · 接单已暂停',releasing:'正在释放训练资源',released:'训练资源已释放 · 接单已暂停',blocked:'执行未完成 · 需要检查'};
  function nodeState(worker){
    const snap=worker.snapshot||{},availability=snap.availability||{},control=snap.compute_control;
    const mode=availability.mode||'available',pending=worker.compute_policy&&worker.compute_policy.revision!==(snap.compute_policy_revision||0);
    const matching=control&&control.policy&&['mode','revision','source','until','reason'].every(key=>(control.policy[key]??null)===(availability[key]??null));
    if(pending)return {label:'策略待 Worker 接收',detail:'已保存的策略尚未被 Worker 接收，不能据此确认实验停止或资源释放。'};
    if(worker.online===false||worker.last_seen!==undefined&&Date.now()/1000-worker.last_seen>=45)
      return {label:'离线 · 当前执行状态未知',detail:matching?'上次回报：'+phaseLabels[control.phase]+'。'+control.detail:'等待 Worker 连接并核验；离线不能视为已释放。'};
    if(matching){
      if(control.phase==='released'&&(!control.resources_released||!control.experiments_stopped)||control.phase==='stopped'&&!control.experiments_stopped)
        return {label:'等待 Worker 核验',detail:'停止或资源释放确认不完整。'};
      return {label:worker.mode==='drain'&&mode==='available'?'已暂停接单':phaseLabels[control.phase]||'等待核验',detail:control.detail};
    }
    if(mode==='stop'||mode==='release')return {label:'等待 Worker 核验',detail:'已请求'+modeLabels[mode]+'；尚未收到匹配的执行结果。'+((snap.capabilities||[]).includes('compute-control-v2')?'':'此 Worker 需要升级后才能回传停止与释放核验。')};
    return {label:mode==='drain'||worker.mode==='drain'?'已暂停接单 · 实验继续':'允许接单',detail:availability.reason||''};
  }
  const localTime=seconds=>{if(!seconds)return '';const d=new Date(seconds*1000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
  const readTime=(input,original)=>input.value===localTime(original)?(original??null):input.value?new Date(input.value).getTime()/1000:null;
  function field(node,parent,label,value,type='text'){
    const box=node('label',label),input=node('input');input.type=type;input.value=value??'';box.append(input);parent.append(box);return input;
  }
  function select(node,parent,label,choices,value){
    const box=node('label',label),input=node('select');
    for(const [id,text] of choices){const option=node('option',text);option.value=id;input.append(option);}
    input.value=value;box.append(input);parent.append(box);return input;
  }
  function policyEditor(worker,ctx,preset){
    const {node,api,refresh,notify}=ctx,original={...defaults(),...clone(worker.compute_policy||{})};
    const expired=original.until!==null&&original.until<=Date.now()/1000,editingUntil=preset?Date.now()/1000+7200:expired?null:original.until;
    const dialog=node('dialog',undefined,'workspace-dialog'),form=node('form'),feedback=node('p','','inline-feedback');form.append(node('h2',`${worker.display_name||worker.id} · 可用策略`));
    const controls=node('div',undefined,'form-grid');form.append(controls);
    const kind=select(node,controls,'节点类型',[['stable','常开节点'],['shared','个人主机'],['portable','便携设备']],original.kind);
    const confirmedControl=(worker.snapshot?.capabilities||[]).includes('compute-control-v2');
    const mode=select(node,controls,'本机实验与资源',Object.entries(modeLabels),preset?'release':expired?'available':original.mode);
    for(const option of mode.children)if(['stop','release'].includes(option.value))option.disabled=!confirmedControl;
    const until=field(node,controls,'策略结束时间（留空则手动恢复）',localTime(editingUntil),'datetime-local');
    const reason=field(node,controls,'原因',preset?'游戏':original.reason);reason.maxLength=300;
    const acLabel=node('label',undefined,'checkbox-label'),ac=node('input');ac.type='checkbox';ac.checked=original.require_ac;acLabel.append(ac,node('span','仅在接通电源时提供算力（供电未知时暂停）'));form.append(acLabel);
    const reliability=field(node,controls,'通常可连续运行的可靠度（0–100%，初始估计）',original.reliability*100,'number');reliability.min=0;reliability.max=100;reliability.step=1;
    const maintenance=node('div'),windows=[];form.append(node('h3','维护时间'),maintenance);
    function addWindow(value={}){
      const row=node('div',undefined,'form-grid'),start=field(node,row,'开始',localTime(value.start),'datetime-local'),end=field(node,row,'结束',localTime(value.end),'datetime-local'),why=field(node,row,'维护说明',value.reason||'维护');
      const remove=node('button','移除','subtle');remove.type='button';const entry={row,start,end,why,original:value};remove.onclick=()=>{row.remove();windows.splice(windows.indexOf(entry),1);};row.append(remove);windows.push(entry);maintenance.append(row);
    }
    for(const window of original.maintenance)addWindow(window);
    const add=node('button','添加维护时间','subtle');add.type='button';add.onclick=()=>addWindow();form.append(add);
    const risks=node('div'),riskRows=[];form.append(node('h3','每日中断风险'),risks);
    function addRisk(value={window:'22:00-08:00',risk:0.5}){
      const row=node('div',undefined,'form-grid'),window=field(node,row,'风险时段（HH:MM-HH:MM）',value.window),risk=field(node,row,'中断风险（%）',value.risk*100,'number');risk.min=0;risk.max=100;
      const remove=node('button','移除','subtle');remove.type='button';const entry={row,window,risk};remove.onclick=()=>{row.remove();riskRows.splice(riskRows.indexOf(entry),1);};row.append(remove);riskRows.push(entry);risks.append(row);
    }
    for(const risk of original.risk_windows)addRisk(risk);
    const addRiskButton=node('button','添加风险时段','subtle');addRiskButton.type='button';addRiskButton.onclick=()=>addRisk();form.append(addRiskButton);
    const semantics=node('p','','muted');
    mode.onchange=()=>{semantics.textContent=({available:'恢复接单不会自动续训手动停止的实验。',drain:'只暂停接单，当前实验继续运行并占用资源。',stop:'请求保存并停止实验，暂停接单；任务和容器环境留在本机，等待显式续训。',release:'请求保存并停止实验，暂停接单；可交接任务归还队列，清理已停止的实验容器，核验训练进程退出。控制代理及 Docker/WSL 保持在线；完全释放虚拟机内存使用 Windows Worker 的“停用并释放 Docker / WSL”。'})[mode.value];};
    mode.onchange();form.append(semantics,node('p','停止会先请求保存；超过停止宽限时间后结束训练进程。没有续训入口的实验会中断；确认停止及检查点回传后才交接。','muted'));
    if(!confirmedControl)form.append(node('p','此 Worker 不能回传停止与释放核验结果，请先升级后使用这两档操作。','inline-feedback'));
    const save=node('button','保存策略'),close=node('button','关闭','subtle'),actions=node('div',undefined,'actions');close.type='button';close.onclick=()=>dialog.close();actions.append(save,close);form.append(actions,feedback);
    let revision=original.revision;
    form.onsubmit=async event=>{
      event.preventDefault();if(save.disabled||!form.reportValidity())return;
      if(!confirmedControl&&['stop','release'].includes(mode.value)){feedback.textContent='请先升级 Worker，当前版本不能核验停止与释放。';return;}
      const policy={revision,kind:kind.value,mode:mode.value,until:readTime(until,editingUntil),reason:reason.value,require_ac:ac.checked,reliability:Number(reliability.value)/100,
        maintenance:windows.map(x=>({start:readTime(x.start,x.original.start),end:readTime(x.end,x.original.end),reason:x.why.value})),risk_windows:riskRows.map(x=>({window:x.window.value,risk:Number(x.risk.value)/100}))};
      if(policy.until!==null&&policy.until<=Date.now()/1000){feedback.textContent='结束时间应晚于现在。';return;}
      save.disabled=true;
      try{const result=await api('/api/compute-policy',{node_id:worker.id,policy});revision=result.policy.revision;feedback.textContent='请求已保存，等待 Worker 执行并核验；可关闭窗口后在节点状态中查看。';await refresh();notify('请求已保存，执行进度以 Worker 回传为准。');}
      catch(error){feedback.textContent='保存未确认：'+error.message+'；输入已保留，可原样重试。';}
      finally{save.disabled=false;}
    };
    dialog.append(form);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();
  }
  function renderNode(box,worker,ctx){
    const {node}=ctx,snap=worker.snapshot||{},supported=(snap.capabilities||[]).includes('dynamic-compute-v1'),availability=snap.availability;
    const state=nodeState(worker);
    if(availability)box.append(node('p',`${state.label}${availability.until?' · 截止 '+new Date(availability.until*1000).toLocaleString():''}`,'resource-sync'),node('p',state.detail,'muted'));
    if(worker.compute_policy&&worker.compute_policy.revision!==(snap.compute_policy_revision||0))box.append(node('p','可用策略待 Worker 确认','muted'));
    const button=node('button',supported?'可用策略 / 维护':'升级 Worker 后设置可用策略','subtle');button.disabled=!supported;button.onclick=()=>policyEditor(worker,ctx,false);box.append(button);
    if(supported){const release=node('button','游戏 · 释放训练资源','subtle');release.disabled=!(snap.capabilities||[]).includes('compute-control-v2');release.title=release.disabled?'先升级 Worker 以获得资源释放核验':'预填两小时释放，可在提交前修改';release.onclick=()=>policyEditor(worker,ctx,true);box.append(release);}
    const delivery=snap.execution_delivery;
    if(delivery){box.append(node('p',`续训检查点待传 ${delivery.pending_checkpoints||0} · 最近备份 ${delivery.last_checkpoint_received_at?new Date(delivery.last_checkpoint_received_at*1000).toLocaleString():'暂无'}`,'muted'));if(delivery.last_error)box.append(node('p','执行资料回传：'+delivery.last_error,'muted'));}
  }
  function renderExecution(job,ctx){
    const {node,$,api,notify,refresh,workers}=ctx;let panel=$('execution-control-panel');
    if(!panel){panel=node('section');panel.id='execution-control-panel';$('detail-state').after(panel);}
    panel.replaceChildren(node('h3','执行状态与续训'));const state=job.execution_state;
    panel.append(node('p',`执行代次 ${job.generation||1} · ${state?.stopped_confirmed?'已确认进程停止':state?.ever_started?'已开始执行':'尚未确认执行状态'}`));
    const checkpoint=state?.checkpoint,backedUp=(job.execution_checkpoints||[]).find(x=>x.manifest.sha256===checkpoint?.sha256&&x.attempt===job.attempt);
    panel.append(node('p',checkpoint?`检查点 step ${checkpoint.step} · ${(checkpoint.size/1048576).toFixed(1)} MiB · ${backedUp?'已在中心校验备份':'等待独立传输'}`:'尚无可用续训检查点','muted'));
    const status={requested:'等待原节点保存并停止',waiting_checkpoint:'等待完整续训入口和已备份检查点',waiting_capacity:'原进程已停止，等待兼容节点接手',assigned:'已交接至目标节点',finished:'实验已结束',canceled:'交接已取消'};
    if(job.handoff)panel.append(node('p',status[job.handoff.status]||job.handoff.status));
    panel.append(node('p','此处用于调度、交接和续训；最终结果由下方结果回传独立记录。','muted'));
    if(job.state==='queued'&&!job.node_id){
      const controls=job.queue_control||{priority:job.spec.priority,hold:false,revision:0},form=node('form'),fields=node('div',undefined,'form-grid');form.append(fields);
      const priority=field(node,fields,'队列优先级',controls.priority,'number');priority.min=-100;priority.max=100;priority.step=1;
      const label=node('label',undefined,'checkbox-label'),hold=node('input');hold.type='checkbox';hold.checked=controls.hold;label.append(hold,node('span','暂缓分配'));form.append(label);
      const save=node('button','更新排队策略','subtle');form.append(save);let revision=controls.revision;
      form.onsubmit=async event=>{event.preventDefault();if(save.disabled||!form.reportValidity())return;save.disabled=true;try{const result=await api('/api/planning/queue',{job_id:job.id,priority:Number(priority.value),hold:hold.checked,revision});revision=result.revision;notify('排队策略已保存。');await refresh();}catch(error){notify(error.message);save.disabled=false;}};panel.append(form);
    }
    const owner=(workers||[]).find(x=>x.id===job.node_id),capable=(owner?.snapshot?.capabilities||[]).includes('execution-state-v1');
    if(capable&&job.spec.result_delivery?.mode==='lightweight'&&job.spec.planning?.migrate!==false&&!['succeeded','canceled'].includes(job.state)){
      const target=select(node,panel,'接手节点',[['','自动选择其他兼容节点'],...(workers||[]).filter(x=>x.id!==job.node_id).map(x=>[x.id,x.display_name||x.id])],'');
      const button=node('button','保存并交接','subtle');button.disabled=['requested','waiting_checkpoint','waiting_capacity'].includes(job.handoff?.status);
      button.onclick=async()=>{button.disabled=true;try{await api('/api/execution/handoff',{job_id:job.id,generation:job.generation||1,target_node_id:target.value||null});notify('交接请求已保存；等待原节点确认停止并完成检查点传输。');await refresh();}catch(error){notify(error.message);button.disabled=false;}};panel.append(button);
    }
  }
  function renderPlanning(container,planning,node){
    const details=node('details');details.append(node('summary','运行时间与交接策略'));const fields=node('div',undefined,'form-grid');details.append(fields);
    const expected=field(node,fields,'预计运行分钟数（可留空）',planning?.expected_seconds!=null?planning.expected_seconds/60:'','number');expected.min=0;expected.step='any';expected.dataset.control='expected';
    const deadline=field(node,fields,'期望完成时间（可留空）',localTime(planning?.deadline),'datetime-local');deadline.dataset.control='deadline';
    const label=node('label',undefined,'checkbox-label'),migrate=node('input');migrate.type='checkbox';migrate.checked=planning?.migrate!==false;migrate.dataset.control='migrate';label.append(migrate,node('span','允许停止确认后跨节点接手（需完整续训能力）'));details.append(label);details.planningOriginal=planning?clone(planning):null;details.dataset.planning='true';container.append(details);
  }
  function readPlanning(container){
    const details=container.querySelector('[data-planning]');if(!details)return undefined;
    const get=name=>details.querySelector('[data-control="'+name+'"]'),expected=get('expected').value,deadline=get('deadline').value,migrate=get('migrate').checked;
    if(!expected&&!deadline&&migrate&&!details.planningOriginal)return undefined;
    if(expected&&(!Number.isFinite(Number(expected))||Number(expected)<0))throw new Error('预计运行时间应为非负数。');
    return {expected_seconds:expected?Number(expected)*60:null,deadline:readTime(get('deadline'),details.planningOriginal?.deadline),migrate};
  }
  function openAssistant(ctx){
    const {node,api,notify,refresh}=ctx,dialog=node('dialog',undefined,'workspace-dialog'),form=node('form'),instructions=node('textarea'),generate=node('button','生成规划建议'),feedback=node('p'),output=node('div'),close=node('button','关闭','subtle');
    instructions.rows=4;instructions.required=true;instructions.maxLength=8000;instructions.placeholder='例如：今晚优先跑完对比实验，ROG 随时可能带走，2080 Ti 夜间可能断电。';
    form.append(node('h2','AI 规划建议'),node('p','点击生成后，将发送你的要求、节点可用策略和未分配任务的资源摘要。建议逐项检查后应用；源码、数据集与训练日志不随请求发送。','muted'),instructions,generate,feedback,output);
    close.type='button';close.onclick=()=>dialog.close();form.append(close);
    form.onsubmit=async event=>{
      event.preventDefault();if(generate.disabled||!form.reportValidity())return;generate.disabled=true;output.replaceChildren();feedback.textContent='正在整理建议…';
      try{
        const result=await api('/api/planning/assist',{instructions:instructions.value});feedback.textContent=result.summary;for(const warning of result.warnings||[])output.append(node('p',warning,'muted'));
        for(const suggestion of result.node_policies||[]){
          const box=node('section'),apply=node('button','应用此节点建议','subtle');apply.type='button';box.append(node('h3',suggestion.node_id),node('pre',JSON.stringify({当前:suggestion.previous,建议:suggestion.policy},null,2)),apply);
          apply.onclick=async()=>{apply.disabled=true;try{await api('/api/compute-policy',{node_id:suggestion.node_id,policy:suggestion.policy});apply.textContent='已应用';await refresh();}catch(error){notify(error.message);apply.disabled=false;}};output.append(box);
        }
        for(const suggestion of result.queue_priorities||[]){
          const box=node('section'),apply=node('button','应用此优先级建议','subtle');apply.type='button';box.append(node('p',`${suggestion.job_id}：优先级 ${suggestion.previous_priority} → ${suggestion.priority}`),apply);
          apply.onclick=async()=>{apply.disabled=true;try{const {previous_priority,...payload}=suggestion;await api('/api/planning/queue',payload);apply.textContent='已应用';await refresh();}catch(error){notify(error.message);apply.disabled=false;}};output.append(box);
        }
      }catch(error){feedback.textContent='未生成建议：'+error.message;}finally{generate.disabled=false;}
    };
    dialog.append(form);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();
  }
  function mount(ctx){
    const button=ctx.node('button','AI 规划建议','subtle');button.type='button';button.onclick=()=>openAssistant(ctx);ctx.$('nodes').before(button);
  }
  root.ExLabCompute={renderNode,renderExecution,renderPlanning,readPlanning,policyEditor,defaults,mount,nodeState};
})(globalThis);
