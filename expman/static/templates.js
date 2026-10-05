'use strict';

// Keep a copy of each template and each local draft while worker polling continues.
let nodeTemplateSession=null,nodeTemplateBusy=false;
function renderNodeTemplates(box,worker){
  const templates=Array.isArray(worker.snapshot?.task_templates)?worker.snapshot.task_templates:[];
  const button=node('button',`任务模板 · ${templates.length}`,'subtle');button.type='button';button.disabled=!templates.length;
  button.title=templates.length?'选择模板并填写实验参数':'节点尚未回报任务模板，请先部署算法项目';
  button.onclick=()=>openNodeTemplates(worker.id);box.append(button);
}
function openNodeTemplates(id){
  const worker=(state.nodes||[]).find(item=>item.id===id),templates=worker?.snapshot?.task_templates;
  if(!Array.isArray(templates)||!templates.length){notify('此节点尚无任务模板，请先部署算法项目。');return;}
  nodeTemplateSession={worker:id,templates:copy(templates),drafts:new Map(),index:null,pending:null};
  $('node-template-title').textContent=(worker.display_name||worker.id)+' · 从模板创建实验';
  $('node-template-search').value='';inlineFeedback('node-template-feedback','');$('node-template-preview-result').replaceChildren();
  selectNodeTemplate(0);$('node-template-dialog').showModal();
}
function renderNodeTemplateChoices(){
  const session=nodeTemplateSession;if(!session)return;
  const search=$('node-template-search').value.trim().toLowerCase(),container=$('node-template-list');container.replaceChildren();
  session.templates.forEach((template,index)=>{
    if(!JSON.stringify([template.name,template.algorithm,template.group,template.params]).toLowerCase().includes(search))return;
    const button=node('button',undefined,'template-choice'+(session.index===index?' active':''));button.type='button';
    button.setAttribute('aria-pressed',String(session.index===index));
    button.append(node('strong',template.name||'未命名模板'),node('small',`${template.algorithm||template.backend} · ${Object.keys(template.params||{}).length} 项参数`));
    button.onclick=()=>selectNodeTemplate(index);container.append(button);
  });
  if(!container.children.length)container.append(node('p','没有匹配的模板。','empty-state'));
  $('node-template-count').textContent=`${session.templates.length} 个模板 · 选择后填写本次实验`;
}
function readNodeTemplateDraft(){
  const session=nodeTemplateSession;if(!session||session.index===null)throw new Error('请先选择任务模板。');
  const draft=session.drafts.get(session.index),spec=copy(draft.spec),name=$('node-template-name').value.trim();
  if(!name||name.length>200)throw new Error('请填写 1–200 字的实验名称。');spec.name=name;
  try{spec.params=JSON.parse($('node-template-params-json').value);}catch{throw new Error('参数 JSON 无效，请检查后重试。');}
  if(!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('实验参数必须是对象。');
  spec.resources=readTaskResources('node-template-resources',spec.resources);applyScheduling(spec,readScheduling('node-template-scheduling'));
  return {spec,tag_ids:[...(draft.tag_ids||[])]};
}
function selectNodeTemplate(index){
  if(nodeTemplateBusy||!nodeTemplateSession)return;
  const session=nodeTemplateSession;
  if(session.index!==null){
    try{session.drafts.set(session.index,readNodeTemplateDraft());}
    catch(error){inlineFeedback('node-template-feedback','切换前请修正当前输入：'+error.message,true);return;}
  }
  session.index=index;
  if(!session.drafts.has(index)){
    const spec=copy(session.templates[index]);spec.scheduling={mode:'manual',node_ids:[session.worker]};
    session.drafts.set(index,{spec,tag_ids:[]});
  }
  fillNodeTemplateDraft();renderNodeTemplateChoices();
}
function renderNodeTemplateParameters(){
  const draft=nodeTemplateSession?.drafts.get(nodeTemplateSession.index);if(!draft)return;
  objectFields($('node-template-params'),draft.spec.params||{},(key,value)=>{draft.spec.params[key]=value;$('node-template-params-json').value=JSON.stringify(draft.spec.params,null,2);});
}
function fillNodeTemplateDraft(){
  const session=nodeTemplateSession,draft=session.drafts.get(session.index),spec=draft.spec,worker=(state.nodes||[]).find(item=>item.id===session.worker);
  $('node-template-name').value=spec.name||'';$('node-template-selected').textContent=session.templates[session.index].name||'任务模板';
  $('node-template-origin').textContent=`${spec.algorithm||spec.backend} · ${spec.group||'未分组'} · 来源节点 ${worker?.display_name||session.worker}`;
  const status=!worker||timingNow()-worker.last_seen>45?'来源节点离线，提交后会排队等待节点重新连接。':worker.mode==='drain'?'来源节点已暂停接单，提交后会排队等待恢复。':'默认在此节点运行；也可以在下方调整算力分配。';
  $('node-template-status').textContent=status;$('node-template-params-json').value=JSON.stringify(spec.params||{},null,2);
  $('node-template-params-json').setCustomValidity('');
  renderNodeTemplateParameters();renderTaskResources('node-template-resources',spec.resources,(key,value)=>{(spec.resources||={})[key]=value;});
  renderScheduling('node-template-scheduling',spec.scheduling,spec.priority||0);
  renderDraftTags('node-template-tags',()=>nodeTemplateSession?.drafts.get(nodeTemplateSession.index)?.tag_ids||[],values=>{nodeTemplateSession.drafts.get(nodeTemplateSession.index).tag_ids=values;});
  $('node-template-spec').textContent=JSON.stringify({backend:spec.backend,source:spec.source,command:spec.command,environments:spec.environments,assets:spec.assets},null,2);
  $('node-template-preview-result').replaceChildren();inlineFeedback('node-template-feedback','');
}
function setNodeTemplateBusy(busy){
  nodeTemplateBusy=busy;
  for(const element of $('node-template-dialog').querySelectorAll('input,select,textarea,button'))element.disabled=busy;
  $('node-template-submit').textContent=busy?'正在处理…':'提交实验';
}
async function submitNodeTemplate(event){
  event.preventDefault();if(nodeTemplateBusy||!$('node-template-form').reportValidity())return;
  const session=nodeTemplateSession;
  try{
    const payload=readNodeTemplateDraft(),fingerprint=JSON.stringify(payload);
    if(session.pending?.fingerprint!==fingerprint)session.pending={fingerprint,payload:{...payload,request_id:requestId()}};
    setNodeTemplateBusy(true);inlineFeedback('node-template-feedback','正在提交实验…');
    const result=await api('/api/jobs',session.pending.payload);session.pending=null;$('node-template-dialog').close();
    $('filter').value='';$('jobs-tag-filter').value='';$('jobs-status-filter').value='';recordPages.jobs.page=1;
    notify(`已提交 ${result.ids.length} 个实验，可在实验记录中查看进度。`);showView('experiments');await refresh();
  }catch(error){inlineFeedback('node-template-feedback','提交未确认：'+error.message+'。输入已保留，直接重试会复用同一次提交标识。',true);}
  finally{setNodeTemplateBusy(false);}
}
async function previewNodeTemplate(){
  if(nodeTemplateBusy||!$('node-template-form').reportValidity())return;
  try{const {spec}=readNodeTemplateDraft();setNodeTemplateBusy(true);
    const result=await api('/api/scheduling/preview',{spec}),allocation=result.allocations?.[0],container=$('node-template-preview-result');container.replaceChildren();
    container.append(node('p',allocation?.node_id?`预计分配到 ${allocation.node_id}${allocation.gpu_uuid?' · '+allocation.gpu_uuid:''}`:'暂时没有符合条件的算力，提交后将排队等待。','resource-sync'));
    for(const warning of result.warnings||[])container.append(node('p',warning,'muted'));
    inlineFeedback('node-template-feedback','预览只检查当前分配情况，点击“提交实验”才会创建任务。');
  }catch(error){inlineFeedback('node-template-feedback',error.message,true);}finally{setNodeTemplateBusy(false);}
}
function initializeNodeTemplateTools(){
  $('node-template-search').oninput=renderNodeTemplateChoices;
  $('node-template-form').onsubmit=submitNodeTemplate;$('node-template-preview').onclick=()=>void previewNodeTemplate();
  $('node-template-close').onclick=()=>$('node-template-dialog').close();
  $('node-template-dialog').oncancel=event=>{if(nodeTemplateBusy)event.preventDefault();};
  $('node-template-reset').onclick=()=>{
    const session=nodeTemplateSession;if(!session||nodeTemplateBusy)return;
    const spec=copy(session.templates[session.index]);spec.scheduling={mode:'manual',node_ids:[session.worker]};
    session.drafts.set(session.index,{spec,tag_ids:session.drafts.get(session.index).tag_ids});fillNodeTemplateDraft();
  };
  $('node-template-params-json').onchange=()=>{
    try{const params=JSON.parse($('node-template-params-json').value);if(!params||typeof params!=='object'||Array.isArray(params))throw new Error('参数必须是对象。');
      nodeTemplateSession.drafts.get(nodeTemplateSession.index).spec.params=params;renderNodeTemplateParameters();$('node-template-params-json').setCustomValidity('');
    }catch(error){$('node-template-params-json').setCustomValidity(error.message);}
  };
  $('node-template-params-json').oninput=()=>$('node-template-params-json').setCustomValidity('');
  $('node-template-matrix').onclick=()=>{
    if(nodeTemplateBusy)return;
    try{const draft=readNodeTemplateDraft();$('node-template-dialog').close();openMatrix({name:(draft.spec.name+' · 矩阵').slice(0,160),description:'',...draft,datasets:[],grid:{}});}
    catch(error){inlineFeedback('node-template-feedback',error.message,true);}
  };
}
