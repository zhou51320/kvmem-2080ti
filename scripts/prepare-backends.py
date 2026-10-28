#!/usr/bin/env python3
"""Initialize pinned backend submodules and apply the repository's integration patches."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def git(directory, *args, check=True, env=None):
    return subprocess.run(['git', '-c', 'core.longpaths=true', '-c', 'core.autocrlf=false',
                           '-C', str(directory), *args], check=check,
                          capture_output=True, text=True, env=env)


@contextmanager
def temporary_index():
    handle, filename = tempfile.mkstemp(prefix='kvmem-backend-', suffix='.index')
    os.close(handle)
    os.unlink(filename)
    env = dict(os.environ, GIT_INDEX_FILE=filename)
    try:
        yield env
    finally:
        if Path(filename).exists():
            os.unlink(filename)


def source_tree(directory):
    # Include patch-added files without changing the developer's index.
    with temporary_index() as env:
        git(directory, 'read-tree', 'HEAD', env=env)
        git(directory, 'add', '-A', env=env)
        return git(directory, 'write-tree', env=env).stdout.strip()


def patch_series(name, entry):
    patches = entry.get('patches')
    if not isinstance(patches, list) or not patches:
        raise RuntimeError(f'{name}: expected a nonempty ordered patches list')
    seen = set()
    paths = set()
    for item in patches:
        identity = item['id']
        if not isinstance(identity, str) or not identity or identity in seen:
            raise RuntimeError(f'{name}: duplicate or invalid patch id: {identity}')
        dependencies = item.get('requires', [])
        if not isinstance(dependencies, list) or any(dep not in seen for dep in dependencies):
            raise RuntimeError(f'{name}/{identity}: dependencies must precede the patch')
        relative = Path(item['path'])
        patch = (ROOT / relative).resolve()
        if relative.is_absolute() or not patch.is_relative_to(ROOT.resolve()) or patch in paths:
            raise RuntimeError(f'{name}/{identity}: invalid or duplicate patch path')
        if hashlib.sha256(patch.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError(f'{name}/{identity}: patch hash differs from versions.json')
        if not isinstance(item['after_tree'], str) or len(item['after_tree']) != 40:
            raise RuntimeError(f'{name}/{identity}: invalid prepared tree hash')
        seen.add(identity)
        paths.add(patch)
    if patches[-1]['after_tree'] != entry['patched_tree']:
        raise RuntimeError(f'{name}: final patch tree differs from patched_tree')
    return patches


def verify_patch_series(directory, entry, patches):
    # Preflight the entire series in an isolated index before changing any source.
    with temporary_index() as env:
        git(directory, 'read-tree', entry['base_commit'], env=env)
        for item in patches:
            git(directory, 'apply', '--cached', '--whitespace=nowarn',
                str(ROOT / item['path']), env=env)
            actual = git(directory, 'write-tree', env=env).stdout.strip()
            if actual != item['after_tree']:
                raise RuntimeError(f'{item["id"]}: patch produces {actual}, '
                                   f'expected {item["after_tree"]}')


def prepare(name, entry, check_only):
    directory = ROOT / entry['path']
    patches = patch_series(name, entry)
    # Exported source bundles contain prepared sources and a per-file manifest, without Git metadata.
    if not (ROOT / '.git').exists():
        manifest = json.loads((ROOT / 'SOURCE-MANIFEST.json').read_text(encoding='utf-8'))
        if manifest['backends'][name] != entry:
            raise RuntimeError(f'{name}: source bundle backend version differs from versions.json')
        files = {path: value for path, value in manifest['files'].items()
                 if path.startswith(entry['path'] + '/')}
        if not files:
            raise RuntimeError(f'{name}: source bundle has no backend files')
        for path, expected in files.items():
            source = (ROOT / path).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise RuntimeError(f'{name}: invalid source manifest path: {path}')
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'{name}: source bundle file differs from manifest: {path}')
        print(f'{name}: verified prepared source bundle', flush=True)
        return
    if not check_only and not (directory / '.git').exists():
        git(ROOT, 'submodule', 'update', '--init', '--recursive', '--depth', '1', '--', entry['path'])
    if not (directory / '.git').exists():
        raise RuntimeError(f'{name}: run scripts/prepare-backends.py --backend {name}')
    head = git(directory, 'rev-parse', 'HEAD').stdout.strip()
    if head != entry['base_commit']:
        raise RuntimeError(f'{name}: expected {entry["base_commit"]}, got {head}')
    verify_patch_series(directory, entry, patches)
    current = source_tree(directory)
    states = [git(directory, 'rev-parse', 'HEAD^{tree}').stdout.strip()]
    states.extend(item['after_tree'] for item in patches)
    if current not in states:
        raise RuntimeError(f'{name}: source has changes outside the recorded patch series; '
                           'preserving the working directory')
    if current != entry['patched_tree']:
        if check_only:
            raise RuntimeError(f'{name}: patch series is incomplete')
        completed = max(i for i, tree in enumerate(states) if tree == current)
        for item in patches[completed:]:
            if source_tree(directory) != current:
                raise RuntimeError(f'{name}: source changed during preparation; stopping')
            git(directory, 'apply', '--check', str(ROOT / item['path']))
            git(directory, 'apply', '--whitespace=nowarn', str(ROOT / item['path']))
            current = source_tree(directory)
            if current != item['after_tree']:
                raise RuntimeError(f'{name}/{item["id"]}: prepared source differs from expected tree')
    print(f'{name}: {head[:12]} + {len(patches)} patches ready', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('llamacpp', 'ninfer', 'polystrata', 'all'), default='all')
    parser.add_argument('--check', action='store_true',
                        help='verify exact prepared sources without fetching or applying patches')
    args = parser.parse_args()
    versions = json.loads((ROOT / 'backends/versions.json').read_text(encoding='utf-8'))
    names = tuple(name for name,entry in versions.items() if isinstance(entry,dict) and 'path' in entry) if args.backend == 'all' else (args.backend,)
    try:
        for name in names:
            prepare(name, versions[name], args.check)
    except (OSError, RuntimeError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.exit(1, f'Backend preparation failed: {detail}\n')


if __name__ == '__main__':
    main()
