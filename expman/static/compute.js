/* Availability, execution control and planning never write final result envelopes. */
(function(root){
  'use strict';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const defaults=()=>({revision:0,kind:'shared',mode:'available',until:null,reason:'',require_ac:false,reliability:1,maintenance:[],risk_windows:[]});
  const modeLabels={available:'允许接单',drain:'暂停接单',stop:'停止实验并暂停接单',release:'停止实验、暂停接单并释放资源'};
  const phaseLabels={available:'允许接单',admission_paused:'已暂停接单 · 实验继续',stopping:'正在保存并停止实验',stopped:'实验已停止 · 接单已暂停',releasing:'资源释放中 · 尚未完成',released:'资源已释放 · 接单已暂停',blocked:'资源释放未完成 · 需要检查'};
  function nodeState(worker){
    const snap=worker.snapshot||{},availability=snap.availability||{},control=snap.compute_control;
    const mode=availability.mode||'available',pending=worker.compute_policy&&worker.compute_policy.revision!==(snap.compute_policy_revision||0);
    const matching=control&&control.policy&&['mode','revision','source','until','reason'].every(key=>(control.policy[key]??null)===(availability[key]??null));
    const hostConfirmed=snap.host_control?.online&&snap.host_control.matches;
    if(hostConfirmed&&snap.host_control.phase==='restarting')return {label:'正在恢复运行环境',detail:snap.host_control.detail};
    if(pending&&!hostConfirmed)return {label:'策略待 Worker 接收',detail:'已保存的策略尚未被 Worker 接收，不能据此确认实验停止或资源释放。'};
    if(worker.online===false||!snap.host_control?.online&&worker.last_seen!==undefined&&Date.now()/1000-worker.last_seen>=45)
      return {label:'离线 · 当前执行状态未知',detail:matching?'上次回报：'+phaseLabels[control.phase]+'。'+control.detail:'等待 Worker 连接并核验；离线不能视为已释放。'};
    if(matching){
      if(control.phase==='released'&&control.runtime_retained)return {label:'训练已停止 · 运行环境仍保留',detail:control.detail};
      if(control.phase==='released'&&(!control.resources_released||!control.experiments_stopped)||control.phase==='stopped'&&!control.experiments_stopped)
        return {label:'等待 Worker 核验',detail:'停止或资源释放确认不完整。'};
      return {label:worker.mode==='drain'&&mode==='available'?'已暂停接单':phaseLabels[control.phase]||'等待核验',detail:control.detail};
    }
    if(mode==='stop'||mode==='release')return {label:'等待 Worker 核验',detail:'已请求'+modeLabels[mode]+'；尚未收到匹配的执行结果。'+((snap.capabilities||[]).includes('compute-control-v2')?'':'此 Worker 需要升级后才能回传停止与释放核验。')};
    if(snap.host_control?.online&&worker.worker_online===false)return {label:'控制在线 · 算力未启动',detail:'Windows 控制代理在线，等待运行环境及 Worker 恢复。'};
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
  function restrictions(worker){
    const snap=worker.snapshot||{},policies=[['本机',snap.local_compute_policy],['Center',worker.compute_policy]],notes=[],now=Date.now()/1000;
    for(const [source,policy] of policies){
      if(!policy)continue;
      if(source==='本机'&&policy.mode&&policy.mode!=='available'&&(policy.until==null||policy.until>now))notes.push('本机也设置了暂停，需要在该节点的“暂停 / 恢复”中恢复。');
      if(policy.require_ac&&snap.ac_power!==true)notes.push(source+'要求接通电源，当前'+(snap.ac_power===false?'未接电源。':'供电状态未知。'));
      for(const window of policy.maintenance||[])if(window.start<=now&&window.end>now)notes.push(source+'正处于维护时间，结束于 '+new Date(window.end*1000).toLocaleString()+'。');
    }
    return [...new Set(notes)];
  }
  function pauseEditor(worker,ctx,preset='pause'){
    const {node,api,refresh,notify}=ctx,snap=worker.snapshot||{},supported=(snap.capabilities||[]).includes('dynamic-compute-v1'),confirmed=(snap.capabilities||[]).includes('compute-control-v2');
    const original={...defaults(),...clone(worker.compute_policy||{})},resuming=preset==='available';
    const holds=restrictions(worker),centerPaused=worker.mode==='drain'||original.mode!=='available'&&(original.until==null||original.until>Date.now()/1000);
    const dialog=node('dialog',undefined,'workspace-dialog compute-dialog'),form=node('form'),feedback=node('p','','inline-feedback');
    form.append(node('h2',`${worker.display_name||worker.id} · ${resuming?'恢复接单':'暂停节点'}`),node('p','当前：'+nodeState(worker).label,'compute-current'));
    const options=[['drain','只暂停接单','当前实验继续运行，仍会占用资源。'],['stop','停止当前实验','保存并停止，保留本机任务和环境，之后手动续训。'],['release','停止并释放资源','保存并停止实验；Windows 同时关闭空闲 Docker / WSL，释放内存。可续训的任务交给其他节点。']];
    let selected=resuming?'available':preset==='release'?'release':original.until!=null&&original.until<=Date.now()/1000?'drain':['drain','stop','release'].includes(original.mode)?original.mode:'drain';
    if(!confirmed&&selected!=='available')selected='drain';
    const cards=[];
    if(!resuming){
      form.append(node('h3','1. 怎样暂停？'));
      const choices=node('div',undefined,'compute-choices');
      for(const [mode,title,description] of options){
        const card=node('label',undefined,'compute-choice'),input=node('input'),copy=node('span');input.type='radio';input.name='compute-pause-mode';input.value=mode;input.dataset.pauseMode=mode;input.disabled=mode!=='drain'&&!confirmed;
        copy.append(node('strong',title),node('small',description));card.append(input,copy);choices.append(card);cards.push({mode,card,input});
        input.onchange=()=>{selected=mode;update();};
      }
      form.append(choices,node('h3','2. 暂停多久？'));
    }else form.append(node('p',holds.length?(centerPaused?'这里仅解除管理端的手动暂停；以下条件仍会阻止接单。':'管理端没有手动暂停。请先处理下方限制。'):'恢复后可以分配新任务；已经停止的实验需要在实验详情中手动续训。'));
    const duration=resuming?null:select(node,form,'恢复接单时间',[['manual','由我手动恢复'],['60','1 小时后'],['120','2 小时后'],['240','4 小时后'],['custom','指定时间']],preset==='release'?'120':'manual');
    const custom=resuming?null:field(node,form,'选择恢复时间','','datetime-local');
    if(custom)custom.parentElement.hidden=true;
    if(duration){duration.disabled=!supported;duration.onchange=()=>{custom.parentElement.hidden=duration.value!=='custom';update();};}
    const summary=node('p','','compute-preview');form.append(summary);
    if(holds.length){const callout=node('div',undefined,'compute-holds');callout.append(node('strong','恢复接单还需满足'));for(const note of holds)callout.append(node('p',note));form.append(callout);}
    if(!confirmed&&!resuming)form.append(node('p','此节点需要升级 Worker 才能停止实验或核验资源释放；当前可以暂停接单。','muted'));
    const scope=node('p','Windows 需要新版客户端接管运行环境释放，完成后保留轻量控制连接，可在这里恢复。其他容器或会话会明确显示为阻塞；原生 Linux 释放训练容器并保留系统服务。','muted');form.append(scope);
    const actions=node('div',undefined,'actions compute-dialog-actions'),save=node('button'),close=node('button','取消','subtle');close.type='button';close.onclick=()=>dialog.close();actions.append(save,close);form.append(actions,feedback);
    function update(){
      for(const item of cards){item.input.checked=item.mode===selected;item.input.autofocus=item.input.checked;item.card.className='compute-choice'+(item.input.checked?' is-selected':'')+(item.input.disabled?' is-disabled':'');}
      const option=options.find(item=>item[0]===selected);
      save.textContent=resuming?(holds.length?(centerPaused?'解除管理端暂停':'请先满足上方条件'):'确认恢复接单'):{drain:'确认暂停接单',stop:'保存并停止实验',release:'停止并释放资源'}[selected];
      save.disabled=resuming&&holds.length>0&&!centerPaused;
      summary.textContent=resuming?'恢复此节点接收新任务，保留电源与维护限制。':option[2]+(duration.value==='manual'?' 直到你手动恢复。':duration.value==='custom'?' 在指定时间恢复接单。':` ${Number(duration.value)/60} 小时后恢复接单。`);
      summary.hidden=resuming&&holds.length>0;
      scope.hidden=selected!=='release';
    }
    update();
    let pending=null;
    form.onsubmit=async event=>{
      event.preventDefault();if(save.disabled||!form.reportValidity())return;
      if(!confirmed&&['stop','release'].includes(selected))return;
      const customTime=custom?.value?new Date(custom.value).getTime()/1000:null;
      if(duration?.value==='custom'&&(!Number.isFinite(customTime)||customTime<=Date.now()/1000)){feedback.textContent='请选择晚于现在的恢复时间。';return;}
      const signature=JSON.stringify([selected,duration?.value,custom?.value]);
      if(!pending||pending.signature!==signature){
        const until=resuming||duration.value==='manual'?null:duration.value==='custom'?customTime:Date.now()/1000+Number(duration.value)*60;
        pending={signature,payload:supported?{node_id:worker.id,expected_node_mode:worker.mode||'run',policy:{...original,mode:selected,until,reason:resuming?'':modeLabels[selected]}}:{node_id:worker.id,mode:resuming?'run':'drain',expected_mode:worker.mode||'run'}};
      }
      save.disabled=true;
      try{await api(supported?'/api/compute-policy':'/api/node-mode',pending.payload);dialog.close();notify(resuming?'恢复接单请求已发送；其他限制会继续显示在节点状态中。':'暂停请求已发送，执行进度请看节点状态。');await refresh();}
      catch(error){feedback.textContent='未确认成功：'+error.message+'。可原样重试。';}
      finally{save.disabled=false;}
    };
    dialog.append(form);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();
    return dialog;
  }
  function policyEditor(worker,ctx,preset){
    if(preset)return pauseEditor(worker,ctx,'release');
    const {node,api,refresh,notify}=ctx,original={...defaults(),...clone(worker.compute_policy||{})};
    const dialog=node('dialog',undefined,'workspace-dialog'),form=node('form'),feedback=node('p','','inline-feedback');form.append(node('h2',`${worker.display_name||worker.id} · 节点长期设置`),node('p','平时暂停或恢复请使用节点卡片上的“暂停 / 恢复”；这里设置电源条件、维护时间和运行特征。','muted'));
    const controls=node('div',undefined,'form-grid');form.append(controls);
    const kind=select(node,controls,'节点类型',[['stable','常开节点'],['shared','个人主机'],['portable','便携设备']],original.kind);
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
    const save=node('button','保存长期设置'),close=node('button','关闭','subtle'),actions=node('div',undefined,'actions');close.type='button';close.onclick=()=>dialog.close();actions.append(save,close);form.append(actions,feedback);
    let revision=original.revision;
    form.onsubmit=async event=>{
      event.preventDefault();if(save.disabled||!form.reportValidity())return;
      const policy={...original,revision,kind:kind.value,require_ac:ac.checked,reliability:Number(reliability.value)/100,
        maintenance:windows.map(x=>({start:readTime(x.start,x.original.start),end:readTime(x.end,x.original.end),reason:x.why.value})),risk_windows:riskRows.map(x=>({window:x.window.value,risk:Number(x.risk.value)/100}))};
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
    const actions=node('div',undefined,'actions compute-node-actions'),pause=node('button','暂停节点…'),resume=node('button','恢复接单','subtle');
    pause.dataset.control='true';resume.dataset.control='true';
    pause.onclick=()=>pauseEditor(worker,ctx);resume.onclick=()=>pauseEditor(worker,ctx,'available');actions.append(pause,resume);box.append(actions);
    for(const note of restrictions(worker))box.append(node('p',note,'compute-constraint'));
    const button=node('button','节点长期设置','subtle compute-settings');button.disabled=!supported;button.onclick=()=>policyEditor(worker,ctx,false);box.append(button);
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
  root.ExLabCompute={renderNode,renderExecution,renderPlanning,readPlanning,policyEditor,pauseEditor,restrictions,defaults,mount,nodeState};
})(globalThis);
