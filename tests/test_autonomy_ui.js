'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{
  constructor(tag,text,cls){this.tagName=tag.toUpperCase();this.textContent=text??'';this.className=cls||'';this.children=[];this.style={};this.value='';this.disabled=false;this.checked=false;}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(key,value){this[key]=value;}reportValidity(){return true;}
  all(){return this.children.flatMap(item=>[item,...item.all()]);}
}
const node=(...args)=>new Element(...args),plain=value=>JSON.parse(JSON.stringify(value));
const context=vm.createContext({Date,JSON,Number,Math,location:{origin:'http://localhost:8765'},globalThis:null});context.globalThis=context;
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../expman/static/autonomy.js'),'utf8'),context);
const initial=()=>({settings:{revision:0,enabled:false,instructions:'优先完成已提交实验',min_interval_seconds:60,review_seconds:300,max_turns:6,max_actions:8,max_calls_per_hour:24,operations:['queue','handoff']},provider:{enabled:true},runs:[],calls_last_hour:0,blocked_reason:null});
const event={preventDefault(){}},flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
async function main(){
  let current=initial(),calls=[],allowed=true,drop=false;
  const host=node('div'),api=async(path,body)=>{calls.push([path,body&&plain(body)]);if(body===undefined)return plain(current);if(path==='/api/agent/settings'){current.settings={...current.settings,...plain(body),revision:current.settings.revision+1};return plain(current.settings);}if(path==='/api/agent/delegate'){if(drop){drop=false;throw new Error('lost reply');}return {run_id:'run-1'};}throw new Error('unexpected endpoint');};
  const ui=context.ExLabAutonomy.mount({node,api,host,mobile:true,canControl:()=>allowed});await ui.poll(true);
  const form=host.all().find(n=>n.tagName==='FORM'),enabled=form.all().find(n=>n.tagName==='INPUT'&&n.type==='checkbox');
  enabled.checked=true;enabled.onchange();await flush();
  assert.equal(current.settings.enabled,true);assert.equal(calls.filter(([p,b])=>p==='/api/agent/settings'&&b).length,1);
  assert.equal(calls.filter(([p])=>p==='/api/planning/assist'||p==='/api/agent/call').length,0);
  console.log('PASS one automation toggle activates the background manager without a manual model or action click');
  const objective=form.all().find(n=>n.tagName==='TEXTAREA');objective.value='保留我正在编辑的目标';objective.oninput();current.settings.instructions='服务器上的旧目标';await ui.poll(true);assert.equal(objective.value,'保留我正在编辑的目标');
  console.log('PASS polling preserves unsaved objectives');
  const delegate=host.all().filter(n=>n.tagName==='FORM')[1],instructions=delegate.all().find(n=>n.tagName==='TEXTAREA');instructions.value='检查离线节点';drop=true;await delegate.onsubmit(event);await delegate.onsubmit(event);
  const submissions=calls.filter(([p])=>p==='/api/agent/delegate');assert.equal(submissions.length,2);assert.deepEqual(submissions[0][1],submissions[1][1]);
  console.log('PASS an uncertain delegation retries the same durable request identifier');
  allowed=false;await ui.poll(true);assert.equal(enabled.disabled,true);const before=calls.length;enabled.checked=false;enabled.onchange();await flush();assert.equal(calls.length,before);assert.ok(delegate.all().filter(n=>n.tagName==='BUTTON').every(n=>n.disabled));
  console.log('PASS Monitor read-only permissions disable writes and direct handler invocation remains guarded');
  allowed=true;current.provider.enabled=false;current.settings.enabled=true;await ui.poll(true);assert.equal(enabled.disabled,false);enabled.checked=false;enabled.onchange();await flush();assert.equal(current.settings.enabled,false);
  console.log('PASS automation can be disabled even while the provider is switched off');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
