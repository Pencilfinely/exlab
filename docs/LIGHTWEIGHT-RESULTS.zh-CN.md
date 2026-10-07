# 轻量结果回传协议与运维

本改动落实 2026-10-06 的轻量回传需求。新增任务默认采用 `result_delivery.mode=lightweight`；每个实验只默认上传一个结果 JSON。检查点、完整日志、训练历史和原证据包保留在节点，按证据 ID 单独请求。既有任务没有该字段时继续按旧协议处理，必须通过显式 dry-run / apply 迁移。本文不表示任何现有远端节点已经升级或队列已经迁移。

## 运行时确认

管理员调用 `GET /api/state`，检查每个节点的 `snapshot.agent_version` 和 `snapshot.capabilities`；不要用主机的 VERSION 文件推断远端版本。以下能力分别表示逐实验结果、按需证据、安全队列迁移：

```text
experiment-results-v1
evidence-on-demand-v1
upload-migration-v1
```

节点凭证调用 `GET /api/node-info`，得到实际运行 Hub 的 `hub_version` 和能力列表。界面显示 Agent / Hub 版本、执行心跳和两条上传器的心跳。没有能力声明时显示未知 / 不支持；新的轻量任务不会分配给旧节点。已有旧任务的执行和节点接单策略保持原状。

## 实验生产者接入

SDK `Run.finish(result)` 保留已有单任务接入：最终 `result.json` 在任务退出后包装成一个结果信封。它不能判断模型特有的训练/测试含义，未知字段明确保持未知。需要批内及时送达时，在每组结束后调用 `publish_result`，不等待整个任务结束：

```python
from expman.sdk import Run

run = Run()
experiment_id = "beauty-seed42"
run.progress(experiment_id, "training")
# 运行原来的训练与测试；不要因传输重试而再次调用它们。
checkpoint = run.evidence("trials/beauty-seed42/best.pt", "best-checkpoint")
run.publish_result(experiment_id, {
    "status": "succeeded",
    "execution": {"status": "succeeded", "exit_code": 0, "stages": {
        "training": {"status": "succeeded", "exit_code": 0},
        "test": {"status": "succeeded", "exit_code": 0}}},
    "metrics": {"protocol": "your-evaluation-protocol-v1",
                "validation": {"score": 0.123456789012345},
                "test": {"score": 0.234567890123456}},
    "selection": {"checkpoint": checkpoint["path"], "sha256": checkpoint["sha256"],
                  "epoch": 17, "rule": "Select one complete checkpoint by validation score"},
    "provenance": {"data_version": "your-version", "scientific_source_sha256": "your-hash",
                   "runtime_versions": {"python": "actual-version", "torch": "actual-version"}},
    "timing": {"stages": {"training_seconds": 100, "test_seconds": 3},
               "note": "Stage times are contained in job elapsed time; do not add them again."},
    "verification": {"remote": "passed", "independent": "pending"},
    "evidence": [checkpoint],
})
```

指标和选模信息由实验生产者提供，平台不硬编码 HR、NDCG、模型名或选模规则。缺失的科学信息不能从最新训练日志推断成测试成绩；未执行的阶段明确使用 `not_run`、退出码 null。失败也调用同一方法，填写 `status=failed`、失败阶段、退出码、简短错误和证据引用。初始化核验未通过时使用 `independent=quarantined`，不能因为结果文件已收到而改为通过。

`publish_result` 原子写入 `.exlab/results/attempt-N/EXPERIMENT_ID.json` 并登记进度元数据。相同实验 ID、attempt、相同结果再次发布复用原文件和完成时间；不同内容拒绝覆盖。ID 使用字母、数字、点、下划线和短横线；一个 job 内必须稳定且唯一。批次不要再生成额外的 job 级结果来重复上传。演示生产者见 `examples/lightweight-batch.py`，全部为模拟数据，不运行科研训练。

不使用 SDK 的任务可以显式声明已原子完成的文件：

