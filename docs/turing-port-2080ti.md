# KVMem 2080 Ti 构建目标

本目录以官方 `kvmem/kvmem` 主线为 KVMem/ActKV 基线，运行时目标为 `ninfer-serve`。
GitHub Actions 在 CUDA 13.2 Ubuntu 24.04 容器中，将 `ninfer-2080ti-22g` 的 Turing
内核层叠加到官方 NInfer 后端，并以 `CMAKE_CUDA_ARCHITECTURES=75` 编译 RTX 2080 Ti。

官方主线将 ActKV 相关能力称为 LRFU 历史加权与 GQA `max` 聚合，默认参数为
`lambda=0.5`、`top-frac=0.9`。本目标不复制旧项目的 KVMem 实现，避免与官方主线分叉。

真实推理仍需要 RTX 2080 Ti、匹配驱动和 `.ninfer` 模型；Actions 工件只包含服务端程序，
不包含模型。
