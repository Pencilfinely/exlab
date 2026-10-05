'use strict';
// Behavioral checks for navigation, label filtering, and template draft/retry flows.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'../expman/static');
const app=fs.readFileSync(path.join(root,'app.js'),'utf8');
const records=fs.readFileSync(path.join(root,'records.js'),'utf8'),templates=fs.readFileSync(path.join(root,'templates.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
function fragment(start,end){const begin=app.indexOf(start),limit=app.indexOf(end,begin);assert.ok(begin>=0&&limit>begin);return app.slice(begin,limit);}
function context(){
  const elements=new Map(),document={activeElement:null};
  function node(tag='div',text,cls){
    const attributes={},element={tagName:tag.toUpperCase(),children:[],value:'',className:cls||'',dataset:{},style:{},scrollTop:0,
      min:'',max:'',step:'',hidden:false,disabled:false,checked:false,open:false,_text:text===undefined?'':String(text),
      classList:{toggle(){}},
      append(...items){this.children.push(...items);if(this.tagName==='SELECT'){const option=items.find(item=>item.selected)||(!this.value?items[0]:null);if(option)this.value=option.value;}},
      replaceChildren(...items){this.children=[];this._text='';this.append(...items);},
      setAttribute(key,value){attributes[key]=String(value);},getAttribute(key){return attributes[key]||'';},
      setCustomValidity(message){this.validationMessage=message;},reportValidity(){return !this.validationMessage&&this.querySelectorAll('input,select,textarea').every(input=>!input.validationMessage);},
      focus(){document.activeElement=this;},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;if(this.onclose)this.onclose();},
      querySelectorAll(selector){const match=child=>selector.split(',').some(raw=>{const value=raw.trim(),control=value.match(/^\[data-control="([^"]+)"\](:checked)?$/);return control?child.dataset.control===control[1]&&(!control[2]||child.checked):child.tagName===value.toUpperCase();});return this.children.flatMap(child=>[...(match(child)?[child]:[]),...child.querySelectorAll(selector)]);},
      querySelector(selector){return this.querySelectorAll(selector)[0]||null;},
    };
    Object.defineProperty(element,'textContent',{get(){return this._text+this.children.map(child=>child.textContent).join('');},set(value){this._text=String(value);this.children=[];}});
    return element;
  }
  const $=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);};
  const env=vm.createContext({node,$,document,console,Map,Set,Date,Number,JSON,copy:plain,
    state:{jobs:[],nodes:[],tags:[]},terminal:['succeeded','failed','paused','canceled','interrupted'],
    names:{succeeded:'已完成',running:'运行中',paused:'已保存停止'},formatTimestamp:value=>Number.isFinite(value)?'time:'+value:'—',
    timingNow:()=>1000,jobTimer:()=>node('span','duration'),matrixDraft:null,matrixResultId:null,
    api:async()=>({ids:['submitted']}),notify(){},detail(){},showView(){},refresh:async()=>{},loadMatrices:async()=>{},
    inlineFeedback(id,message){$(id).textContent=message;},requestId:(()=>{let id=0;return()=>String(++id);})(),
  });
  env.eval=code=>vm.runInContext(code,env);
  env.eval(records+templates);
  for(const [start,end] of [['function lines(','async function deleteProject('],['function objectFields(','function renderProjectParameters('],['function renderScheduling(','function renderImportEditors('],['function renderJobs(){',"$('filter').oninput"],['function renderMatrices(){','function matrixPayload('],['function renderMatrixResultJobs(){',"$('matrix-result-refresh').onclick"]])env.eval(fragment(start,end));
  env.eval('initializeRecordTools();initializeNodeTemplateTools();');
  return env;
}
const checks=[];function check(name,callback){checks.push([name,callback]);}
function jobs(count){return Array.from({length:count},(_,index)=>({id:String(index),created:100+index,updated:200+index,state:index%2?'succeeded':'running',node_id:'worker',spec:{name:'Run '+index,algorithm:'Model',group:'Study'},tags:index%3?[{id:'a',name:'基线',color:'#2563eb'}]:[],metrics:{}}));}
check('pagination bounds rendered rows, preserves navigation and scroll on refresh, and clamps shrinking results',()=>{
  const env=context();env.state.jobs=jobs(67);env.renderJobs();assert.equal(env.$('jobs').children.length,20);
  env.$('jobs-next').onclick();assert.equal(env.$('jobs-page').value,'2');assert.match(env.$('jobs-page-summary').textContent,/21–40/);
  env.$('jobs-scroll').scrollTop=240;env.renderJobs();assert.equal(env.$('jobs-scroll').scrollTop,240);assert.equal(env.$('jobs-page').value,'2');
  env.$('jobs-last').onclick();assert.equal(env.$('jobs').children.length,7);assert.equal(env.$('jobs-next').disabled,true);
  env.state.jobs=env.state.jobs.slice(0,23);env.renderJobs();assert.equal(env.$('jobs-page').value,'2');assert.equal(env.$('jobs').children.length,3);
  env.$('jobs-page-size').onchange({target:{value:'50'}});assert.equal(env.$('jobs').children.length,23);assert.equal(env.$('jobs-scroll').scrollTop,0);
});
check('keyword, status, and label filters combine and reset the page without blank results',()=>{
  const env=context();env.state.jobs=jobs(67);env.state.tags=[{id:'a',name:'基线',color:'#2563eb'}];env.refreshTagControls();env.renderJobs();env.$('jobs-last').onclick();
  env.$('jobs-tag-filter').value='a';env.$('jobs-tag-filter').onchange();assert.equal(env.$('jobs-page').value,'1');assert.equal(env.filteredJobs().length,44);
  env.$('jobs-status-filter').value='succeeded';env.$('jobs-status-filter').onchange();assert.equal(env.filteredJobs().length,22);
  env.$('filter').value='基线';env.$('filter').oninput();assert.equal(env.filteredJobs().length,22);
  env.$('jobs-tag-filter').value='__none';env.$('jobs-tag-filter').onchange();assert.equal(env.filteredJobs().length,0);assert.match(env.$('empty').textContent,/没有匹配/);
  env.$('filter').value='';env.$('filter').oninput();assert.equal(env.filteredJobs().length,11);
});
check('matrix pagination shows creation times and stays on the current page after polling',()=>{
  const env=context();env.fixture=Array.from({length:23},(_,index)=>({id:String(index),name:'Matrix '+index,created:100+index,count:4,job_count:0,revision:1,tags:[]}));
  env.eval('matrixRecords=fixture');env.renderMatrices();assert.equal(env.$('matrix-list').children.length,10);
  env.$('matrices-next').onclick();const first=env.$('matrix-list').children[0].textContent;assert.match(first,/Matrix 10/);assert.match(first,/创建于 time:110/);
  env.$('matrices-scroll').scrollTop=320;env.renderMatrices();assert.equal(env.$('matrices-page').value,'2');assert.equal(env.$('matrices-scroll').scrollTop,320);
  env.$('matrix-filter').value='Matrix 22';env.$('matrix-filter').oninput();assert.equal(env.$('matrix-list').children.length,1);assert.equal(env.$('matrices-page').value,'1');
});
check('completion dates use worker timestamps and label legacy confirmation times without inventing a running completion',()=>{
  const env=context();assert.equal(env.completionCell({state:'running',updated:999}).textContent,'—');
  assert.match(env.completionCell({state:'succeeded',updated:999,timing:{finished_at:700}}).textContent,/time:700完成/);
  assert.match(env.completionCell({state:'succeeded',updated:999}).textContent,/time:999.*主控确认/);
  assert.match(env.completionCell({state:'paused',updated:999,timing:{finished_at:700}}).textContent,/已保存停止/);
});
check('label edits keep names as text, preserve selected IDs and prune globally deleted labels',()=>{
  const env=context();env.state.tags=[{id:'a',name:'<script>baseline</script>',color:'#2563eb'}];env.refreshTagControls();
  const chip=env.tagChip(env.state.tags[0]);assert.equal(chip.children[1].textContent,'<script>baseline</script>');assert.equal(chip.children[1].children.length,0);
  env.$('jobs-tag-filter').value='a';env.state.tags[0].name='Renamed';env.refreshTagControls();assert.equal(env.$('jobs-tag-filter').value,'a');
  let selected=['a'];env.renderDraftTags('matrix-tags',()=>selected,values=>selected=values);
  env.state.tags=[];env.refreshTagControls();assert.deepEqual(plain(selected),[]);assert.equal(env.$('jobs-tag-filter').value,'');
});
function templateContext(){
  const env=context();const originals=[{name:'First',backend:'demo',algorithm:'Model',group:'Study',params:{seed:42,steps:5},resources:{gpu_memory_mb:0,cpu:1,ram_mb:256,exclusive:false}},
    {name:'Second',backend:'demo',params:{seed:10},resources:{gpu_memory_mb:0,cpu:1,ram_mb:256,exclusive:false}}];
  env.state.nodes=[{id:'worker',last_seen:1000,mode:'run',snapshot:{task_templates:originals}}];env.originals=plain(originals);env.openNodeTemplates('worker');return env;
}
check('node templates open as forms with source scheduling, keep per-template edits, and never modify worker templates',()=>{
  const env=templateContext();assert.equal(env.$('node-template-dialog').open,true);
  const name=env.$('node-template-name');name.value='Custom run';const seed=env.$('node-template-params').children[0].children[0];seed.value='99';seed.oninput();seed.focus();
  const inputs=env.$('node-template-resources').resourceInputs;inputs.ram_mb.value='512';inputs.ram_mb.oninput();
  const payload=env.readNodeTemplateDraft();assert.equal(payload.spec.params.seed,99);assert.equal(payload.spec.resources.ram_mb,512);assert.deepEqual(plain(payload.spec.scheduling.node_ids),['worker']);
  env.renderNodeTemplates(env.node('div'),env.state.nodes[0]);assert.equal(env.document.activeElement,seed);
  env.selectNodeTemplate(1);env.$('node-template-name').value='Second draft';env.selectNodeTemplate(0);
  assert.equal(env.$('node-template-name').value,'Custom run');assert.equal(env.readNodeTemplateDraft().spec.params.seed,99);
  assert.deepEqual(plain(env.state.nodes[0].snapshot.task_templates),env.originals);
  env.$('node-template-reset').onclick();assert.equal(env.readNodeTemplateDraft().spec.params.seed,42);
});
check('lost template submission responses retry the identical request and retain the draft',async()=>{
  const env=templateContext(),requests=[];let calls=0;
  env.api=async(endpoint,payload)=>{assert.equal(endpoint,'/api/jobs');requests.push(plain(payload));if(calls++===0)throw Error('Response lost');return{ids:['created']};};
  env.$('node-template-name').value='Retry experiment';await env.submitNodeTemplate({preventDefault(){}});
  assert.equal(env.$('node-template-dialog').open,true);assert.equal(env.$('node-template-name').value,'Retry experiment');
  await env.submitNodeTemplate({preventDefault(){}});assert.deepEqual(requests[0],requests[1]);assert.equal(env.$('node-template-dialog').open,false);
});
check('template to matrix carries user parameters, resource edits and labels without submitting a job',()=>{
  const env=templateContext();env.state.tags=[{id:'a',name:'基线',color:'#2563eb'}];env.refreshTagControls();
  env.eval("nodeTemplateSession.drafts.get(0).tag_ids=['a']");env.$('node-template-name').value='Selected run';let definition;env.openMatrix=value=>definition=value;
  env.$('node-template-matrix').onclick();assert.equal(definition.name,'Selected run · 矩阵');assert.deepEqual(plain(definition.tag_ids),['a']);assert.equal(definition.spec.params.seed,42);
  env.$('node-template-name').value='x'.repeat(200);env.$('node-template-matrix').onclick();assert.equal(definition.name.length,160);assert.equal(definition.spec.name.length,200);
});
(async()=>{for(const [name,callback] of checks){await callback();console.log('PASS '+name);}console.log(checks.length+' record/template UI checks passed');})().catch(error=>{console.error(error);process.exitCode=1;});