```json
"result_delivery": {"mode": "lightweight", "files": [
  {"experiment_id": "trial-a", "path": "run_output/trial-a/000_result.json", "stream": true},
  {"experiment_id": "trial-b", "path": "run_output/trial-b/000_result.json", "stream": true}
]}
```

路径相对 `EXPERIMENT_OUTPUT`。默认 `result.json` 的 `stream=false`，避免退出码未确定和恢复后旧文件被提前登记。`stream=true` 的文件发布后必须保持不变。新 attempt 必须重新发布；建议使用 attempt 专属目录或 SDK 登记目录。不能把 `run_output/**/*` 作为结果规则。

外置 harness 支持 `harness.json.result_files`，路径相对原程序的工作副本：

```json
"result_files": [
  {"experiment_id": "trial-a", "path": "run_output/trial-a/000_result.json"},
  {"experiment_id": "trial-b", "path": "run_output/trial-b/000_result.json"}
]
```

harness 每 0.25 秒检查并登记这些文件；指标和证据引用沿用生产者数据。harness 的 evidence.path 相对工作副本，登记时转换成节点输出路径。`artifacts` 仅用于本地证据采集，不能代替结果声明。已发布旧项目包中的 SDK/harness 不会自动变成新版；不重新发布训练任务的迁移方式见下节。

## 信封、回执与核验

最终信封 `schema_version=1` 含 `identity`、`execution`、`metrics`、`selection`、`provenance`、`timing`、`verification`、`evidence` 和保留未识别生产者字段的 `summary`。平台填入项目、job、attempt、实验、节点/GPU、完成时间、配置哈希、源码和输入引用；生产者填入指标、完整被选 checkpoint 的 SHA-256、轮数和规则、数据/科学源/实际环境版本以及阶段耗时。无法确定的内容明确留空/未知，不补造。

`provenance.platform` 单独保存实际 Agent 版本、原任务配置 SHA-256、source/inputs、所选环境及已缓存的实际容器镜像 ID，生产者字段不能覆盖它。生产者自己的科学配置、环境和源码声明仍可保留；未采集到的实际版本或镜像 ID 为 null。

节点的独立结果线程持续扫描登记，不受主循环的 Docker 准备、日志采集、证据复制和大文件上传阻塞。结果通过节点认证 `POST /api/results {sha256,data:base64}` 发送。哈希是完整传输文件的 SHA-256，记录在传输层，不嵌入文件自身。Hub 对 `(project,job,attempt,experiment_id)` 去重，重复相同字节返回原回执；不同内容返回 409，保存冲突内容与原哈希，保留原结果。失败只重试传输，不调用启动、训练、测试或任务提交。

建议信封不超过 64 KiB；超出会在界面、回执和事件中提示，保留字段。当前 HTTP 实现有 1 MiB 的硬传输上限。超过上限、损坏或无法解析的终态生产者结果会明确登记结果生成/登记失败，原始文件作为按需证据保存；不丢弃原数组、不重训。正在执行的生产者异常仍保留原文件，并显示节点登记错误。大数组、张量、base64 模型和逐用户排名应由生产者移到证据文件。

`GET /api/job?id=JOB` 返回 `experiment_results`、`experiment_progress`、`evidence` 和 `result_conflicts`。总体 job 执行状态仍由执行报告决定，结果到达不让整个批次提前变为完成。页面、CSV 和矩阵报告明确呈现独立验收状态，待验收与隔离结果不会被标记已验收。

只有管理员可以调用 `POST /api/results/verification`，必须绑定 job、attempt、实验和已接收的结果 SHA-256。`status=passed` 必须附非空 `references` 独立审阅/证据记录。远端 `passed` 和生产者的独立验收声明只保留为声明，不能自动赋予本机通过状态。适用于已有初始化差异隔离记录；本改动没有复核或解除任何科研隔离。

## 按需证据与状态

