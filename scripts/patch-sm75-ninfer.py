#!/usr/bin/env python3
"""Apply the small compatibility edits needed by the pinned NInfer tree on sm_75."""

from pathlib import Path


def main() -> None:
    root = Path("backends/ninfer")
    cmake = root / "CMakeLists.txt"
    text = cmake.read_text()
    text = text.replace("^(80|86|89|120a)$", "^(70|75|80|86|89|120a)$")
    if "NINFER_SM75=1" not in text:
        marker = "project(ninfer LANGUAGES C CXX CUDA)"
        text = text.replace(marker, marker + "\nadd_compile_definitions(NINFER_SM75=1)", 1)
    cmake.write_text(text)

    warp = root / "src/ops/common/warp.cuh"
    warp_text = warp.read_text()
    helper = """\ntemplate <int Width>\n__device__ __forceinline__ unsigned warp_reduce_max_u32(unsigned mask, unsigned x) {\n    static_assert(Width > 0 && Width <= 32 && (Width & (Width - 1)) == 0);\n#pragma unroll\n    for (int offset = Width / 2; offset > 0; offset >>= 1) {\n        const unsigned other = __shfl_xor_sync(mask, x, offset, Width);\n        x = other > x ? other : x;\n    }\n    return x;\n}\n"""
    if "warp_reduce_max_u32" not in warp_text:
        warp_text = warp_text.replace("namespace ninfer::ops {", "namespace ninfer::ops {" + helper, 1)
        warp.write_text(warp_text)

    sampling = root / "src/ops/kernel/sampling_device.cuh"
    sampling_text = sampling.read_text()
    if '#include "ops/common/warp.cuh"' not in sampling_text:
        sampling_text = sampling_text.replace("#pragma once", '#pragma once\n\n#include "ops/common/warp.cuh"', 1)
    sampling_text = sampling_text.replace(
        "__reduce_max_sync(mask, value)",
        "warp_reduce_max_u32<Width>(mask, value)",
    )
    for value in ("high", "low", "key"):
        sampling_text = sampling_text.replace(
            f"__reduce_max_sync(kMergeMask, {value})",
            f"warp_reduce_max_u32<kSamplingTileWarps>(kMergeMask, {value})",
        )
    sampling.write_text(sampling_text)


if __name__ == "__main__":
    main()
