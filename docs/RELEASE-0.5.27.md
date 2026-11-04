# ExLab 0.5.27 — Windows Worker 恢复算力与资源释放

Docker 的 Windows 引擎就绪不代表所选 Ubuntu 的集成、socket 和原引擎身份已通过检查。启用算力现在等待 Ubuntu 的实际连接，再启动原 Worker；等待超时会保留具体失败原因。

- 主控仍要求释放此节点时，手动启用明确提示先恢复“允许接单”，避免一边启动一边释放。
- 资源释放失败采用逐步延长的重试间隔，策略变化后立即重新检查；操作忙时按钮显示原因。
- Docker 已关闭但 `docker desktop status` 超时时，额外检查 Windows Docker 进程、引擎管道及 Docker WSL。只有三项均证实关闭，且没有原生或远程引擎，才认定 Docker 已停止；查询失败不构成关闭证明。
- Docker 将错误写到标准输出时也保留诊断，避免出现无内容的 `docker failed (1)`。

回归覆盖 Python 实际 Docker 状态与原生客户端之间的启动顺序、集成未就绪、等待超时，以及 Windows 关闭证据不完整或有存活引擎时拒绝误判。未加入 Windows 未登录自启动功能。
