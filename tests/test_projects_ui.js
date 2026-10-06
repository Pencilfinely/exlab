'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'../expman/static'),app=fs.readFileSync(path.join(root,'app.js'),'utf8');
const records=fs.readFileSync(path.join(root,'records.js'),'utf8'),projects=fs.readFileSync(path.join(root,'projects.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
function fragment(start,end){const begin=app.indexOf(start),limit=app.indexOf(end,begin);assert.ok(begin>=0&&limit>begin);return app.slice(begin,limit);}
function context(){
  const elements=new Map(),document={activeElement:null};
  function node(tag='div',text,cls){
    const attributes={},element={tagName:tag.toUpperCase(),children:[],value:'',className:cls||'',dataset:{},style:{},scrollTop:0,
      min:'',max:'',step:'',hidden:false,disabled:false,checked:false,required:false,open:false,_text:text===undefined?'':String(text),
      append(...items){for(const item of items){if(item.parentElement)item.parentElement.children=item.parentElement.children.filter(child=>child!==item);item.parentElement=this;this.children.push(item);}if(this.tagName==='SELECT'){const option=items.find(item=>item.selected)||(!this.value?items[0]:null);if(option)this.value=option.value;}},
      replaceChildren(...items){for(const child of this.children)child.parentElement=null;this.children=[];this._text='';this.append(...items);},
      setAttribute(key,value){attributes[key]=String(value);},getAttribute(key){return attributes[key]||'';},
      setCustomValidity(message){this.validationMessage=message;this.validity={customError:!!message};},
      checkValidity(){return this.disabled||(!this.validationMessage&&(!this.required||(this.type==='checkbox'?this.checked:!!this.value)));},
      reportValidity(){const invalid=this.querySelectorAll('input,select,textarea').find(input=>!input.checkValidity());if(invalid){invalid.focus();return false;}if(!this.checkValidity())this.focus();return this.checkValidity();},
      focus(){document.activeElement=this;},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;if(this.onclose)this.onclose();},
      matches(selector){return selector.split(',').some(raw=>{const value=raw.trim(),data=value.match(/^\[data-([a-z-]+)(?:="([^"]+)")?\](:checked)?$/);if(data){const key=data[1].replace(/-([a-z])/g,(_,char)=>char.toUpperCase());return key in this.dataset&&(data[2]===undefined||this.dataset[key]===data[2])&&(!data[3]||this.checked);}return value.startsWith('.')?this.className.split(' ').includes(value.slice(1)):this.tagName===value.toUpperCase();});},
      querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);},
      querySelector(selector){return this.querySelectorAll(selector)[0]||null;},
      closest(selector){for(let current=this;current;current=current.parentElement)if(current.matches(selector))return current;return null;},
    };
    element.classList={toggle(name,on){const classes=new Set(element.className.split(' ').filter(Boolean));on?classes.add(name):classes.delete(name);element.className=[...classes].join(' ');}};
    Object.defineProperty(element,'textContent',{get(){return this._text+this.children.map(child=>child.textContent).join('');},set(value){this._text=String(value);this.replaceChildren();this._text=String(value);}});
    Object.defineProperty(element,'options',{get(){return this.children;}});return element;
  }
  const $=id=>{if(!elements.has(id)){const element=node();element.id=id;elements.set(id,element);}return elements.get(id);};
  const controls={select:['project-preset','projects-page-size','projects-sort','projects-status-filter','jobs-status-filter'],input:['project-run-name','projects-search','projects-page','project-node-search','import-source','import-name','import-project-id','import-cwd','import-reviewed'],textarea:['project-params','import-sample-log']};
  for(const [tag,ids] of Object.entries(controls))for(const id of ids)$(id).tagName=tag.toUpperCase();$('import-reviewed').type='checkbox';$('import-reviewed').required=true;
  $('project-run-form').tagName='FORM';$('project-run-form').append(...['project-run-name','project-preset','project-param-fields','project-params','project-resources','project-scheduling','project-run-tags','project-run-submit'].map($));
  $('project-dialog').append(...['project-node-list','project-run-form','project-node-search','project-close','project-deploy'].map($));
  const importBody=node('div',undefined,'import-dialog-body');$('import-dialog').append($('import-wizard'));$('import-wizard').append(importBody);
  const panes={};for(const section of ['source','parameters','data','metrics','publish']){const pane=node('section');pane.dataset.importSection=section;panes[section]=pane;}
  $('import-source-form').tagName='FORM';panes.source.append($('import-source-form'));$('import-source-form').append($('import-source'));importBody.append(panes.source,$('import-review-form'));
  $('import-review-form').tagName='FORM';$('import-review-form').append(...Object.values(panes).slice(1));
  panes.parameters.append($('import-name'),$('import-project-id'),$('import-cwd'));panes.data.append($('import-assets'));panes.metrics.append($('import-sample-log'));panes.publish.append($('import-reviewed'));
  const env=vm.createContext({node,$,document,console,Map,Set,Date,Number,JSON,copy:plain,state:{projects:[],nodes:[],jobs:[],tags:[]},projectSelections:new Map(),projectPresets:[],
    importDraft:null,importJob:null,importPolling:false,importJsonDirty:false,
    timingNow:()=>1000,formatTimestamp:value=>'time:'+value,renderProjectCleanups(){},writeClipboardText:async()=>{},deleteProject(){},
    api:async()=>({ids:['job']}),notify(){},showView(){},refresh:async()=>{},inlineFeedback(id,message){$(id).textContent=message;},requestId:(()=>{let id=0;return()=>String(++id);})(),
    importMessage(message){$('import-status').textContent=message;},waitImportJob:async job=>job,
  });
  env.eval=code=>vm.runInContext(code,env);env.eval(records+projects);
  for(const [start,end] of [['function lines(','async function deleteProject('],['function objectFields(','function demoTemplate('],['function renderScheduling(','function renderImportEditors('],['function importBusy(','async function loadImportHistory('],['async function saveImport(publish){',"$('import-save').onclick"]])env.eval(fragment(start,end));
  env.eval('initializeProjectTools();');env.panes=panes;return env;
}
function fixture(count=1){
  const env=context(),worker={id:'worker',display_name:'GPU node',last_seen:1000,snapshot:{capabilities:['project-bundle-v1'],task_templates:[]}},projects=[];
  for(let i=0;i<count;i++){
    const project={digest:String(i+1).padStart(64,'0'),bundle_id:'bundle-'+i,project_id:'algo-'+i,name:'Algorithm '+i,created:1000+i,size:1048576,deployments:[{node_id:'worker',status:'installed',revision:1}]};projects.push(project);
    for(const preset of ['Short','Formal'])worker.snapshot.task_templates.push({name:project.name+' / '+preset,algorithm:project.name,project_id:project.project_id,project_bundle_id:project.bundle_id,experiment_id:preset,
      backend:'docker',params:{seed:42,epochs:preset==='Short'?3:100},resources:{cpu:2,ram_mb:8192,gpu_memory_mb:6000,exclusive:true}});
  }
  env.state.projects=projects;env.state.nodes=[worker];env.project=projects[0];env.worker=worker;return env;
}
function startRun(env){env.openProjectWorkspace(env.project,'run');return env;}
const checks=[],check=(name,callback)=>checks.push([name,callback]);
check('project lists bound rows, retain pagination and scroll, combine search with status, and separate package versions',()=>{
  const env=fixture(43);env.renderProjects();assert.equal(env.$('projects').children.length,10);assert.match(env.$('projects-page-summary').textContent,/43/);
  env.$('projects-next').onclick();env.$('projects-scroll').scrollTop=88;env.renderProjects();assert.equal(env.$('projects-scroll').scrollTop,88);assert.equal(env.eval('recordPages.projects.page'),2);
  env.$('projects-search').value='algo-17';env.$('projects-search').oninput();assert.equal(env.$('projects').children.length,1);assert.match(env.$('projects').textContent,/Algorithm 17/);
  env.$('projects-status-filter').value='failed';env.$('projects-status-filter').onchange();assert.match(env.$('projects').textContent,/没有匹配/);assert.equal(env.eval('recordPages.projects.page'),1);
  const previous=plain(env.project);env.worker.snapshot.task_templates[0].project_bundle_id='older-version';assert.equal(env.projectPresetsFor(env.project).length,1);assert.deepEqual(plain(env.project),previous);
});
check('opening project forms shows their identity immediately and preserves drafts, tags and resource edits across presets and close/reopen',()=>{
  const env=fixture();env.state.tags=[{id:'label',name:'基线',color:'#2563eb'}];startRun(env);const original=plain(env.worker.snapshot.task_templates);
  assert.equal(env.$('project-dialog').open,true);assert.equal(env.$('project-title').textContent,'Algorithm 0');assert.equal(env.$('project-run-form').hidden,false);
  env.$('project-run-name').value='Edited';const seed=env.$('project-param-fields').paramInputs.seed;seed.value='77';seed.oninput();
  env.$('project-resources').resourceInputs.ram_mb.value='2048';env.$('project-resources').resourceInputs.ram_mb.oninput();
  const tag=env.$('project-run-tags').querySelector('input');tag.checked=true;tag.onchange();
  const first=env.$('project-preset').value,second=env.$('project-preset').options[1].value;env.$('project-preset').value=second;env.$('project-preset').onchange();env.$('project-run-name').value='Second draft';
  env.$('project-preset').value=first;env.$('project-preset').onchange();assert.equal(env.$('project-run-name').value,'Edited');assert.equal(env.readProjectRunDraft().spec.params.seed,77);
  env.$('project-dialog').close();startRun(env);const draft=env.readProjectRunDraft();assert.equal(draft.spec.resources.ram_mb,2048);assert.deepEqual(plain(draft.tag_ids),['label']);assert.deepEqual(plain(env.worker.snapshot.task_templates),original);
});
check('invalid parameter text stays in its own draft and cannot silently submit the prior valid value',async()=>{
  const env=startRun(fixture()),first=env.$('project-preset').value,second=env.$('project-preset').options[1].value;
  let seed=env.$('project-param-fields').paramInputs.seed;seed.value='invalid';seed.oninput();env.$('project-preset').value=second;env.$('project-preset').onchange();env.$('project-preset').value=first;env.$('project-preset').onchange();
  seed=env.$('project-param-fields').paramInputs.seed;assert.equal(seed.value,'invalid');assert.throws(()=>env.readProjectRunDraft(),/无效输入/);
  seed.value='9';seed.oninput();assert.equal(env.readProjectRunDraft().spec.params.seed,9);
  env.$('project-params').value='{broken';env.$('project-params').onchange();env.$('project-dialog').close();startRun(env);assert.equal(env.$('project-params').value,'{broken');assert.throws(()=>env.readProjectRunDraft(),/JSON 无效/);
});
check('polling preserves focused experiment inputs and deployment checkboxes while status text changes',()=>{
  const env=startRun(fixture()),seed=env.$('project-param-fields').paramInputs.seed;seed.value='91';seed.oninput();seed.focus();env.renderProjects();assert.equal(env.$('project-param-fields').paramInputs.seed,seed);assert.equal(env.document.activeElement,seed);
  env.setProjectTab('deploy');env.project.deployments=[];env.renderProjectWorkspace();const checkbox=env.$('project-node-list').querySelector('input');checkbox.checked=true;checkbox.onchange();checkbox.focus();env.worker.last_seen=0;env.renderProjects();
  assert.equal(env.$('project-node-list').querySelector('input'),checkbox);assert.equal(checkbox.checked,true);assert.equal(env.document.activeElement,checkbox);assert.match(env.$('project-node-list').textContent,/离线/);
});
check('bulk selection skips installed, pending and unsupported nodes; offline selection queues once and survives searching',async()=>{
  const env=fixture();for(const [id,online,supported] of [['new',true,true],['offline',false,true],['pending',true,true],['legacy',true,false]])env.state.nodes.push({id,last_seen:online?1000:0,snapshot:{capabilities:supported?['project-bundle-v1']:[],task_templates:[]}});
  env.project.deployments.push({node_id:'pending',status:'downloading'});env.openProjectWorkspace(env.project,'deploy');env.$('project-select-online').onclick();assert.deepEqual([...env.projectSelections.get(env.project.digest)],['new']);
  const offline=env.$('project-node-list').querySelectorAll('input').find(input=>input.value==='offline');offline.checked=true;offline.onchange();env.$('project-node-search').value='new';env.$('project-node-search').oninput();assert.equal(env.projectSelections.get(env.project.digest).size,2);
  let release;const pending=new Promise(resolve=>release=resolve),requests=[];env.api=async(path,payload)=>{requests.push([path,plain(payload)]);await pending;return {};};
  const first=env.deployLibraryProject();await env.deployLibraryProject();release();await first;assert.equal(requests.length,1);assert.deepEqual(requests[0][1].node_ids,['new','offline']);assert.equal(env.projectSelections.get(env.project.digest).size,0);
});
check('lost submission responses reuse the request identifier and leave the edited form intact',async()=>{
  const env=startRun(fixture()),requests=[];let calls=0;env.api=async(path,payload)=>{requests.push(plain(payload));if(!calls++)throw Error('lost reply');return {ids:['one']};};env.$('project-run-name').value='Retry run';
  await env.submitProjectRun({preventDefault(){}});assert.equal(env.$('project-dialog').open,true);assert.equal(env.$('project-run-name').value,'Retry run');await env.submitProjectRun({preventDefault(){}});
  assert.deepEqual(requests[0],requests[1]);assert.equal(env.$('project-dialog').open,false);
});
check('preview and matrix conversion carry the same overrides and never submit an experiment',async()=>{
  const env=startRun(fixture()),requests=[];env.$('project-resources').resourceInputs.cpu.value='0.5';env.$('project-resources').resourceInputs.cpu.oninput();env.$('project-run-name').value='x'.repeat(200);
  env.api=async(path,payload)=>{requests.push([path,plain(payload)]);return {allocations:[{}],warnings:['Offline worker']};};await env.previewProjectRun();assert.equal(requests[0][0],'/api/scheduling/preview');assert.equal(requests[0][1].spec.resources.cpu,0.5);assert.match(env.$('project-run-preview-result').textContent,/排队/);
  let matrix;env.openMatrix=value=>matrix=plain(value);env.$('project-run-matrix').onclick();assert.equal(requests.length,1);assert.equal(matrix.name.length,160);assert.equal(matrix.spec.name.length,200);assert.equal(matrix.spec.resources.cpu,0.5);
});
check('deployment completion exposes new presets without replacing the workspace or losing selected nodes',()=>{
  const env=fixture();env.project.deployments=[];env.openProjectWorkspace(env.project,'run');assert.equal(env.$('project-run-form').hidden,true);env.project.deployments=[{node_id:'worker',status:'installed'}];env.renderProjects();assert.equal(env.$('project-run-form').hidden,false);assert.equal(env.$('project-preset').options.length,2);
});
check('keyboard tab navigation moves focus and skips import steps that are not ready',()=>{
  const env=startRun(fixture());const key=value=>({key:value,preventDefault(){}});
  env.$('project-tab-run').onkeydown(key('ArrowRight'));assert.equal(env.document.activeElement,env.$('project-tab-info'));assert.equal(env.$('project-pane-run').hidden,true);
  env.$('project-tab-info').onkeydown(key('Home'));assert.equal(env.document.activeElement,env.$('project-tab-deploy'));
  env.openProjectImport();env.$('import-step-source').onkeydown(key('End'));assert.equal(env.document.activeElement,env.$('import-step-source'));assert.equal(env.$('import-step-data').tabIndex,-1);
});
check('import steps gate on discovery, preserve inputs, reveal hidden invalid fields and return to the current step after saving',async()=>{
  const env=context();env.openProjectImport();assert.equal(env.$('import-step-data').disabled,true);env.showProjectImportStep('publish');assert.equal(env.eval('projectImportStep'),'source');
  env.importDraft={project:{name:'Imported',source:'E:/source',assets:{data:{path:'E:/data'}},runtime:{requirements:['numpy==2']}},harness:{command:['python','train.py'],metrics:[]},experiments:[{params:{seed:42}}]};env.importJob={id:'import'};env.$('import-source').value='E:/source';env.$('import-sample-log').value='epoch 1 loss 0.2';
  env.showProjectImportStep('publish');assert.match(env.$('import-publish-summary').textContent,/Imported/);env.showProjectImportStep('metrics');assert.equal(env.$('import-sample-log').value,'epoch 1 loss 0.2');
  const details=env.node('details'),invalid=env.node('input');invalid.required=true;details.append(invalid);env.$('import-assets').append(details);let calls=0;env.api=async()=>{calls++;return {status:'ready',draft:plain(env.importDraft)};};
  await env.saveImport(false);assert.equal(calls,0);assert.equal(env.eval('projectImportStep'),'data');assert.equal(details.open,true);assert.equal(env.document.activeElement,invalid);
  invalid.value='E:/data';env.showProjectImportStep('metrics');env.renderImportDraft=draft=>{env.importDraft=draft;env.showProjectImportStep('parameters');};await env.saveImport(false);assert.equal(calls,1);assert.equal(env.eval('projectImportStep'),'metrics');
});
(async()=>{for(const [name,callback] of checks){await callback();console.log('PASS '+name);}console.log(checks.length+' project workflow UI checks passed');})().catch(error=>{console.error(error);process.exitCode=1;});
