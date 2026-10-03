# 应用图标

`center.svg`、`worker.svg`、`monitor.svg` 是即时设计导出的定稿母版；原始的
`@1x.png`（48 px）、`@0.5x.png`（24 px）保留作为设计交付。

`center.ico`、`worker.ico`、`monitor.ico` 由母版直接生成，对应 Center、Worker 与 Monitor。
每个 ICO 包含 16、20、24、28、32、36、40、44、48、56、64、72、80、88、96、112、128、256 px，透明 PNG
预览在 `generated/`。每个尺寸直接渲染 SVG，未放大原始 PNG。

## 重新生成

需要 Node.js 和 sharp（本次生成使用的版本见 `generated/renderer.json`）。
可在项目内的临时依赖目录安装，然后在 PowerShell 执行：

```powershell
npm install --prefix .runtime/icon-tools --no-package-lock sharp@0.35.4
$env:NODE_PATH = (Resolve-Path .runtime/icon-tools/node_modules).Path
node scripts/build_icons.cjs
```

生成脚本更新 ICO、`generated/`、网页图标与 Android / HarmonyOS 图标资源，
保留即时设计导出的文件。
修改母版后应重新生成并一同提交；常规发布直接使用已生成的 ICO，
无需安装 Node.js 或 sharp。

## 程序接入

`scripts/build_desktop.py` 按角色把 ICO 写入 EXE 的 Windows 图标资源，
同时嵌入 `AppIcon` 供窗口和托盘读取。安装程序使用相同构建入口；
桌面和开始菜单快捷方式引用目标 EXE 的第一个图标。
管理端网页通过 `/favicon.ico` 使用同一份 Center 图标。

48×48 是 SVG 母版的设计坐标，不限制输出分辨率，无需放大原始 PNG。
桌面程序声明系统 DPI 感知，按窗口/任务栏所需 DPI 选择图标帧；
225% 缩放使用独立渲染的 36 px 小图标和 72 px 大图标。
实验台独立 Edge 窗口也绑定 Center 图标，仅匹配本应用专用浏览器配置目录。

## Monitor 手机图标

沿用 Monitor 母版的粉色底、白色 M 与红色 Ex，直接渲染各尺寸。

- Android：mdpi / hdpi / xhdpi / xxhdpi / xxxhdpi 启动 PNG，圆形 PNG，自适应背景与矢量前景；Android 13+ 另含主题单色矢量。
- 自适应前景以 108 dp 坐标生成，完整标记放在中央 66 dp 安全区域内，适配圆形、圆角矩形等启动器遮罩。
- 网页：Monitor favicon、SVG、180 / 192 / 512 px PNG；移动页面使用独立的 Monitor 图标，电脑主控继续使用 Center 图标。
- HarmonyOS：`AppScope/resources/base/media/app_icon.svg` 同步为 Monitor 母版；HAP 编译和真机验收另行完成。
