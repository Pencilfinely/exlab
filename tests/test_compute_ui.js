'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
  constructor(tag,text,cls){this.tagName=tag.toUpperCase();this.textContent=text===undefined?'':String(text);this.className=cls||'';this.children=[];this.dataset={};this.disabled=false;this.checked=false;this._value='';}
  set value(value){this._value=String(value);}get value(){return this._value;}
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
  compute.policyEditor({id:'rog',compute_policy:original},ctx,true);
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
  assert.equal(box.all().filter(x=>x.tagName==='BUTTON').length,1);assert.equal(box.all().find(x=>x.tagName==='BUTTON').disabled,true);
  console.log('PASS unsupported workers cannot invoke new policies or game release');
  calls=[];ctx.api=async(path,payload)=>{calls.push([path,plain(payload)]);return {summary:'先安排稳定节点',warnings:[],node_policies:[{node_id:'rog',previous:original,policy:{...original,revision:7,mode:'release'}}],queue_priorities:[]};};
  compute.mount(ctx);const ai=body.children.find(x=>x.textContent==='AI 规划建议');ai.onclick();
  const assistant=body.children.at(-1).children[0];assistant.all().find(x=>x.tagName==='TEXTAREA').value='今晚运行';
  await assistant.onsubmit(event);assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/planning/assist');
  const apply=assistant.all().find(x=>x.textContent==='应用此节点建议');assert.ok(apply);await apply.onclick();assert.equal(calls[1][0],'/api/compute-policy');
  console.log('PASS AI advice stays a preview until an individual apply action');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
