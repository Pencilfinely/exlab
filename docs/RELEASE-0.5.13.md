# ExLab 0.5.13 — dynamic compute and autonomous AI management

## 动态算力与实验接手

- 节点可设置常开 / 动态使用策略、维护时段、插电要求和可用时长；Windows Worker 提供游戏释放入口，Linux Worker 提供相同的 CLI 与控制协议。调度结合资源、负载、风险和预计运行时长选择节点。
- 实验支持队列优先级、暂停接单、临时释放和协作式检查点接手。只有原进程确认退出、检查点完整上传并校验后才可接手；突然断电不会触发未经确认的重复训练。
- **执行状态与最终结果分离**：执行状态供规划、恢复和续训使用，检查点独立传输；最终实验结果继续采用轻量、不可变回传协议。

## AI 自动托管与外部 agent

- 在设置中启用 AI，并勾选自动化介入，主 Center 即可按状态变化和复查周期自主观察、规划、操作与检查反馈，无需每次点击。默认关闭，可限制允许操作、调用频率和每轮行动数。
- 支持 Chat Completions / Responses 形式的模型接口。外部 GPT、Codex 等 agent 可通过 MCP（HTTP 或 stdio 桥）及 HTTP 工具接口读取拓扑和任务、直接操作，或委托系统 AI。
- 外部 agent 使用独立、可撤销、可过期的权限凭证。自动托管保留人工释放和维护限制，操作使用版本检查及幂等记录；停用设置、撤销凭证和主 Center 切换会阻止旧规划继续执行。
- Windows / Linux Worker 和 Monitor 共用上述状态及控制能力。Monitor 控制凭证可管理托管和节点可用性，只读凭证保持只读。

配置与使用：[AI 接入](AI-AGENT.zh-CN.md)、[动态算力与恢复条件](DYNAMIC-COMPUTE.zh-CN.md)。Center 升级后才会提供新版共享网页和后台服务；执行状态与检查点接手需要相关 Worker 同步升级，并由算法提供可恢复的检查点。

## 发布内容

| 平台 / 角色 | 文件 |
| --- | --- |
| Windows x64 Center | `ExLab-0.5.13-windows-controller-x64-Setup.exe`、完整 ZIP |
| Windows x64 Worker | `ExLab-0.5.13-windows-worker-x64-Setup.exe`、完整 ZIP |
| Ubuntu / Linux x64 Worker | `ExLab-0.5.13-ubuntu-worker-x64.zip` |
| Android 9+ Monitor | `ExLabMonitor-0.5.13-android-debug.apk`，保留原签名，versionCode 4 |
| HarmonyOS 6 Monitor | 原生工程随源码标签更新至 0.5.13；本次不提供 HAP，缺少 API 20 SDK 与设备签名配置 |

同时提供 `SHA256SUMS.txt` 和 `build-info.json`，并在旧仓库发布逐字节相同的 Windows 迁移安装器。Windows 安装包未做代码签名；Android 为沿用原身份的调试签名测试包。鸿蒙可继续通过手机浏览器访问升级后 Center 的 `/mobile/`；原生 HAP 需按[移动端构建说明](../mobile/README.md)配置 SDK 和设备签名后生成。

## 验证范围

Python 完整回归共 608 项，7 项按环境条件跳过，其余通过；共享 JavaScript 逻辑测试与 Windows 原生编译通过。Android 包已通过单元测试、构建、Lint 和签名连续性校验。未执行真实模型付费调用、真实 GPU 迁移演练、手机真机验收或 HarmonyOS 编译。

## Worker database recovery

Workers could stop reporting heartbeats after an internal SQLite interface error,
while their Docker training containers kept running. Result discovery also shared
a database connection and cache filenames across execution, result, and migration
threads without serializing the complete discovery operation.

This version serializes result discovery, keeps result scanning outside the
execution/heartbeat loop, and reopens the management agent after a runtime SQLite
interface error. Recovery reuses the original configuration and persisted task
records; it does not stop Docker containers or create another training attempt.
Errors during save/exit and failed cleanup remain failures. Exception logs now
include a redacted traceback, exception type, Python/SQLite versions and the SQLite
error code/name when available.

The two A6000 incidents recorded only an error string. These fixes remove verified
concurrency and failure-containment gaps; their exact historical triggering call
cannot be established retroactively without a traceback.
