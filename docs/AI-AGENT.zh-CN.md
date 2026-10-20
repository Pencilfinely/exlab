# AI 自动托管与外部 agent 接入

Center 使用两条独立循环：高频规则巡检，以及低频 AI“观察 → 规划 → 工具执行 → 回传核验”。关闭网页或 Monitor 后仍在后台运行。规则检查无需模型接口；开启 AI 服务并勾选 **AI 自动介入** 后，模型按需处理异常和全局规划，无需逐次点击或监督。原来的“AI 规划建议”仍可用于手动预览。

## 开启托管

1. 在 Center「设置 → AI 辅助」配置 API 地址、协议、模型和 Key，保存并测试连接。
2. Chat Completions 兼容服务选择 `chat_completions`；OpenAI Responses 接口选择 `responses`，基础地址填写 `https://api.openai.com/v1`。模型名使用该服务、该账户实际开放的 API 模型 ID。
3. 在下方「巡检与托管」勾选「AI 自动介入」。在折叠的「巡检设置」填写长期目标，例如“优先完成已提交的对比实验；ROG 外出前释放；夜间优先安排可续训任务”。
4. 分别设置规则间隔、AI 间隔、每小时研判轮数和每轮交互上限。默认允许调整队列、交接、恢复可续训任务、设置可用策略及暂停接单；提交新实验、取消、资源预算、项目部署等需在操作列表中单独开启。

默认规则每 **10 秒**检查一次；AI 自动研判间隔至少 **15 分钟**，活跃任务每 **60 分钟**全局复查；每小时最多 **4 轮**，每轮最多 **6 次模型交互、8 个操作**。预算按完整研判轮次计算，最多 24 次交互，不会再因两个互相独立的调用额度中途冲突。自动与临时委托共用小时额度；额度用完后保留排队委托，等待滚动一小时窗口恢复。AI 最短间隔约束自动介入，一次性委托仍可立即入队。

节点上下线、可用策略/确认、队列与执行状态变化，以及规则产生的新异常，会在 AI 冷却结束后触发研判；活跃任务还会定期复查。没有变化且没有待复查任务时不调用模型，普通进度心跳与显存数值波动不会逐条触发。模型失败采用 60 秒至 1 小时退避，并继续遵守 AI 最短间隔。旧设置首次升级为较低频的轮数预算，不提高原有最坏调用额度；旧外部接口显式使用 `max_calls_per_hour` 时仍兼容按调用次数限流，网页保存设置后统一切为轮数预算。

概况只显示最近一次摘要和少量异常；「研判记录」每页 5 条，完整报告按需打开弹窗，工具过程与回执折叠显示。规则发现按“规则 + 对象 + 连续异常”合并，恢复时记录解除，不会每 10 秒生成一份报告。

关闭托管会在下一项工具执行前作废本轮自动计划；关闭 AI 服务、修改模型接口、修改托管设置、撤销委托凭证或中止本轮，也会阻止旧计划继续发出操作。已经送达 Worker 的请求继续通过真实状态核验，不伪装成回滚。

「临时交给 AI 研判」用于一次性目标，也可由外部 agent 发起。开启 AI 服务即可接受委托，不要求开启周期自动介入；委托仍使用系统配置的操作范围和预算。更改托管设置会终止基于旧版本设置的排队或执行计划。

## 高频规则与 AI 编写能力

规则循环与模型线程独立。关闭 AI、预算耗尽、模型响应缓慢或失败退避时，规则仍检查节点离线、策略执行错误、回传异常、长时间未确认的指令、队列等待、接手等待和实验失败。只有主控运行规则。关闭自动介入后规则仅观察、记录，不再自动执行控制操作。

开启「允许 AI 编写和维护受限规则」后，AI 可读取规则语言与字段，预览命中，再保存常见检查供本地循环消费。AI 只能修改自己创建的规则；人工或内置规则受保护。人工接管编辑一条 AI 规则后，该规则成为人工规则。规则库允许人工新建、预览、编辑、停用和删除自定义规则；内置规则可编辑或停用。

使用 `exlab-rules-v1` JSON，不运行 Python、JavaScript、Shell 或任意文件/网络操作。条件支持 `all` / `any`、公开巡检字段和有限比较（`eq/ne/gt/gte/lt/lte/in/contains/exists`）。效果只允许记录、请求 AI 研判、调整队列、暂停接单、暂停实验和续训交接。暂停接单只能设为 drain，不能自动解除人工限制。实际控制还需开启自动介入、获得操作授权，并通过原有 CAS、人工限制和检查点保护。

