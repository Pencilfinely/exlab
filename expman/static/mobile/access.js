'use strict';
(() => {
  const $=id=>document.getElementById(id), el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  let token='';try{token=sessionStorage.getItem('expman_token')||'';}catch{}
  function message(text){$('feedback').textContent=text;$('feedback').hidden=!text;}
  function hideIssued(){$('issued-token').value='';$('issued-pairing').value='';$('issued').hidden=true;}
  async function api(path,body){
    const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',redirect:'error'});
    const reply=await response.json();
    if(!response.ok){if(response.status===401){$('admin-login').hidden=false;$('access-workspace').hidden=true;hideIssued();}throw new Error(reply.error||'请求失败');}return reply;
  }
  function mobileAddress(){
    const url=new URL($('address').value);
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=='/')throw new Error('请选择手机可访问的主控地址');
    return url.origin+'/mobile/';
  }
  function updateAddress(){try{const address=mobileAddress();$('mobile-link').href=address;$('mobile-link').hidden=false;$('issued-pairing').value=$('issued-token').value?address+'#pair='+encodeURIComponent($('issued-token').value):'';}catch{$('mobile-link').hidden=true;$('issued-pairing').value='';}}
  async function load(){
    try{
      const result=await api('mobile/devices');$('admin-login').hidden=true;$('access-workspace').hidden=false;
      $('devices').replaceChildren(...result.devices.map(device=>{
        const box=el('div',undefined,'device'),info=el('div'),active=!device.revoked&&(device.expires===0||device.expires>result.time);
        const heading=el('div',undefined,'device-heading');heading.append(el('strong',device.name),el('span',device.revoked?'已撤销':active?'已配对':'已过期','badge '+(active?'online':'offline')));info.append(heading);
        info.append(el('p',`${device.permission==='control'?'可查看与操作':'只读监控'} · ${device.expires===0?'长期有效':new Date(device.expires*1000).toLocaleDateString()+' 到期'}`,'muted small'));box.append(info);
        if(!device.revoked){
          const actions=el('div',undefined,'device-actions');
          function action(label,cls,path,payload,success){const b=el('button',label,cls);b.onclick=async()=>{b.disabled=true;try{await api(path,payload);message(success);await load();}catch(error){message(error.message);b.disabled=false;}};actions.append(b);}
          if(device.expires!==0)action('设为长期','quiet','mobile/renew',{id:device.id,days:0},'已改为长期配对，原凭证继续使用。');
          action('撤销','danger','mobile/revoke',{id:device.id},'已撤销访问，该设备需要重新配对。');box.append(actions);
        }
        return box;
      }));
      if(!result.devices.length)$('devices').append(el('p','还没有配对设备，从上方添加第一台手机。','empty'));
    }catch(error){message(error.message);}
  }
  async function start(){await load();try{const info=await api('setup-info');$('addresses').replaceChildren(...info.addresses.map(address=>{const o=el('option',address);o.value=address;return o;}));if(!$('address').value)$('address').value=info.addresses.find(value=>!value.includes('127.0.0.1')&&!value.includes('localhost'))||location.origin;updateAddress();}catch(error){message(error.message);}}
  $('admin-form').onsubmit=async event=>{event.preventDefault();token=$('admin-token').value.trim();$('admin-token').value='';try{sessionStorage.setItem('expman_token',token);}catch{}message('');await start();};
  $('enroll-form').onsubmit=async event=>{event.preventDefault();$('create').disabled=true;hideIssued();try{mobileAddress();const reply=await api('mobile/devices',{name:$('device-name').value.trim(),permission:$('permission').value,days:Number($('days').value)});$('issued-token').value=reply.token;updateAddress();$('issued').hidden=false;message('在手机粘贴配对信息即可，下次打开会自动接入。');await load();}catch(error){message(error.message);}finally{$('create').disabled=false;}};
  async function copy(id,success){const field=$(id);if(!field.value){message('请先选择有效的主控地址');return;}try{await navigator.clipboard.writeText(field.value);message(success);}catch{field.focus();field.select();message('请复制已选中的配对信息。');}}
  $('copy-pairing').onclick=()=>copy('issued-pairing','已复制配对信息。');$('copy-token').onclick=()=>copy('issued-token','已复制移动凭证。');
  $('hide-token').onclick=hideIssued;$('reload').onclick=load;$('address').oninput=updateAddress;
  if(token)void start();else $('admin-login').hidden=false;
})();
