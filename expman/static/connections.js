'use strict';
let centerSettings=null;
function connectionCode(value){return 'exlab://connect/'+btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function parseConnection(text){
  text=text.trim();if(text.startsWith('exlab://connect/')){let raw=text.slice('exlab://connect/'.length).replace(/-/g,'+').replace(/_/g,'/');text=new TextDecoder().decode(Uint8Array.from(atob(raw.padEnd(Math.ceil(raw.length/4)*4,'=')),c=>c.charCodeAt(0)));}
  if(text.length>16384)throw new Error('连接凭证过大');return JSON.parse(text);
}
async function createWorkerConnection(copyToClipboard){
  const form=$('pair-form');if(!form.reportValidity())return;
  for(const button of form.querySelectorAll('button'))button.disabled=true;
  try{
    const pairing=await api('/api/enroll',{node_id:$('pair-name').value.trim(),hub_url:$('pair-url').value.trim()});
    if(copyToClipboard){await writeClipboardText(connectionCode(pairing));notify('Worker 凭证已复制。到算力端点击“粘贴连接凭证”。');}
    else downloadContent(JSON.stringify(pairing,null,2),pairing.node_id+'.pairing.json','application/json');
    form.hidden=true;await refresh();
  }catch(error){notify(error.message);}finally{for(const button of form.querySelectorAll('button'))button.disabled=false;}
}
$('pair-copy').onclick=()=>void createWorkerConnection(true);
$('pair-form').onsubmit=event=>{event.preventDefault();void createWorkerConnection(false);};
async function loadCenterSettings(){
  try{
    centerSettings=await api('/api/local/centers/status');
    const modes={primary:'调度主控',replica:'同步主控',handoff:'已交接'};
    $('center-current').textContent=centerSettings.name+' · '+(modes[centerSettings.mode]||centerSettings.mode);
    $('center-sync-state').textContent=(centerSettings.last_sync?'副本更新于 '+new Date(centerSettings.last_sync*1000).toLocaleString('zh-CN'):'主控数据保存在本机')+(centerSettings.error?' · '+centerSettings.error:'');
    if(document.activeElement!==$('center-name'))$('center-name').value=centerSettings.name;$('center-promote').hidden=centerSettings.mode!=='replica';$('center-sync').hidden=centerSettings.mode!=='replica';
    $('center-create-form').hidden=centerSettings.mode!=='primary';$('center-join-form').hidden=centerSettings.mode==='replica';
    $('center-devices').replaceChildren();for(const device of centerSettings.devices||[]){const row=node('div',undefined,'list-row'),label=node('div');label.append(node('strong',device.name),node('small',device.revoked?'凭证已撤销':'已授权同步主控'));row.append(label);if(!device.revoked&&centerSettings.mode==='primary'){const revoke=node('button','撤销','subtle');revoke.onclick=async()=>{try{await api('/api/centers/revoke',{center_id:device.id});await loadCenterSettings();}catch(error){notify(error.message);}};row.append(revoke);}$('center-devices').append(row);}
    if(!$('center-own-url').value)$('center-own-url').value=centerSettings.addresses?.[0]||location.origin;
    if(!$('center-export-url').value)$('center-export-url').value=centerSettings.addresses?.[0]||location.origin;
  }catch(error){$('center-sync-state').textContent='请在此 Center 的本机窗口配置多主控连接。';$('center-create-form').hidden=true;$('center-join-form').hidden=true;}
}
$('center-name-form').onsubmit=async event=>{event.preventDefault();try{await api('/api/local/centers/rename',{name:$('center-name').value});await loadCenterSettings();notify('主控名称已保存。');}catch(error){notify(error.message);}};
$('center-create-form').onsubmit=async event=>{event.preventDefault();try{const credential=await api('/api/centers/enroll',{name:$('center-peer-name').value,hub_url:$('center-export-url').value});const text=connectionCode(credential);$('center-code').value=text;$('center-code-box').hidden=false;await writeClipboardText(text);notify('Center 凭证已复制。到另一台 Center 的设置页粘贴连接。');await loadCenterSettings();}catch(error){notify(error.message);}};
$('center-code-copy').onclick=async()=>{try{await writeClipboardText($('center-code').value);notify('主控凭证已复制。');}catch(error){notify('请选中凭证后按 Ctrl+C。');}};
$('center-join-form').onsubmit=async event=>{
  event.preventDefault();const button=$('center-join');button.disabled=true;button.textContent='正在连接与同步…';
  try{await api('/api/local/centers/connect',{credential:parseConnection($('center-credential').value),hub_url:$('center-own-url').value});$('center-credential').value='';localImportAvailable=null;await loadCenterSettings();await refresh();notify('已连接同一工作空间，之后自动同步。');}
  catch(error){notify('连接尚未完成：'+error.message+'。输入已保留，可以重试。');}finally{button.disabled=false;button.textContent='连接工作空间';}
};
$('center-sync').onclick=async()=>{try{await api('/api/local/centers/sync',{});await loadCenterSettings();notify('副本已更新。');}catch(error){notify('同步未完成：'+error.message);}};
$('center-promote').onclick=async()=>{
  if(!confirm('将先冻结原主控，完整同步项目与结果，再由这台电脑接管调度。需要原主控在线，交接完成后可将它关机。继续吗？'))return;
  const button=$('center-promote');button.disabled=true;button.textContent='正在交接…';
  try{await api('/api/local/centers/promote',{});localImportAvailable=null;await loadCenterSettings();await refresh();notify('这台 Center 已接管。算力端会使用同步的备用地址重新连接。');}
  catch(error){notify('交接尚未完成：'+error.message+'。请保持两台 Center 在线，在此重试交接。');}finally{button.disabled=false;button.textContent='由本机接管调度';}
};
if(token&&currentView==='settings')void loadCenterSettings();
setInterval(()=>{if(token&&currentView==='settings')void loadCenterSettings();},30000);
