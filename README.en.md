# ExLab

[简体中文](README.md) · **English**

Mobile monitoring: `/mobile/` provides a shared phone UI with revocable read-only or limited-control credentials issued from Settings → Mobile access. An Android test APK is available; the native HarmonyOS 6 project is included as source and has not been compiled. Neither platform has completed device validation. See the [mobile build and acceptance guide](mobile/README.md).

Manage GPU experiments, computers and algorithm projects in one application window.
Choose an algorithm's original root folder, review discovered parameters, publish it and send experiments to your workers. Keep the original source unchanged.

**Desktop version: 0.5.10. Monitor APK: 0.5.2.** ExLab Center and ExLab Worker are separate applications. Install both on a computer that should manage experiments and contribute its GPU. See the [0.5.10 release notes](docs/RELEASE-0.5.10.md). Monitor restores pairing on launch, supports long-term revocable device access and includes an Android in-app updater. Desktop shortcuts use the ExLab names; see the [0.5.2 release](docs/RELEASE-0.5.2.md).

The algorithm library now supports search, deployment filters, bounded pagination and import timestamps. A five-step import opens node deployment when complete. Project dialogs provide experiment forms, allocation previews and matrix conversion, retaining inputs within the current page when switching presets or closing and reopening.

Experiment and matrix lists now have bounded scrolling, pagination, creation and completion timestamps, and searchable colored labels. Worker templates open as forms for editing parameters, previewing allocation, submitting experiments, or creating matrices.

Keep Center open and use **停用并释放资源 (Deactivate and release resources)** in Worker to request local experiments to save and stop, then release idle Docker and WSL resources. Other Ubuntu sessions require confirmation before closing; other WSL distributions remain available. The window reports whether resources were released, and the same button retries release after deactivation. Enable compute to resume. A separate preference controls automatic resource release when exiting the client.
Adds Docker startup prompts, deactivation and resource release, synchronized Centers with explicit handover, clipboard connections, editable login startup preferences and device names. The applications and release assets now use the ExLab brand. See the [release notes and multi-Center setup](docs/RELEASE-0.5.0.md).

## Download and install

