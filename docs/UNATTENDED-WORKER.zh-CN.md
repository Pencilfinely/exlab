# Windows 未登录启动 Worker

此模式使用 Windows 开机任务启动无窗口管理进程，管理进程启动指定 WSL2 中的独立 Docker Engine 和原 Worker。Docker Desktop 不参与此模式。Windows 登录启动窗口仍可单独配置；关闭窗口不停止无人值守后台。

## 一键迁移（0.6.2 支持选择数据盘）

已有 Docker Desktop 节点无需手动安装 Docker Engine 或搬运配置。安装新版本后，打开 **Worker → 概览 → 设置开机启动**。

1. 使用拥有原 WSL 的同一 Windows 账户授权管理员权限，再输入该账户的登录密码（不是 PIN）。向导先验证任务账户凭据，密码不写入日志、备份、命令行或任务 XML。
2. 选择 **Worker 数据位置**，例如 `E:\ExLabWorker\Ubuntu-Data`，点击 **检测空间**。新位置需为空目录；保留当前 Ubuntu 位置也可以。配置备份目录在折叠选项内，和数据目录分开。
3. 点击 **迁移并开启**。向导暂停接单、等待执行中的实验结束，再停止管理代理。队列和已暂停的记录随数据库保留。可点击取消；复制或安装进行中时会等当前步骤结束再恢复。
4. 保存小型配置备份。若更改数据位置，先短暂停止空闲 Docker Desktop、关闭原 Ubuntu，确认没有其他运行中的发行版后释放 WSL 磁盘，再由 WSL 移动磁盘并恢复 Desktop。随后从官方签名软件源准备 ExLab 独立 Docker Engine 和 NVIDIA Container Toolkit。Ubuntu 名称、用户、项目、输出和数据库保留，不替换 Desktop 的 Docker 命令或 socket。
5. 镜像逐个直接传输；自有仓库卷流式复制并核验哈希。核对节点配置和数据库，在每张已启用 GPU 上执行对应环境的 CUDA 运算，通过后登记开机任务并交接。随后尝试回收旧引擎中已经验证的 ExLab 副本。

一键路径支持专用 x64 Ubuntu 22.04、24.04、26.04、26.10，要求原节点已配置、Docker Desktop 可连接，且已有启用并验证过的 NVIDIA GPU。带自定义 WSL 开机命令、额外挂载、共享节点状态目录、使用待关闭 Ubuntu 挂载的容器、共享 Worker 仓库卷的容器或自定义镜像仓库布局的环境会说明阻塞原因，可使用下方高级配置。远程私有镜像若依赖 Desktop 凭据助手，可能需要先配置该仓库的独立凭据；不会将未验证镜像替换成其他版本。

0.5.30 起，不相关的 Docker Desktop 容器可以继续运行。需要暂停的容器会显示名称及挂载原因；无法确认来源的 Linux 挂载仍会拦截。迁移只操作自己的节点和仓库，不自动停止这些容器。

**改盘需要一次空闲维护窗口**：先结束其他 Docker 容器和 WSL 环境中的工作。向导会列出具体名称，不会强行关闭它们。保留原数据位置时，不需要重启 Desktop。改盘要求 WSL 2.7.14 或更新版本（`wsl --update`），以及支持 `docker desktop start/stop` 的 Docker Desktop。

### 空间、恢复与卸载

0.6.0 不再复制整套 Ubuntu，也不产生完整镜像 tar。迁移临时空间主要是目标镜像缓存、仓库和引擎，另留安装余量；原始数据必须保留到新环境验证通过，因此不能保证零额外空间。新环境仍使用 WSL 和 Docker，训练仍消耗任务要求的资源。

跨盘时，目标盘还需容纳现有 Ubuntu 的整个 VHDX 文件；检测按实际文件长度和新环境空间计算，显示可用量与差额。同盘改目录不重复计入 VHDX。空间不足会在暂停原节点前停止；等待实验结束和改盘前再次核对。WSL 直接移动磁盘，不在 C 盘导出中转 tar；移动完成后原磁盘文件被移除。

选 E 盘会把该 Ubuntu 内的实验、输出和新引擎大文件放到 E 盘，但不会移动原 Docker Desktop 的共享磁盘、Windows 程序目录或全局 WSL swap。外部数据集维持原路径，所用磁盘必须开机可访问。

交接前失败会恢复原配置、启动入口和接单策略，清理本次创建的独立引擎副本。异常关机后重新打开向导：未交接时点击 **恢复原节点**；已验证并进入交接时点击 **完成交接并启动**。正在运行的安装步骤会先结束；完成交接后不回退旧数据库。

若改盘已经成功，恢复原节点时会保留新的数据位置，不把大文件搬回 C 盘。恢复前核对发行版登记和磁盘身份；结果不明确时保留两端文件并停止，不注销 Ubuntu 或擅自删除副本。被本次改盘暂停的 Desktop 会在正常流程或恢复流程中重新启动并核对引擎身份。

备份目录显示在向导中，记录文件为 `migration-active.json`，目录内包含 `manifest.json`、校验记录和 `progress.log`。备份含节点凭据，访问权限限制为当前 Windows 账户、管理员和 SYSTEM，不应公开上传。

原 Ubuntu 和配置备份保留，不自动卸载 Docker Desktop。完成下方未登录验收、确认其他工作不依赖 Desktop 后，可卸载 Desktop；不要注销正在使用的 Ubuntu。删除文件不等于 Windows 虚拟磁盘立即变小，见 [存储清理与磁盘回收](STORAGE.zh-CN.md)。

独立引擎使用 `exlab-docker.service`、`/opt/exlab/docker`、`/var/lib/exlab-docker` 和专用 socket。训练容器保持无网络；环境准备使用 host 网络，仓库只监听本机回环地址，不改写其他引擎的桥接网络和防火墙。旧版独立 `docker.service` 环境继续兼容。

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
