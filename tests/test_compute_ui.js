'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
  constructor(tag,text,cls){this.tagName=tag.toUpperCase();this.textContent=text===undefined?'':String(text);this.className=cls||'';this.children=[];this.dataset={};this.disabled=false;this.checked=false;this._value='';}
  set value(value){this._value=String(value);}get value(){return this._value;}
  get parentElement(){return this.parent;}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  addEventListener(){}showModal(){this.open=true;}close(){this.open=false;}reportValidity(){return true;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  before(item){if(this.parent){const index=this.parent.children.indexOf(this);this.parent.children.splice(index,0,item);item.parent=this.parent;}}
  after(item){if(this.parent){const index=this.parent.children.indexOf(this);this.parent.children.splice(index+1,0,item);item.parent=this.parent;}}
  all(){return this.children.flatMap(item=>[item,...item.all()]);}
  querySelector(selector){return this.all().find(item=>selector==='[data-planning]'?item.dataset.planning!==undefined:/data-control="([^"]+)"/.test(selector)&&item.dataset.control===selector.match(/data-control="([^"]+)"/)[1])||null;}
}
const node=(...args)=>new Element(...args),body=node('body'),document={body},elements=new Map();
const context=vm.createContext({document,Date,JSON,Number,globalThis:null});context.globalThis=context;
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../expman/static/compute.js'),'utf8'),context);
const compute=context.ExLabCompute,plain=value=>JSON.parse(JSON.stringify(value));
const $=id=>{if(!elements.has(id)){const element=node('div');element.id=id;elements.set(id,element);body.append(element);}return elements.get(id);};
const event={preventDefault(){}};
async function main(){
  let calls=[],fail=true;
  const ctx={node,$,notify(){},refresh:async()=>{},api:async(path,payload)=>{calls.push([path,plain(payload)]);if(fail){fail=false;throw new Error('lost response');}return {policy:{...payload.policy,revision:8}};}};
  const original={...plain(compute.defaults()),revision:7,kind:'portable',require_ac:true,reliability:.8,maintenance:[{start:2000000000,end:2000001000,reason:'维护'}],risk_windows:[{window:'22:00-08:00',risk:.6}]};
  compute.policyEditor({id:'rog',mode:'run',compute_policy:original,snapshot:{capabilities:['dynamic-compute-v1','compute-control-v2']}},ctx,true);
  const dialog=body.children.at(-1),form=dialog.children[0];
  await form.onsubmit(event);await form.onsubmit(event);
  assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);
  const policy=calls[1][1].policy;assert.equal(policy.revision,7);assert.equal(policy.mode,'release');assert.equal(policy.require_ac,true);assert.equal(policy.reliability,.8);assert.deepEqual(policy.maintenance,original.maintenance);assert.deepEqual(policy.risk_windows,original.risk_windows);assert.ok(policy.until>Date.now()/1000+7000);
  assert.equal(original.mode,'available');
  console.log('PASS release preview preserves restrictions, maintenance, risk and identical retry revisions');
  const planning=node('div');compute.renderPlanning(planning,{expected_seconds:0,deadline:null,migrate:false},node);
  assert.deepEqual(plain(compute.readPlanning(planning)),{expected_seconds:0,deadline:null,migrate:false});
  const untouched=node('div');compute.renderPlanning(untouched,undefined,node);assert.equal(compute.readPlanning(untouched),undefined);
  const timed=node('div');compute.renderPlanning(timed,{deadline:2000000001.25},node);assert.equal(compute.readPlanning(timed).deadline,2000000001.25);
  console.log('PASS planning preserves explicit zero and disabled migration, with no silent default changes');
  const box=node('div');compute.renderNode(box,{id:'old',snapshot:{capabilities:[]}},ctx);
  assert.equal(box.all().filter(x=>x.tagName==='BUTTON').length,3);assert.equal(box.all().find(x=>x.textContent==='节点长期设置').disabled,true);
  calls=[];const legacy=compute.pauseEditor({id:'old',mode:'run',snapshot:{capabilities:[]}},ctx).children[0];
  assert.equal(legacy.all().filter(x=>x.type==='radio'&&x.disabled).length,2);
  await legacy.onsubmit(event);assert.equal(calls[0][0],'/api/node-mode');assert.equal(calls[0][1].mode,'drain');
  console.log('PASS older workers retain simple admission pause with unsupported stop/release disabled');
  const desired={mode:'release',revision:8,source:'center',until:null,reason:'游戏'};
  const worker={id:'70ti',online:true,mode:'run',compute_policy:{revision:8},snapshot:{compute_policy_revision:8,availability:desired}};
  assert.equal(compute.nodeState(worker).label,'等待 Worker 核验');
  worker.snapshot.compute_control={policy:{...desired},phase:'stopping',experiments_stopped:false,resources_released:false,detail:'正在保存'};
  assert.equal(compute.nodeState(worker).label,'正在保存并停止实验');
  worker.snapshot.compute_control.phase='blocked';assert.equal(compute.nodeState(worker).label,'执行未完成 · 需要检查');
  worker.snapshot.compute_control={policy:{...desired},phase:'released',experiments_stopped:true,resources_released:true,detail:'控制代理及 Docker/WSL 保留'};
  assert.equal(compute.nodeState(worker).label,'训练资源已释放 · 接单已暂停');
  worker.snapshot.availability={...desired,reason:'新的请求'};assert.equal(compute.nodeState(worker).label,'等待 Worker 核验');
  worker.snapshot.availability=desired;worker.online=false;assert.equal(compute.nodeState(worker).label,'离线 · 当前执行状态未知');
  worker.online=true;worker.compute_policy.revision=9;assert.equal(compute.nodeState(worker).label,'策略待 Worker 接收');
  console.log('PASS policy ACK, stale receipt, blocked stop and offline nodes never masquerade as released');
  const modern={id:'70ti',mode:'drain',compute_policy:original,snapshot:{capabilities:['dynamic-compute-v1','compute-control-v2']}};
  calls=[];const stopForm=compute.pauseEditor(modern,ctx).children[0],stopSelect=stopForm.all().find(x=>x.dataset.pauseMode==='stop');
  assert.equal(calls.length,0);assert.equal(stopForm.all().filter(x=>x.type==='radio').length,3);stopSelect.onchange();
  assert.ok(stopForm.all().some(x=>x.textContent.includes('保留本机任务和环境')));
  calls=[];await stopForm.onsubmit(event);assert.equal(calls[0][1].policy.mode,'stop');
  assert.equal(calls[0][1].policy.until,null);assert.equal(calls[0][1].expected_node_mode,'drain');
  assert.deepEqual(calls[0][1].policy.maintenance,original.maintenance);
  assert.equal(stopForm.all().filter(x=>x.type==='number').length,0);
  calls=[];const resume=compute.pauseEditor(modern,ctx,'available').children[0];await resume.onsubmit(event);
  assert.equal(calls[0][1].policy.mode,'available');assert.equal(calls[0][1].policy.until,null);assert.equal(calls[0][1].expected_node_mode,'drain');
  assert.equal(calls[0][1].policy.require_ac,true);
  calls=[];const customForm=compute.pauseEditor(modern,ctx).children[0],duration=customForm.all().find(x=>x.tagName==='SELECT');
  duration.value='custom';duration.onchange();await customForm.onsubmit(event);assert.equal(calls.length,0);
  customForm.all().find(x=>x.type==='datetime-local').value='2035-01-01T10:00';await customForm.onsubmit(event);assert.equal(calls.length,1);
  const notes=compute.restrictions({...modern,snapshot:{local_compute_policy:{mode:'release',require_ac:true},ac_power:false}});
  assert.ok(notes.some(x=>x.includes('本机也设置了暂停')));assert.ok(notes.some(x=>x.includes('未接电源')));
  calls=[];const blockedResume=compute.pauseEditor({...modern,mode:'run',compute_policy:plain(compute.defaults()),
    snapshot:{...modern.snapshot,local_compute_policy:{mode:'drain'},ac_power:false}},ctx,'available').children[0];
  assert.ok(blockedResume.all().some(x=>x.tagName==='BUTTON'&&x.disabled&&x.textContent==='请先满足上方条件'));
  await blockedResume.onsubmit(event);assert.equal(calls.length,0);
  compute.policyEditor({...modern,compute_policy:{...original,mode:'stop',until:2000000000}},ctx,false);
  const settingsForm=body.children.at(-1).children[0];calls=[];await settingsForm.onsubmit(event);
  assert.equal(calls[0][1].policy.mode,'stop');assert.equal(calls[0][1].policy.until,2000000000);
  console.log('PASS simple pause/resume, manual/custom expiry, restrictions and independent long-term settings');
  calls=[];ctx.api=async(path,payload)=>{calls.push([path,plain(payload)]);return {summary:'先安排稳定节点',warnings:[],node_policies:[{node_id:'rog',previous:original,policy:{...original,revision:7,mode:'release'}}],queue_priorities:[]};};
  compute.mount(ctx);const ai=body.children.find(x=>x.textContent==='AI 规划建议');ai.onclick();
  const assistant=body.children.at(-1).children[0];assistant.all().find(x=>x.tagName==='TEXTAREA').value='今晚运行';
  await assistant.onsubmit(event);assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/planning/assist');
  const apply=assistant.all().find(x=>x.textContent==='应用此节点建议');assert.ok(apply);await apply.onclick();assert.equal(calls[1][0],'/api/compute-policy');
  console.log('PASS AI advice stays a preview until an individual apply action');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
