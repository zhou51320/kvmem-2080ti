# Paths and device identities anonymized; measurements unchanged.
# CUDA Graph 启动探测

自动预留现在先用临时 Program 捕获当前配置的 CUDA Graph，读取驱动显存增量，
释放临时资源后再规划正式运行时。MTP、DFlash2 和普通解码使用同一规则：

- 最终预留不低于 **256 MiB**。
- 第一次测量超过 256 MiB 时重测一次，取两个有效读数中较小的值，再应用下限。
- 第二次没有有效读数时使用第一次；第一次没有有效读数时保留原静态估计。
- `--cuda-graph-allowance-mib N` 的非零手动值优先，不运行探针。
- `--no-cuda-graph` 不运行探针，也不增加 Graph 预留。

探针自动用于 Qwen 核心的单设备稠密执行路径。多设备、expert streaming、prompt
graft、suspend 和磁盘缓存配置保留静态规划，避免临时实例重复打开正式磁盘存储。
探测使用当前物理 KV 窗口并覆盖已配置的
n-gram Graph 家族；不能用一个虚拟占位页代表会实际运行内核的完整窗口。

显存分配失败、容量不足或 Host 分配失败会回退；非法 CUDA 执行等错误继续报错，
不会被当成“测不出来”忽略。日志显示测量值、是否重测以及正式规划采用的预留。

这是带保底的测量估计，不是显存占用的严格上界。驱动缓存、WDDM 驻留、模型和
Graph 形状都会影响读数；临时捕获也会增加启动时间和启动阶段峰值。256 MiB 下限
可能高于小模型原有静态估计。手动预留仍可用于已经独立验证的配置。

## 来源与署名

移植自 [melis2023/ninfer-kvmem-actkv](https://github.com/melis2023/ninfer-kvmem-actkv)，
固定参考版本 `664cb6daa8cba30dfe39b0d10113a72b20587853`，对应探针提交 `b4d8096`
和超下限重测提交 `591537b`。本地保留当前 KVMem 规划、手动优先与错误分类。

Co-authored-by: melis2023 <zhuangisme@gmail.com>

## 验证

`ninfer_graph_probe_test` 覆盖无读数、零读数、下限边界、两次测量的选择、第二次
失败和执行异常传播。原生补丁及其 SHA256、完成后的 Git tree 都记录在
`backends/versions.json`，可用 `scripts/prepare-backends.py --backend ninfer --check`
验证完整补丁序列。

2026-10-27 实测：RTX 5060 Ti（`GPU-A`），
Qwen3.8-27B IQ3_S 主模型 + Q4 DFlash2，context 262144、budget 36864、reserve 2048、
INT8 KV、FP32 GDN、prefill 256、DFlash2 7、n-gram 31、并发 1、CPU vision 1024。
旧自动规划需要 2,791,748,352 字节，超过当时可用的 2,290,089,984 字节而启动失败。
新探针测得 64 MiB，采用 256 MiB 预留；运行时规划降至 2,053,550,848 字节
（减少 704 MiB），启动后剩余 416 MiB。复制输出是原文的精确前缀，接受了 107 个
n-gram token。手动 256 MiB 对照也通过。

Qwen3.5-4B 的 MTP 自动、手动 64 MiB、关闭 Graph、MTP + n-gram，以及普通解码
启动与回答检查均通过。自动 MTP 和普通解码都应用 256 MiB 下限。
探针预热后，正式捕获的驱动增量可能显示 0；这不代表 Graph 不占显存，正式容量
规划仍保留 256 MiB，并读取探针结束后的可用显存。

这些是启动和功能回归，不是长上下文质量或吞吐性能对比。测试结果见
[`benchmarks/cuda-graph-probe-20261027.json`](benchmarks/cuda-graph-probe-20261027.json)。
