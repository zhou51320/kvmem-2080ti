#!/usr/bin/env bash
# Use: source scripts/gpu.sh [UUID|INDEX|local-profile]
_kvmem_gpu_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gpu_env.py"
gpu_for() {
    "${PYTHON:-python3}" "$_kvmem_gpu_script" --uuid "${1:-auto}"
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    gpu_for "${1:-auto}"
    exit $?
fi
_kvmem_gpu_exports="$("${PYTHON:-python3}" "$_kvmem_gpu_script" --shell "${1:-auto}")" || return $?
eval "$_kvmem_gpu_exports"
unset _kvmem_gpu_exports
