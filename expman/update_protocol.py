"""Compatibility with the cooperative stop implemented by the running backend."""

UPDATE_STOP_PROTOCOL = 1

# These published backends implement the protocol but predate its explicit marker.
# Unknown releases must advertise their capability instead of inheriting a guess.
_UNMARKED_RELEASES = frozenset(('0.3.0rc2', '0.3.0rc3', '0.3.0-rc.2', '0.3.0-rc.3'))


def supports_update_stop(state):
    if 'update_stop_protocol' in state:
        protocol = state['update_stop_protocol']
        return type(protocol) is int and protocol == UPDATE_STOP_PROTOCOL
    version = state.get('version')
    return isinstance(version, str) and version in _UNMARKED_RELEASES


def manual_stop_required(state, *, worker):
    version = state.get('version')
    version_text = version if isinstance(version, str) and version else '未知'
    role = '算力端' if worker else '主控'
    if worker or state.get('managed'):
        action = '请先完成实验和文件回传，再在客户端点击「停用并释放资源」，然后重试安装。若从终端启动，请在原终端按 Ctrl+C 正常退出。'
    else:
        action = '请在原主控启动终端按 Ctrl+C 正常退出，再重试安装。客户端可以保留开启，原数据与连接凭证会保留。'
    return dict(state, ready_for_update=False, manual_stop_required=True,
                backend_version=version,
                detail=f'后台{role}仍是 {version_text}，当前启动方式不支持自动停止更新。' + action)
