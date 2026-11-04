'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../expman/static/app.js'),'utf8');
const start=source.indexOf('async function loadDesktopSettings()'),end=source.indexOf("$('quick-worker')",start);
assert.ok(start>=0&&end>start);
function fixture(api){
  const elements=Object.fromEntries(['desktop-settings-open','desktop-settings-feedback','settings-data-root'].map(id=>[id,{disabled:false,textContent:''}]));
  const context=vm.createContext({$:id=>elements[id],api});vm.runInContext(source.slice(start,end),context);
  return {elements,context};
}
(async()=>{
  const calls=[];
  const local=fixture(async(path,body)=>{calls.push([path,body]);return {available:true,data_root:'E:\\Original Center',detail:body?'桌面设置已打开':'可调整启动选项'};});
  await vm.runInContext('loadDesktopSettings()',local.context);
  assert.equal(local.elements['settings-data-root'].textContent,'E:\\Original Center');
  assert.equal(local.elements['desktop-settings-open'].disabled,false);
  await local.elements['desktop-settings-open'].onclick();
  assert.equal(calls[1][0],'/api/local/desktop/settings');assert.deepEqual(Object.keys(calls[1][1]),[]);
  assert.equal(local.elements['desktop-settings-feedback'].textContent,'桌面设置已打开');
  const remote=fixture(async()=>{throw new Error('local only');});
  await vm.runInContext('loadDesktopSettings()',remote.context);
  assert.equal(remote.elements['desktop-settings-open'].disabled,true);
  assert.match(remote.elements['desktop-settings-feedback'].textContent,/本机/);
  const unavailable=fixture(async()=>({available:false,data_root:'/srv/center',detail:'Windows 本机'}));
  await vm.runInContext('loadDesktopSettings()',unavailable.context);
  assert.equal(unavailable.elements['desktop-settings-open'].disabled,true);
  assert.equal(unavailable.elements['settings-data-root'].textContent,'/srv/center');
  console.log('PASS desktop settings UI: original directory, local settings action, remote and unavailable feedback.');
})().catch(error=>{console.error(error);process.exitCode=1;});