证据索引包含绑定身份的 `evidence_id`、相对文件名、种类、大小、SHA-256、保留/可用状态和传输状态。尚未完成索引或保留时明确显示 pending / 未知。节点在 `retained_evidence/JOB/ATTEMPT/EXPERIMENT/ID` 保存独立快照，位于容器临时目录和工作副本之外；保存/校验在证据线程执行，不延迟结果通道。默认不自动删除源文件或留存快照。复制空间不足会提示并保留源文件；保留/清理策略需另行配置，不偷偷删除或重跑。

页面选择某个证据，或管理员调用：

```json
POST /api/evidence/request
{"request_id":"review-best-unique-id","job_id":"JOB_UUID_HEX","attempt":1,
 "experiment_id":"beauty-seed42","evidence_ids":["EVIDENCE_ID"]}
```

一次只发送明确选择的 1..100 个证据 ID。请求不接受任意路径或命令，使用已有角色和所属节点校验。节点以 512 KiB 分块、已确认偏移继续传输，Hub 校验最终大小和 SHA-256 后才登记完整文件；大文件终态哈希计算不会持有 Hub 全局锁。下载仍使用管理员 `GET /api/artifact`。证据缺失、过期、内容变化或节点离线会明确报告原因，完全不重跑训练/测试。

`snapshot.delivery` 区分待传结果/按需证据/旧自动队列的数量和剩余字节，当前各通道已确认字节、最近字节进展、最近完整文件、最近错误/时间、重试次数、下次重试和上传器心跳。认证的独立 poll/report 通道让主循环忙时也能发送上传器状态。没有部分确认时为未知或零个已确认字节，不据文件数不变宣称卡死，不显示未经测量的 ETA。失败信息去除节点凭证。

## 现有积压的 dry-run 与迁移

使用实际 Hub / Agent 能力检查后，仅选择一个节点上指定项目和明确 job 列表。保持原 node-mode、任务进程、任务 ID、attempt、request_id、原结果/证据哈希和分块记录；不重新发布或提交训练。

```powershell
python -m expman.transfer_tool --url http://CENTER:8765 --hub-config E:/ExperimentCenter/hub.json migrate --project-id IntentPreference --jobs JOB1 JOB2 --result-files E:/review/result-files.json --request-id preview-unique-id
```

`result-files.json` 示例：

```json
[
  {"job_id":"JOB1","experiment_id":"beauty-seed42","path":"run_output/beauty/000_result.json"},
  {"job_id":"JOB1","experiment_id":"sports-seed42","path":"run_output/sports/000_result.json"}
]
```

默认只识别显式任务结果规则或根目录 `result.json`；遇到未映射的 `000_result.json`，清单标记 unresolved 并拒绝 apply，不能从文件名字典序猜科研身份。dry-run 返回每个文件的结果/证据分类、原 SHA、大小、已有偏移、在途标记和预计减少字节，任务身份以及清单指纹。它不改变队列。

审阅完成后，用返回的 dry-run request_id：

```powershell
python -m expman.transfer_tool --url http://CENTER:8765 --hub-config E:/ExperimentCenter/hub.json migrate --project-id IntentPreference --jobs JOB1 JOB2 --apply-plan PREVIEW_ID --request-id apply-unique-id
python -m expman.transfer_tool --url http://CENTER:8765 --hub-config E:/ExperimentCenter/hub.json status --id apply-unique-id
```

apply 再核对任务身份和清单指纹，将原自动上传改为节点保留并优先登记小结果。正在上传的文件在下一次已确认分块边界让出；不删除源、缓存或 Hub 部分文件，不设置伪完成标记。报告保存 before/after 清单。相同 request_id 的重试幂等；身份/文件变更需要重新生成 dry-run。只针对对应项目/job，不触及其他队列。