```json
{
  "id": "ai-progress-stalled",
  "name": "运行进度长时间停滞",
  "enabled": true,
  "target": "job",
  "when": {"field": "progress_stalled_seconds", "op": "gte", "value": 900},
  "for_seconds": 60,
  "cooldown_seconds": 900,
  "severity": "warning",
  "effect": {"type": "escalate"}
}
```

持续时间从 Center 首次观察到条件开始；队列、待确认指令和进度计时跨重启保存，普通心跳不会重置。预览只计算当前命中，不等待持续时间，也不写入观察或执行操作。每条规则最多 16 KiB、48 个条件节点、6 层深度；最多 24 条自定义规则。每次扫描最多尝试 4 个控制操作，每小时最多发出 60 个控制请求。同一连续异常仅尝试一次，恢复后再次触发仍受每对象冷却约束；拒绝和不确定回执不会被盲目反复重放。AI 在思考期间出现的新异常仍留待下一轮研判。

## 自动操作与个人使用

AI 调用与人工控制共用原有调度、版本校验和执行协议。它不能启用本地未授权的 GPU，也不能越过本地可用策略、运行时段、数据/镜像兼容性或资源上限。

自动托管保留人工设置的插电要求、维护窗口、游戏释放、暂停接单和队列暂缓。AI 可以撤回自己设置的临时限制，但不能借后续操作抹掉原有人工限制。人工重新保存策略会成为新的约束基线。

节点操作明确区分 `drain`（只暂停接单）、`stop`（保存并停止实验，保留本机任务/环境）和 `release`（停止后归还可交接任务、清理已停止的实验容器并释放训练资源）。`node_mode=drain` 不能被描述为释放。`availability` 的策略 ACK 也不能证明实验停止或资源释放：必须检查当前策略匹配的 `compute_control`，其中 `experiments_stopped` 与 `resources_released` 是独立确认。离线、核验失败或回执过期均不能标记完成。原生 Windows、Linux 状态、Monitor 及外部模型拓扑接口都保留此回执；规则可以检查 `compute_phase`、`resources_released`，持续的阻塞默认升级为异常。Windows 中控释放由 Windows 客户端关闭空闲 Docker / WSL，必须检查 `host_control` 中策略匹配且新鲜的双退出确认；`runtime_retained=true` 仅表示训练容器已清理。轻量 Windows 控制连接保持在线供远程恢复，`worker_online=false` 的节点不可调度，旧资源测量不作为当前值。原生 Linux 保留系统服务。

断电或丢失心跳只表示失联。跨节点接手仍须原进程确认停止；已开始的实验还须声明完整续训能力，并具备中心完整接收、校验的检查点。无续训能力的任务不会被 AI 自动抢占后假装成功续训。用户手动停止、取消交接的实验不会被自动恢复。

操作记录中的 `requested` 表示请求已持久化。运行记录的 `outcomes` 根据实际队列、指令确认、执行代次、Worker 策略版本和部署回报更新为 `verified`、`pending` 或 `needs_review`。入队操作的 verified 只代表已经入队。请求未确认时不能仅凭模型文字判断执行完成。进程中途退出遗留的操作标记为不确定，不盲目重放。

## Windows、Linux 与 Monitor

| 端 | 使用入口 |
|---|---|
| Windows / Linux Center | 同一网页设置、托管记录、委托和外部接入面板；只有调度主控执行托管，副本转发操作 |
| Windows Worker | 桌面「可用时间 / 游戏释放」，区分本地暂停接单、停止并保留、释放训练资源；支持时长和插电要求；状态窗口显示执行核验结果。完全退出使用「停用并释放 Docker / WSL」 |
| Linux / WSL Worker | 下列 CLI，以及 `status` 返回的 `controller_agent` 和 `availability`；同一 Worker 执行协议 |
| ExLab Monitor（Android、鸿蒙、浏览器） | 总览中的托管开关、委托和记录；节点可用策略/游戏释放；详情的执行状态、续训与轻量结果。查看凭证保持只读，控制凭证允许操作 |

