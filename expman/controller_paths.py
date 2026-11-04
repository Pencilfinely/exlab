"""Keep desktop, command-line and login launches on the selected workspace."""
from pathlib import Path
import os

from .common import read_json


def controller_settings_root():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'ExperimentManager' / 'desktop' / 'Controller'


def controller_settings():
    folder = controller_settings_root()
    settings = None
    error = None
    primary = False
    for name in ('settings.json', 'settings.json.bak'):
        try:
            value = read_json(folder / name)
            if value is None and not (folder / name).exists():
                continue
            if not isinstance(value, dict):
                raise ValueError('Center settings must be an object')
            settings = value
            primary = name == 'settings.json'
            break
        except ValueError as invalid:
            error = invalid
    binding = read_json(folder / 'controller-root.json', {})
    if not isinstance(binding, dict):
        raise ValueError('Center 数据目录记录损坏，请从桌面设置选择原数据目录')
    if settings is None:
        if error and not binding.get('data_root'):
            raise ValueError('Center 设置损坏，请恢复 settings.json.bak 或选择原数据目录') from error
        settings = {}
    if (not primary or 'data_root' not in settings) and binding.get('data_root'):
        if settings.get('data_root') != binding['data_root']:
            settings = {key: value for key, value in settings.items() if key not in ('workspace_id', 'hub_initialized')}
        settings = {**settings, **binding}
    return settings


def default_controller_root():
    settings = controller_settings()
    selected = settings.get('data_root')
    if selected is not None:
        if not isinstance(selected, str) or not selected.strip() or not Path(selected).is_absolute():
            raise ValueError('Center 数据目录设置无效，请选择原数据目录')
        return Path(selected)
    return controller_settings_root().parents[1] / 'controller'


def validate_controller_workspace(root):
    """A temporarily missing drive/file must never create a replacement identity."""
    binding = read_json(controller_settings_root() / 'controller-root.json')
    if binding is None:
        try:
            binding = controller_settings()
        except ValueError:
            return  # An explicit --root can repair a damaged desktop profile.
    if not isinstance(binding, dict):
        raise ValueError('Center 数据目录记录损坏，请从桌面设置选择原数据目录')
    selected = binding.get('data_root')
    if not selected or Path(selected).resolve() != Path(root).resolve() or not binding.get('hub_initialized'):
        return
    config = read_json(Path(root) / 'hub.json')
    if not isinstance(config, dict) or not (Path(root) / 'hub.sqlite3').is_file():
        raise ValueError('原 Center 数据目录不可用或缺少配置/数据库；请检查磁盘和路径，尚未创建空工作空间：' + str(root))
    expected = binding.get('workspace_id')
    if expected and config.get('workspace_id') != expected:
        raise ValueError('Center 工作空间身份与保存的配置不一致，请选择原数据目录：' + str(root))
