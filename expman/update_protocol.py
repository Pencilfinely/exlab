"""Compatibility with the cooperative stop implemented by the running backend."""

UPDATE_STOP_PROTOCOL = 2

# These published backends implement the protocol but predate its explicit marker.
# Unknown releases must advertise their capability instead of inheriting a guess.
_UNMARKED_RELEASES = frozenset(('0.3.0rc2', '0.3.0rc3', '0.3.0-rc.2', '0.3.0-rc.3'))


def supports_update_stop(state):
    if 'update_stop_protocol' in state:
        protocol = state['update_stop_protocol']
        return type(protocol) is int and protocol in (1, UPDATE_STOP_PROTOCOL)
    version = state.get('version')
    return isinstance(version, str) and version in _UNMARKED_RELEASES


def preserves_worker_experiments(state):
    """Recognize the handoff protocol or published agents with process-only stop.

    Protocol 1 only cooperatively stops when idle. These released supervisors
    also implement identity-checked SIGTERM and close without stopping Docker;
    use that existing operation for the first migration to protocol 2.
    """
    protocol = state.get('update_stop_protocol')
    if type(protocol) is int and protocol == UPDATE_STOP_PROTOCOL:
        return True
    legacy = {f'0.4.{minor}' for minor in range(6)} | {f'0.5.{minor}' for minor in range(10)}
    return type(protocol) is int and protocol == 1 and state.get('version') in legacy


def manual_stop_required(state, *, worker):
    version = state.get('version')
    version_text = version if isinstance(version, str) and version else '未知'
    role = '算力端' if worker else '主控'
    if worker:
        action = '请只退出原管理代理，再重试安装；Docker 实验和待回传数据保留。若从终端启动，请在原终端按 Ctrl+C 正常退出。不要使用「停用并释放资源」来交接更新。'
    elif state.get('managed'):
        action = '请停止原主控管理服务并退出旧客户端，再重试安装；远端实验、数据与连接凭证会保留。'
    else:
        action = '请在原主控启动终端按 Ctrl+C 正常退出，再重试安装。客户端可以保留开启，原数据与连接凭证会保留。'
    return dict(state, ready_for_update=False, manual_stop_required=True,
                backend_version=version,
                detail=f'后台{role}仍是 {version_text}，当前启动方式不支持自动停止更新。' + action)
