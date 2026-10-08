"""GitHub release updates for native Ubuntu workers, including optional user timers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

from . import __version__, common, worker_service as service
from .launcher import InstanceLock
from .worker_setup import private_write
from .worker_upgrade import _candidate, discover_configs

REPOSITORY = 'https://github.com/Pencilfinely/exlab'
RELEASES_API = 'https://api.github.com/repos/Pencilfinely/exlab/releases?per_page=100'
ROLE = 'ubuntu-worker-x64'
CHECK_INTERVAL = 3600
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
VERSION = re.compile(r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$')
HASH = re.compile(r'^[0-9a-fA-F]{64}$')


def version_key(value):
    value = re.sub(r'^(\d+\.\d+\.\d+)rc(\d+)$', r'\1-rc.\2', value.removeprefix('v'))
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError('无效的版本号: ' + value)
    prerelease = match[4]
    parts = []
    for part in prerelease.split('.') if prerelease else ():
        if part.isdecimal() and len(part) > 1 and part[0] == '0':
            raise ValueError('无效的预览版本号')
        parts.append((0, int(part)) if part.isdecimal() else (1, part))
    return tuple(int(match[i]) for i in (1, 2, 3)), prerelease is None, tuple(parts)


def same_version(left, right):
    try:
        return version_key(left) == version_key(right)
    except (ValueError, TypeError, AttributeError):
        return False


def _release_url(tag, name):
    return REPOSITORY + '/releases/download/' + urllib.parse.quote(tag, safe='') + '/' + urllib.parse.quote(name, safe='')


def select_release(releases, current):
    current_key = version_key(current)
    if not isinstance(releases, list):
        raise ValueError('GitHub 更新列表格式无效')
    selected = None
    for item in releases:
        if not isinstance(item, dict) or item.get('draft'):
            continue
        tag = item.get('tag_name', '')
        try:
            key = version_key(tag)
        except (ValueError, AttributeError, TypeError):
            continue
        if (current_key[1] and (not key[1] or item.get('prerelease'))) or key <= current_key:
            continue
        if selected and key <= version_key(selected['version']):
            continue
        version = tag.removeprefix('v')
        assets = item.get('assets', [])
        names = (f'ExLab-{version}-{ROLE}.zip', f'ExperimentManager-{version}-{ROLE}.zip')
        packages, checksums = [], []
        for asset in assets if isinstance(assets, list) else ():
            if not isinstance(asset, dict) or asset.get('state') != 'uploaded':
                continue
            if asset.get('name') in names:
                packages.append(asset)
            if asset.get('name') == 'SHA256SUMS.txt':
                checksums.append(asset)
        if not packages or not checksums:
            continue
        if len(packages) != 1 or len(checksums) != 1:
            raise ValueError('发布包含重复 Ubuntu 安装包或校验文件')
        package, checksum = packages[0], checksums[0]
        size = package.get('size')
        if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES:
            raise ValueError('Ubuntu 安装包大小无效')
        for asset in (package, checksum):
            if asset.get('browser_download_url') != _release_url(tag, asset['name']):
                raise ValueError('更新地址不属于本项目的 GitHub Release')
        selected = dict(version=version, tag=tag, name=package['name'], size=size,
                        url=package['browser_download_url'], checksum_url=checksum['browser_download_url'])
    return selected


def parse_checksum(text, name):
    values = []
    for line in text.lstrip('\ufeff').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        if match and match[2] == name:
            values.append(match[1].lower())
    if len(values) != 1:
        raise ValueError('Ubuntu 安装包缺少唯一的 SHA256 校验值')
    return values[0]


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        _allow_download_url(url)
        return super().redirect_request(request, response, code, message, headers, url)


def _allow_download_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in ('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')):
        raise ValueError('更新下载重定向到不受信任的地址')


def _open(url):
    if url != RELEASES_API:
        _allow_download_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'ExLab-Ubuntu-Updater', 'Accept': 'application/vnd.github+json'})
    return urllib.request.build_opener(_ReleaseRedirect()).open(request, timeout=30)


def _read(url, limit):
    with _open(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('GitHub 更新信息超过大小限制')
    return data.decode('utf-8-sig')


def check(current):
    release = select_release(json.loads(_read(RELEASES_API, 8 * 1024 * 1024)), current)
    if release:
        release['sha256'] = parse_checksum(_read(release['checksum_url'], 256 * 1024), release['name'])
    return release


def _validate_release(release):
    version_key(release['version'])
    if release['name'] not in (f'ExLab-{release["version"]}-{ROLE}.zip', f'ExperimentManager-{release["version"]}-{ROLE}.zip'):
        raise ValueError('更新安装包角色不匹配')
    if (release['url'] != _release_url(release['tag'], release['name'])
            or release['checksum_url'] != _release_url(release['tag'], 'SHA256SUMS.txt')
            or release['tag'].removeprefix('v') != release['version']
            or type(release['size']) is not int or not 0 < release['size'] <= MAX_PACKAGE_BYTES
            or not isinstance(release.get('sha256'), str) or not HASH.fullmatch(release['sha256'])):
        raise ValueError('更新安装包元数据无效')


def _valid_download(path, release):
    return (path.is_file() and not path.is_symlink() and path.stat().st_size == release['size']
            and common.sha256_file(path) == release['sha256'].lower())


def download(release, folder, progress=None):
    _validate_release(release)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / release['name']
    if _valid_download(target, release):
        return target
    partial = folder / ('.download-' + uuid.uuid4().hex)
    try:
        started = time.monotonic()
        total = 0
        digest = hashlib.sha256()
        with _open(release['url']) as response, partial.open('xb') as output:
            declared = response.headers.get('Content-Length')
            if declared is not None and int(declared) != release['size']:
                raise ValueError('下载大小与发布信息不一致')
            while True:
                if time.monotonic() - started > 20 * 60:
                    raise TimeoutError('Ubuntu 更新下载超时')
                block = response.read(128 * 1024)
                if not block:
                    break
                total += len(block)
                if total > release['size']:
                    raise ValueError('下载超过安装包声明大小')
                output.write(block)
                digest.update(block)
                if progress:
                    progress(total, release['size'])
            output.flush()
            os.fsync(output.fileno())
        if total != release['size'] or digest.hexdigest() != release['sha256'].lower():
            raise ValueError('Ubuntu 安装包大小或 SHA256 校验失败')
        os.replace(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def extract_package(archive_path, release, folder):
    """Verify the complete public manifest before any downloaded code can run."""
    _validate_release(release)
    if not _valid_download(Path(archive_path), release):
        raise ValueError('缓存安装包校验失败')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / release['sha256'].lower()
    staging = folder / ('.extract-' + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise ValueError('更新包解压大小超过限制')
            names = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (name in names or not name or name.endswith('/') or path.is_absolute() or '\\' in name
                        or ':' in name or any(part in ('', '.', '..') for part in name.split('/'))
                        or kind not in (0, stat.S_IFREG)):
                    raise ValueError('更新包含有不安全或重复的路径')
                names.add(name)
            if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 8 * 1024 * 1024:
                raise ValueError('更新包缺少有效文件清单')
            manifest = json.loads(archive.read('manifest.json'))
            if not isinstance(manifest, dict) or names != set(manifest) | {'manifest.json'}:
                raise ValueError('更新包文件与清单不一致')
            required = {'release-role.json', 'expman/__init__.py', 'expman/worker_service.py', 'expman/worker_update.py'}
            if not required <= names:
                raise ValueError('更新包缺少 Ubuntu worker 及更新程序')
            staging.mkdir(mode=0o700)
            for name in names:
                data = archive.read(name)
                if name != 'manifest.json':
                    expected = manifest[name]
                    if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int
                            or expected['bytes'] != len(data) or expected.get('sha256') != hashlib.sha256(data).hexdigest()):
                        raise ValueError('更新包文件校验失败: ' + name)
                output = staging / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
            marker = common.read_json(staging / 'release-role.json')
            if marker != {'role': ROLE, 'version': release['version']}:
                raise ValueError('更新包角色或版本不匹配')
        if target.exists():
            contents = list(target.rglob('*'))
            if (target.is_symlink() or any(path.is_symlink() for path in contents)
                    or {path.relative_to(target).as_posix() for path in contents if path.is_file()} != names
                    or any((target / name).is_symlink() or not (target / name).is_file()
                    or (target / name).read_bytes() != (staging / name).read_bytes() for name in names)):
                raise ValueError('已解压的更新代码被修改')
        else:
            staging.rename(target)
        return target
    finally:
        # Only this newly created, workspace-local staging directory is removed.
        if staging.exists() and staging.parent.resolve() == folder.resolve() and staging.name.startswith('.extract-'):
            shutil.rmtree(staging)


def _state(root):
    return common.read_json(root / 'updates' / 'state.json', {})


def _save(root, **changes):
    state = _state(root)
    state.update(changes, updated_at=time.time())
    private_write(root / 'updates' / 'state.json', state)
    return public_status(root)


def public_status(root):
    state = _state(root)
    result = {key: state[key] for key in ('phase', 'detail', 'current_version', 'target_version', 'checked_at',
              'next_check_at', 'retry_at', 'downloaded_bytes', 'total_bytes', 'manual_stop_required') if key in state}
    result.update(auto_update=common.read_json(root / 'updates' / 'settings.json', {}).get('enabled') is True,
                  service_root=str(root), installed_version=service._settings(root).get('version'))
    return result


def write_launcher(root, package, python):
    package = Path(package)
    if not (package / 'expman' / 'worker_update.py').is_file():
        # Bootstrap/rollback may keep an older backend that predates this updater.
        package = Path(__file__).resolve().parents[1]
    launch = root / 'Update-Worker.sh'
    if launch.is_symlink():
        raise ValueError('更新入口不能是目录链接')
    text = ('#!/usr/bin/env sh\nexport PYTHONPATH=' + shlex.quote(str(package)) +
            '\nexec ' + shlex.quote(str(python)) + ' -B -u -m expman.worker_update --service-root ' +
            shlex.quote(str(root)) + ' "$@"\n')
    temporary = root / ('.update-launcher-' + uuid.uuid4().hex)
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.chmod(0o700)
        os.replace(temporary, launch)
    finally:
        temporary.unlink(missing_ok=True)
    return launch


def _register(root, config=None):
    settings = service._settings(root)
    if settings.get('config'):
        if config and Path(config).expanduser().resolve() != Path(settings['config']).resolve():
            raise ValueError('该服务目录属于另一节点；请为不同 worker 指定不同 --service-root')
        return settings
    if not config:
        candidates = discover_configs()
        if len(candidates) != 1:
            raise ValueError('请用 --config 指定原节点配置；不会新建节点身份')
        config = candidates[0]['path']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    selected = service.select_configuration(root, config=config)
    if not selected.get('config'):
        raise ValueError(selected.get('detail', '无法复用原节点配置'))
    return selected


def _require_installed(settings):
    if not settings.get('version') or not settings.get('package_dir'):
        raise RuntimeError('原节点尚未安装后台更新入口。请先正常退出旧终端代理，在新版解压目录用 '
                           'bash Install-Worker.sh --service-root 原服务目录 --config 原node.ready.json 安装一次；'
                           '之后使用原服务目录中的 Update-Worker.sh。节点配置和实验数据保留。')


def _invoke(package, root, action, *options):
    reply = subprocess.run([sys.executable, '-B', '-m', 'expman.worker_service', action, '--service-root', str(root), *options],
        cwd=package, env=dict(os.environ, PYTHONPATH=str(package), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    result = json.loads(reply.stdout)
    if reply.returncode or not isinstance(result, dict):
        raise RuntimeError(result.get('detail', '新版 worker 操作失败') if isinstance(result, dict) else '新版 worker 未返回有效状态')
    return result


def _wait_stopped(root):
    deadline = time.monotonic() + 35
    while True:
        state = service.status(root)
        if state.get('running') is False and state.get('status') in ('stopped', 'failed'):
            return
        if time.monotonic() >= deadline or state.get('status') in ('failed', 'exit_failed', 'external_running'):
            raise RuntimeError(state.get('detail', '代理尚未安全停止'))
        time.sleep(0.25)


def _restart_and_verify(package, root, release):
    _invoke(package, root, 'start')
    deadline = time.monotonic() + 35
    while True:
        result = _invoke(package, root, 'status')
        if result.get('running') is True and same_version(result.get('version'), release['version']):
            return
        if result.get('status') in ('failed', 'exit_failed') or time.monotonic() >= deadline:
            raise RuntimeError('新版 worker 启动未确认: ' + result.get('detail', '请查看 worker.log'))
        time.sleep(0.25)


def _restore(root, before, resume, target_version):
    """Restore application settings only; never touch experiment databases or containers."""
    with InstanceLock(root / 'control.lock'):
        current = service.status(root)
        from .desktop import _lock_is_held
        if (current['running'] or current.get('status') in ('starting', 'preparing', 'stopping', 'shutting_down')
                or _lock_is_held(root / 'supervisor.lock')):
            return ' 后台仍在运行，保留当前代码和数据。'
        settings = service._settings(root)
        if (settings.get('config') != before.get('config') or settings.get('node_id') != before.get('node_id')
                or (settings.get('package_dir') != before.get('package_dir') and not same_version(settings.get('version'), target_version))):
            return ' 后台安装位置已变化，保留当前代码和数据。'
        service._save_settings(root, before)
        service._write_status(root, status='stopped', pid=None, process_identity=None, online=False, stop_requested=True)
        if before.get('backend') == 'systemd':
            unit_text = _state(root).get('before_unit')
            if not isinstance(unit_text, str):
                raise RuntimeError('原后台配置已恢复，但缺少 systemd 单元备份；请保留停止状态并查看更新日志')
            unit = Path.home() / '.config/systemd/user' / service._unit_name(root)
            if unit.is_symlink():
                raise ValueError('原 systemd 单元位置已变化')
            temporary = unit.with_name('.restore-unit-' + uuid.uuid4().hex)
            try:
                temporary.write_text(unit_text, encoding='utf-8')
                os.replace(temporary, unit)
            finally:
                temporary.unlink(missing_ok=True)
            reply = subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, text=True, timeout=15)
            if reply.returncode:
                raise RuntimeError('原后台代码和单元已恢复，但 systemd 重载失败: ' + reply.stderr[-300:])
        write_launcher(root, before.get('package_dir') or Path(__file__).resolve().parents[1], before.get('python') or sys.executable)
    if resume and not before.get('deactivated'):
        restored = service.start(root)
        if restored.get('running') is not True and restored.get('status') not in ('starting', 'preparing'):
            raise RuntimeError('原后台代码已恢复，但启动失败: ' + restored.get('detail', '请查看 worker.log'))
        return ' 已恢复原后台启动；节点配置和实验数据保留。'
    return ' 已恢复原后台代码，保持停止；节点配置和实验数据保留。'


def _apply(root, package, release):
    state = _state(root)
    settings = service._settings(root)
    before = state.get('before_settings')
    # Complete a durable handoff interrupted after installation but before restart.
    if before and same_version(settings.get('version'), release['version']):
        if settings.get('config') != before.get('config') or settings.get('node_id') != before.get('node_id'):
            raise ValueError('安装后的原节点身份不匹配')
        try:
            if state.get('resume_service') and not settings.get('deactivated'):
                _restart_and_verify(package, root, release)
        except Exception as error:
            raise RuntimeError(str(error) + _restore(root, before, state.get('resume_service'), release['version'])) from error
        return _save(root, phase='completed', release=None, before_settings=None, before_unit=None, attempts=0, retry_at=0, current_version=release['version'],
                     detail='Ubuntu worker 已更新；节点配置、实验数据和原运行选择保留')
    if settings.get('version') and version_key(settings['version']) >= version_key(release['version']):
        # A separate manual install can supersede an update that was waiting.
        return _save(root, phase='completed', release=None, before_settings=None, before_unit=None,
                     attempts=0, retry_at=0, manual_stop_required=False, current_version=settings['version'],
                     detail='已安装相同或更新版本；原更新队列已完成，保留当前后台')
    ready = service.update_status(root)
    if ready.get('ready_for_update') is not True:
        return _save(root, phase='waiting', detail=ready.get('detail', '等待实验与回传完成'),
                     manual_stop_required=ready.get('manual_stop_required', False) or ready.get('status') == 'external_running')
    if common.read_json(root / 'updates' / 'settings.json', {}).get('enabled') is not True and state.get('automatic'):
        return _save(root, phase='paused', detail='自动更新已关闭；已下载安装包保留')
    before = state.get('before_settings') or dict(settings)
    resume = (bool(ready.get('running')) or bool(state.get('before_settings') and state.get('resume_service'))) and not settings.get('deactivated')
    before_unit = state.get('before_unit')
    if before.get('backend') == 'systemd' and before_unit is None:
        unit = Path.home() / '.config/systemd/user' / service._unit_name(root)
        if unit.is_symlink() or not unit.is_file():
            raise RuntimeError('无法备份原 systemd 用户服务，尚未停止代理')
        before_unit = unit.read_text(encoding='utf-8')
    _save(root, phase='stopping', before_settings=before, before_unit=before_unit, resume_service=resume, manual_stop_required=False,
          detail='正在等待代理在空闲边界停止')
    stopped = service.stop_for_update(root)
    if stopped.get('ready_for_update') is not True:
        return _save(root, phase='waiting', detail=stopped.get('detail', '安全停止未完成'))
    try:
        _wait_stopped(root)
        final = service.update_status(root)
        if final.get('ready_for_update') is not True or service.install_status(root).get('ready_for_install') is not True:
            raise RuntimeError('代理停止后的安装检查未通过')
        _save(root, phase='installing', detail='正在切换 Ubuntu worker 后台代码')
        result = _invoke(package, root, 'install', '--no-start', '--backend', before.get('backend', 'detached'),
                         '--config', before['config'])
        if (result.get('running') is not False or result.get('status') != 'stopped'
                or not same_version(result.get('installed_version'), release['version']) or result.get('config') != before['config']
                or result.get('node_id') != before['node_id']):
            raise RuntimeError('新版后台安装结果未确认')
        if resume:
            _save(root, phase='restarting', detail='正在恢复更新前运行的代理')
            _restart_and_verify(package, root, release)
    except Exception as error:
        raise RuntimeError(str(error) + _restore(root, before, resume, release['version'])) from error
    return _save(root, phase='completed', release=None, before_settings=None, before_unit=None, attempts=0, retry_at=0, current_version=release['version'],
                 detail='Ubuntu worker 已更新；节点配置、实验数据和原运行选择保留')


def update_once(root, *, config=None, automatic=False):
    root = service._root(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with InstanceLock(root / 'update.lock'):
        if automatic and common.read_json(root / 'updates' / 'settings.json', {}).get('enabled') is not True:
            return public_status(root)
        try:
            settings = _register(root, config)
            _require_installed(settings)
            state = _state(root)
            now = time.time()
            if automatic and state.get('retry_at', 0) > now:
                return public_status(root)
            release = state.get('release')
            if not release:
                if automatic and state.get('next_check_at', 0) > now:
                    return public_status(root)
                current = settings.get('version') or __version__
                _save(root, phase='checking', current_version=current, automatic=automatic, detail='正在检查 GitHub Ubuntu 发布')
                release = check(current)
                _save(root, checked_at=now, next_check_at=now + CHECK_INTERVAL, retry_at=0, manual_stop_required=False)
                if not release:
                    return _save(root, phase='up_to_date', target_version=None, attempts=0, detail='当前已是此渠道最新 Ubuntu worker')
                _save(root, release=release, target_version=release['version'], before_settings=None, before_unit=None,
                      phase='downloading', downloaded_bytes=0, total_bytes=release['size'])
            else:
                _save(root, automatic=automatic)
            last = [0.0]
            def progress(done, total):
                if done == total or time.monotonic() - last[0] >= 1:
                    _save(root, phase='downloading', downloaded_bytes=done, total_bytes=total, detail='正在下载 Ubuntu 更新')
                    last[0] = time.monotonic()
            archive = download(release, root / 'updates' / 'downloads', progress)
            package = extract_package(archive, release, root / 'updates' / 'releases')
            return _apply(root, package, release)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            attempts = _state(root).get('attempts', 0) + 1
            return _save(root, phase='failed', attempts=attempts, retry_at=time.time() + min(3600, 60 * 2 ** min(attempts - 1, 6)),
                         detail=service._redact(root, str(error))[:1000])


def configure_auto(root, enabled, config=None):
    service._require_linux()
    root = service._root(root)
    preferences = root / 'updates' / 'settings.json'
    timer_name = service._unit_name(root).removesuffix('.service') + '-update.timer'
    unit_dir = Path.home() / '.config' / 'systemd' / 'user'
    if not enabled:
        private_write(preferences, {'enabled': False})
        if (unit_dir / timer_name).is_file():
            reply = subprocess.run(['systemctl', '--user', 'disable', '--now', timer_name], capture_output=True, text=True, timeout=15)
            if reply.returncode:
                raise RuntimeError('自动更新已关闭，但定时器停用失败: ' + reply.stderr[-300:])
        return public_status(root)
    if not service._systemd_available():
        raise RuntimeError('自动更新需要可用的 systemd 用户会话；一条命令手动更新仍可使用')
    settings = _register(root, config)
    _require_installed(settings)
    launch = write_launcher(root, settings.get('package_dir') or Path(__file__).resolve().parents[1],
                            settings.get('python') or sys.executable)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / timer_name.replace('.timer', '.service')
    unit.write_text('[Unit]\nDescription=ExLab Ubuntu worker release updater\n\n[Service]\nType=oneshot\n'
        'ExecStart=/bin/bash ' + service._quoted_unit(launch) +
        ' --automatic\nKillMode=process\nTimeoutStartSec=25min\n', encoding='utf-8')
    (unit_dir / timer_name).write_text('[Unit]\nDescription=Check ExLab worker updates and resume waiting installations\n\n'
        '[Timer]\nOnActiveSec=30s\nOnUnitInactiveSec=60s\nAccuracySec=5s\n\n[Install]\nWantedBy=timers.target\n', encoding='utf-8')
    prior = common.read_json(preferences, {'enabled': False})
    private_write(preferences, {'enabled': True})
    try:
        for command in (['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', '--now', timer_name]):
            reply = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if reply.returncode:
                raise RuntimeError('自动更新定时器启用失败: ' + reply.stderr[-300:])
    except Exception:
        private_write(preferences, prior)
        raise
    return public_status(root)


def main(argv=None):
    # Windows' legacy Update-Worker.cmd still uses this shell entry in WSL.
    if service._release_role().get('role') == 'windows-worker-x64':
        from .worker_upgrade import main as legacy
        return legacy(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Check GitHub without downloading or installing')
    mode.add_argument('--status', action='store_true', help='Show the saved update and automatic-update state')
    mode.add_argument('--auto', choices=('enable', 'disable', 'status'), help='Opt in/out of hourly release checks and safe updates')
    mode.add_argument('--automatic', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--run-existing', action='store_true', help='Use the legacy foreground launcher without downloading')
    parser.add_argument('--config', help='Original node configuration; no new identity or GPU setup')
    parser.add_argument('--service-root', help='Original worker lifecycle directory; use one directory per node')
    args = parser.parse_args(argv)
    root = service._root(args.service_root)
    try:
        if args.status or args.auto == 'status':
            result = public_status(root)
        elif args.check:
            current = service._settings(root).get('version') or __version__
            result = dict(current_version=current, release=check(current))
        elif args.run_existing:
            from .worker_upgrade import main as legacy
            return legacy(['--config', args.config] if args.config else [])
        else:
            service._require_linux()
            if service._release_role().get('role') not in (None, ROLE) or platform.machine().lower() not in ('x86_64', 'amd64'):
                raise RuntimeError('此更新入口仅安装 Ubuntu x64 worker')
            if args.auto:
                result = configure_auto(root, args.auto == 'enable', args.config)
            else:
                previous = None
                while True:
                    result = update_once(root, config=args.config, automatic=args.automatic)
                    if result.get('detail') != previous:
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        previous = result.get('detail')
                    if args.automatic or result.get('phase') != 'waiting' or result.get('manual_stop_required'):
                        return 1 if result.get('phase') == 'failed' or result.get('manual_stop_required') else 0
                    time.sleep(15)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except KeyboardInterrupt:
        print('更新等待已停止，已下载文件和实验数据保留；再次运行同一命令可继续。', flush=True)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'phase': 'failed', 'detail': service._redact(root, str(error))[:1000]}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
