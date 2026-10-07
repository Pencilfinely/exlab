'use strict';
(function(scope){
  const requests=new Map(),plans=new Map(),mappings=new Map();
  const states={pending:'待完成',passed:'已通过',failed:'未通过',quarantined:'隔离',unknown:'未知',
    retained:'节点保留',queued:'等待传输',uploading:'传输中',delivered:'已送达',retrying:'等待重试',
    unavailable:'不可用',available:'可提取',missing:'已缺失',expired:'已过期',changed:'内容已变化',
    succeeded:'执行完成',completed:'完成',running:'执行中',training:'训练中',test:'测试中',unsupported:'不支持'};
  function bytes(value){if(!Number.isFinite(value))return '未知';if(value<1024)return value+' B';if(value<1024**2)return (value/1024).toFixed(1)+' KiB';return (value/1024**2).toFixed(1)+' MiB';}
  function label(value){return states[value]||value||'未知';}
  function nodeSummary(delivery,now=Date.now()/1000){
    if(!delivery)return ['回传详情未知；旧节点需要升级后提供结果与证据状态。'];
    const lines=[`待回传结果 ${delivery.pending_results??'未知'} 个 · ${bytes(delivery.pending_result_bytes)}`,
      `按需证据 ${delivery.pending_evidence??'未知'} 个 · ${bytes(delivery.pending_evidence_bytes)}`];
    if(delivery.legacy_pending_files)lines.push(`旧队列 ${delivery.legacy_pending_files} 个文件 · ${bytes(delivery.legacy_pending_bytes)}`);
    if(delivery.blocked_results)lines.push(`需要处理的结果 ${delivery.blocked_results} 个`);
    for(const [name,key] of [['结果','current_result'],['证据','current_evidence'],['旧队列','current_legacy']]){
      const current=delivery[key];if(current)lines.push(`${name}传输：${bytes(current.confirmed_bytes)} / ${bytes(current.total_bytes)}`);
    }
    lines.push('结果上传器：'+(Number.isFinite(delivery.result_worker_at)?(now-delivery.result_worker_at<10?'正常':'心跳过期'):'未知'));
    lines.push('证据上传器：'+(Number.isFinite(delivery.evidence_worker_at)?(now-delivery.evidence_worker_at<10?'正常':'心跳过期'):'未知'));
    if(delivery.last_error)lines.push(`最近回传错误：${delivery.last_error} · 重试 ${delivery.retry_count??'未知'} 次`);
    return lines;
  }
  async function render(job,context){
    const {node,$,api,download,requestId,notify,refresh,formatTimestamp,worker}=context;
    const box=$('delivery-status');box.replaceChildren();
    const results=job.experiment_results||[],progress=job.experiment_progress||[],evidence=job.evidence||[];
    box.append(node('h3','实验结果与验收'));
    if(!results.length&&!progress.length)box.append(node('p','尚无逐实验结果确认。旧任务需接入结果协议或迁移队列。','muted'));
    const received=new Map(results.map(result=>[result.attempt+':'+result.experiment_id,result]));
    for(const item of progress){
      const result=received.get(item.attempt+':'+item.experiment_id),line=node('div',undefined,'delivery-row');
      line.append(node('strong',`${item.experiment_id} · 尝试 ${item.attempt}`),
        node('p',`阶段 ${label(item.phase)} · 执行${item.complete?'已完成':'未完成'} · 结果${result?'已送达':'未确认'}`));
      if(result){
        const verification=result.summary.verification||{};
        const remote=typeof verification.remote==='object'?verification.remote.status:verification.remote;
        line.append(node('p',`远端核验 ${label(remote)} · 本机独立验收 ${label(result.independent)}`));
        const metrics=node('details'),caption=node('summary','查看指标与选模信息'),values=node('pre',JSON.stringify({metrics:result.summary.metrics,selection:result.summary.selection},null,2));metrics.append(caption,values);line.append(metrics);
        for(const warning of result.warnings||[])line.append(node('p',warning,'muted'));
        const button=node('button','下载结果 JSON','subtle');button.onclick=()=>download('/api/artifact?job_id='+encodeURIComponent(job.id)+'&sha256='+encodeURIComponent(result.sha256),result.name.split('/').pop());line.append(button);
      }
      box.append(line);
    }
    if(job.result_conflicts?.length)box.append(node('p',`存在 ${job.result_conflicts.length} 个结果内容冲突，原结果已保留。`,'inline-feedback'));
    const browser=$('evidence-browser');browser.replaceChildren(node('h3','节点保留的证据'));
    if(!evidence.length)browser.append(node('p','证据索引尚未登记。完成索引后可逐项提取。','muted'));
    for(const item of evidence){
      const line=node('div',undefined,'delivery-row');line.append(node('strong',`${item.experiment_id} / ${item.name}`),
        node('p',`${bytes(item.size)} · ${label(item.availability)} · ${label(item.transfer_status)}`));
      if(item.error)line.append(node('p',item.error,'muted'));
      if(item.transfer_status==='delivered'){
        const button=node('button','下载证据','subtle');button.onclick=()=>download('/api/artifact?job_id='+encodeURIComponent(job.id)+'&sha256='+encodeURIComponent(item.sha256),item.name.split('/').pop());line.append(button);
      }else{
        const button=node('button','提取此文件','subtle');button.disabled=!['available','pending'].includes(item.availability)||['queued','uploading','retrying'].includes(item.transfer_status);
        button.onclick=async()=>{button.disabled=true;const key=job.id+':'+item.attempt+':'+item.evidence_id;
          let payload=requests.get(key);if(!payload){payload={request_id:requestId(),job_id:job.id,attempt:item.attempt,experiment_id:item.experiment_id,evidence_ids:[item.evidence_id]};requests.set(key,payload);}
          try{await api('/api/evidence/request',payload);notify('已请求所选文件；可在此查看回传状态。');await refresh();}
          catch(error){notify('证据提取未确认：'+error.message);button.disabled=false;}
        };line.append(button);
      }
      const hash=node('small',item.sha256?'SHA-256 '+item.sha256:'SHA-256 尚未知','muted');line.append(hash);browser.append(line);
    }
    const migration=$('delivery-migration');migration.replaceChildren();
    if(job.spec.result_delivery?.mode==='lightweight')return;
    migration.append(node('h3','精简此任务的旧回传队列'));
    const config=node('details'),summary=node('summary','批内结果对应关系（可选）'),input=node('textarea');input.rows=4;input.value=mappings.get(job.id)||'[]';input.setAttribute('aria-label','旧队列结果文件与实验 ID 对应关系');input.oninput=()=>mappings.set(job.id,input.value);config.append(summary,input);migration.append(config);
    const preview=node('button','生成 dry-run 清单','subtle');preview.disabled=!worker?.snapshot?.capabilities?.includes('upload-migration-v1');
    if(preview.disabled)migration.append(node('p','运行中的算力端尚未声明安全队列迁移能力，请先升级并确认。','muted'));
    preview.onclick=async()=>{try{const payload={request_id:requestId(),action:'dry-run',project_id:job.spec.project_id||job.spec.algorithm,job_ids:[job.id],result_files:JSON.parse(input.value)};
      const result=await api('/api/transfers/migrate',payload);plans.set(job.id,{id:result.request_id});await refresh();
    }catch(error){notify('队列预览失败：'+error.message);}};migration.append(preview);
    const stored=plans.get(job.id);if(!stored)return;
    try{
      const plan=await api('/api/transfers/request?id='+encodeURIComponent(stored.applyId||stored.id));
      migration.append(node('p',`迁移请求：${label(plan.status)}`));
      if(plan.report.error)migration.append(node('p',plan.report.error));
      if(!stored.applyId&&plan.status==='completed'){
        migration.append(node('p',`预计减少 ${bytes(plan.report.estimated_reduced_bytes)} 待传证据。源文件和训练进程保留。`));
        const details=node('details'),title=node('summary','审阅文件清单'),raw=node('pre',JSON.stringify(plan.report,null,2));details.append(title,raw);migration.append(details);
        const apply=node('button','应用已审阅清单');apply.disabled=Boolean(plan.report.unresolved_results?.length);
        if(apply.disabled)migration.append(node('p','请补充清单中的结果文件与实验 ID 对应关系，再重新预览。'));
        apply.onclick=async()=>{try{stored.applyRequestId ||= requestId();const result=await api('/api/transfers/migrate',{request_id:stored.applyRequestId,action:'apply',plan_id:stored.id});stored.applyId=result.request_id;await refresh();}catch(error){notify('迁移未确认：'+error.message);}};migration.append(apply);
      }
    }catch(error){migration.append(node('p','读取迁移状态失败：'+error.message));}
  }
  const exported={bytes,label,nodeSummary,render};scope.ExLabDelivery=exported;
  if(typeof module==='object'&&module.exports)module.exports=exported;
})(globalThis);