```sh
./Client-Worker.sh compute-policy --compute-mode release --minutes 120
./Client-Worker.sh compute-policy --require-ac
./Client-Worker.sh compute-policy --compute-mode available
./Client-Worker.sh status
```

开发目录可使用 `python -m expman.worker_service` 替换发行包的 `./Client-Worker.sh`。本地限制写入节点配置旁的 `compute-override.json`，Center 离线时仍生效。释放训练资源与退出整个 Docker/WSL 的生命周期操作分别保留。

托管设置、运行记录、规则、发现、计时及 agent 凭证哈希随 Center 数据库同步；模型 API Key 保存在各 Center 本机。切换到另一台调度主控后，规则继续运行，需要在新主控配置并启用其模型接口才会继续调用内置 AI。

Monitor 使用 Center 提供的共享界面；主控升级后，各原生 WebView 和浏览器加载同样的功能，不需要各写一套调度器。设备离线或凭证仅有查看权限时不能写入。

## 外部 GPT、Codex 与其他 agent

支持两种配合方式，可同时使用：

- **系统内置 AI**：Center 调用配置的模型 API，自动观察、执行工具、检查反馈。需要该 API 的凭证。
- **外部 agent**：Codex 或其他模型客户端通过 MCP/HTTP 直接观察、控制，或者使用 `exlab_delegate` 委托系统 AI。直接观察/控制不依赖内置 AI 开关，也不需要 Center 使用同一家模型服务。

因此 Codex 当前会话可用自己的模型调用 ExLab 工具。Center 不读取 Codex 的登录信息，也不会把 ChatGPT/Codex 的登录凭证当成模型 API Key。当前外部客户端是否自动批准工具，由该客户端自己的配置决定；Center 内置的自动托管不需要逐项批准。

在 Center「巡检与托管 → 巡检设置 → 外部 GPT / Codex 接入」生成独立凭证，分别授权 `read`、`control`、`delegate`、`automation`（包含规则管理）。凭证只显示一次，数据库仅存哈希，可设有效期并独立撤销；管理员凭证不应交给外部模型。每个 HTTP/MCP 请求都会重新校验权限，委托执行也会重新检查委托凭证。

### Codex 使用 HTTP MCP

在 Codex 所在进程的环境中设置 `EXLAB_AGENT_TOKEN` 为刚签发的独立凭证。将以下配置加入自己的 MCP 设置，地址按实际 Center 修改：

```toml
[mcp_servers.exlab]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "EXLAB_AGENT_TOKEN"
```

Windows 和 Linux 的 MCP 客户端均可使用该地址。`127.0.0.1` 指运行外部客户端的电脑；异机连接须填写可达的 Center 地址。远程连接沿用项目现有的网络部署和 HTTPS/内网访问方案。

### 使用 stdio MCP 桥接

如果客户端使用 stdio，可以从项目根目录或安装包的 Python 环境启动：

```sh
python -m expman.agent_mcp --url http://127.0.0.1:8765
```

凭证默认从 `EXLAB_AGENT_TOKEN` 读取，也可用 `--credential-file /path/to/agent-token.txt`。stdout 只输出逐行 JSON-RPC，不混入状态文本。桥接进程负责转发到 Center，不运行第二个调度器。

Codex 的 stdio 配置示例（Python 路径与工作目录按实际安装修改）：

```toml
[mcp_servers.exlab_stdio]
command = "python"
args = ["-m", "expman.agent_mcp", "--url", "http://127.0.0.1:8765"]
cwd = "/path/to/experiment_manager"
env_vars = ["EXLAB_AGENT_TOKEN"]
```

HTTP 与 stdio 选择一种即可。项目不会自行修改外部客户端的全局配置。

### 工具与权限

| 工具 | 权限 | 用途 |
|---|---|---|
| `exlab_topology` | read | Center、全部节点、任务状态与队列分页；包含当前操作参数定义 |
| `exlab_job` | read | 单实验状态、日志尾部、检查点清单、事件、轻量结果和证据引用 |
| `exlab_catalog` | read | 项目、矩阵或轻量结果分页 |
| `exlab_events` | read | 按游标读取持久化任务事件 |
| `exlab_agent_status` / `exlab_agent_run` | read | 系统 AI 状态、预算、计划、工具步骤及反馈核验 |
| `exlab_agent_history` | read | AI 摘要分页，默认 5 条，最多 20 条 |
| `exlab_rules` / `exlab_inspection` | read | 规则库与语言约束 / 当前发现分页 |
| `exlab_rule_preview` | read | 验证规则并计算命中；不保存或执行 |
| `exlab_rule_save` / `exlab_rule_delete` | automation | 按 revision 保存 / 删除自定义规则 |
| `exlab_control` | control | 使用固定操作及参数直接控制系统 |
| `exlab_delegate` | delegate | 给系统 AI 一个目标，立即返回持久化 run_id |
| `exlab_automation` | automation | 按 revision 启停或设置自动托管 |

