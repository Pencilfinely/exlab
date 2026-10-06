'use strict';

const projectPendingStates=new Set(['queued','downloading','installing']);
const projectWorkspaces=new Map();
let projectWorkspaceSession=null,projectWorkspaceBusy=false,projectImportStep='source';
recordPages.projects={page:1,size:10};

function projectWorkerOnline(worker){return timingNow()-worker.last_seen<=45;}
function projectWorkerTemplates(project,worker){
  if(!(project.deployments||[]).some(item=>item.node_id===worker.id&&item.status==='installed'))return [];
  return (worker.snapshot?.task_templates||[]).filter(template=>template.project_id===project.project_id
    &&(!project.bundle_id||template.project_bundle_id===project.bundle_id));
}
function projectPresetsFor(project){
  const presets=new Map();
  for(const worker of state.nodes||[])for(const template of projectWorkerTemplates(project,worker)){
    const key=JSON.stringify([template.experiment_id||template.name,template.params,template.resources]);
    if(!presets.has(key))presets.set(key,{key,template:copy(template),workers:[]});
    presets.get(key).workers.push(worker.id);
  }
  return [...presets.values()];
}
function projectStats(project){
  const deployments=project.deployments||[],ready=(state.nodes||[]).filter(worker=>projectWorkerTemplates(project,worker).length);
  return {ready:ready.length,online:ready.filter(projectWorkerOnline).length,installed:deployments.filter(item=>item.status==='installed').length,
    pending:deployments.filter(item=>projectPendingStates.has(item.status)).length,failed:deployments.filter(item=>item.status==='failed').length};
}
function filteredProjects(){
  const search=$('projects-search').value.trim().toLowerCase(),status=$('projects-status-filter').value;
  const projects=(state.projects||[]).filter(project=>{
    const stats=projectStats(project);
    if(status==='ready'&&!stats.ready||status==='pending'&&!stats.pending&&stats.installed<=stats.ready
      ||status==='failed'&&!stats.failed||status==='undeployed'&&(project.deployments||[]).length)return false;
    return JSON.stringify([project.name,project.project_id,project.digest,...(project.deployments||[]).map(item=>item.node_id)]).toLowerCase().includes(search);
  });
  return projects.sort((a,b)=>($('projects-sort').value==='name'?a.name.localeCompare(b.name,'zh-CN'):0)
    ||(b.created||0)-(a.created||0)||a.digest.localeCompare(b.digest));
}
function renderProjects(){
  const scrollTop=$('projects-scroll').scrollTop,projects=state.projects||[];
  const ready=projects.filter(project=>projectStats(project).ready).length,failed=projects.filter(project=>projectStats(project).failed).length;
  $('projects-summary').textContent=`${projects.length} 个项目版本 · ${ready} 个可创建实验${failed?' · '+failed+' 个存在部署失败':''}`;
  $('projects').replaceChildren();
  const records=filteredProjects();
  for(const project of pageRecords(records,'projects')){
    const row=node('tr'),title=node('td'),stats=projectStats(project);
    title.append(node('strong',project.name),node('small',project.project_id),node('small',`版本 ${project.digest.slice(0,12)} · ${(project.size/1024/1024).toFixed(1)} MiB`));title.title=project.digest;
    const status=node('td'),label=stats.ready?'可创建实验':stats.pending?'分发中':stats.failed?'部署失败':stats.installed?'等待配置回报':'未部署';
    status.append(node('span',label,'badge '+(stats.ready?'succeeded':stats.failed?'failed':'queued')),
      node('small',`${stats.ready} 台已准备 · ${stats.online} 台在线${stats.pending?' · '+stats.pending+' 台分发中':''}${stats.failed?' · '+stats.failed+' 台失败':''}`));
    const actions=node('td'),buttons=node('div',undefined,'actions compact');
    buttons.append(smallButton(stats.ready?'创建实验':'准备算力',event=>{event.stopPropagation();openProjectWorkspace(project,stats.ready?'run':'deploy');}),
      smallButton('部署 / 详情',event=>{event.stopPropagation();openProjectWorkspace(project,'deploy');}));actions.append(buttons);
    row.append(title,node('td',formatTimestamp(project.created),'record-time'),status,actions);row.tabIndex=0;
    row.onclick=()=>openProjectWorkspace(project,stats.ready?'run':'deploy');
    row.onkeydown=event=>{if(event.target===row&&['Enter',' '].includes(event.key)){event.preventDefault();row.onclick();}};
    $('projects').append(row);
  }
  if(!records.length){const row=node('tr'),cell=node('td',projects.length?'没有匹配的项目，调整搜索或部署状态筛选。':'项目库还是空的。导入文件夹或上传项目包，即可准备第一次实验。','empty-state');cell.colSpan=4;row.append(cell);$('projects').append(row);}
  $('projects-scroll').scrollTop=scrollTop;renderProjectCleanups();renderProjectWorkspace();
}
function activeLibraryProject(){return (state.projects||[]).find(project=>project.digest===projectWorkspaceSession?.digest);}
function openProjectWorkspace(project,tab='deploy'){
  if(projectWorkspaceBusy)return;
  const current=(state.projects||[]).find(item=>item.digest===project.digest);
  if(!current){notify('此项目版本已不在项目库中，请刷新后选择。');return;}
  captureProjectRunDraft();
  if(!projectWorkspaces.has(current.digest))projectWorkspaces.set(current.digest,{digest:current.digest,presets:[],drafts:new Map(),key:null,rows:new Map(),nodeKeys:''});
  projectWorkspaceSession=projectWorkspaces.get(current.digest);projectWorkspaceSession.rows.clear();projectWorkspaceSession.nodeKeys='';
  $('project-node-search').value='';inlineFeedback('project-feedback','');ensureProjectPresets(current);
  if(projectWorkspaceSession.key)fillProjectPreset();
  showView('projects');setProjectTab(tab);
  if(!$('project-dialog').open)$('project-dialog').showModal();renderProjectWorkspace();
}
function openProjectRun(project){openProjectWorkspace(project,'run');}
function setProjectTab(tab){
  if(!['deploy','run','info'].includes(tab))return;
  for(const name of ['deploy','run','info']){
    $('project-pane-'+name).hidden=name!==tab;
    const button=$('project-tab-'+name);button.setAttribute('aria-selected',String(name===tab));button.classList.toggle('active',name===tab);button.tabIndex=name===tab?0:-1;
  }
  if(projectWorkspaceSession)projectWorkspaceSession.tab=tab;
}
function ensureProjectPresets(project){
  const session=projectWorkspaceSession,presets=projectPresetsFor(project);let changed=false;
  for(const preset of presets){const existing=session.presets.find(item=>item.key===preset.key);if(existing)existing.workers=preset.workers;else{session.presets.push(preset);changed=true;}}
  projectPresets=session.presets;
  if(changed)$('project-preset').replaceChildren(...session.presets.map(preset=>{const option=node('option',preset.template.name);option.value=preset.key;return option;}));
  if(!session.key&&session.presets.length){session.key=session.presets[0].key;fillProjectPreset();}
  $('project-preset').value=session.key||'';
}
function renderProjectWorkspace(){
  if(!projectWorkspaceSession||!$('project-dialog').open)return;
  const project=activeLibraryProject();
  if(!project){$('project-dialog').close();notify('此项目版本已被移除。');return;}
  ensureProjectPresets(project);const stats=projectStats(project);
  $('project-title').textContent=project.name;$('project-subtitle').textContent=`${project.project_id} · 版本 ${project.digest.slice(0,12)} · 导入于 ${formatTimestamp(project.created)}`;
  $('project-deploy-summary').textContent=stats.ready?`${stats.ready} 台节点已准备，${stats.online} 台在线。可以继续分发到其他节点，或切换到“创建实验”。`
    :stats.pending?'节点正在下载或安装。此窗口会自动更新，完成后即可创建实验。':stats.installed?'节点已安装项目，正在等待实验配置回报。':'选择目标算力节点并分发；安装完成后，这里会出现可用的实验配置。';
  $('project-run-availability').textContent=stats.ready?`${stats.ready} 台节点已有实验配置，${stats.online} 台在线。${stats.online?'提交前可预览当前分配情况。':'提交后会排队等待节点上线。'}`
    :!(state.nodes||[]).length?'还没有接入算力节点。添加一台节点后，分发项目即可准备实验。':stats.pending?'项目正在分发，安装并回报实验配置后即可填写参数。':'尚无可用的实验配置，请先部署项目或检查安装失败原因。';
  $('project-run-form').hidden=!sessionHasProjectRun();$('project-run-prepare').hidden=!!stats.ready;
  $('project-go-run').disabled=projectWorkspaceBusy||!stats.ready;
  $('project-run-submit').disabled=projectWorkspaceBusy||!stats.ready;
  $('project-run-preview').disabled=projectWorkspaceBusy||!stats.ready;$('project-run-matrix').disabled=projectWorkspaceBusy||!stats.ready;
  renderProjectDeployments(project);
  const info=$('project-info');info.replaceChildren();
  for(const [label,value] of [['项目名称',project.name],['项目标识',project.project_id],['导入时间',formatTimestamp(project.created)],['项目包大小',(project.size/1024/1024).toFixed(1)+' MiB'],['项目包 SHA-256',project.digest],['代码与数据版本',project.bundle_id||'旧版项目包'],['已准备节点',stats.ready+' 台']])info.append(node('dt',label),node('dd',value));
}
function sessionHasProjectRun(){return !!projectWorkspaceSession?.key;}
function projectNodeSelection(project){
  if(!projectSelections.has(project.digest))projectSelections.set(project.digest,new Set());
  const chosen=projectSelections.get(project.digest);
  for(const id of chosen){const worker=(state.nodes||[]).find(item=>item.id===id),deployment=(project.deployments||[]).find(item=>item.node_id===id);
    if(!worker||(worker.snapshot?.capabilities||[]).includes('project-bundle-v1')===false||projectPendingStates.has(deployment?.status))chosen.delete(id);}
  return chosen;
}
function renderProjectDeployments(project){
  const session=projectWorkspaceSession,chosen=projectNodeSelection(project),search=$('project-node-search').value.trim().toLowerCase();
  const workers=(state.nodes||[]).filter(worker=>JSON.stringify([worker.id,worker.display_name]).toLowerCase().includes(search));
  const container=$('project-node-list'),rows=[];
  for(const worker of workers){
    if(!session.rows.has(worker.id)){
      const root=node('div',undefined,'project-node-row'),label=node('label'),check=node('input'),title=node('strong'),status=node('span',undefined,'badge'),detail=node('p',undefined,'project-node-detail'),actions=node('div',undefined,'actions compact');
      check.type='checkbox';check.value=worker.id;check.dataset.projectNode=worker.id;
      check.onchange=()=>{if(check.checked)chosen.add(worker.id);else chosen.delete(worker.id);renderProjectDeployments(activeLibraryProject());};
      label.append(check,title);root.append(label,status,detail,actions);session.rows.set(worker.id,{root,check,title,status,detail,actions});
    }
    const row=session.rows.get(worker.id),deployment=(project.deployments||[]).find(item=>item.node_id===worker.id),supported=(worker.snapshot?.capabilities||[]).includes('project-bundle-v1'),online=projectWorkerOnline(worker);
    const pending=projectPendingStates.has(deployment?.status),ready=projectWorkerTemplates(project,worker).length;
    row.check.checked=chosen.has(worker.id);row.check.disabled=projectWorkspaceBusy||!supported||pending;
    row.title.textContent=(worker.display_name||worker.id)+(worker.display_name?' · '+worker.id:'');
    const labels={queued:online?'等待领取':'离线 · 等待领取',downloading:'下载中',installing:'安装中',installed:ready?'已准备实验':'已安装 · 等待配置',failed:'安装失败'};
    row.status.textContent=!supported?'需更新算力端':(labels[deployment?.status]||'未部署')+(deployment?.status==='queued'?'':online?' · 在线':' · 离线');
    row.status.className='badge '+(deployment?.status==='failed'?'failed':ready?'succeeded':'queued');
    row.detail.textContent=deployment?.detail||(!supported?'更新算力端并连接一次后，即可自动分发。':pending?'完成后会自动更新为已准备。':'');row.detail.hidden=!row.detail.textContent;
    if(deployment?.status==='failed'&&!row.actions.children.length){
      row.actions.append(smallButton('选择并重试',()=>{if(!projectWorkspaceBusy){chosen.add(worker.id);renderProjectDeployments(activeLibraryProject());inlineFeedback('project-feedback','已选择失败节点，点击“分发项目”重试安装。');}}),
        smallButton('复制错误',async()=>{try{await writeClipboardText(row.detail.textContent);inlineFeedback('project-feedback','安装错误已复制。');}catch(error){inlineFeedback('project-feedback',error.message,true);}}));
    }else if(deployment?.status!=='failed')row.actions.replaceChildren();
    rows.push(row.root);
  }
  const keys=workers.map(worker=>worker.id).join('\n');
  if(session.nodeKeys!==keys||!container.children.length){
    const focused=document.activeElement?.dataset?.projectNode;container.replaceChildren(...rows);session.nodeKeys=keys;
    if(focused&&session.rows.has(focused)&&workers.some(worker=>worker.id===focused))session.rows.get(focused).check.focus({preventScroll:true});
  }
  if(!workers.length){container.replaceChildren(node('p',(state.nodes||[]).length?'没有匹配的节点。':'尚无算力节点，点击下方“添加算力节点”。','empty-state'));session.nodeKeys=keys;}
  $('project-node-selection').textContent=`已选择 ${chosen.size} 台节点${search?'（包含搜索范围外的选择）':''}`;
  $('project-deploy').textContent=projectWorkspaceBusy?'正在处理…':`分发到 ${chosen.size} 台节点`;
  $('project-deploy').disabled=projectWorkspaceBusy||!chosen.size;$('project-select-online').disabled=projectWorkspaceBusy;
}
function setProjectBusy(busy){
  projectWorkspaceBusy=busy;
  for(const element of $('project-dialog').querySelectorAll('input,select,textarea,button')){
    if(busy){element.projectWasDisabled=element.disabled;element.disabled=true;}else element.disabled=!!element.projectWasDisabled;
  }
  if(!busy)renderProjectWorkspace();
}
async function deployLibraryProject(){
  const project=activeLibraryProject();if(projectWorkspaceBusy||!project)return;
  const selected=[...projectNodeSelection(project)];if(!selected.length)return;
  try{setProjectBusy(true);inlineFeedback('project-feedback','正在提交分发请求…');
    await api('/api/projects/deploy',{digest:project.digest,node_ids:selected});projectSelections.get(project.digest).clear();
    inlineFeedback('project-feedback','分发已排队。节点会自动下载并安装，此窗口会更新部署状态。');await refresh();
  }catch(error){inlineFeedback('project-feedback','分发未确认：'+error.message+'。请查看节点状态后再重试。',true);}
  finally{setProjectBusy(false);}
}

