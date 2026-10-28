"""Install a publisher-hash-verified psutil wheel into an offline Python runtime.

Build-time utility only; never downloads or installs dependencies at UI startup.
"""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import zipfile


def install(wheel, expected, runtime):
    wheel, runtime = Path(wheel), Path(runtime)
    if not re.fullmatch(r'psutil-7\.2\.2-cp3[0-9]+-abi3-win_amd64\.whl', wheel.name):
        raise ValueError('Supply the psutil 7.2.2 CPython ABI3 Windows x64 wheel')
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected.lower():
        raise ValueError('psutil wheel SHA-256 mismatch')
    target = runtime / 'Lib/site-packages'
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        for entry in entries:
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in entry.filename
                    or ':' in entry.filename or not name.parts
                    or name.parts[0] not in ('psutil', 'psutil-7.2.2.dist-info')):
                raise ValueError('Unexpected wheel path: ' + entry.filename)
            if (target / name).exists():
                raise ValueError('Refusing to replace dependency files')
        target.mkdir(parents=True, exist_ok=True)
        archive.extractall(target)
    # Official embedded Python omits site-packages. Keep isolated imports while
    # making this bundled dependency visible, including under -I / -S.
    for path in runtime.glob('python*._pth'):
        content = path.read_text(encoding='utf-8')
        if 'Lib/site-packages' not in content.splitlines():
            path.write_text(content.rstrip() + '\nLib/site-packages\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    install(args.wheel, args.sha256, args.runtime)
