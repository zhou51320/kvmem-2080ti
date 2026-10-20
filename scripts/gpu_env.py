"""Portable, explicit GPU selection for validation tools."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def find_binary(name: str) -> Path:
    names = (name + '.exe', name) if os.name == 'nt' else (name, name + '.exe')
    for directory in (ROOT / 'build/bin', ROOT / 'build'):
        for filename in names:
            path = directory / filename
            if path.is_file():
                return path
    raise SystemExit(f'{name} not found; build it or supply an explicit worker path where supported')


def local_config(env: dict) -> dict:
    path = Path(env.get("KVMEM_LOCAL_CONFIG", ROOT / ".kvmem-local.json"))
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("Local validation configuration must be an object")
    return config


def inventory(env: dict) -> list[dict]:
    executable = shutil.which("nvidia-smi", path=env.get("PATH"))
    if not executable:
        raise ValueError("nvidia-smi is required to resolve a validation GPU")
    result = subprocess.run([executable,
        "--query-gpu=index,uuid,name,pci.bus_id,memory.total", "--format=csv,noheader,nounits"],
        env=env, check=True, capture_output=True, text=True, encoding="utf-8")
    devices = []
    for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 5:
            raise ValueError("Unexpected nvidia-smi inventory format")
        index, uuid, name, bus, memory = (part.strip() for part in row)
        devices.append(dict(index=index, uuid=uuid, name=name, bus=bus, memory_mib=int(memory)))
    if not devices:
        raise ValueError("No NVIDIA GPU is available")
    return devices


def select_device(devices: list[dict], env: dict, selector: str | None, config: dict) -> dict:
    selector = selector if selector not in (None, "", "auto") else env.get("KVMEM_GPU")
    inherited = not selector and "CUDA_VISIBLE_DEVICES" in env
    if inherited:
        selector = env["CUDA_VISIBLE_DEVICES"].strip()
        if not selector or selector == "-1":
            raise ValueError("CUDA_VISIBLE_DEVICES disables CUDA; choose --gpu explicitly to override it")
        if "," in selector:
            raise ValueError("Validation needs one GPU; choose --gpu explicitly")
        if selector.isdecimal():
            if env.get("CUDA_DEVICE_ORDER") != "PCI_BUS_ID":
                raise ValueError("Inherited numeric CUDA ordering is ambiguous; use its UUID or --gpu INDEX")
            ordered = sorted(devices, key=lambda d: d["bus"].lower())
            ordinal = int(selector)
            if ordinal >= len(ordered):
                raise ValueError("Inherited CUDA device ordinal is unavailable")
            return ordered[ordinal]
    selector = str(selector or config.get("default_gpu", "auto"))
    profiles = config.get("gpu_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("gpu_profiles must be an object")
    seen = set()
    while selector in profiles:
        if selector in seen:
            raise ValueError("GPU profile contains a cycle")
        seen.add(selector)
        selector = str(profiles[selector])
    if selector == "auto":
        if len(devices) != 1:
            raise ValueError("Multiple GPUs are available; specify --gpu UUID/INDEX or a local default_gpu")
        return devices[0]
    matches = [d for d in devices if d["index"] == selector or
               (selector.startswith("GPU-") and d["uuid"].startswith(selector))]
    if len(matches) != 1:
        raise ValueError(f"GPU selector {selector!r} is unavailable or ambiguous")
    return matches[0]


def toolchain_env(env: dict, config: dict | None = None) -> dict:
    """Discover optional CUDA runtime paths without replacing inherited paths."""
    cuda = env.get("CUDA_HOME") or env.get("CUDA_PATH") or (config or {}).get("cuda_root")
    if not cuda:
        nvcc = shutil.which("nvcc", path=env.get("PATH"))
        if nvcc:
            cuda = str(Path(nvcc).resolve().parent.parent)
    bins, libraries = [ROOT / "build/bin"], [ROOT / "build/bin"]
    if cuda:
        bins.append(Path(cuda) / "bin")
        libraries += [Path(cuda) / "lib64", Path(cuda) / "lib"]
    def prepend(key, paths):
        existing = env.get(key, "").split(os.pathsep) if env.get(key) else []
        additions = [str(p) for p in paths if p.is_dir() and str(p) not in existing]
        if additions:
            env[key] = os.pathsep.join(additions + existing)
    prepend("PATH", bins)
    if os.name != "nt":
        prepend("LD_LIBRARY_PATH", libraries)
    return env


def apply_gpu(env: dict, which: str | None = None) -> dict:
    config = local_config(env)
    device = select_device(inventory(env), env, which, config)
    env.update(CUDA_VISIBLE_DEVICES=device["uuid"], CUDA_DEVICE_ORDER="PCI_BUS_ID", KVMEM_GPU=device["uuid"],
               KVMEM_GPU_UUID=device["uuid"], KVMEM_GPU_NAME=device["name"],
               KVMEM_GPU_INDEX=device["index"], KVMEM_GPU_MEMORY_MIB=str(device["memory_mib"]))
    return toolchain_env(env, config)


def add_gpu_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gpu", default=None,
        help="nvidia-smi index, GPU UUID or ignored local profile; default respects the environment")


def configure(selector: str | None = None) -> str:
    try:
        env = apply_gpu(os.environ.copy(), selector)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    os.environ.update(env)
    return env["KVMEM_GPU_UUID"]


def require_device(stderr: str, expected: dict | None = None) -> None:
    expected = expected if expected is not None else os.environ
    uuid, name = expected.get("KVMEM_GPU_UUID"), expected.get("KVMEM_GPU_NAME")
    if not uuid or expected.get("CUDA_VISIBLE_DEVICES") != uuid:
        raise SystemExit("Resolve one validation GPU UUID before launching the worker")
    # Older llama logs expose names, not UUIDs. The launch mask fixes physical
    # identity; the log confirms that initialization and offload actually occurred.
    if not name or name not in stderr or "offloaded" not in stderr or "to GPU" not in stderr:
        raise SystemExit("Selected GPU initialization/offload was not confirmed in the worker log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selector", nargs="?", default=None)
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--uuid", action="store_true")
    args = parser.parse_args()
    try:
        env = apply_gpu(os.environ.copy(), args.selector)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if args.shell:
        for key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "KVMEM_GPU", "KVMEM_GPU_UUID", "KVMEM_GPU_NAME",
                    "KVMEM_GPU_INDEX", "KVMEM_GPU_MEMORY_MIB", "PATH", "LD_LIBRARY_PATH"):
            if key in env:
                print(f"export {key}={shlex.quote(env[key])}")
    else:
        print(env["KVMEM_GPU_UUID"])


if __name__ == "__main__":
    main()
