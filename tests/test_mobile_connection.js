'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
require('../expman/static/mobile/model.js');
const script=fs.readFileSync(require.resolve('../expman/static/mobile/mobile.js'),'utf8');
const credential='m'.repeat(43), key='expman_mobile_token';
const state={jobs:[],nodes:[],time:1000,version:'0.5.2',session:{name:'Test phone',permission:'monitor',expires:0}};
const storage=seed=>{const values=new Map(Object.entries(seed||{}));return {getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};};
function page(local=storage(),session=storage(),replies=[state],hash=''){
  const elements=new Map(),scheduled=new Map(),requests=[];
  function element(){return {hidden:false,checked:false,disabled:false,value:'',textContent:'',className:'',children:[],dataset:{},append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];},setAttribute(){},removeAttribute(){},focus(){},after(){}};}
  const node=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  for(const id of ['reconnect','workspace','detail','tabs','confirm'])node(id).hidden=true;
  node('filter').value='all';
  let sequence=0,reloaded=false,replaced='';
  const context={MobileModel:globalThis.MobileModel,ExperimentTiming:{},URL,URLSearchParams,AbortController,Date,
    document:{hidden:false,getElementById:node,createElement:element,querySelectorAll:()=>[],addEventListener(){}},
    location:{origin:'https://lab.example',pathname:'/mobile/',search:'',hash,href:'https://lab.example/mobile/'+hash,reload(){reloaded=true;}},
    history:{replaceState(_data,_title,target){replaced=target;}},localStorage:local,sessionStorage:session,
    setTimeout(fn,delay){const id=++sequence;scheduled.set(id,{fn,delay});return id;},clearTimeout(id){scheduled.delete(id);},setInterval(){},
    fetch:async(path,options)=>{requests.push({path,...options});let reply=replies.shift();if(reply instanceof Error)throw reply;if(typeof reply==='function')reply=await reply();return {ok:!reply.status,status:reply.status||200,json:async()=>reply.status?{error:'Device rejected'}:reply};},
  };
  context.window=context;context.addEventListener=()=>{};context.scrollTo=()=>{};
  vm.runInNewContext(script,context,{filename:'mobile.js'});
  return {node,local,session,requests,replies,scheduled,get reloaded(){return reloaded;},get replaced(){return replaced;},
    async retry(){const timer=[...scheduled.entries()].find(([,item])=>item.delay===5000);assert.ok(timer);scheduled.delete(timer[0]);await timer[1].fn();}};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const local=storage({[key]:credential});
  const restored=page(local);
  assert.equal(restored.node('login').hidden,true,'Saved pairing should never flash the login form');
  await flush();assert.equal(restored.node('workspace').hidden,false);assert.equal(restored.node('reconnect').hidden,true);
  assert.equal(restored.requests[0].headers.Authorization,'Bearer '+credential);
  restored.node('update-web').onclick();assert.equal(restored.reloaded,true);assert.equal(local.getItem(key),credential);
  const reopened=page(local);await flush();assert.equal(reopened.node('workspace').hidden,false);
  const outage=page(local,storage(),[new TypeError('offline'),state]);await flush();
  assert.equal(outage.node('login').hidden,true);assert.equal(outage.node('reconnect').hidden,false);assert.equal(local.getItem(key),credential);
  await outage.retry();assert.equal(outage.node('workspace').hidden,false);assert.equal(outage.node('notice').hidden,true);
  assert.ok(outage.requests.every(request=>request.method==='GET'),'Reconnect must never replay operations');
  const revoked=page(storage({[key]:credential}),storage({[key]:credential}),[{status:401}]);await flush();
  assert.equal(revoked.node('login').hidden,false);assert.equal(revoked.node('workspace').hidden,true);assert.equal(revoked.local.getItem(key),null);assert.equal(revoked.session.getItem(key),null);
  const first=page(storage(),storage(),[state],'#pair='+credential);await flush();
  assert.equal(first.replaced,'/mobile/');assert.equal(first.local.getItem(key),credential);assert.equal(first.node('workspace').hidden,false);
  first.node('logout').onclick();assert.equal(first.local.getItem(key),null);assert.equal(first.node('login').hidden,false);
  const legacy=page(storage(),storage({[key]:credential}));await flush();assert.equal(legacy.local.getItem(key),credential,'Previous session pairing should become persistent after validation');
  const shared=page(storage({expman_mobile_remember:'no'}),storage({[key]:credential}));await flush();assert.equal(shared.local.getItem(key),null);assert.equal(shared.session.getItem(key),credential);
  let complete;
  const race=page(storage({[key]:credential}),storage(),[()=>new Promise(resolve=>{complete=resolve;})]);
  race.node('logout').onclick();complete(state);await flush();assert.equal(race.node('workspace').hidden,true);assert.equal(race.local.getItem(key),null);
  console.log('Monitor restores pairing, retries read-only after outages, retains credentials through UI reload, clears revoked/logout state and fences stale replies.');
})().catch(error=>{console.error(error);process.exitCode=1;});
