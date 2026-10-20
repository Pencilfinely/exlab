# ExLab 0.5.20 — 修复释放后恢复接单时 Docker 启动失败

修复 Windows 节点恢复接单时，Docker Desktop 因 `engine.sock`、`sailor-ingest.sock` 或 `dockerInference` 残留套接字无法访问而启动失败的问题。该现象也有 [Docker 上游记录](https://github.com/docker/for-win/issues/15064)。

- 启动前仅在 Docker Desktop 与引擎均未运行时检查已知 AF_UNIX 套接字。发现残留后，将 `docker-secrets-engine` 或 `Docker/run` 整个目录改名保留，再重建空运行目录。原镜像、容器、数据盘、设置和实验文件保留。
- 目录存在重定向、检查失败或检测到其他 Docker 启动时停止自动处理。不会关闭其他应用或清理未知目录；保留目录位置记录在 Windows 客户端的 `docker-recovery.json`。
- Docker 只启动一次，再检查 Windows 侧 Linux 引擎就绪状态，不再因 CLI 超时重复启动，也不通过启动 WSL 轮询 Docker。兼容按用户安装的 Docker Desktop 路径。
- 恢复失败时，中控立即收到具体阻塞原因。相同请求的自动重试间隔逐步从 30 秒增加至最多 10 分钟；新策略或恢复成功后重置。
- 恢复过程中取消“资源已释放”状态，防止正在恢复的运行环境继续显示为已经释放。

包含 0.5.19 的 Windows 独立控制连接、中控释放与恢复、资源核验及主控性能修复。

发布 Windows Center/Worker 安装器与 ZIP、Ubuntu Worker ZIP、Android Monitor 0.5.20 APK（versionCode 11，沿用原签名）、SHA-256 和构建信息。旧仓库提供相同字节的 Windows 安装器。HarmonyOS 工程同步版本，未提供原生 HAP。
