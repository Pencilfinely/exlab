"""Observed compute transitions, separate from desired scheduling policy.

Releasing training resources leaves the small control agent and shared runtime
online. Fully stopping Docker/WSL belongs to the existing desktop lifecycle.
"""
from __future__ import annotations

import re
import subprocess
import sys

from . import common, compute_policy


def observe(agent, availability):
    from .agent import ACTIVE, TERMINAL, _pid_alive

    mode = availability.get("mode", "available")
    records = agent.records()
    active = [record for record in records if record["state"] in ACTIVE]
    preparing = agent.preparation is not None
    installing = agent.project_delivery.installing is not None
    receipt = {"mode": mode, "intent": compute_policy.control_intent(availability),
               "policy": {key: availability.get(key) for key in ("mode", "revision", "source", "until", "reason")},
               "observed_at": common.now(), "experiments_stopped": False, "resources_released": False,
               "active_jobs": len(active), "preparing": preparing, "project_operation": installing,
               "runtime_retained": True}

    def result(phase, detail):
        return dict(receipt, phase=phase, detail=detail[:1000])

    if mode == "available":
        return result("available", "允许接单；已停止的实验仍需显式续训或交接")
    if mode == "drain":
        return result("admission_paused", "已暂停接单；当前实验继续运行，资源保留")
    if active:
        overdue = any(record.get("stop_at") is not None and common.now() - record["stop_at"] >
                      float(agent.config.get("stop_grace_seconds", 60)) + 45 for record in active)
        return result("blocked" if overdue else "stopping",
                      "实验停止尚未确认：" + str(agent.last_error or "请检查实验进程和容器") if overdue else
                      f"正在保存并停止 {len(active)} 个实验；尚未确认资源释放")
    if preparing or installing:
        return result("releasing" if mode == "release" else "stopping",
                      "正在结束本机准备或项目操作；尚未确认停止和资源释放")
    if any(record["state"] not in TERMINAL for record in records):
        return result("stopping", "仍有未停止的本机任务")

    try:
        # Terminal task labels alone are insufficient: verify the execution
        # handles, including containers left behind by an older agent.
        for record in records:
            if record["spec"]["backend"] == "demo":
                process = agent.processes.get(record["id"])
                if (process is not None and process.poll() is None) or _pid_alive(record.get("worker_pid")):
                    return result("blocked", "实验进程仍存在，尚未确认资源释放")
                continue
            if not record.get("container_name"):
                if record.get("ever_started") and not record.get("execution_stopped"):
                    return result("blocked", "实验缺少容器句柄，无法确认训练进程已停止")
                continue
            container = agent._inspect(record)  # Exact name and ownership labels checked here.
            if container is None:
                continue
            state = container.get("State", {})
            if state.get("Running") or state.get("Restarting") or state.get("Paused"):
                return result("blocked", "仍检测到运行中的实验容器；保留状态以待核查")
            if state.get("Status") not in ("exited", "dead", "created"):
                return result("blocked", "实验容器状态未知，尚未确认资源释放")
            if mode == "release":
                # Never force-remove or act on a name that could have been reused.
                identity = container.get("Id", "")
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    return result("blocked", "无法确认实验容器身份，清理未完成")
                agent._exec(["docker", "rm", identity], timeout=30)
                if agent._inspect(record) is not None:
                    return result("blocked", "实验容器清理尚未确认")
                agent._save(record, container_name=None, gpu_uuid=None, execution_stopped=True)
        if (sys.platform == "linux" and not agent.config.get("allow_demo", False) or
                any(record["spec"]["backend"] == "docker" for record in records)):
            live = agent._exec(["docker", "ps", "--filter", "label=expman.node=" + agent.config["node_id"],
                                "--filter", "label=expman.job", "--format", "{{.ID}}"])
            if live.stdout.strip():
                return result("blocked", "仍有本节点实验容器运行，可能有旧记录之外的实验；尚未确认释放")
        receipt["experiments_stopped"] = True
        if mode == "stop":
            return result("stopped", "实验已确认停止，接单已暂停；保留任务归属和运行环境")
        receipt["resources_released"] = True
        return result("released", "训练进程已退出，实验容器已清理，训练资源已释放；控制代理及 Docker/WSL 保留")
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
        return result("blocked", "资源核验未完成：" + str(error))
