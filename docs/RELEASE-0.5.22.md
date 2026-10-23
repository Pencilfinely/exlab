# ExLab 0.5.22 — 网络回复中断后保持 Worker 运行

修复 Worker 在读取主控回复时遇到 `http.client.IncompleteRead` 异常后退出，导致服务器仍可连接但实验台长期显示节点离线的问题。

- 不完整回复、分块传输中断和异常 HTTP 状态行统一进入现有网络错误处理流程。Worker 保留任务和未确认回报，继续尝试已配置的主控地址或在下次同步时重试。
- 不接受半截回复，也不在通用 HTTP 客户端中盲目重发可能已提交的 POST 请求。回报确认仍由原协议处理，已有训练的节点归属和执行次数保持不变。
- 认证失败、请求冲突及 JSON 内容错误保留原有错误类型和处理方式。
- Windows / Ubuntu Worker 使用同一修复。已退出的 Worker 需要重新启动或安装更新；只有更新实际安装到算力端后，才能避免这一异常再次导致退出。

回归测试通过真实本机 HTTP 连接覆盖固定长度与分块回复截断、异常状态行、错误语义保持、Worker 恢复同步与回报重放，以及备用主控切换。

提供 Windows Center / Worker 安装器与 ZIP、Ubuntu Worker ZIP、Android Monitor 0.5.22 APK（versionCode 13，沿用原签名）、SHA-256 与构建信息。旧仓库提供相同字节的 Windows 安装器。HarmonyOS 工程同步版本，未提供原生 HAP。

Ubuntu 使用原服务目录中的 `Update-Worker.sh` 更新；管理代理交接保留 Docker 实验、节点配置和待回传记录。安装完成后核对 Worker 版本与新鲜心跳。
