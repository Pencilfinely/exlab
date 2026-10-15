/* Shared Center/Monitor autonomous-manager controls. All output uses textContent. */
(function(root){
  'use strict';
  const labels={queue:'队列优先级 / 暂缓',handoff:'续训交接',resume:'恢复可续训任务',availability:'节点可用策略',node_mode:'暂停接单',pause:'暂停实验',cancel:'取消实验',resources:'资源预算',submit:'提交固定实验',matrix_start:'启动已有矩阵',project_deploy:'部署已有项目'};
  const id=()=>root.crypto?.randomUUID?.()||'ui-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  function mount(ctx){
    const {node,api,host}=ctx;if(!host)return {render(){}};
    let current=null,loading=false,last=0,dirty=false,delegationIntent=null;
    const box=node('section',undefined,'card'),feedback=node('p','','inline-feedback'),state=node('p','','muted');
    const form=node('form'),enabled=node('input'),toggle=node('label',undefined,'checkbox-label');enabled.type='checkbox';
    toggle.append(enabled,node('span','自动化介入 · AI 托管'));form.append(toggle);
    const objective=node('textarea');objective.rows=3;objective.maxLength=8000;objective.setAttribute('aria-label','托管目标');objective.oninput=()=>dirty=true;
    const advanced=node('details'),settings=node('div',undefined,'form-grid'),fields={};advanced.append(node('summary','目标、调用频率与可执行操作'),objective,settings);
    for(const [key,label,min,max] of [['min_interval_seconds','最短调用间隔（秒）',10,86400],['review_seconds','活跃任务复查间隔（秒）',60,86400],['max_turns','每轮模型调用上限',1,20],['max_actions','每轮操作上限',1,50],['max_calls_per_hour','每小时调用上限',1,120]]){
      const wrap=node('label',label),input=node('input');input.type='number';input.min=min;input.max=max;input.required=true;input.oninput=()=>dirty=true;fields[key]=input;wrap.append(input);settings.append(wrap);
    }
    const permissions=node('div',undefined,'form-grid'),checks={};
    for(const [name,label] of Object.entries(labels)){const wrap=node('label',undefined,'checkbox-label'),input=node('input');input.type='checkbox';input.onchange=()=>dirty=true;checks[name]=input;wrap.append(input,node('span',label));permissions.append(wrap);}
    const save=node('button','保存托管设置');advanced.append(permissions,save);form.append(advanced);
    const delegate=node('form'),instructions=node('textarea'),send=node('button','委托系统 AI 处理');instructions.rows=2;instructions.maxLength=8000;instructions.required=true;instructions.setAttribute('aria-label','一次性委托目标');instructions.placeholder='例如：检查所有节点，优先推进今晚能完成的实验，并核验结果回传。';delegate.append(instructions,send);
    instructions.oninput=()=>{delegationIntent=null;};
    const runs=node('div'),detail=node('pre');detail.style.maxHeight='28rem';detail.style.overflow='auto';
    box.append(node('h2','AI 自主规划与检查'),state,form,node('p','开启后由状态变化和复查周期按需调用模型，自动执行允许的操作并追踪节点反馈。离电、手动释放、维护与检查点校验继续生效。','muted'),delegate,feedback,runs,detail);host.append(box);
    function writable(){return ctx.canControl?ctx.canControl():true;}
    function paint(value){
      current=value;const c=value.settings;
      state.textContent=(c.enabled?'托管已开启':'托管未开启')+' · '+(value.blocked_reason|| (value.active_run?'正在自主检查':'等待状态变化'))+` · 最近一小时 ${value.calls_last_hour}/${c.max_calls_per_hour} 次调用`;
      if(!dirty){enabled.checked=c.enabled;objective.value=c.instructions;for(const [key,input] of Object.entries(fields))input.value=c[key];for(const [key,input] of Object.entries(checks))input.checked=c.operations.includes(key);}
      const allowed=writable();for(const input of [enabled,objective,save,instructions,send,...Object.values(fields),...Object.values(checks)])input.disabled=!allowed;
      enabled.disabled=!allowed||(!value.provider.enabled&&!c.enabled);send.disabled=!allowed||!value.provider.enabled;
      runs.replaceChildren();
      for(const run of (value.runs||[]).slice(0,10)){const row=node('div',undefined,'actions'),view=node('button',`${new Date(run.created*1000).toLocaleString()} · ${run.trigger==='automatic'?'自动巡检':'委托'} · ${run.state}`,'subtle');view.type='button';
        view.onclick=async()=>{try{detail.textContent=JSON.stringify(await api('/api/agent/run?id='+encodeURIComponent(run.id)),null,2);}catch(e){feedback.textContent=e.message;}};row.append(view);
        if(['queued','running'].includes(run.state)&&allowed){const stop=node('button','中止本轮','subtle');stop.type='button';stop.onclick=async()=>{try{await api('/api/agent/interrupt',{run_id:run.id});await poll(true);}catch(e){feedback.textContent=e.message;}};row.append(stop);}
        if(run.summary||run.error)row.append(node('p',(run.error||run.summary).slice(0,600),'muted'));runs.append(row);
      }
    }
    async function poll(force=false){if(loading||(!force&&Date.now()-last<4000))return;loading=true;last=Date.now();try{paint(await api('/api/agent/status'));}catch(e){feedback.textContent=e.message;}finally{loading=false;}}
    async function update(payload){if(!current||!writable())return;enabled.disabled=true;save.disabled=true;try{await api('/api/agent/settings',{revision:current.settings.revision,...payload});if(Object.keys(payload).length>1)dirty=false;feedback.textContent=payload.enabled===false?'自动化介入已关闭；后续操作将停止。':'托管设置已保存，将自动按需介入。';await poll(true);}catch(e){feedback.textContent=e.message;enabled.checked=current.settings.enabled;}finally{enabled.disabled=!writable()||(!current.provider.enabled&&!current.settings.enabled);save.disabled=!writable();}}
    enabled.onchange=()=>void update({enabled:enabled.checked});
    form.onsubmit=event=>{event.preventDefault();if(!form.reportValidity())return;void update({enabled:enabled.checked,instructions:objective.value,...Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,Number(v.value)])),operations:Object.keys(checks).filter(k=>checks[k].checked)});};
    delegate.onsubmit=async event=>{event.preventDefault();if(send.disabled||!writable()||!delegate.reportValidity())return;send.disabled=true;const intent=delegationIntent||{instructions:instructions.value,request_id:id()};delegationIntent=intent;try{const reply=await api('/api/agent/delegate',intent);feedback.textContent='已加入自主处理队列：'+reply.run_id;if(delegationIntent===intent){instructions.value='';delegationIntent=null;}await poll(true);}catch(e){feedback.textContent=e.message;}finally{send.disabled=!writable();}};
    if(!ctx.mobile){
      const external=node('details'),credentials=node('div'),credentialForm=node('form'),name=node('input'),days=node('input'),scopeBox=node('div',undefined,'actions'),scopes={},result=node('pre');name.placeholder='例如 Codex / GPT';name.required=true;name.maxLength=80;days.type='number';days.min=0;days.max=365;days.value=30;days.setAttribute('aria-label','有效天数，0 表示长期');
      for(const [key,label] of [['read','查看'],['control','直接操作'],['delegate','委托系统 AI'],['automation','管理托管设置']]){const wrap=node('label',undefined,'checkbox-label'),input=node('input');input.type='checkbox';input.checked=['read','control','delegate'].includes(key);input.disabled=key==='read';scopes[key]=input;wrap.append(input,node('span',label));scopeBox.append(wrap);}
      const create=node('button','生成独立 agent 凭证'),reload=node('button','刷新凭证列表','subtle');reload.type='button';credentialForm.append(name,days,scopeBox,create);external.append(node('summary','外部 GPT / Codex 接入'),node('p','MCP 地址：'+location.origin+'/mcp；也可使用 HTTP 工具接口。凭证按范围授权，可随时撤销。'),credentialForm,reload,result,credentials);box.append(external);
      async function list(){try{const values=await api('/api/agent/credentials');credentials.replaceChildren();for(const item of values.credentials){const row=node('div',undefined,'actions');row.append(node('span',item.name+' · '+item.scopes.join(', ')+' · '+(item.revoked?'已撤销':item.expires?new Date(item.expires*1000).toLocaleString():'长期有效')));if(!item.revoked){const revoke=node('button','撤销','subtle');revoke.type='button';revoke.onclick=async()=>{try{await api('/api/agent/credentials/revoke',{id:item.id});result.textContent='';await list();}catch(e){feedback.textContent=e.message;}};row.append(revoke);}credentials.append(row);}}catch(e){feedback.textContent=e.message;}}
      reload.onclick=()=>void list();credentialForm.onsubmit=async event=>{event.preventDefault();if(!credentialForm.reportValidity())return;create.disabled=true;try{const reply=await api('/api/agent/credentials',{name:name.value,days:Number(days.value),scopes:Object.keys(scopes).filter(k=>scopes[k].checked)});result.textContent='仅本次显示，请保存到外部 agent 的环境变量 EXLAB_AGENT_TOKEN：\n'+reply.credential;await list();}catch(e){feedback.textContent=e.message;}finally{create.disabled=false;}};
    }
    return {render(){void poll();},poll};
  }
  root.ExLabAutonomy={mount};
})(globalThis);
