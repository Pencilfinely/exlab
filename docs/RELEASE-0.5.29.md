# ExLab 0.5.29

Windows Worker 新增 **无人值守启动 → 一键迁移并启用**，将已有 Docker Desktop 节点迁入专用 WSL 2，自动安装独立 Docker Engine 与 NVIDIA Container Toolkit，配置未登录开机启动。

- 自动检测原节点、GPU、数据位置、任务账户凭据和空间；暂停接单并等待执行中的实验结束。
- 保留原节点身份、配置、数据库、Linux 文件和待执行记录，单独备份并恢复 Worker 镜像仓库及镜像。核对镜像内容，执行实际 CUDA 运算后才交接。
- 失败自动尝试恢复原配置与策略；异常退出可在向导恢复。已交接的新节点禁止回退到旧数据库，避免丢失新实验数据。
- 新环境移除 Docker Desktop 专用的客户端依赖。原 Ubuntu、Desktop 数据及备份保留，不自动卸载或删除。

升级后打开 **Worker → 无人值守启动… → 一键迁移并启用（推荐）…**。需要同一 Windows 账户的管理员授权、账户密码（不是 PIN）、联网及额外可用磁盘空间。支持专用 x64 Ubuntu 22.04、24.04、26.04、26.10；自定义开机命令、外部共享节点目录等特殊布局会说明阻塞原因。

开发验收包括迁移校验与故障恢复测试、Windows 原生编译和更新回归。此次发布未迁移当前 5070 Ti，也未代替远程服务器的实际验收：配置后仍须重启并保持 Windows 未登录，从主控实际运行一次 GPU 任务，再考虑卸载 Docker Desktop、清理原环境。

详见 [Windows 无人值守与一键迁移说明](https://github.com/Pencilfinely/exlab/blob/v0.5.29/docs/UNATTENDED-WORKER.zh-CN.md)。