function projectCurrentDraft(){return projectWorkspaceSession?.drafts.get(projectWorkspaceSession.key);}
function captureInput(input){return {value:input.value,checked:input.checked,validity:input.validity?.customError?input.validationMessage:''};}
function restoreInput(input,raw){if(!input||!raw)return;input.value=raw.value;input.checked=raw.checked;input.setCustomValidity(raw.validity||'');}
function captureProjectRunDraft(){
  const draft=projectCurrentDraft();if(!draft||$('project-run-form').hidden)return;
  draft.tag_ids=[...projectRunTagIds];draft.raw={name:$('project-run-name').value,json:$('project-params').value,
    params:Object.fromEntries(Object.entries($('project-param-fields').paramInputs||{}).map(([key,input])=>[key,captureInput(input)])),
    resources:Object.fromEntries(Object.entries($('project-resources').resourceInputs||{}).map(([key,input])=>[key,captureInput(input)])),
    scheduling:[...$('project-scheduling').querySelectorAll('[data-control]')].map(input=>({role:input.dataset.control,...captureInput(input)}))};
  try{const params=JSON.parse(draft.raw.json);if(params&&typeof params==='object'&&!Array.isArray(params))draft.spec.params=params;}catch{}
}
function fillProjectPreset(){
  const session=projectWorkspaceSession,preset=session?.presets.find(item=>item.key===session.key);if(!preset)return;
  if(!session.drafts.has(session.key)){const spec=copy(preset.template);spec.scheduling={mode:'auto',node_ids:[],preferred_node_ids:[],gpu_uuids:[]};session.drafts.set(session.key,{spec,tag_ids:[],pending:null});}
  const draft=projectCurrentDraft(),spec=draft.spec;
  $('project-preset').value=session.key;$('project-run-name').value=spec.name;$('project-params').value=JSON.stringify(spec.params||{},null,2);$('project-params').setCustomValidity('');
  renderProjectParameters();renderTaskResources('project-resources',spec.resources,()=>{});renderScheduling('project-scheduling',spec.scheduling,spec.priority||0);
  projectRunTagIds=[...(draft.tag_ids||[])];renderDraftTags('project-run-tags',()=>projectRunTagIds,values=>projectRunTagIds=values);
  if(draft.raw){
    $('project-run-name').value=draft.raw.name;$('project-params').value=draft.raw.json;
    for(const [key,raw] of Object.entries(draft.raw.params))restoreInput($('project-param-fields').paramInputs?.[key],raw);
    for(const [key,raw] of Object.entries(draft.raw.resources))restoreInput($('project-resources').resourceInputs?.[key],raw);
    const controls=[...$('project-scheduling').querySelectorAll('[data-control]')];
    for(const raw of draft.raw.scheduling){const input=controls.find(item=>item.dataset.control===raw.role&&(!['candidate','gpu'].includes(raw.role)||item.value===raw.value));restoreInput(input,raw);}
    $('project-scheduling').querySelector('[data-control="mode"]').onchange();
  }
  $('project-run-form').hidden=false;inlineFeedback('project-run-feedback','');$('project-run-preview-result').replaceChildren();
}
function readProjectRunDraft(){
  const draft=projectCurrentDraft();if(!draft||!activeLibraryProject())throw new Error('请先选择项目与实验配置。');
  for(const input of $('project-run-form').querySelectorAll('input,select,textarea'))if(!input.checkValidity()){
    for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    input.reportValidity();throw new Error('请修正实验表单中的无效输入。');
  }
  const spec=copy(draft.spec);spec.name=$('project-run-name').value.trim();if(!spec.name||spec.name.length>200)throw new Error('请填写 1–200 字的实验名称。');
  try{spec.params=JSON.parse($('project-params').value);}catch{throw new Error('参数 JSON 无效，请检查后重试。');}
  if(!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('实验参数必须是对象。');
  spec.resources=readTaskResources('project-resources',spec.resources);applyScheduling(spec,readScheduling('project-scheduling'));
  return {spec,tag_ids:[...projectRunTagIds]};
}
async function submitProjectRun(event){
  event.preventDefault();if(projectWorkspaceBusy)return;
  const draft=projectCurrentDraft();
  try{const payload=readProjectRunDraft(),fingerprint=JSON.stringify(payload);captureProjectRunDraft();
    if(draft.pending?.fingerprint!==fingerprint)draft.pending={fingerprint,payload:{...payload,request_id:requestId()}};
    setProjectBusy(true);inlineFeedback('project-run-feedback','正在提交实验…');
    const result=await api('/api/jobs',draft.pending.payload);draft.pending=null;$('project-dialog').close();
    $('filter').value='';$('jobs-tag-filter').value='';$('jobs-status-filter').value='';recordPages.jobs.page=1;
    notify(`已提交 ${result.ids.length} 个实验，可在实验记录中查看进度。`);showView('experiments');await refresh();
  }catch(error){inlineFeedback('project-run-feedback','提交未确认：'+error.message+'。输入已保留，直接重试会复用同一次提交标识。',true);}
  finally{setProjectBusy(false);}
}
async function previewProjectRun(){
  if(projectWorkspaceBusy)return;
  try{const {spec}=readProjectRunDraft();captureProjectRunDraft();setProjectBusy(true);
    const result=await api('/api/scheduling/preview',{spec}),allocation=result.allocations?.[0],container=$('project-run-preview-result');container.replaceChildren();
    container.append(node('p',allocation?.node_id?`预计分配到 ${allocation.node_id}${allocation.gpu_uuid?' · '+allocation.gpu_uuid:''}`:'暂时没有符合条件的算力，提交后将排队等待。','resource-sync'));
    for(const warning of result.warnings||[])container.append(node('p',warning,'muted'));inlineFeedback('project-run-feedback','预览未创建任务，点击“提交实验”才会开始。');
  }catch(error){inlineFeedback('project-run-feedback',error.message,true);}finally{setProjectBusy(false);}
}

const projectImportSteps=['source','parameters','data','metrics','publish'];
function openProjectImport(){
  showProjectImportStep(importDraft?projectImportStep:'source');if(!$('import-dialog').open)$('import-dialog').showModal();
}
function showProjectImportStep(step){
  if(!projectImportSteps.includes(step))step='source';if(!importDraft&&step!=='source')step='source';
  projectImportStep=step;
  $('import-wizard').querySelectorAll('[data-import-section]').forEach(section=>section.hidden=section.dataset.importSection!==step);
  $('import-review-form').hidden=!importDraft||step==='source';updateProjectImportControls();
  if(step==='publish'){
    const container=$('import-publish-summary');container.replaceChildren();
    for(const [label,value] of [['项目名称',importDraft.project.name],['训练入口',(importDraft.harness.command||[]).join(' ')],['实验配置',importDraft.experiments.length+' 套'],['数据目录',Object.keys(importDraft.project.assets||{}).length+' 个'],['指标规则',(importDraft.harness.metrics||[]).length+' 条'],['额外依赖',(importDraft.project.runtime?.requirements||[]).join('、')||'无']])container.append(node('dt',label),node('dd',value));
  }
  $('import-dialog').querySelector('.import-dialog-body').scrollTop=0;
}
function updateProjectImportControls(){
  const index=projectImportSteps.indexOf(projectImportStep),descriptions=['选择项目根目录，读取训练入口与参数。','确认训练入口，调整默认实验配置。','选择一同分发的数据，核对依赖与资源预算。','配置需要绘制曲线的指标；也可保留日志，稍后配置。','核对文件和配置，加入项目库后选择算力端部署。'];
  $('import-step-description').textContent=descriptions[index];
  for(const step of projectImportSteps){const button=$('import-step-'+step);button.classList.toggle('active',step===projectImportStep);button.setAttribute('aria-selected',String(step===projectImportStep));button.tabIndex=step===projectImportStep?0:-1;button.disabled=importPolling||step!=='source'&&!importDraft;}
  $('import-source-continue').hidden=!importDraft;$('import-source-continue').disabled=importPolling;
  $('import-back').disabled=importPolling;$('import-next').disabled=importPolling;
  $('import-next').hidden=index===projectImportSteps.length-1;$('import-next').textContent=projectImportStep==='metrics'?'下一步：检查与发布':'下一步';
  $('import-publish').hidden=projectImportStep!=='publish';
}
function revealProjectImportInput(input){
  const section=input.closest('[data-import-section]');if(section)showProjectImportStep(section.dataset.importSection);
  for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
}
function finishProjectImport(job){
  const visible=$('import-dialog').open;$('import-dialog').close();importDraft=null;importJob=null;showProjectImportStep('source');
  $('project-progress').hidden=false;$('project-progress').textContent=`${job.project.name} 已加入项目库。选择算力节点部署后即可创建实验。`;
  if(visible)openProjectWorkspace(job.project,'deploy');
}
function bindProjectTabs(names,prefix,select){
  for(const name of names){const button=$(prefix+name);button.onclick=()=>select(name);button.onkeydown=event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const enabled=names.filter(item=>!$(prefix+item).disabled);if(!enabled.length)return;
    event.preventDefault();const index=enabled.indexOf(name);
    const target=event.key==='Home'?enabled[0]:event.key==='End'?enabled[enabled.length-1]:enabled[(index+(event.key==='ArrowRight'?1:-1)+enabled.length)%enabled.length];
    select(target);$(prefix+target).focus();
  };}
}
function initializeProjectTools(){
  for(const id of ['projects-search','projects-status-filter','projects-sort'])$(id)[id==='projects-search'?'oninput':'onchange']=()=>resetRecordPage('projects',renderProjects);
  for(const action of ['first','prev','next','last'])$('projects-'+action).onclick=()=>{const paging=recordPages.projects,last=Number($('projects-page').max)||1;paging.page=action==='first'?1:action==='last'?last:paging.page+(action==='next'?1:-1);$('projects-scroll').scrollTop=0;renderProjects();};
  $('projects-page').onchange=event=>{const value=Number(event.target.value);if(Number.isInteger(value)&&value>=1)recordPages.projects.page=value;$('projects-scroll').scrollTop=0;renderProjects();};
  $('projects-page-size').onchange=event=>{const size=Number(event.target.value);if([10,20,50,100].includes(size))recordPages.projects.size=size;resetRecordPage('projects',renderProjects);};
  bindProjectTabs(['deploy','run','info'],'project-tab-',setProjectTab);
  $('project-close').onclick=()=>$('project-dialog').close();$('project-run-close').onclick=()=>$('project-dialog').close();
  $('project-dialog').onclose=captureProjectRunDraft;$('project-dialog').oncancel=event=>{if(projectWorkspaceBusy)event.preventDefault();};
  $('project-node-search').oninput=()=>{const project=activeLibraryProject();if(project)renderProjectDeployments(project);};
  $('project-clear-nodes').onclick=()=>{const project=activeLibraryProject();if(project){projectNodeSelection(project).clear();renderProjectDeployments(project);}};
  $('project-select-online').onclick=()=>{const project=activeLibraryProject();if(!project)return;const chosen=projectNodeSelection(project);
    for(const worker of state.nodes||[]){const deployment=(project.deployments||[]).find(item=>item.node_id===worker.id);if(projectWorkerOnline(worker)&&(worker.snapshot?.capabilities||[]).includes('project-bundle-v1')&&(!deployment||deployment.status==='failed'))chosen.add(worker.id);}renderProjectDeployments(project);};
  $('project-deploy').onclick=()=>void deployLibraryProject();$('project-go-run').onclick=()=>setProjectTab('run');$('project-run-prepare').onclick=()=>setProjectTab('deploy');
  $('project-add-worker').onclick=()=>{$('project-dialog').close();showView('compute');$('add-worker').click();};
  $('project-delete').onclick=()=>{const project=activeLibraryProject();if(project)void deleteProject(project,$('project-delete'));};
  $('project-preset').onchange=()=>{captureProjectRunDraft();projectWorkspaceSession.key=$('project-preset').value;fillProjectPreset();};
  $('project-run-reset').onclick=()=>{const session=projectWorkspaceSession;if(session){session.drafts.delete(session.key);fillProjectPreset();}};
  $('project-params').onchange=()=>{try{const params=JSON.parse($('project-params').value);if(!params||typeof params!=='object'||Array.isArray(params))throw new Error('参数必须是对象。');$('project-params').setCustomValidity('');renderProjectParameters();}catch(error){$('project-params').setCustomValidity(error.message);inlineFeedback('project-run-feedback',error.message,true);}};
  $('project-params').oninput=()=>$('project-params').setCustomValidity('');
  $('project-run-form').onsubmit=submitProjectRun;$('project-run-preview').onclick=()=>void previewProjectRun();
  $('project-run-matrix').onclick=()=>{try{const draft=readProjectRunDraft();captureProjectRunDraft();$('project-dialog').close();openMatrix({name:(draft.spec.name+' · 矩阵').slice(0,160),description:'',...draft,datasets:[],grid:{}});}catch(error){inlineFeedback('project-run-feedback',error.message,true);}};
  bindProjectTabs(projectImportSteps,'import-step-',showProjectImportStep);
  $('import-next').onclick=()=>{if(importPolling)return;const section=$('import-wizard').querySelector('[data-import-section="'+projectImportStep+'"]');
    for(const input of section.querySelectorAll('input,select,textarea'))if(!input.checkValidity()){revealProjectImportInput(input);input.reportValidity();return;}
    showProjectImportStep(projectImportSteps[projectImportSteps.indexOf(projectImportStep)+1]);};
  $('import-back').onclick=()=>{if(!importPolling)showProjectImportStep(projectImportSteps[Math.max(0,projectImportSteps.indexOf(projectImportStep)-1)]);};
  $('import-source-continue').onclick=()=>showProjectImportStep('parameters');$('import-ai-settings').onclick=()=>{$('import-dialog').close();showView('settings');};
  updateProjectImportControls();
}
