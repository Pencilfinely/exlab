'use strict';
const assert=require('node:assert/strict');
const delivery=require('../expman/static/delivery.js');
function element(tag='div',text){
  return {tag,children:[],value:'',disabled:false,text:text===undefined?'':String(text),
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[];this.text='';this.append(...items);},setAttribute(){},
    get textContent(){return this.text+this.children.map(item=>item.textContent).join('');}};
}
function find(root,text){if(root.tag==='button'&&root.text===text)return root;for(const child of root.children){const match=find(child,text);if(match)return match;}return null;}
function context(){const elements=new Map(),calls=[];let id=0;return {node:element,$:name=>{if(!elements.has(name))elements.set(name,element());return elements.get(name);},
  api:async(path,payload)=>{calls.push({path,payload});return {request_id:payload?.request_id};},download(){},requestId:()=>String(++id),notify(){},refresh:async()=>{},
  formatTimestamp:String,worker:{snapshot:{capabilities:['upload-migration-v1']}},calls};}
async function main(){
  assert.equal(delivery.bytes(null),'未知');assert.equal(delivery.bytes(undefined),'未知');
  assert.match(delivery.nodeSummary(null).join(''),/未知/);
  const summary=delivery.nodeSummary({pending_results:2,pending_result_bytes:123,pending_evidence:1,pending_evidence_bytes:456,
    current_evidence:{confirmed_bytes:null,total_bytes:900},result_worker_at:100},101).join(' ');
  assert.match(summary,/待回传结果 2/);assert.match(summary,/按需证据 1/);assert.match(summary,/未知 \/ 900 B/);assert.doesNotMatch(summary,/剩余时间|卡死/);
  const ctx=context(),job={id:'job',spec:{algorithm:'research',result_delivery:{mode:'lightweight'}},
    experiment_progress:[{attempt:1,experiment_id:'first',phase:'succeeded',complete:1}],
    experiment_results:[{attempt:1,experiment_id:'first',independent:'quarantined',sha256:'sha',name:'result.json',
      summary:{verification:{remote:'passed'},metrics:{test:{score:0.123456789012345}},selection:{}}}],
    evidence:[{attempt:1,experiment_id:'first',evidence_id:'best',name:'best.bin',size:1024,sha256:'hash',availability:'available',transfer_status:'retained'},
      {attempt:1,experiment_id:'first',evidence_id:'last',name:'last.bin',size:2048,availability:'missing',transfer_status:'unavailable'}]};
  await delivery.render(job,ctx);
  assert.match(ctx.$('delivery-status').textContent,/执行已完成.*结果已送达/);
  assert.match(ctx.$('delivery-status').textContent,/远端核验 已通过.*本机独立验收 隔离/);
  assert.match(ctx.$('delivery-status').textContent,/0.123456789012345/);
  const button=find(ctx.$('evidence-browser'),'提取此文件');await button.onclick();
  assert.equal(ctx.calls[0].path,'/api/evidence/request');assert.deepEqual(ctx.calls[0].payload.evidence_ids,['best']);
  const firstId=ctx.calls[0].payload.request_id;
  await delivery.render(job,ctx);await find(ctx.$('evidence-browser'),'提取此文件').onclick();
  assert.equal(ctx.calls[1].payload.request_id,firstId);
  const missing=ctx.$('evidence-browser').children.find(item=>item.textContent.includes('last.bin'));
  assert.equal(find(missing,'提取此文件').disabled,true);
  console.log('Delivery UI behavior checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
