# ExLab 0.5.16 — Ubuntu 更新源限流恢复

Ubuntu 自动更新可能已经开启、定时器也在运行，却因访问 GitHub 超时或 REST API 限流一直停留在旧版本。此版本修复正式版在 API 限流时无法继续查询新版本的问题。

- GitHub REST API 明确返回限流（403 / 429）时，正式版更新器改用本项目官方 `releases/latest/download/SHA256SUMS.txt` 获取候选版本，再核对该指定版本的校验清单和安装包大小。保持原有 HTTPS 来源限制、完整下载 SHA-256、角色、版本及文件清单验证。
- 不将其他 403 权限错误解释成限流，不降级安装，也不把预览版静默改成正式版渠道。CLI 检查结果可显示 `source: github-release-download`。
- 超时仍需解决节点到更新源的网络连接。通过 `https_proxy` / `HTTPS_PROXY` 指定的代理须对更新进程可用；终端变量不会自动写入 systemd 用户服务。依赖 Xshell 反向转发的代理也依赖该连接持续在线。
- 补齐 Center 的 `/compute.css` 静态资源路由，使网页和 Monitor 能实际加载 0.5.15 新暂停窗口的独立样式。

已受 API 限流影响的旧更新器不包含备用检查逻辑，可从指定版本的官方 ZIP 下载入口获取此版本，核对 SHA-256、解压后使用新版更新入口并指定原服务目录完成交接；保留原节点身份、Docker 实验、队列和待回传数据。无需创建 GitHub 个人访问令牌。

提供 Windows Center/Worker 安装器与 ZIP、Ubuntu Worker ZIP、Android Monitor 0.5.16 APK（versionCode 7，沿用原调试签名），附 SHA-256 清单及构建信息。旧仓库更新渠道提供逐字节相同的 Windows 安装器。HarmonyOS 工程版本同步更新，本次不提供原生 HAP，可使用 `/mobile/` 网页。

验证包括限流及更新交接回归、拒绝错误角色/版本/大小/校验值、真实 HTTP 静态资源、跨平台 CI、Android 构建/Lint、发行包及新旧升级渠道下载校验。隔离测试不操作真实 GPU 训练；A6000 的实际安装与心跳需另行确认。
