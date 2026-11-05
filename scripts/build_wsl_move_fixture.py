"""Create a tiny disposable rootfs from read-only binaries in an existing WSL.

This writes only the requested tar in the repository's .test-runs directory.
It never changes the distribution used to build the fixture.
"""
import argparse
import io
from pathlib import Path
import re
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = Path(args.output).resolve()
    if not target.is_relative_to(root / '.test-runs') or target.exists():
        raise SystemExit('A new fixture tar under .test-runs is required')
    binaries = {'/bin/sh', '/bin/cat', '/usr/bin/id'}
    for binary in tuple(binaries):
        output = subprocess.check_output(['ldd', binary], text=True)
        binaries.update(re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)', output, re.MULTILINE))
    files = {path.lstrip('/'): Path(path).read_bytes() for path in binaries}
    files.update({'etc/passwd': b'root:x:0:0:root:/root:/bin/sh\nfixture:x:1000:1000:fixture:/home/fixture:/bin/sh\n',
                  'etc/group': b'root:x:0:\nfixture:x:1000:\n',
                  'etc/wsl.conf': b'[boot]\nsystemd=false\n[user]\ndefault=fixture\n[interop]\nenabled=false\nappendWindowsPath=false\n',
                  'etc/os-release': b'ID=exlab-fixture\nNAME=ExLabMoveFixture\n',
                  'fixture/preserved.txt': b'ExLab move fixture: outputs and identity survive.\n'})
    directories = {'proc', 'sys', 'dev', 'tmp', 'root', 'home', 'home/fixture', 'run'}
    for path in files:
        directories.update(p.as_posix() for p in Path(path).parents if p.as_posix() != '.')
    with tarfile.open(target, 'x:') as archive:
        for name in sorted(directories):
            item = tarfile.TarInfo(name);item.type = tarfile.DIRTYPE;item.mode = 0o755
            if name == 'home/fixture':
                item.uid = item.gid = 1000
            archive.addfile(item)
        for name, content in sorted(files.items()):
            item = tarfile.TarInfo(name);item.size = len(content);item.mode = 0o755 if name.startswith(('bin/', 'usr/', 'lib')) else 0o644
            archive.addfile(item, io.BytesIO(content))
    print('Built disposable rootfs:', target.name)


if __name__ == '__main__':
    main()
