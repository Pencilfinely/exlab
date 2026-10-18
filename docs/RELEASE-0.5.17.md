# ExLab 0.5.17 — 停用后真正释放 Docker / WSL

修复点击“停用并释放 Docker / WSL”后，因 ExLab 自带的 `expman-harness-registry` 被误判为“其他容器”，只停用算力却一直保留 Docker / WSL 的问题。

- 同时识别算力镜像仓库与算法依赖镜像仓库。完成实验保存、停止并核实代理退出后，依据确切名称、归属标签及完整容器 ID 停止自带仓库；容器、镜像、数据卷与实验文件保留。
- Ubuntu 正在运行和已经关闭的两条 Windows 释放路径均可清理自带仓库；已关闭的 Ubuntu 不会为检查或释放而被重新启动。Windows 新客户端也能处理旧 WSL 后台返回的仓库阻塞状态，前提是已确认代理停止且 Docker 身份一致。
- 确有其他容器时，列出名称、镜像与容器 ID，用户可明确确认停止这些容器并继续释放。自动退出不会停止外部容器；确认后出现新容器或 Docker 身份变化时需要重新检查，旧确认不能授权关闭新工作。
- 停用与资源释放分别显示。未关闭容器、保留其他会话、检查失败或 Docker/WSL 被其他程序重新启动时，显示“资源释放未完成”。Docker stop 命令返回成功后仍会查询实际状态；只有核实 Docker 停止且 WSL 完全关闭，才显示“资源已释放”。
- 其他算力代理和其他 WSL 系统仍受保护。原生 Ubuntu 更新自带仓库识别和阻塞容器详情，不停止系统 Docker 服务。定时“释放训练资源”仍保留轻量控制代理与共享运行环境；退出整个虚拟机使用 Windows Worker 的上述入口。

提供 Windows Center/Worker 安装器与 ZIP、Ubuntu Worker ZIP、Android Monitor 0.5.17 APK（versionCode 8，沿用原签名），附 SHA-256 清单与构建信息。旧仓库提供相同字节的 Windows 安装器。HarmonyOS 工程同步版本，未提供原生 HAP。

验证覆盖双仓库、名称/标签/ID 不符、外部容器明确确认、拒绝过期确认、新增容器、停止后仍运行、Docker 实际状态与 WSL 关闭复查；同时执行 Python、原生 Windows、Android 构建与跨平台 CI。隔离测试不停止真实 GPU 训练，实机释放结果另行核实。