Get application installers or complete ZIPs from the [latest release](https://github.com/Pencilfinely/exlab/releases/latest). GitHub's automatically generated Source code archives are not installers.

Existing desktop clients can upgrade to ExLab using their built-in updater. The original repository remains a migration download channel; upgraded clients automatically use the ExLab repository. Update Center and Worker separately.

| Computer | Recommended asset suffix | Application | Prerequisites |
|---|---|---|---|
| Windows 11 controller | windows-controller-x64-Setup.exe | **ExLab Center**, ExLabCenter.exe | Microsoft Edge; no WSL, Docker or separate Python installation |
| Windows 11 NVIDIA worker | windows-worker-x64-Setup.exe | **ExLab Worker**, ExLabWorker.exe | WSL2, Ubuntu 22.04+, Docker Desktop with integration enabled for that Ubuntu, NVIDIA Windows driver |
| Native Ubuntu 22.04+ NVIDIA server | ubuntu-worker-x64.zip | Background worker after installation | Docker Engine, NVIDIA Linux driver and NVIDIA Container Toolkit; normal-user access to Docker |

Windows installers install for the current user without administrator privileges. Firewall or system dependency changes may still require elevation.
Portable Windows ZIPs provide the same .exe applications: extract the whole archive before opening them. Current builds target x86-64 and NVIDIA GPUs.
First worker setup downloads large images; allow at least 8 GiB plus space for code, datasets and experiment outputs.

## First use: three steps

### 1. Open ExLab Center

Start **ExLab Center**. Its sidebar contains Overview, Experiments, Experiment Matrices, Compute, Algorithm Projects and Settings.
The controller service runs in the background; closing the page window does not stop it. Reopen it through the application or tray.
Local application launch signs you in automatically. You do not need to copy an administrator token or keep a terminal open.

The controller keeps data separately from application files. For an existing deployment, select its original data directory, such as **E:/ExperimentCenter**, to retain experiment history.
Do not use the replaceable application directory as your data directory.

### 2. Connect a worker

In **Compute**, add a computer, give it a unique name and select a controller address **that the worker can reach**. Download its pairing file.
Copy the worker installer/package and **NAME.pairing.json** to that computer.

- **Windows:** install and open ExLab Worker, choose the pairing file and Ubuntu distribution, then start setup. The application checks prerequisites, verifies GPUs, creates configuration and accepts work in the background. Progress and logs stay available in its window.
- **Ubuntu:** extract the worker ZIP and run this command as your normal user from that directory, replacing the pairing-file path:

    bash Install-Worker.sh --pairing /path/to/node.pairing.json

The command returns while first setup continues in the background. Check progress with **bash Worker-Status.sh**; view logs with **bash Client-Worker.sh logs**.
After setup, the computer appears online in the controller. Subsequent starts reuse its identity and configuration.

A pairing file is one worker's credential and never contains the administrator token. Use a distinct identity for each computer; reuse the original identity when reinstalling that same computer.
Computers must already be reachable over a LAN or trusted private network. Do not use localhost for a remote worker. Sharing a campus network does not guarantee reachability.
Workers initiate connections to the controller and need no inbound worker port.

### 3. Import and run an algorithm in the same window

1. Open **Algorithm Projects → 导入文件夹 (Import folder)**, choose the original root folder and discover its entry and parameters. Select the original main.py or another supported entry.
2. Review entry and parameters, data and environment, then metric collection. Add experiment presets and save a draft at any step.
3. Review the summary and selected files in **检查与发布 (Review and publish)**, then add the immutable snapshot to the library.
4. The deployment dialog opens immediately. Select workers and deploy; installation progress updates automatically while workers prepare code, data and the Docker environment.
5. Switch to **创建实验 (Create experiment)**, select a preset and adjust parameters, resources, allocation and tags. Preview and submit a short test, or convert it into a matrix. Follow progress, logs, metrics and result files in **Experiments**.

Experiment matrices combine datasets and parameter values: save the configuration, preview allocation, launch a batch, and export Markdown results. Allocation supports automatic selection, candidate/preferred workers, and a manually selected worker/GPU. Import forms include optional AI advice, and project removal cleans managed deployments while preserving original files. See the [workflow guide (Chinese)](docs/EXPERIMENT-MATRICES.zh-CN.md).

Local-folder import is available in the application on the controller computer. A remote browser can upload a prepared project ZIP; it cannot browse the controller's filesystem.

**The original algorithm is not rewritten.** The external harness calls its original entry in an isolated working copy, passes configuration and collects outputs.
Native resume can be configured when the original entry supports it. Otherwise resume stays unavailable; a saved model alone is not advertised as full training recovery.
Discovery is a draft: dynamic arguments, dependency versions and log meanings need review. Windows .bat entries do not run directly in Linux GPU containers; choose the Python/bash entry they actually invoke.

For a concrete example, see [Review an unchanged SASRec_Original import](docs/EXTERNAL-HARNESS.md). It uses **E:/PythonProjects/SASRec_Original/src/main.py** and one **Video_Games.test.txt** file, not the different implementation containing experiment.py.

## Distribution, outputs and background operation

Publish a project once and let selected workers receive it. Current distribution sends **immutable code/data snapshots through the controller**, with private Git snapshots and Docker environments prepared on each worker. Routine use requires no node-side Git or Docker commands.
**Direct GitHub/GitLab account integration and third-party registry publishing controls are not implemented.** Publish another version when source or data changes; create another experiment when only learning rate, seed or similar parameters change.

New tasks return one result JSON per experiment by default. Models, full logs and training history remain on the worker and can be requested individually. Sequential batches can publish each result before the job ends. Execution, result delivery, evidence delivery and independent acceptance are separate states. Existing queues require a reviewed, explicit migration; see [result protocol and migration](docs/LIGHTWEIGHT-RESULTS.zh-CN.md).
Task completion and file upload completion may occur at different times. Check pending uploads before shutting down.

The overview, experiment list and details show **cumulative runtime**. Timing starts with execution, freezes when it stops, and accumulates across resumed attempts; queueing, preparation and stopped periods are excluded. Workers persist timing, so closing the page or losing the controller connection does not reset it. Live values are estimates until confirmed by the worker. Details show submission, first start and latest stop times; CSV exports include timing fields. Update both Center and Worker for complete timing support. Old records remain unavailable, and uncertain history is marked incomplete.

The Windows worker can continue after its window closes, but still depends on the current user's WSL and Docker Desktop.
**Background operation does not mean training continues through sleep, logout or power-off.** Optional login startup is not a Windows service running without user login.
Native Ubuntu prefers user-level systemd when available and otherwise uses a detached process. Startup before login or persistence after logout depends on the machine's existing user-service/lingering settings; the installer does not silently change those system policies.

## Scheduling capabilities

| Mode | Current support |
|---|---|
| One experiment on one GPU | Supported; an experiment can request exclusive use within this manager |
| Separate experiments on separate GPUs | Supported when node concurrency and resource budgets allow |
| Several independent experiments on one GPU | Supported when tasks allow sharing, the GPU job limit allows it and budgets fit |
| One experiment across multiple GPUs or machines | Not implemented; requires algorithm and scheduler support |
| Automatically measure and choose the fastest allocation | Not implemented |

All GPUs in a shared machine may participate; another person's program using a GPU does not itself prevent admission. Each start checks current free VRAM/RAM, headroom, existing experiment reservations, CPU/RAM budgets, disk space and concurrency limits. Sharing may affect speed and does not impose a hard GPU memory limit. The manager does not silently change batch size, precision or learning rate to make a task fit.

Use **Compute → Resource settings** to edit node and GPU concurrency, CPU/RAM budgets and VRAM headroom. Updated workers receive saved changes after reconnecting, and the UI distinguishes pending from applied settings. Older workers need an upgrade. Lower limits affect future starts without stopping existing experiments. Fresh installations allow up to four experiments per enabled GPU and four times the enabled GPU count per node; conservative CPU/RAM defaults still apply and can be changed in the form. Upgrades preserve existing GPU choices and budgets.

Single experiments and matrices have their own resource and sharing controls. Exclusive use only excludes other experiments managed by this software from the same GPU; it does not lock out external programs. Since current telemetry cannot attribute memory to each managed process, admission also conservatively reserves existing experiments' declared budgets; the free VRAM shown by system tools alone does not guarantee immediate admission.
Workers can continue already assigned, cached tasks during a temporary controller outage and return records after reconnecting. An offline task is not silently duplicated onto another machine.

## Existing deployments and everyday use

**Upgrading from 0.3.0-rc.1 or earlier:** download and install this release manually once. Exit only the old management process and same-role client, retaining Docker experiments and pending transfers. If an old entry still requires an idle worker, use the new package's management handoff command; see the operations guide.

**From 0.3.0-rc.2 onward on Windows:** choose **Check for updates** in the application's status window or tray menu. Download the matching Center or Worker installer and verify its size and SHA-256. From 0.5.10, manual and automatic updates hand off only management processes; Docker experiments continue, and queues and transfers resume after restart. Data-directory selection, Ubuntu distribution, node configuration and startup preferences are retained.

Preview versions check for newer previews and stable releases; stable versions check for stable releases only. From 0.5.9, Ubuntu workers check, download and install verified GitHub packages with `bash Update-Worker.sh` in their original service directory. Use `--auto enable` to opt into hourly safe updates through a systemd user timer. Upgrade to a package containing this new entry once; later updates reuse the same command. Experiments, uploads, node identity, GPU policy and Docker endpoints are preserved. See [Ubuntu update instructions](docs/OPERATIONS.md).

0.5.8 adds optional compute startup for Worker and automatic updates for Center/Worker, both off by default. Updates can be queued once, retry automatically and wait for safe installation; closing the update view or restarting the client keeps the request. Successful installation cleans up its installer. See [queue and preference instructions](docs/OPERATIONS.md).

Never run old and new agents against the same node directory at once. See [Everyday operations](docs/OPERATIONS.md) for upgrading, backups, background controls and troubleshooting.
Use the application on localhost or a trusted private network; it is not a public multi-tenant service.

## Development and license

Controller logic uses Python's standard library; a source checkout needs Python 3.10+.
Release builds select explicit application files. Credentials, user algorithms and research datasets are excluded from public packages.

    python -m unittest discover -s tests -t . -v
    python scripts/build_release.py --download-python

[Protocol](CONTRACT.md) · [Issues](https://github.com/Pencilfinely/exlab/issues) · [Legacy SDK reference for developers](docs/ALGORITHM-INTEGRATION.md).

MIT — see [LICENSE](LICENSE). CPython and separately downloaded components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
