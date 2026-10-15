# AI 自动托管与外部 agent 接入

Center 具备持续运行的“观察 → 规划 → 工具执行 → 回传核验”循环。关闭网页或 Monitor 后，循环仍在 Center 后台运行。先开启 AI 服务，再勾选 **自动化介入 · AI 托管**，无需逐次点击生成建议或监督每个动作。原来的“AI 规划建议”仍可用于手动预览。

## 开启托管

1. 在 Center「设置 → AI 辅助」配置 API 地址、协议、模型和 Key，保存并测试连接。
2. Chat Completions 兼容服务选择 `chat_completions`；OpenAI Responses 接口选择 `responses`，基础地址填写 `https://api.openai.com/v1`。模型名使用该服务、该账户实际开放的 API 模型 ID。
3. 在下方「AI 自主规划与检查」勾选「自动化介入 · AI 托管」。填写长期目标，例如“优先完成已提交的对比实验；ROG 外出前释放；夜间优先安排可续训任务”。
4. 按需要调整复查频率、调用预算和允许的操作。默认允许调整队列、交接、恢复可续训任务、设置可用策略及暂停接单；提交新实验、取消、资源预算、项目部署等需在操作列表中单独开启。

默认最短调用间隔 60 秒，活跃任务每 300 秒复查，每轮最多 6 次模型调用、8 个操作，每小时最多 24 次调用。没有变化且没有待复查任务时不调用模型。节点上下线、可用策略/确认变化、任务状态、指令确认、队列及交接等变化会触发重新检查，进度心跳不会逐条触发付费调用。失败采用 60 秒至 1 小时退避，所有自动和委托调用共用小时预算。

关闭托管会在下一项工具执行前作废本轮自动计划；关闭 AI 服务、修改模型接口、修改托管设置、撤销委托凭证或中止本轮，也会阻止旧计划继续发出操作。已经送达 Worker 的请求继续通过真实状态核验，不伪装成回滚。

「委托系统 AI 处理」用于一次性目标，也可由外部 agent 发起。开启 AI 服务即可接受委托，不要求开启周期自动介入；委托仍使用系统配置的操作范围和预算。更改托管设置会终止基于旧版本设置的排队或执行计划。

## 自动操作与个人使用

AI 调用与人工控制共用原有调度、版本校验和执行协议。它不能启用本地未授权的 GPU，也不能越过本地可用策略、运行时段、数据/镜像兼容性或资源上限。

自动托管保留人工设置的插电要求、维护窗口、游戏释放、暂停接单和队列暂缓。AI 可以撤回自己设置的临时限制，但不能借后续操作抹掉原有人工限制。人工重新保存策略会成为新的约束基线。

断电或丢失心跳只表示失联。跨节点接手仍须原进程确认停止；已开始的实验还须声明完整续训能力，并具备中心完整接收、校验的检查点。无续训能力的任务不会被 AI 自动抢占后假装成功续训。用户手动停止、取消交接的实验不会被自动恢复。

操作记录中的 `requested` 表示请求已持久化。运行记录的 `outcomes` 根据实际队列、指令确认、执行代次、Worker 策略版本和部署回报更新为 `verified`、`pending` 或 `needs_review`。入队操作的 verified 只代表已经入队。请求未确认时不能仅凭模型文字判断执行完成。进程中途退出遗留的操作标记为不确定，不盲目重放。

## Windows、Linux 与 Monitor

| 端 | 使用入口 |
|---|---|
| Windows / Linux Center | 同一网页设置、托管记录、委托和外部接入面板；只有调度主控执行托管，副本转发操作 |
| Windows Worker | 桌面「可用时间 / 游戏释放」，设置本地允许/排空/释放、时长和插电要求；状态窗口显示 AI 托管和本机可用状态 |
| Linux / WSL Worker | 下列 CLI，以及 `status` 返回的 `controller_agent` 和 `availability`；同一 Worker 执行协议 |
| ExLab Monitor（Android、鸿蒙、浏览器） | 总览中的托管开关、委托和记录；节点可用策略/游戏释放；详情的执行状态、续训与轻量结果。查看凭证保持只读，控制凭证允许操作 |

```sh
./Client-Worker.sh compute-policy --compute-mode release --minutes 120
./Client-Worker.sh compute-policy --require-ac
./Client-Worker.sh compute-policy --compute-mode available
./Client-Worker.sh status
```

开发目录可使用 `python -m expman.worker_service` 替换发行包的 `./Client-Worker.sh`。本地限制写入节点配置旁的 `compute-override.json`，Center 离线时仍生效。释放训练资源与退出整个 Docker/WSL 的生命周期操作分别保留。

托管设置、运行记录和 agent 凭证哈希随 Center 数据库同步；模型 API Key 保存在各 Center 本机。切换到另一台调度主控后，需要在新主控配置并启用其模型接口，才会继续调用内置 AI。

Monitor 使用 Center 提供的共享界面；主控升级后，各原生 WebView 和浏览器加载同样的功能，不需要各写一套调度器。设备离线或凭证仅有查看权限时不能写入。

## 外部 GPT、Codex 与其他 agent

支持两种配合方式，可同时使用：

- **系统内置 AI**：Center 调用配置的模型 API，自动观察、执行工具、检查反馈。需要该 API 的凭证。
- **外部 agent**：Codex 或其他模型客户端通过 MCP/HTTP 直接观察、控制，或者使用 `exlab_delegate` 委托系统 AI。直接观察/控制不依赖内置 AI 开关，也不需要 Center 使用同一家模型服务。

因此 Codex 当前会话可用自己的模型调用 ExLab 工具。Center 不读取 Codex 的登录信息，也不会把 ChatGPT/Codex 的登录凭证当成模型 API Key。当前外部客户端是否自动批准工具，由该客户端自己的配置决定；Center 内置的自动托管不需要逐项批准。

在 Center「AI 自主规划与检查 → 外部 GPT / Codex 接入」生成独立凭证，分别授权 `read`、`control`、`delegate`、`automation`。凭证只显示一次，数据库仅存哈希，可设有效期并独立撤销；管理员凭证不应交给外部模型。每个 HTTP/MCP 请求都会重新校验权限，委托执行也会重新检查委托凭证。

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
GET  /api/agent/run?id=<run_id>
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
