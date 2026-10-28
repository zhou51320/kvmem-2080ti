#!/usr/bin/env bash
# Linux source/package entry. Dependency preparation is an explicit separate step.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
python="${KVMEM_PYTHON:-python3}"
open_browser=true
check=false
args=()
for argument in "$@"; do
    case "$argument" in
        --no-browser) open_browser=false ;;
        --check) check=true ;;
        *) args+=("$argument") ;;
    esac
done
for relative in scripts/local-console.py scripts/launcher/config.py scripts/launcher/plan.py \
    scripts/launcher/export.py scripts/launcher/systemd.py scripts/launcher/process_linux.py \
    scripts/launcher/services.py scripts/launcher/worker.py \
    scripts/launcher-capabilities.json scripts/console_chat.py scripts/model_health.py \
    scripts/console_hardware.py ui/monitor/index.html ui/monitor/launcher.js; do
    if [[ ! -f "$root/$relative" ]]; then
        printf 'Incomplete KVMem package: missing %s\n' "$relative" >&2
        exit 1
    fi
done
if [[ -d "$root/runtime/python-libs" ]]; then
    export PYTHONPATH="$root/runtime/python-libs${PYTHONPATH:+:$PYTHONPATH}"
fi
export PYTHONNOUSERSITE=1
export PYTHONUTF8=1
if ! "$python" -c 'import sys, psutil; assert sys.version_info >= (3,10); assert sys.maxsize > 2**32' ; then
    printf '%s\n' 'Use 64-bit Python 3.10+ with the packaged psutil dependency, or explicitly install scripts/requirements-console.txt into your Python environment.' >&2
    exit 1
fi
"$python" - "$root" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts'))
from launcher.plan import capabilities
capabilities(root)
package = root / 'console-build.json'
if package.is_file():
    metadata = json.loads(package.read_text())
    if metadata.get('launch_contract_version') != 3:
        raise SystemExit('Console components do not match; use a complete package.')
    for relative, expected in metadata.get('controller_files', {}).items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit('Console file mismatch: ' + relative)
for candidate in (root/'ui/dist', root/'workers/share/kvmem/ui', root/'build/share/kvmem/ui'):
    manifest = candidate/'ui-build.json'
    if (candidate/'index.html').is_file() and manifest.is_file():
        if json.loads(manifest.read_text()).get('console_chat_protocol') == 1:
            break
else:
    raise SystemExit('Unified chat files are missing or incompatible. Use a complete console package.')
PY
if "$check"; then
    printf '%s\n' 'KVMem UI files and Linux Python runtime OK. No model or GPU was loaded.'
    exit 0
fi
if "$open_browser"; then
    args+=(--open-browser)
fi
exec "$python" -B "$root/scripts/local-console.py" "${args[@]}"
