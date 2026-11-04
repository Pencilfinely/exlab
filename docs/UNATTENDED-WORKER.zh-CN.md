# Windows 未登录启动 Worker

此模式使用 Windows 开机任务启动无窗口管理进程，管理进程启动指定 WSL2 中的独立 Docker Engine 和原 Worker。Docker Desktop 不参与此模式。Windows 登录启动窗口仍可单独配置；关闭窗口不停止无人值守后台。

## 环境准备

先准备独立 Ubuntu WSL2 环境，安装 Docker Engine 和 NVIDIA Container Toolkit，启用 systemd。该环境不要启用 Docker Desktop 的 WSL 集成。使用 Windows NVIDIA 驱动，不在 WSL 中安装 Linux 显卡驱动。

Worker 本身使用 `detached` 后台，由 Windows 统一启动；Docker Engine 使用系统级 systemd 服务。若原 Worker 使用 systemd 用户服务，先停止并禁用对应的 `experiment-manager-worker-*.service`（以原服务实际名称为准），避免 Ubuntu 被其他程序唤醒时绕过 Windows 的停用策略。检测会拒绝仍启用旧启动入口的环境。

完成数据迁移及 GPU 容器验证后，在目标 Linux 普通用户下用完整包安装原配置，例如（将路径替换为服务器的实际路径）：

```bash
bash /mnt/c/ExLab/Client-Worker.sh install --backend detached --no-start \
  --config /home/worker/.local/share/experiment-manager/worker/node.ready.json
```

自定义服务目录时加 `--service-root /原服务目录`，随后在 Windows 设置窗口填写相同目录。

- WSL 安装：https://learn.microsoft.com/windows/wsl/install
- systemd：https://learn.microsoft.com/windows/wsl/systemd
- Docker Engine：https://docs.docker.com/engine/install/ubuntu/
- NVIDIA Container Toolkit：https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
- WSL GPU：https://docs.nvidia.com/cuda/wsl-user-guide/index.html

原节点迁移需要先停止实验并确认容器退出。保留原 Ubuntu、Docker Desktop 数据及节点配置的备份；迁移项目目录、数据集、检查点、Worker SQLite 数据库、配置和需要的 Docker 镜像/卷。Docker Desktop 的卷和镜像不包含在普通 Ubuntu 导出中。禁止仅改写 `setup-state.json` 的 Docker ID 来掩盖未完成的迁移。使用新引擎重新完成 Worker 环境配置及 GPU 验证，核对原 node_id、目录、结果和检查点。原后台与新后台不能同时管理同一节点。

现有环境依赖 Docker Desktop 时，设置窗口会明确拒绝开启。此版本提供启动管理和环境检测，**不自动迁移现有 Docker 数据、不自动安装 WSL 或卸载 Docker Desktop**。迁移属于单独的停机操作，应先完成盘点和备份。

## 开启

1. 安装完整 Windows Worker 包；先在独立环境完成原节点配置和 GPU 容器验证，随后停止原管理进程。
2. Worker → **无人值守启动…**，填写 WSL 发行版、Linux 普通用户名，以及原服务目录（通常可留空）。点击 **检测独立环境**。
3. 检测通过后，点击 **启用未登录开机启动**。创建开机任务需要同一 Windows 账户的管理员权限。若出现权限提升窗口，使用同一账户；其他管理员账户无法代替拥有此 WSL 的账户。
4. 输入 Windows 账户密码而不是 PIN。密码仅传给 Windows Task Scheduler 注册接口，不写进 ExLab 文件、任务 XML、日志或命令行。密码变更后需重新登记。
5. 任务使用密码型批处理登录，不启用 Windows 自动登录。账户必须允许批处理登录，且启动时能访问安装目录、WSL 和数据盘。
6. 若 Ubuntu 确实专供此 Worker 使用，勾选“此 Ubuntu 专供 Worker 使用，释放资源时允许关闭它”。未勾选时可以停止算力，但不会关闭发行版；勾选后仍会核查其他工作，存在阻塞时保留环境。

后台会等待主控策略确认；网络不可用时重试，已有已确认的释放策略继续生效。主控要求“停止实验”时由 Worker 执行；“释放资源”或本机停用会停止 Worker，并仅在确认没有其他容器或用户工作后关闭所选 Ubuntu。遇到阻塞会显示原因，不关闭其他发行版。

登录后的 **启用算力**、**停用并释放 Docker / WSL** 控制同一后台。主控仍要求释放时，本机启用不能覆盖主控策略。关闭开机启动只禁用下次开机任务，保留当前运行环境及记录。

## 验收与诊断

任务登记和环境检测不等于未登录验收。必须重启机器、保持在 Windows 登录界面，从另一设备确认原节点上线并实际完成一个 GPU 容器任务；再验证登录后没有重复后台、停用不被自动拉起、断网重试和更新交接。

配置在 `%LOCALAPPDATA%\ExperimentManager\desktop\Worker\boot-config.json`；最新状态在 `boot-status.json`，启动失败记录在 `client-lifecycle.jsonl` / `last-error.json`。任务名为 `ExLab-Worker-Boot-<Windows SID>`。如果任务未启动，请在任务计划程序查看任务最后运行结果和账户凭据。

停止后台或更新前会请求交接；本机更新使用稳定启动脚本定位已安装版本。升级失败时保留节点数据。旧任务运行的长操作可能使更新等待，超时会报错而非强行终止实验。

只有上述验收及数据核对通过后，才能考虑卸载 Docker Desktop；卸载会删除 Desktop 自己管理的本地镜像、容器和卷。
