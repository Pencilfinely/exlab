# ExLab 0.5.19 — 中控释放 Windows 运行环境，主控恢复响应

修复中控“释放资源”只停止实验、保留 Docker / WSL，却显示释放成功的问题。Windows Worker 新增独立于 WSL 的轻量控制连接：中控释放请求由 Windows 客户端接管，保存并停止实验，清理自带镜像仓库，再关闭空闲 Docker 与 WSL；只有分别核实两者退出，才显示资源已释放。

- Windows 运行环境关闭后，控制连接继续在线，不通过启动 WSL 轮询状态。中控恢复接单或暂停到期后，Windows 根据最新策略恢复原节点；本机手动停用和插电限制仍有效。
- 其他容器、其他算力代理、其他 WSL 或会话阻止完整关闭时，回传明确阻塞状态，保留相关工作。中控不会替用户确认停止外部工作。
- 训练停止、训练容器清理、Docker 退出、WSL 退出分别核验。旧回执、过期回执、策略版本不符和 Windows 失联均不能证明整机资源已经释放。
- 控制连接在线与算力代理在线分开；已关闭的 Worker 不参与调度，旧内存和显存读数不显示成当前测量。Center、Monitor、原生 Windows 状态和外部模型拓扑接口同步适配。
- 包含 0.5.18 主控性能修复：815 条历史实验的状态响应从约 94 MB 缩减到约 2.6 MB，启动与更新改用轻量健康检查，页面慢请求不再叠加。旧主控正常停止后可保留原数据目录升级。

Windows 节点需同时升级 Center 与 Windows Worker，并保持 Windows 客户端运行；控制连接使用本节点凭证，不使用管理员凭证。原生 Linux 释放训练容器并保留系统服务。Windows 本机“停用并释放 Docker / WSL”入口继续保留。

提供 Windows Center/Worker 安装器与 ZIP、Ubuntu Worker ZIP、Android Monitor 0.5.19 APK（versionCode 10，沿用原签名）、SHA-256 与构建信息。旧仓库提供相同字节的 Windows 安装器。HarmonyOS 工程同步版本，未提供原生 HAP。
