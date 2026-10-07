# 轻量结果回传验收记录

执行日期：2026-10-06。范围：本仓库 Agent、Hub、SDK、harness、管理页面及迁移工具；使用隔离临时目录、模拟文件、SQLite 和本机临时 HTTP 服务，未执行科研训练，也未连接或变更现有远端任务与上传队列。

## 已执行回归

| 检查 | 实际结果 | 记录 |
| --- | --- | --- |
| Python 全量回归 | 493 项，486 通过、7 跳过，144.348 秒 | `.test-runs/lightweight-final-python.log` |
| 后续迁移安全、源文件保护及选模约束的相关模块回归 | 122 项，120 通过、2 跳过，32.162 秒 | `.test-runs/lightweight-verified.log` |
| 最后新增平台来源信息后的结果与 Hub 回归 | 47 项全部通过，25.255 秒 | `.test-runs/lightweight-provenance-verified.log` |
| 平台来源防覆盖断言及发行包内容检查 | 6 项全部通过，1.363 秒；打包使用临时模拟安装器 | `.test-runs/lightweight-final-packaging.log` |
| 前端行为回归 | 8 个 JavaScript 测试文件全部通过 | `.test-runs/lightweight-final-ui.log` |

全量回归之后增加的四个保护场景已包含在 122 项定向回归中：旧队列多 attempt 缺少逐文件绑定、缓存缺失拒绝 apply、部分上传确认不能冒充完整文件、拒绝逐指标拼接 checkpoint。跳过项由既有测试的运行环境条件决定。测试日志仅保存在本地被忽略的 `.test-runs` 目录，不进入发行包；本记录保留检查范围与结果。

可复现命令（在仓库根目录运行，使用已安装的 Python 和 Node.js）：

```powershell
python -m unittest discover -s tests -q
python -m unittest tests.test_result_delivery tests.test_agent tests.test_harness tests.test_core tests.test_matrix tests.test_release -q
python -m unittest tests.test_result_delivery tests.test_hub -q
rg --files tests -g '*.js' | ForEach-Object {
    node $_
    if ($LASTEXITCODE -ne 0) { throw "JavaScript regression failed: $_" }
}
```

## 需求场景与结果

| 场景 | 验证结果及对应测试 |
| --- | --- |
| 10 个实验分别完成 | 每组恰好一个结果 JSON，原检查点保留，无自动大包回传；`test_ten_experiments_have_one_default_result_each_and_no_checkpoint_upload` |
| 批内 5 个顺序实验 | 首个结果在 job 仍 running 时可见，最终 5 份结果且无额外默认结果；`test_first_of_five_sequential_experiments_is_visible_during_the_batch`，另有 harness 子进程运行期间的登记验证 |
| 大文件阻塞 | 在 Hub 大文件最终哈希被受控阻塞时，另一实验通过独立结果线程与真实 HTTP 在 5 秒内可见；`test_result_enters_independent_lane_within_five_seconds_while_large_hash_is_blocked` |
| 断网后恢复 | 小结果重试保留原 attempt，不调用任务启动；进程重启后仍可续传；`test_disconnect_reconnect_keeps_attempt_and_prioritizes_small_results`、`test_result_retry_survives_agent_restart_without_rerunning` |
| 回执丢失与内容冲突 | 相同字节只保留一份并返回原回执；冲突内容记录且原结果不被覆盖；`test_lost_receipt_resends_same_bytes_and_persists_one_result`、`test_conflicting_final_content_is_recorded_without_overwriting` |
| 单项 checkpoint | 只传请求的证据，完整大小与 SHA-256 一致，其他证据不跟随上传；删除原输出目录后留存快照仍可用；`test_request_one_checkpoint_only_and_keep_evidence_after_output_removal` |
| 旧队列迁移 | dry-run 不变更队列；apply 保留 job/attempt、原文件、缓存 SHA/偏移、其他项目队列，并支持幂等重试；`test_queue_migration_dry_run_and_apply_preserve_offsets_sources_and_other_projects` |
| 在途文件让出 | 在已确认分块边界停止旧自动上传，已有分块不伪装完整；`test_legacy_queue_yields_at_acknowledged_chunk_boundary`、`test_partial_ack_cannot_masquerade_as_a_completed_legacy_file` |
| 中断与可观测性 | 证据回执丢失后从 Hub 已确认的 512 KiB 偏移继续；错误去除凭据，执行状态不改变；`test_evidence_ack_loss_resumes_at_server_confirmed_offset`、`test_transport_errors_are_redacted_and_do_not_change_execution` |
| 初始化隔离与独立验收 | 隔离仍隔离，远端通过不能建立本机独立通过，已验收数量保持独立；`test_quarantined_initialization_stays_quarantined_when_result_arrives`、`test_remote_audit_cannot_establish_independent_acceptance` |
| 离线或证据缺失 | 明确返回离线、不支持、missing 等原因，不重跑训练；`test_old_agent_or_offline_node_returns_explicit_unsupported_unavailable`、`test_missing_retained_evidence_reports_unavailable_without_rerun` |
| 失败与超限结果 | 无伪造测试指标；损坏 JSON 明确登记失败并保留原文件；超过 64 KiB 提示且完整保留字段；相关 failure/malformed/oversized 测试 |
| 身份与权限 | 跨节点、跨项目、越界路径、通配目录、NaN/Infinity 拒绝；实际平台来源独立于生产者声明；相关 roles/protocol 测试及 10 实验测试 |

结果传输模块共 29 个测试，前端新增 `tests/test_delivery_ui.js` 验证未知进度、独立核验与隔离、按 ID 请求单项证据以及请求重试幂等。所有文件和训练产物均为模拟数据。

## 上线前尚需现场确认

远端实际 Agent / Hub 版本与能力、真实多 GPU 节点、跨网吞吐及现有服务维护升级流程未在本机回归中验证。旧队列若没有足够的 attempt/实验绑定，工具明确拒绝自动迁移。外部 IntentPreference 旧收集器没有在本次仓库范围内改写，其兼容接入、现场 dry-run/apply 和回滚步骤见 [实施说明](LIGHTWEIGHT-RESULTS.zh-CN.md)。
