'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Element{
  constructor(tag,text,cls){this.tagName=tag.toUpperCase();this.textContent=text??'';this.className=cls||'';this.children=[];this.style={};this.value='';this.disabled=false;this.checked=false;}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=[];this.append(...items);}
  setAttribute(key,value){this[key]=value;}reportValidity(){return true;}focus(){this.focused=true;}
  all(){return this.children.flatMap(item=>[item,...item.all()]);}
}
const node=(...args)=>new Element(...args),plain=value=>JSON.parse(JSON.stringify(value));
const context=vm.createContext({Date,JSON,Number,Math,location:{origin:'http://localhost:8765'},globalThis:null});context.globalThis=context;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../expman/static/autonomy.js'),'utf8'),context);
const initial=()=>({settings:{revision:0,enabled:false,instructions:'优先完成已提交实验',rule_interval_seconds:10,min_interval_seconds:900,review_seconds:3600,max_runs_per_hour:4,max_turns:6,max_actions:8,max_calls_per_hour:24,allow_rule_authoring:true,operations:['queue','handoff']},provider:{enabled:true},runs:[],run_total:0,runs_last_hour:0,calls_last_hour:0,rules:{open_count:0,last_scan_at:100,findings:[]},blocked_reason:null});
const event={preventDefault(){}},flush=async()=>{for(let i=0;i<14;i++)await Promise.resolve();};
const byClass=(host,cls)=>host.all().find(n=>n.className.split(' ').includes(cls));
const byText=(host,text)=>host.all().find(n=>n.tagName==='BUTTON'&&n.textContent===text);
const field=(host,label)=>host.all().find(n=>n['aria-label']===label);
function setup(){
  const fixture={current:initial(),calls:[],allowed:true,drop:false,history:[],rules:[],report:null},host=node('div');
  const api=async(p,body)=>{fixture.calls.push([p,body&&plain(body)]);if(p==='/api/agent/status')return plain(fixture.current);
    if(p.startsWith('/api/agent/history')){const offset=Number(new URL(p,'http://local').searchParams.get('offset'));return {runs:plain(fixture.history.slice(offset,offset+5)),total:fixture.history.length,next_offset:offset+5<fixture.history.length?offset+5:null};}
    if(p.startsWith('/api/agent/run?')){if(fixture.report)return fixture.report;return {state:'completed',created:1,summary:'完整报告正文',outcomes:[{operation:'queue',status:'verified'}],steps:[{seq:1,name:'exlab_topology',arguments:{},result:{}}]};}
    if(p==='/api/agent/rules')return {rules:plain(fixture.rules),fields:{job:['state']},operators:['eq'],effects:['escalate'],limits:{custom_rules:24}};
    if(p==='/api/agent/rules/preview')return {matched:2};
    if(p==='/api/agent/rules/save')return {revision:1};
    if(p==='/api/agent/settings'){fixture.current.settings={...fixture.current.settings,...plain(body),revision:fixture.current.settings.revision+1};return plain(fixture.current.settings);}
    if(p==='/api/agent/delegate'){if(fixture.drop){fixture.drop=false;throw Error('lost reply');}return {run_id:'run-1'};}
    if(p.startsWith('/api/agent/inspection'))return {items:[],total:0,next_offset:null,events:[]};
    throw Error('unexpected endpoint '+p);
  };
  fixture.host=host;fixture.ui=context.ExLabAutonomy.mount({node,api,host,mobile:true,canControl:()=>fixture.allowed});return fixture;
}
async function main(){
  const f=setup(),{host,ui}=f;await ui.poll(true);
  const enabled=field(host,'AI 自动介入'),form=byClass(host,'agent-settings-form');
  enabled.checked=true;enabled.onchange();await flush();
  assert.equal(f.current.settings.enabled,true);assert.equal(f.calls.filter(([p,b])=>p==='/api/agent/settings'&&b).length,1);
  assert.equal(f.calls.filter(([p])=>p==='/api/planning/assist'||p==='/api/agent/call').length,0);
  console.log('PASS one toggle activates the background manager without a manual model click');
  const objective=field(host,'托管目标');objective.value='保留正在编辑的目标';objective.oninput();f.current.settings.instructions='旧目标';await ui.poll(true);assert.equal(objective.value,'保留正在编辑的目标');
  console.log('PASS polling preserves unsaved objectives');
  assert.equal(field(host,'AI 最短间隔（分钟）').value,15);assert.equal(field(host,'AI 定期复查（分钟）').value,60);
  field(host,'AI 最短间隔（分钟）').value=20;field(host,'AI 定期复查（分钟）').value=90;field(host,'每小时 AI 研判轮数').value=3;await form.onsubmit(event);await flush();
  const setting=f.calls.filter(([p,b])=>p==='/api/agent/settings'&&b).at(-1)[1];assert.equal(setting.min_interval_seconds,1200);assert.equal(setting.review_seconds,5400);assert.equal(setting.max_runs_per_hour,3);assert.ok(!('max_calls_per_hour' in setting));assert.equal(field(host,'规则检查间隔（秒）').value,10);
  console.log('PASS independent rule seconds / AI minutes and round budgets avoid conflicting call limits');
  const delegate=byClass(host,'agent-delegate-form'),instructions=field(host,'一次性委托目标');instructions.value='检查离线节点';f.drop=true;await delegate.onsubmit(event);await delegate.onsubmit(event);
  const submissions=f.calls.filter(([p])=>p==='/api/agent/delegate');assert.equal(submissions.length,2);assert.deepEqual(submissions[0][1],submissions[1][1]);
  console.log('PASS an uncertain delegation retries the same durable request identifier');
  f.allowed=false;await ui.poll(true);assert.equal(enabled.disabled,true);const before=f.calls.length;enabled.checked=false;enabled.onchange();await flush();assert.equal(f.calls.length,before);assert.ok(delegate.all().filter(n=>n.tagName==='BUTTON').every(n=>n.disabled));
  console.log('PASS read-only Monitor disables writes and direct handlers remain guarded');
  f.allowed=true;f.current.provider.enabled=false;f.current.settings.enabled=true;await ui.poll(true);assert.equal(enabled.disabled,false);enabled.checked=false;enabled.onchange();await flush();assert.equal(f.current.settings.enabled,false);assert.equal(byText(delegate,'提交研判').disabled,true);
  console.log('PASS disabling automation works with provider off and manual submissions stay disabled');
  const g=setup();g.history=Array.from({length:13},(_,i)=>({id:'r'+i,state:'completed',created:100-i,summary:'报告摘要 '+i+' '+('长'.repeat(200)),trigger:'automatic'}));g.current.runs=g.history.slice(0,5);g.current.run_total=13;await g.ui.poll(true);
  assert.equal(g.calls.filter(([p])=>p.startsWith('/api/agent/run?')).length,0);assert.equal(byClass(g.host,'agent-overlay').hidden,true);assert.equal(byClass(g.host,'agent-history-list').children.length,5);
  await byText(g.host,'研判记录').onclick();await flush();
  const historyView=g.host.all().filter(n=>n.className==='agent-view')[1];await byText(historyView,'下一页').onclick();await flush();assert.ok(byClass(g.host,'agent-history-list').all().some(n=>n.textContent.startsWith('报告摘要 5')));await g.ui.poll(true);assert.ok(byClass(g.host,'agent-history-list').all().some(n=>n.textContent.startsWith('报告摘要 5')));
  await byText(historyView,'查看').onclick();await flush();assert.equal(byClass(g.host,'agent-overlay').hidden,false);assert.ok(byClass(g.host,'agent-dialog-body').all().some(n=>n.textContent==='完整报告正文'));assert.ok(byClass(g.host,'agent-dialog-body').all().filter(n=>n.tagName==='DETAILS').every(n=>!n.open));byText(g.host,'关闭').onclick();assert.equal(byClass(g.host,'agent-overlay').hidden,true);assert.equal(byClass(g.host,'agent-dialog-body').children.length,0);
  console.log('PASS reports load only on demand in a dismissible modal; five-row history pagination survives polling');
  let resolve;g.report=new Promise(r=>{resolve=r;});byText(historyView,'查看').onclick();byText(g.host,'关闭').onclick();resolve({summary:'迟到的正文',outcomes:[],steps:[]});await flush();assert.equal(byClass(g.host,'agent-dialog-body').children.length,0);
  console.log('PASS late report responses cannot reopen or populate a closed modal');
  await byText(g.host,'规则库').onclick();await flush();byText(g.host,'新规则').onclick();const editor=field(g.host,'受限规则 JSON');const draft=JSON.parse(editor.value);draft.name='自定义检查';editor.value=JSON.stringify(draft);await g.ui.poll(true);assert.equal(JSON.parse(editor.value).name,'自定义检查');await byText(g.host,'预览命中').onclick();assert.ok(byClass(g.host,'agent-dialog-body').all().some(n=>n.textContent.includes('命中 2 个对象')));await byText(g.host,'保存规则').onclick();const saved=g.calls.find(([p])=>p==='/api/agent/rules/save')[1];assert.equal(saved.revision,0);assert.equal(saved.document.name,'自定义检查');assert.equal(byClass(g.host,'agent-overlay').hidden,true);
  console.log('PASS rule preview is separate from versioned save and polling preserves the editor draft');
  g.allowed=false;await g.ui.poll(true);assert.equal(byText(g.host,'新规则').disabled,true);const count=g.calls.length;byText(g.host,'新规则').onclick();assert.equal(g.calls.length,count);assert.equal(byClass(g.host,'agent-overlay').hidden,true);
  console.log('PASS read-only rule access cannot create rules via direct handler invocation');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
