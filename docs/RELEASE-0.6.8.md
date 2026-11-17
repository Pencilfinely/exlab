# ExLab 0.6.8

修复 WSL 搬盘或重启后，Windows 侧 Docker Desktop 已启动、Ubuntu 内 Docker 连接尚未建立时，迁移立即失败的问题。准备独立引擎、重启后传输以及失败恢复现在会等待 Ubuntu 内连接就绪，再核对原引擎身份和存储模式；等待最多 120 秒，超时会提示检查 WSL 集成。

独立引擎配置错误通过结构化回执显示具体原因，避免只出现“WSL 迁移步骤失败（退出码 1）”。保留 0.6.7 的迁移运行锁修复，以及原地迁移和镜像去重空间预算。

同时显式配置独立引擎自带的 docker-proxy 路径，修复纯 Docker Desktop 环境没有全局 docker-proxy 时的 Docker 配置校验失败。

Ubuntu 已运行 systemd 时直接启动独立引擎，避免多余的 WSL 终止触发 Docker 集成反复报错。需要启用 systemd 时先协调暂停 Docker 再重启；恢复时等待独立引擎的挂载释放，避免过早清理导致“资源正忙”。

Windows Worker 使用“检查更新 → 下载并排队安装”升级。如果之前已经成功搬盘，保留当前 Ubuntu 数据位置即可，无需再次搬盘。
