"""Release entry points. Controller and worker roles remain separate."""
import argparse
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

from .common import read_json


class InstanceLock:
    def __init__(self, path, *, timeout=0):
        self.path = Path(path)
        self.timeout = max(0, float(timeout))
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b'0')
                    self.stream.flush()
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stream.close()
                    self.stream = None
                    raise RuntimeError('This data directory is already in use / 此数据目录已有程序运行')
                time.sleep(min(0.01, remaining))

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


def default_controller_root():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'ExperimentManager' / 'controller'


def browser_url(root, port):
    token = read_json(Path(root) / 'hub.json')['admin_token']
    # Fragment stays in the local browser: it is never sent in HTTP requests/logs.
    return f'http://127.0.0.1:{port}/#token=' + urllib.parse.quote(token, safe='')


def reopen_controller(root, open_browser=True):
    settings = read_json(Path(root) / 'launcher.json', {})
    config = read_json(Path(root) / 'hub.json', {})
    port = settings.get('port')
    if not isinstance(port, int) or not config.get('admin_token'):
        return False
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/state',
        headers={'Authorization': 'Bearer ' + config['admin_token']})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            if response.status != 200:
                return False
    except OSError:
        return False
    if open_browser:
        webbrowser.open(browser_url(root, port))
    print(f'Controller already running: http://127.0.0.1:{port}', flush=True)
    return True


def controller(root, port=None, host='0.0.0.0', open_browser=True):
    root = Path(root).expanduser().resolve()
    if reopen_controller(root, open_browser):
        return
    # CLI and native clients must publish the same owner and handle the same
    # cooperative stop requests. A separate serve_forever silently bypassed
    # desktop deactivation and updates, including Start-Controller.cmd.
    from .desktop import controller_serve
    controller_serve(root, 8765 if port is None else port, host,
                     fixed_port=port is not None, open_browser=open_browser, foreground=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='role', required=True)
    center = sub.add_parser('controller')
    center.add_argument('--root', default=str(default_controller_root()))
    center.add_argument('--port', type=int)
    center.add_argument('--host', default='0.0.0.0')
    center.add_argument('--no-browser', action='store_true')
    worker = sub.add_parser('worker')
    worker.add_argument('--pairing')
    worker.add_argument('--root')
    worker.add_argument('--gpu', action='append')
    worker.add_argument('--prepare-only', action='store_true')
    worker.add_argument('--setup-network', choices=('bridge', 'host'),
                        help='Registry/build network during setup; host supports a dedicated Docker daemon without a bridge')
    worker.add_argument('--configure', action='store_true', help='Recheck GPUs and regenerate setup before starting')
    args = parser.parse_args()
    release = read_json(Path(__file__).resolve().parents[1] / 'release-role.json')
    if release:
        permitted = 'controller' if 'controller' in release['role'] else 'worker'
        if args.role != permitted:
            raise ValueError(f'This package contains the {permitted} edition. Download the other edition to use both roles.')
    if args.role == 'controller':
        controller(args.root, args.port, args.host, not args.no_browser)
    else:
        from .worker_setup import start
        start(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print('Cannot start / 无法启动: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
