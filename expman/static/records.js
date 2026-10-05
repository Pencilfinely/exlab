'use strict';

// List navigation and user labels live outside the task's scheduling definition.
const recordPages={jobs:{page:1,size:20},matrices:{page:1,size:10},'matrix-result':{page:1,size:20}};
let matrixRecords=[],matrixResultRecords=[],tagEditingId=null,tagPickerDraft=null,projectRunTagIds=[];

function pageRecords(records,prefix){
  const paging=recordPages[prefix],pages=Math.max(1,Math.ceil(records.length/paging.size));
  paging.page=Math.max(1,Math.min(pages,paging.page));
  const start=(paging.page-1)*paging.size;
  $(prefix+'-page-summary').textContent=`共 ${records.length} 条 · ${records.length?start+1:0}–${Math.min(start+paging.size,records.length)} 条 · ${pages} 页`;
  for(const suffix of ['first','prev'])$(prefix+'-'+suffix).disabled=paging.page===1;
  for(const suffix of ['next','last'])$(prefix+'-'+suffix).disabled=paging.page===pages;
  const input=$(prefix+'-page');input.max=String(pages);
  if(document.activeElement!==input)input.value=String(paging.page);
  $(prefix+'-page-size').value=String(paging.size);
  return records.slice(start,start+paging.size);
}
function resetRecordPage(prefix,render){recordPages[prefix].page=1;$(prefix+'-scroll').scrollTop=0;render();}
function matchesTag(record,value){return !value||(value==='__none'?!(record.tags||[]).length:(record.tags||[]).some(tag=>tag.id===value));}
function filteredJobs(){
  const text=$('filter').value.trim().toLowerCase(),tag=$('jobs-tag-filter').value,status=$('jobs-status-filter').value;
  return (state.jobs||[]).filter(job=>matchesTag(job,tag)&&(!status||job.state===status)
    &&JSON.stringify([job.spec.name,job.spec.algorithm,job.spec.group,job.node_id,...(job.tags||[]).map(item=>item.name)]).toLowerCase().includes(text));
}
function filteredMatrices(){
  const text=$('matrix-filter').value.trim().toLowerCase(),tag=$('matrices-tag-filter').value;
  return matrixRecords.filter(matrix=>matchesTag(matrix,tag)&&JSON.stringify([matrix.name,matrix.description,...(matrix.tags||[]).map(item=>item.name)]).toLowerCase().includes(text));
}
function tagChip(tag){
  const chip=node('span',undefined,'tag-chip'),dot=node('i');dot.setAttribute('aria-hidden','true');
  const color=/^#[0-9a-f]{6}$/i.test(tag.color)?tag.color:'#246657';
  dot.style.backgroundColor=color;chip.style.backgroundColor=color+'12';chip.style.borderColor=color+'40';
  chip.append(dot,node('span',tag.name));return chip;
}
function recordTagButtons(target,record){
  const container=node('div',undefined,'record-tags');
  for(const tag of record.tags||[])container.append(tagChip(tag));
  const edit=node('button',(record.tags||[]).length?'编辑标签':'+ 标签','tag-edit');edit.type='button';
  edit.setAttribute('aria-label','编辑“'+(record.name||record.spec?.name||'记录')+'”的标签');
  edit.onclick=event=>{event.stopPropagation();void openTagPicker(target,record);};container.append(edit);return container;
}
function completionCell(job){
  const cell=node('td',undefined,'record-time');
  if(!terminal.includes(job.state)){cell.textContent='—';return cell;}
  const reported=job.timing?.finished_at,value=Number.isFinite(reported)?reported:job.updated;
  cell.append(node('time',formatTimestamp(value)),node('small',(job.state==='succeeded'?'完成':names[job.state]||'停止')+(Number.isFinite(reported)?'':' · 主控确认')));
  cell.title=Number.isFinite(reported)?'算力端记录的最近完成 / 停止时间':'此记录没有算力端结束时间，显示主控最近确认状态的时间';
  return cell;
}
function refreshTagControls(){
  const tags=state.tags||[],signature=JSON.stringify(tags.map(tag=>[tag.id,tag.name,tag.color]));
  for(const prefix of ['jobs','matrices']){
    const select=$(prefix+'-tag-filter');if(select.tagSignature===signature)continue;
    const selectedTag=select.value;
    const choices=[['','全部标签'],['__none','未打标签'],...tags.map(tag=>[tag.id,tag.name])];
    select.replaceChildren(...choices.map(([value,text])=>{const option=node('option',text);option.value=value;return option;}));
    select.value=choices.some(([value])=>value===selectedTag)?selectedTag:'';select.tagSignature=signature;
    if(selectedTag&&select.value!==selectedTag)recordPages[prefix].page=1;
  }
  for(const id of ['matrix-tags','project-run-tags','node-template-tags']){
    const container=$(id);if(container.tagGetter&&container.tagSignature!==signature)renderDraftTags(id,container.tagGetter,container.tagSetter);
  }
  if(tagPickerDraft&&$('tag-picker').open&&$('tag-picker-list').tagSignature!==signature)renderTagPicker();
  if($('tag-manager').open&&$('tag-manager-list').tagSignature!==signature)renderTagManager();
}
function renderDraftTags(id,getter,setter){
  const container=$(id),tags=state.tags||[],selectedIds=getter()||[],chosen=new Set(selectedIds),valid=tags.map(tag=>tag.id);
  setter(selectedIds.filter(value=>valid.includes(value)));
  container.tagGetter=getter;container.tagSetter=setter;container.tagSignature=JSON.stringify(tags.map(tag=>[tag.id,tag.name,tag.color]));
  container.replaceChildren();const heading=node('div',undefined,'section-title');heading.append(node('h3','标签'));
  const manage=node('button','管理标签','subtle');manage.type='button';manage.onclick=()=>void openTagManager();heading.append(manage);container.append(heading);
  const choices=node('div',undefined,'tag-choices');
  for(const tag of tags){const label=node('label',undefined,'tag-choice'),input=node('input');input.type='checkbox';input.checked=chosen.has(tag.id);
    input.onchange=()=>{const values=new Set(getter()||[]);input.checked?values.add(tag.id):values.delete(tag.id);setter([...values]);};
    label.append(input,tagChip(tag));choices.append(label);}
  if(!tags.length)choices.append(node('p','还没有标签，点击“管理标签”创建。','muted'));container.append(choices);
}
async function fetchTags(){const result=await api('/api/tags');state.tags=result.tags||[];refreshTagControls();}
async function openTagManager(){
  if(!$('tag-manager').open){resetTagForm();$('tag-manager-search').value='';$('tag-manager').showModal();}
  renderTagManager();try{await fetchTags();renderTagManager();}catch(error){inlineFeedback('tag-manager-feedback',error.message,true);}
}
function resetTagForm(){tagEditingId=null;$('tag-name').value='';$('tag-color').value='#246657';$('tag-save').textContent='创建标签';$('tag-edit-cancel').hidden=true;}
function renderTagManager(){
  const container=$('tag-manager-list'),search=$('tag-manager-search').value.trim().toLowerCase(),tags=(state.tags||[]).filter(tag=>tag.name.toLowerCase().includes(search));
  container.replaceChildren();container.tagSignature=JSON.stringify((state.tags||[]).map(tag=>[tag.id,tag.name,tag.color]));
  for(const tag of tags){const row=node('div',undefined,'tag-manager-row'),label=node('div');label.append(tagChip(tag),node('small',`实验 ${tag.job_count||0} · 矩阵 ${tag.matrix_count||0}`));
    const actions=node('div',undefined,'actions compact');
    actions.append(smallButton('编辑',()=>{tagEditingId=tag.id;$('tag-name').value=tag.name;$('tag-color').value=tag.color;$('tag-save').textContent='保存修改';$('tag-edit-cancel').hidden=false;$('tag-name').focus();}),
      smallButton('删除',async event=>{
        if(!confirm(`删除标签“${tag.name}”？此标签会从所有实验和矩阵中移除，实验和结果仍保留。`))return;
        const button=event.currentTarget;button.disabled=true;
        try{await api('/api/tags/delete',{id:tag.id});if(tagEditingId===tag.id)resetTagForm();await reloadTaggedRecords();inlineFeedback('tag-manager-feedback','标签已删除。');}
        catch(error){inlineFeedback('tag-manager-feedback',error.message,true);}finally{button.disabled=false;}
      },true));row.append(label,actions);container.append(row);
  }
  if(!tags.length)container.append(node('p',search?'没有匹配的标签。':'还没有标签，在上方填写名称并选择颜色。','empty-state'));
}
async function reloadTaggedRecords(){await fetchTags();await refresh();await loadMatrices();renderTagManager();}
async function saveTag(event){
  event.preventDefault();if($('tag-save').disabled)return;$('tag-save').disabled=true;
  try{const payload={name:$('tag-name').value.trim(),color:$('tag-color').value};if(tagEditingId)payload.id=tagEditingId;
    await api('/api/tags/save',payload);resetTagForm();inlineFeedback('tag-manager-feedback','标签已保存，名称和颜色已同步到关联记录。');await reloadTaggedRecords();
  }catch(error){inlineFeedback('tag-manager-feedback',error.message,true);}finally{$('tag-save').disabled=false;}
}
async function openTagPicker(target,record){
  tagPickerDraft={target,id:record.id,ids:new Set((record.tags||[]).map(tag=>tag.id))};
  $('tag-picker-title').textContent='标签 · '+(record.name||record.spec?.name||'实验');$('tag-picker-search').value='';inlineFeedback('tag-picker-feedback','');
  renderTagPicker();if(!$('tag-picker').open)$('tag-picker').showModal();
  try{await fetchTags();renderTagPicker();}catch(error){inlineFeedback('tag-picker-feedback',error.message,true);}
}
function renderTagPicker(){
  if(!tagPickerDraft)return;const container=$('tag-picker-list'),search=$('tag-picker-search').value.trim().toLowerCase(),tags=state.tags||[];
  const valid=new Set(tags.map(tag=>tag.id));tagPickerDraft.ids=new Set([...tagPickerDraft.ids].filter(id=>valid.has(id)));
  container.replaceChildren();container.tagSignature=JSON.stringify(tags.map(tag=>[tag.id,tag.name,tag.color]));
  for(const tag of tags.filter(item=>item.name.toLowerCase().includes(search))){const label=node('label',undefined,'tag-choice'),input=node('input');input.type='checkbox';input.checked=tagPickerDraft.ids.has(tag.id);
    input.onchange=()=>input.checked?tagPickerDraft.ids.add(tag.id):tagPickerDraft.ids.delete(tag.id);label.append(input,tagChip(tag));container.append(label);}
  if(!container.children.length)container.append(node('p',search?'没有匹配的标签。':'还没有标签，点击“管理标签”创建。','empty-state'));
}
async function saveRecordTags(event){
  event.preventDefault();if(!tagPickerDraft||$('tag-picker-save').disabled)return;$('tag-picker-save').disabled=true;
  try{const result=await api('/api/tags/assign',{target:tagPickerDraft.target,id:tagPickerDraft.id,tag_ids:[...tagPickerDraft.ids]});
    for(const record of [...(state.jobs||[]),...matrixRecords,...matrixResultRecords])if(record.id===result.id)Object.assign(record,result);
    if(matrixDraft?.id===result.id){matrixDraft.tag_ids=result.tag_ids;renderDraftTags('matrix-tags',()=>matrixDraft.tag_ids||[],values=>matrixDraft.tag_ids=values);}
    $('tag-picker').close();notify('标签已保存。');renderJobs();renderMatrices();renderMatrixResultJobs();await reloadTaggedRecords();
  }catch(error){inlineFeedback('tag-picker-feedback',error.message,true);}finally{$('tag-picker-save').disabled=false;}
}
function initializeRecordTools(){
  const renderers={jobs:renderJobs,matrices:renderMatrices,'matrix-result':renderMatrixResultJobs};
  for(const [prefix,render] of Object.entries(renderers)){
    for(const action of ['first','prev','next','last'])$(prefix+'-'+action).onclick=()=>{
      const paging=recordPages[prefix],last=Number($(prefix+'-page').max)||1;
      paging.page=action==='first'?1:action==='last'?last:paging.page+(action==='next'?1:-1);$(prefix+'-scroll').scrollTop=0;render();
    };
    $(prefix+'-page').onchange=event=>{const value=Number(event.target.value);if(Number.isInteger(value)&&value>=1)recordPages[prefix].page=value;$(prefix+'-scroll').scrollTop=0;render();};
    $(prefix+'-page-size').onchange=event=>{const size=Number(event.target.value);if([10,20,50,100].includes(size))recordPages[prefix].size=size;resetRecordPage(prefix,render);};
  }
  $('filter').oninput=() =>resetRecordPage('jobs',renderJobs);
  for(const id of ['jobs-tag-filter','jobs-status-filter'])$(id).onchange=()=>resetRecordPage('jobs',renderJobs);
  $('matrix-filter').oninput=()=>resetRecordPage('matrices',renderMatrices);
  $('matrices-tag-filter').onchange=()=>resetRecordPage('matrices',renderMatrices);
  for(const id of ['jobs-manage-tags','matrices-manage-tags','tag-picker-manage'])$(id).onclick=()=>void openTagManager();
  $('tag-form').onsubmit=saveTag;$('tag-edit-cancel').onclick=resetTagForm;$('tag-manager-search').oninput=renderTagManager;
  $('tag-manager-close').onclick=()=>$('tag-manager').close();$('tag-picker-search').oninput=renderTagPicker;
  $('tag-picker-form').onsubmit=saveRecordTags;$('tag-picker-close').onclick=()=>$('tag-picker').close();
  $('tag-picker').onclose=()=>{tagPickerDraft=null;};
  $('tag-picker').oncancel=event=>{if($('tag-picker-save').disabled)event.preventDefault();};
}
