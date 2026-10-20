# NInfer 召回传输合并与诊断

> Device identifiers and private paths have been anonymized; reported measurements are unchanged.

日期：2026-10-19。基线：主仓库 `c558e08`，NInfer 固定基线 `08fd7019925f`。

## 贡献归属与共同作者署名更正

[PR #155](https://github.com/kvmem/kvmem/pull/155) 的连续页召回搬运合并借鉴了
[melis2023 的原始修正](https://github.com/melis2023/ninfer-kvmem-actkv/commit/b6c8337a6833ceee4d65447e624398425f3d2028)。
本仓库在既有64 MiB分批搬运路径上完成适配，并补充默认关闭的诊断及本机验证。

该 PR 的代码提交 `5378c6155a42d08d98873961134ef421ada0ce8d` 和合并提交
`feb4742cb0327c115b17c13e4c291675866555ee` 遗漏了 Git 共同作者 trailer。
本次追加署名更正提交，使用原作者公开提交中的身份：
`Co-authored-by: melis2023 <zhuangisme@gmail.com>`。
这是后续提交中的归属更正，不追溯修改原提交的作者信息或标识，也不改变运行时代码与验证结果。

## 行为与范围

本次借鉴 [melis2023 的连续页搬运修正](https://github.com/melis2023/ninfer-kvmem-actkv/commit/b6c8337a6833ceee4d65447e624398425f3d2028)：
`record_groups` 表示 Host 记录所属的锁页分配区，原窗口桥接层误传逻辑块 ID，
使同一中转缓冲中的连续物理页也被拆成逐页拷贝。现在 Main/MTP 的 spill、restore 四处调用
均以空 groups 表示同一分配区，仍由底层检查物理页连续性、Host 步长及设备最大 copy pitch。

保留主线的 64 MiB 目标分批中转；单页超过目标时仍至少容纳一页。每批完成原有同步后才读写或
复用该缓冲，分批之间不保存中转指针。同步恢复的 resident 读取租约、异步存储完成边界、内容
epoch 检查、原生 KV/scales 和 recurrent state 精确校验均保留。完整页和尾页清零策略不变。
本次不调整选块、DFlash2 floor、草稿调度或启动参数默认值。

## 诊断开关与计时口径

启动 worker 前设置环境变量 `KVMEM_TRANSFER_TRACE=1`。默认关闭；关闭时不创建 CUDA 事件、
不扫描拷贝规划、不读取时钟、不打印传输日志。值在进程中首次使用时读取，修改后需重启 worker。

```powershell
$env:KVMEM_TRANSFER_TRACE = '1'
./scripts/windows/start-backend.ps1 -Backend ninfer -Model 'D:/models/model.ninfer'
```

每次成功完成的 `start_spill` / `start_restore` 输出一条 `KVMEM_TRANSFER`，包括：

| 字段 | 含义 |
|---|---|
| `direction`, `async`, `pages` | D2H/H2D、是否使用异步存储工作、逻辑记录数 |
| `batches`, `staging_bytes` | 当前传输的批数、Program 复用的锁页缓冲容量 |
| `pool_runs` | 各池真实合并规划的 run 数之和；Main/MTP 分别计数 |
| `copy_calls` | 按该规划实际需要的 `cudaMemcpy2DAsync` 调用数，HeadMajor 逐 head 计数 |
| `payload_bytes` | Main/MTP 数据和 scales 的有效字节合计，不含 Host 对齐填充 |
| `prepare_us`, `host_us` | 准备/重映射等剩余 CPU 时间、批内 Host 数据读写时间 |
| `submit_us`, `wait_us` | CUDA 提交时间、原有批同步的等待时间 |
| `gpu_span_ms` | 每批 CUDA 事件区间，跨 rank 取最大值后按批相加 |
| `total_us` | 诊断作用域墙钟，不含退出时的局部对象析构 |

规划统计复用底层 `host_record_run_end`，同时考虑物理碎片、Host 步长和分配区边界。
CUDA 事件只在已有批同步后读取，不增加同步。事件区间可能包含 CPU 提交期间的 stream 空闲，
不能当作 copy engine 的纯忙碌时间；也不能与 CPU 提交/等待时间再次相加。
异步存储 worker 的排队和磁盘 I/O 不包含在 start 调用的总时间内。
传输字节和调用计数不包含 `--kvmem-verify-transfers` 的校验回读；开启校验时其额外 CPU 工作
仍计入 `prepare_us` / `total_us`。性能对照关闭该额外校验。

## RTX 5050 正确性证据

设备：RTX 5050 Laptop 8 GB，UUID `GPU-A`。
工具链：CUDA 13.2、MSVC 19.44；编译并行上限 8。Python 验证工具使用已有 3.12.13 环境。

- 独立字节布局 oracle 覆盖 PageMajor/HeadMajor、碎片页、不同锁页分配区、分 plane 恢复、
  K/V 与 scales；新增统计检查覆盖空传输、Host 步长和每记录独立分组。
- HTTP 使用已有四层 27B 执行夹具（3 GDN + 1 Attention，保留原始投影、heads、权重及可选 MTP）。
  该夹具用于内存/状态正确性，不代表完整模型回答质量。请求包含 58,438-token 越窗输入、
  相同历史复用及末尾指令改写；旧分组、新合并、诊断关闭三种运行的确定性输出一致。
- BF16、无 MTP 的 512 页恢复仍分两批；旧版为 512 runs / 1,024 copy calls，
  合并后为 2 runs / 4 calls。640 页 spill 仍分三批，调用数 1,280 → 6，传输字节相同。
- INT8 + MTP2 + 并发容量 2 + Host384MiB / SSD512MiB，长请求、双请求并发、历史回读和
  断开流后的短请求恢复通过。16 次恢复完成 Main/MTP K/V、scales 和 recurrent state 精确校验。
  实际 SSD 写入 497,936,384 bytes，回读 147,202,048 bytes，存储错误 0，峰值 admitted=2。
- 合并版单池 pin 最大 64MiB，Main+MTP 为 63.938MiB；诊断关闭时无传输日志。

初次 HTTP 启动受网络沙箱限制，未计为产品失败；在允许本地连接的隔离环境中重跑通过。
初次编译发现 span 重载歧义，移除多余重载后所有选定目标编译通过。

## 性能与构建范围

RTX 5060 Ti，UUID `GPU-B`，驱动 617.42；
测量时链路为 PCIe Gen4 x4，与贡献者的 Gen4 x8 环境不同。完整
`Qwen3.8-27B-GSQ-RCO-IQ3_S-vision-mtp.ninfer`，启用文本与 MTP2、INT8 KV、CUDA Graph，
关闭设备校准、ngram、thinking；context65536、B32768/R8192、prefill512、Host12GiB、SSD关闭、
并发1、sink128/recent512。每次新进程先短请求预热，再重放同一个 58,438-token 输入，
greedy、seed42、输出上限32；均自然停止于4个输出 tokens，输入缓存命中为0。

两版使用相同诊断，顺序 A/B/B/A。计数和分段时间仅含测量请求，排除短预热。

| 指标 | 旧分组，两次 | 连续页合并，两次 | 均值变化 |
|---|---|---|---|
| 完整请求墙钟 | 101.687 / 101.047 s | 99.922 / 99.735 s | 101.367 → 99.829 s，−1.52% |
| spill + restore 调用总时间 | 4.124 / 3.906 s | 2.685 / 2.650 s | 4.015 → 2.667 s，−33.57% |
| 计划的 CUDA 拷贝调用数 | 201,688 / 201,688 | 7,480 / 7,480 | −96.29% |
| 有效传输字节 | 6,815,440,896 / 同值 | 同值 / 同值 | 相同 |
| 批数 | 110 / 110 | 110 / 110 | 相同 |
| Host 批内读写 | 0.485 / 0.443 s | 0.425 / 0.394 s | 0.464 → 0.410 s |
| CPU 提交 | 2.417 / 2.273 s | 0.039 / 0.042 s | 2.345 → 0.041 s |
| 原有同步等待 | 1.050 / 1.060 s | 2.075 / 2.071 s | 1.055 → 2.073 s |
| CUDA 事件区间 | 3.457 / 3.324 s | 2.105 / 2.104 s | 3.391 → 2.105 s |

所有四次的输出内容、停止原因、MTP接受数2/2一致。同步等待增加，同时 CPU 提交显著减少，
总传输时间下降；不能把等待字段单独解读为传输回退。扣除传输后，其余墙钟均值
97.352 → 97.161 s（−0.20%），没有把整段推理速度差归因到新算法。
短 decode 段为旧67.3/67.7、新66.6/66.0 token/s；仅4个输出 tokens，不足以判定 decode 性能变化，
不宣称解码提速或接受率改善。现有64MiB分批和本机链路下未复现贡献者的“两倍传输”结果。

关闭诊断、相同模型/输入/配置的新进程 A/B 控制：旧100.656 s，新99.641 s（−1.01%），
输出与四次诊断运行一致，无 `KVMEM_TRANSFER` 日志。该控制仅一对样本，不作为稳定收益估计。
诊断开启均值相对各自关闭样本分别慢0.71 s / 0.19 s，包含插桩成本及运行间变化，
不能直接将诊断开启的1.52%视为默认模式收益；正常模式与诊断模式均观察到完整请求耗时下降。
RTX 5050 的前述数据只作正确性证据；本轮未测256K、多GPU诊断、模型回答质量变化或所有量化/并发排列。

本次冻结既有 DFlash2 构建的 21 个项目链接输入，独立编译本次 core/window/test 单元，
以及与当前主线不同的三个 serving 单元。对照与合并构建只有窗口分组实参不同，计时方法相同。
已逐文件核对其余生产 C++/CUDA 源码与既有构建源一致；没有重新编译未改动的 CUDA 内核，
不将本地重链接描述为全量干净构建。完整版本补丁、摘要与 prepared tree 通过
`scripts/prepare-backends.py --backend ninfer --check` 校验。

发布时主线已前进到 `5298a568f37c3b73b8c22da291e1f0201e4efc78`（PR #154）。
本次在该基线上保留双方补丁并重新生成版本摘要和 prepared tree；传输相关五个文件
与上述性能测量版本逐字节相同。针对主线新增的 serving 布局和公开链接依赖，重新编译
所有受影响的 HttpServer 调用方、入口、选项、指标和运行时单元后重链接。
RTX 5050 补跑物理容器独立字节检查，以及 INT8/MTP2/SSD/C2 的58,438-token
历史、改写、并发复用和断流恢复；全部通过，进程正常退出，原生 payload/state 校验通过。
上表保留原始测量基线，并非重新测量 PR #154 合入后的完整请求性能。
