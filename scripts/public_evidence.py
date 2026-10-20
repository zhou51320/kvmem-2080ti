"""Export evidence without physical device identities or private home paths."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
GPU = re.compile(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
HOME = re.compile(r'''(?i)(?:[A-Z]:[\\/]+Users[\\/]+|/(?:home|Users)/)([^\\/\r\n"'<>`]+(?=[\\/])|[^\\/\s"'<>`]+)''')
WORK = re.compile(r'(?i)\b[A-Z]:[\\/]+Codex_work(?=[\\/]|\b)')
SECRET = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{25,}|'
                    r'hf_[A-Za-z0-9]{24,}|AKIA[A-Z0-9]{16}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|'
                    r'xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{30,})\b|'
                    r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')
GENERIC_USERS = {'user', 'username', 'example', 'runner', 'your_username'}
SENSITIVE_KEYS = {'apikey', 'accesstoken', 'authtoken', 'authorization', 'password', 'secret', 'clientsecret', 'token'}
SECRET_FLAGS = {'--api-key', '--api-key-file', '--password', '--token', '--access-token'}


def synthetic_uuid(value: str) -> bool:
    return value.startswith('GPU-00000000-0000-0000-0000-')


def violations(text: str, patch: bool = False) -> list[tuple[int, str]]:
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        if patch and (not line.startswith('+') or line.startswith('+++')):
            continue  # Unchanged/removed upstream context is not our added source.
        if any(not synthetic_uuid(m.group()) for m in GPU.finditer(line)):
            result.append((number, 'physical GPU UUID'))
        if any(m.group(1).lower() not in GENERIC_USERS for m in HOME.finditer(line)) or WORK.search(line):
            result.append((number, 'private home/workspace path'))
        if SECRET.search(line):
            result.append((number, 'credential-like value'))
    return result


class Sanitizer:
    def __init__(self, aliases: dict[str, str] | None = None):
        self.aliases = dict(aliases or {})

    def text(self, text: str) -> str:
        def device(match):
            uuid = match.group()
            if synthetic_uuid(uuid):
                return uuid
            if uuid not in self.aliases:
                self.aliases[uuid] = 'GPU-' + (chr(65 + len(self.aliases)) if len(self.aliases) < 26 else str(len(self.aliases) + 1))
            return self.aliases[uuid]
        text = GPU.sub(device, text)
        # Source/build identities and measurements remain intact. These prefixes
        # describe locations only; the original evidence is kept separately.
        text = re.sub(r'''(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\r\n"'<>`]+[\\/]+AppData[\\/]+Local[\\/]+KVMem''',
                      lambda _: '$ARTIFACTS', text)
        text = re.sub(r'''(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\r\n"'<>`]+[\\/]+\.cache[\\/]+codex-runtimes''',
                      lambda _: '$RUNTIME', text)
        text = re.sub(r'''/(?:home|Users)/[^/\s"'<>`]+/kvmem_qw3/\.cu13-env''', lambda _: '$CUDA_ROOT', text)
        text = re.sub(r'''/(?:home|Users)/[^/\s"'<>`]+/kvmem_llamacpp''', lambda _: '$REPO', text)
        text = re.sub(r'''/(?:home|Users)/[^/\s"'<>`]+/kvmem_qw3''', lambda _: '$REFERENCE', text)
        text = HOME.sub(lambda m: m.group() if m.group(1).lower() in GENERIC_USERS else '$HOME', text)
        return WORK.sub(lambda _: '$ARTIFACTS', text)

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            result, secret_next = [], False
            for item in value:
                if secret_next:
                    result.append('<redacted>')
                    secret_next = False
                elif isinstance(item, str) and item.split('=', 1)[0] in SECRET_FLAGS:
                    flag, equal, _ = item.partition('=')
                    result.append(flag + '=<redacted>' if equal else flag)
                    secret_next = not equal
                else:
                    result.append(self.value(item))
            return result
        if isinstance(value, dict):
            return {key: '<redacted>' if isinstance(item, str) and
                    re.sub('[-_]', '', key.lower()) in SENSITIVE_KEYS else self.value(item)
                    for key, item in value.items()}
        return value


def public_json(value: dict, raw: bytes, sanitizer: Sanitizer | None = None) -> dict:
    result = (sanitizer or Sanitizer()).value(value)
    result['_sanitization'] = {
        'version': 1, 'original_sha256': hashlib.sha256(raw).hexdigest(),
        'scope': 'Device identities/private paths anonymized, credential fields redacted; measurements unchanged.'}
    return result


def write_report(path: Path, value: dict, private_root: Path | None = None) -> None:
    """Keep the original locally and write a public JSON copy to the requested path."""
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    private_root = private_root or ROOT / 'private-evidence'
    private_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    backup = private_root / (digest + '.json')
    if path.resolve() == backup.resolve():
        raise ValueError('Public output must differ from its private original')
    if not backup.exists():
        with backup.open('xb') as file:
            file.write(raw)
    elif backup.read_bytes() != raw:
        raise ValueError('Existing private evidence does not match its recorded hash')
    exported = json.dumps(public_json(value, raw), ensure_ascii=False, indent=2) + '\n'
    if violations(exported):
        raise ValueError('Public report still contains a private identity or credential-like value')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(exported, encoding='utf-8')


def check_tree(root: Path) -> list[str]:
    listed = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=root, check=True, capture_output=True).stdout
    failures = []
    for name in sorted(set(p.decode('utf-8') for p in listed.split(b'\0') if p)):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            continue
        for line, category in violations(text, name.endswith('.patch')):
            failures.append(f'{name}:{line}: {category}')
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        try:
            failures = check_tree(ROOT)
        except subprocess.CalledProcessError:
            parser.error('Cannot read Git inventory; use a Git executable that supports this checkout')
        for failure in failures:
            print(failure)  # Locations only: never repeat a credential or private value.
        raise SystemExit(bool(failures))
    if not args.input or not args.output:
        parser.error('Export requires INPUT and --output PATH')
    if args.input.resolve() == args.output.resolve():
        parser.error('Use a separate output path so the original evidence is retained')
    raw = args.input.read_bytes()
    text = raw.decode('utf-8-sig')
    if args.input.suffix.lower() == '.json':
        value = json.loads(text)
        if not isinstance(value, dict):
            parser.error('A JSON report must be an object')
        result = json.dumps(public_json(value, raw), ensure_ascii=False, indent=2) + '\n'
    else:
        result = '# Paths and device identities anonymized; measurements unchanged.\n' + Sanitizer().text(text)
    if violations(result):
        parser.error('Export still contains a private path, device identity or credential-like value')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding='utf-8')


if __name__ == '__main__':
    main()