`exlab_control` 支持 `queue`、`handoff`、`pause`、`resume`、`cancel`、`availability`、`node_mode`、`resources`、`submit`、`matrix_start`、`project_deploy`。精确参数以 `tools/list` 或拓扑的 `operation_schemas` 为准。没有任意 shell 或任意文件操作工具。直接控制仍须满足版本、执行所有权及 Worker 资源规则；外部直接控制具有人工控制的语义，可显式解除策略。系统内置 AI 额外遵守人工限制基线。

拓扑提供执行摘要，完整诊断按任务查询。`exlab_job` 可用 `fields:["execution_state"]` 单独获取较大的执行状态；数组字段支持 `offset/limit`，日志可通过 `log_chars` 限制长度。诊断数据多时不会把所有完整状态塞进首次模型请求。

### HTTP 工具接口

所有请求带 `Authorization: Bearer <agent credential>`，POST 使用 JSON。

```text
GET  /api/agent/tools
GET  /api/agent/status
GET  /api/agent/history?offset=0&limit=5
GET  /api/agent/inspection?offset=0&limit=10
GET  /api/agent/rules
GET  /api/agent/run?id=<run_id>
POST /api/agent/rules/preview {"document":{...}}
POST /api/agent/rules/save    {"revision":0,"document":{...}}
POST /api/agent/rules/delete  {"id":"...","revision":1}
POST /api/agent/call          {"name":"exlab_topology","arguments":{"offset":0,"limit":20}}
POST /api/agent/delegate      {"instructions":"检查所有节点并推进队列","request_id":"unique-intent"}
POST /api/agent/settings      {"revision":0,"enabled":true}
POST /api/agent/interrupt     {"run_id":"..."}
```

签发、列举、撤销 agent 凭证的 `/api/agent/credentials` 和 `/api/agent/credentials/revoke` 仅允许主控管理员。外部 agent 凭证不能访问通用管理员、文件夹导入、Worker 或移动配对接口。

控制请求通过 `exlab_control` 调用，例如调整尚未分配的任务：

```json
{
  "name": "exlab_control",
  "arguments": {
    "operation": "queue",
    "arguments": {"job_id": "...", "priority": 5, "hold": false, "revision": 0},
    "request_id": "prioritize-comparison-001"
  }
}
```

同一意图在不确定响应后重试时复用 `request_id`；不同意图使用新标识。同一标识不能绑定不同参数。不确定操作先读状态核验，不能不断换标识重复发送。

MCP 使用 `/mcp` 的 stateless Streamable HTTP JSON 响应，支持初始化、工具、拓扑/托管资源与 ping；不提供 SSE。通知返回 202，GET 返回 405；校验 Bearer、Origin 和协议版本（2025-03-26 / 2025-06-18）。

## 状态、结果和数据范围

调度执行状态、续训检查点、AI 决策/操作记录和最终轻量结果分别存储。AI 记录不会塞进实验结果 JSON；检查点仍走独立传输通道。

启用托管意味着允许模型看到有预算限制的系统状态，并按需查询单个任务的配置、日志尾部、结果和证据目录。已知的 Center、Worker、模型凭证和敏感字段会脱敏；不扫描上传数据集或完整源码。外部 read 凭证可查看同样的系统内容，应发给你信任的客户端。

已验证自动循环、双开关、版本中断、凭证撤销、重试幂等、预算、人工限制、Chat/Responses 工具消息、MCP HTTP/stdio、Monitor 权限和 Windows 编译。真实 GPT 服务、四台实际 GPU、Linux 电源设备及 Android/鸿蒙真机仍需部署验收；未配置模型凭证时不会假装已经调用真实模型。

协议参考：[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Codex MCP 配置](https://developers.openai.com/codex/mcp)、[MCP transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)。