旧 uploads 表没有逐文件 attempt 字段。因此旧 job 已恢复到多个 attempt，或同一路径保留多个内容版本而没有实验/attempt 绑定时，工具明确返回 `unsupported`，不猜测、不变更队列。应先用生产者的 attempt 专属清单完成兼容审阅，保留原缓存并按明确证据清单提取；不能通过重训或修改内部表补造身份。缓存已缺失/大小改变也拒绝 apply。超过 512 KiB 的审阅清单应缩小 job 范围。`in_flight_yield_pending` 表示策略已应用但还有一个已发出的分块等待确认。

要恢复某项迁移后证据的传输，用其 evidence_id：

```powershell
python -m expman.transfer_tool --url http://CENTER:8765 --hub-config E:/ExperimentCenter/hub.json evidence --job JOB1 --attempt 1 --experiment-id beauty-seed42 --ids EVIDENCE_ID --request-id restore-best-unique-id
```

旧 Agent 没有迁移能力时返回“不支持，先升级并确认能力”，不能直接改服务内部数据库。升级应保存 node.sqlite3、配置和输出/缓存目录；旧自动更新的“回传未完成”检查不会被此工具绕过。需要维护升级时使用现有后台服务停止入口，仅重启 Agent；Docker 科研容器应继续保留，并在重启后核对相同 ownership labels 与 attempt。无法确认该停止入口只停止 Agent 时，先在维护窗口确认升级路径，不能停止科研任务来实施队列迁移。

## 主机收集器兼容

新收集器常规读取 `experiment_results[].summary` 或下载该单个 JSON。保存回执 SHA 和 `independent` 状态；读取 test / validation 对应对象，不把训练最新指标拼成测试成绩。只在独立复核需要时请求具体初始化证据或被选 checkpoint，待下载大小/hash 验证和独立复核完成后才记录验收。

外部 IntentPreference 的 `tools/collect.py` 位于另一项目，本次未修改该项目。若其仍强制下载完整证据 ZIP，必须显示“旧收集器需要完整证据，结果已收到，验收待完成”的兼容状态；不能宣称完整验收自动完成。不要重新运行 publish.py 提交原训练来获得新协议文件。历史结果可以通过上述显式路径/实验 ID 映射登记。

## 部署、回滚与验收

先在隔离目录运行模拟验收，再部署 Center 和各 Worker 的同一份应用代码，保留原持久数据目录。没有修改模型、科学源码、训练参数、选模规则、测试次数、实验预算或节点训练并发。上线后用认证 API 检查实际运行能力，再逐项目 dry-run / apply；本仓库测试不能替代远端升级确认。

回滚前备份当前数据库和 `result_cache`、`retained_evidence`、`runs`、`upload_cache`，仅停止应用代理/管理服务，保留科研容器。数据库新增表是附加的，既有任务身份和执行状态没有改写。未迁移队列时可以换回旧应用代码，保留当前数据库和输出。已迁移的 Worker 应继续使用能识别保留策略的 Agent；直接回退旧 Agent 会忽略新增保留策略并再次上传大文件。此时优先回退 UI/Center 展示或保持新 Agent，通过按需证据请求恢复指定文件，不恢复过时的 tasks 快照，不改内部数据库伪造完成。Hub 暂时不支持时结果继续留存；恢复支持后重新发现能力并补传。

自动验收见 `tests/test_result_delivery.py`、`tests/test_harness.py` 和 `tests/test_delivery_ui.js`。覆盖每组单文件、顺序批内及时可见、受控大文件阻塞下 5 秒内进入结果通道、断线/回执丢失、冲突、按项 checkpoint、迁移清单/源文件/偏移/其他项目不变、分块安全边界、隔离保持、离线/证据不可用、认证/路径限制与无训练重执行。真实多 GPU 节点、跨网实际吞吐和旧节点维护升级需另行上线验收，测试没有裁减当前远端队列。

本次实际执行的回归数量、命令和场景结果见 [验收记录](LIGHTWEIGHT-RESULTS-VALIDATION.zh-CN.md)。
