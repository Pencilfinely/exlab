/* Runtime evidence never proves scientific progress or completion. */
(function(root){
'use strict';
function describe(job,now){
  const active=['starting','running','stopping'].includes(job.state);
  if(!active)return '';
  const r=job.runtime;
  if(!r||r.attempt!==job.attempt)return '节点在线不代表训练推进；当前算力端未提供运行诊断。';
  if(!Number.isFinite(r.received_at)||now-r.received_at>45)return '运行诊断已过期，请等待算力端回传。';
  const c=r.container||{},l=r.log||{};
  if(c.status==='paused')return '容器已被暂停；任务尚未结束，训练不会继续推进。';
  if(c.status==='restarting')return '容器正在重启；尚未确认训练恢复。';
  if(c.error)return '读取容器状态失败：'+c.error+'；保留原任务。';
  if(l.error)return '日志读取失败：'+l.error+'；下方显示上次成功读取的日志。';
  if(Number.isFinite(l.changed_at)&&r.observed_at-l.changed_at>=120)return '日志至少 '+Math.floor((r.observed_at-l.changed_at)/60)+' 分钟未变化；需结合进程快照判断原因，尚未确认训练失败。';
  return '已收到运行诊断；容器存活不等于训练持续推进。';
}
const api={describe};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
else root.ExperimentRuntime=api;
})(typeof globalThis!=='undefined'?globalThis:this);
