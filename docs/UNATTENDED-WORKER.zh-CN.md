# Windows 未登录启动 Worker

此模式使用 Windows 开机任务启动无窗口管理进程，管理进程启动指定 WSL2 中的独立 Docker Engine 和原 Worker。Docker Desktop 不参与此模式。Windows 登录启动窗口仍可单独配置；关闭窗口不停止无人值守后台。

## 一键迁移（0.5.29 起）

已有 Docker Desktop 节点无需手动安装 Docker Engine 或搬运配置。安装新版本后，打开 **Worker → 概览 → 设置开机启动**。

1. 使用拥有原 WSL 的同一 Windows 账户授权管理员权限，再输入该账户的登录密码（不是 PIN）。向导先验证任务账户凭据，密码不写入日志、备份、命令行或任务 XML。
2. 确认原 Ubuntu 专供 Worker 使用，选择新环境与备份目录。默认选择空间最大的本机固定磁盘；数据集所在磁盘也必须在开机时可用。加密盘应事先配置自动解锁。
3. 点击 **迁移并开启**。向导暂停接单、等待执行中的实验结束，再停止管理代理。队列和已暂停的记录随数据库保留。可点击取消；复制或安装进行中时会等当前步骤结束再恢复。
4. 自动备份 Ubuntu、Docker 镜像及 Worker 自有的两个镜像仓库卷；导入专用 WSL 2；从官方签名仓库安装 Docker Engine 和 NVIDIA Container Toolkit。无需在 WSL 中安装 Linux GPU 驱动。
5. 自动恢复镜像，核对原节点配置、数据库和镜像内容，在每张原先启用的 GPU 上用对应运行环境执行 CUDA 运算，最后登记未登录开机任务并交接后台。

一键路径支持专用 x64 Ubuntu 22.04、24.04、26.04、26.10，要求原节点已配置、Docker Desktop 可连接，且已有启用并验证过的 NVIDIA GPU。带自定义 WSL 开机命令、额外挂载、共享节点状态目录、使用待关闭 Ubuntu 挂载的容器、共享 Worker 仓库卷的容器或自定义镜像仓库布局的环境会说明阻塞原因，可使用下方高级配置。远程私有镜像若依赖 Desktop 凭据助手，可能需要先配置该仓库的独立凭据；不会将未验证镜像替换成其他版本。

0.5.30 起，不相关的 Docker Desktop 容器可以继续运行。需要暂停的容器会显示名称及挂载原因；无法确认来源的 Linux 挂载仍会拦截。迁移只操作自己的节点和仓库，不自动停止这些容器。

### 空间、恢复与卸载

迁移保留原 Ubuntu 和 Docker Desktop 的数据，额外创建导出备份和新 WSL 虚拟磁盘。空间检查包含 Linux 文件系统、镜像备份、仓库及新引擎，并留出余量；因此迁移期间占用明显高于迁移完成后的单套环境。新环境仍使用 WSL 和 Docker Engine，训练时仍消耗相应 CPU、内存、GPU 与磁盘。

迁移失败会尝试恢复原节点配置、启动入口与接单策略，并清理本次尚未交接的副本，保留备份。异常关机后重新打开向导，点击 **恢复原节点**；若旧的复制或安装进程还在运行，先等待它结束。已经完成交接的新节点可能产生新实验数据，此时只允许 **启动迁移后的后台**，不会自动回退到旧数据库。

备份目录显示在向导中，记录文件为 `migration-active.json`，目录内包含 `manifest.json`、校验记录和 `progress.log`。备份含节点凭据，访问权限限制为当前 Windows 账户、管理员和 SYSTEM，不应公开上传。

原环境与备份不会在成功后自动删除，也不自动卸载 Docker Desktop。完成下方“未登录重启验收”，并确认其他工作不依赖 Desktop 后，可卸载 Desktop。新节点使用独立引擎；卸载 Desktop 不会删除新 WSL，但会影响 Desktop 自己的镜像、容器和卷。清理旧环境前应核对项目、数据集、结果和检查点。

## 高级配置：已准备好的独立环境

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

手动“检测独立环境”仍会拒绝直接使用 Docker Desktop。已有 Desktop 节点请使用上方一键迁移入口。

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
