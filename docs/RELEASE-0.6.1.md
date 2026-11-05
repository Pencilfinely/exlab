# ExLab 0.6.1 · Windows 迁移连接修复

- 修复一键迁移误报“无法识别 Docker Desktop 独立连接，请检查 WSL 集成”。0.6.0 错误地要求 socket 路径位于 `/mnt/wsl/`；现在支持 Desktop 的标准 `/var/run/docker.sock`、`/run/docker.sock` 代理及旧版共享路径。
- 按实际引擎身份、Linux 模式和 socket 隔离验证连接，保留原客户端入口；WSL 重启后继续核对原引擎身份。不会把其他 Docker 引擎接管为迁移源。
- 修正特权安装阶段相同的路径限制，并让新旧两种镜像存储的集成测试直接使用标准 socket 完成传输、校验、重试与回退。

升级 Worker 后重新点击一键迁移即可。该报错发生在迁移预检阶段，尚未修改原环境，无需因这条报错重装 Docker 或 WSL。

完整生活质量更新见 [0.6.0](RELEASE-0.6.0.md)。远程服务器的 GPU 迁移和未登录重启仍需在目标机器验收。
