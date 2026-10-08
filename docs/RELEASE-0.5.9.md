# ExLab 0.5.9

本版修复 Windows Worker 的 Docker 启动、退出与更新判断，并为原生 Ubuntu Worker 增加 GitHub Release 一条命令更新和可选自动更新。Windows 与 Ubuntu 包包含相同的 Python 后台代码。

Windows 登录自启的客户端现在也可手动点击“启用算力”启动 Docker Desktop。WSL 中确认 Docker Desktop 已停止后，可完成保存退出及安装更新；历史实验、未启动任务和本地检查点保留。无法确认停止或另有原生 Docker daemon 时继续保留运行环境。

Ubuntu 安装后会在原服务目录生成稳定的 `Update-Worker.sh`。运行该入口即可检查同渠道的 GitHub Ubuntu ZIP、下载并校验 SHA-256 与完整文件清单，在实验与回传完成后停止代理、安装新版后台并恢复原运行选择。节点身份、GPU 策略、专用 Docker 地址、镜像及实验数据保留。旧终端代理不能自动接管时明确提示人工退出旧入口，不强杀实验。

`--auto enable` 开启 systemd 用户定时器，默认每小时检查版本；已下载的更新每分钟重试空闲条件，失败按退避时间重试。`--auto disable` 关闭自动更新，`--status` 查看状态。自动更新默认关闭，各服务目录使用独立定时器。用户服务注销与重启后的运行仍取决于原有用户会话和 linger 配置。

更新失败可恢复原后台代码；中断在安装后、恢复运行前时，下次命令或定时器继续完成交接。更新程序不重新配对、准备 GPU 或重建训练镜像。Windows 旧 `Update-Worker.cmd` 保留前台复用配置的行为；Ubuntu 可用 `--run-existing` 使用同一旧入口。

完整命令与首次升级说明见 [运维说明](OPERATIONS.zh-CN.md)。
