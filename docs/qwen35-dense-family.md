# Qwen3.5 dense 家族适配与验收（2026-10-21）

范围为 dense 文本塔及配套原生 MTP，覆盖两个后端。MoE、视觉塔、其他草稿算法及性能调优不在本轮范围。这里的“形状支持”不等于每个模型都经过整模型验收。

| 模型规格 | hidden / FFN | 层数 | Attention Q/KV | GDN key/value heads | embedding/head 共享 | 验证范围 |
|---|---|---|---|---|---|---|
| 0.8B | 1024 / 3584 | 24 | 8 / 2 | 16 / 16 | 是 | 配置、转换契约、数值与状态测试 |
| 2B | 2048 / 6144 | 24 | 8 / 2 | 16 / 16 | 是 | 配置、转换契约、数值与状态测试 |
| 4B | 2560 / 9216 | 32 | 16 / 4 | 16 / 32 | 是 | 完整模型与 MTP，两后端实测 |
| 9B | 4096 / 12288 | 32 | 16 / 4 | 16 / 32 | 否 | 配置、转换契约、数值与状态测试 |
| 27B | 5120 / 17408 | 64 | 24 / 4 | 16 / 48 | 否 | 保留原数值路径；既有切片 graph/eager 回归通过，完整 MTP 未在本轮实测 |

## 实现

NInfer 的 `qwen3_5_dense_gguf` 转换配方按文本配置校验 GGUF 元数据、张量清单、尺寸和类型，按实际层数定位 MTP；共享 embedding/head 不重复存储。GDN 控制权重接受 F32/F16/BF16 及已有量化格式，按照正确的值头顺序转换为 BF16。量化主权重保留原 ggml blocks。

运行时的 GGUF GDN 输入投影、卷积状态和控制投影改用实际尺寸；新增 GQA=4 的 8/2 与 16/4 注意力规格，以及 16 值头的 MTP record/fold。小规格控制投影采用通用 FP32 累加实现，注意力复用 prompt 内核处理各宽度。27B 与原 MoE 形状的既有优化分派保留。新增路径尚未做吞吐调优，不能据此声称性能等价或提升。

llama.cpp 的 MTP replay 准入、记录容量和 CUDA fold 按模型 key/value heads 与卷积通道计算，移除只允许 64 层、48 值头的限制。仍要求受支持的 dense Qwen3.5、128 维 GDN、4 tap 卷积及完整的原生 MTP 配置。

## 模型与资源

只下载 `unsloth/Qwen3.5-4B-MTP-GGUF` 的 `Qwen3.5-4B-Q4_K_M.gguf`，固定 revision `86835bf9949e4d14d6860f7910b1340ad4f271a9`。GGUF 为 2,834,975,040 bytes，SHA-256 为 `3874209241c9a397e2f62cd3f70f80fd2dfbf0dfccb6838416bdb48a714e8630`。同一文件供 llama.cpp 直接使用并转换为完整 32+1 层 `.ninfer`（2,827,176,704 bytes），二者合计约 **5.27 GiB**。未下载其他规格权重、不同量化或视觉组件。

配置、tokenizer 与模板来自对应 Qwen 官方仓库。官方 4B 仓库没有 generation_config.json，转换资源明确从其 text_config 的 EOS 248044 生成最小配置，未借用 27B 配置。转换方法见后端 [转换文档](../backends/ninfer/docs/weight-conversion.md)。

测试只使用 RTX 5050 Laptop；未使用 RTX 5060 Ti，未做性能比较。Windows / MSVC 2022 / CUDA 13.2，默认八任务编译。转换与 Python 测试使用现有 Python 3.12 环境（本机无要求的 3.11），未安装新依赖。完整 NInfer 4B+MTP 在此测试配置下峰值采样显存约 2868 MiB；这不是任意上下文/并发配置的显存上限。

## 验收

- 转换契约和相关 Python 回归：52 项通过。五种尺寸、共享输出头、MTP tensor inventory、头排列与损坏输入拒绝均覆盖。
- NInfer：新控制投影和原 27B/35B 路径直接对独立数值参考通过；GGUF Q4_K/Q5_K/Q6_K/Q8_0 在五种 hidden 宽度通过。新注意力规格的九种 KV 编码、分页映射、CUDA Graph 和不同有效长度通过。16/32 值头的 MTP fold 零/部分/全接受及双请求不同接受长度通过；原 27B/35B 的 record/fold 回归通过。
- llama.cpp：GDN replay/fold 的新 16/32 值头与原 48 值头通过独立 FP64 及原生状态检查；完整 4B+MTP 的 KV 类型、transfer、接受前缀和 snapshot 恢复检查通过。
- NInfer 完整 4B：MTP4 与普通生成分别通过 graph/eager 两种模式。每种模式包含 C1 prompt 2045/2046/2047/2048、C2 1919+129、C2 1792+257 及后续短请求；检查真实并发、精确输出上限、资源释放、引擎存活。每组图/非图 token 一致，仅作辅助证据。MTP 真实 drafted=629、accepted=621；简单聊天指令正确。
- llama.cpp 完整 4B：普通生成、MTP snapshots、MTP replay，各含独立与并发 129/257 token 输出、两个不同标记及历史追问。真实并发2、token上限、标记隔离和结束后的健康状态通过；修复后的日志均无会话提交错误。

模型正确性不以两个后端输出逐 token 相同为唯一标准；量化、存储与运算边界不同。本轮不包含能力评测、长上下文召回质量评测、完整 27B+MTP、多卡或其他 GPU 架构实测。

## 独立修复：会话提交与已释放旧 owner

双请求服务回归发现 `KVMEM lane commit failed: invalid map<K, T> key`。适配前 `61569ff4` 服务在同一 4B 工作负载第二轮也复现，第一轮没有复现。成功 attach 已释放 outgoing owner，其他 lane 可以淘汰或重新占用它；原 finish 仍访问并清理 outgoing，导致当前提交快照被异常清空，或释放后来请求的 switching 保护。

用户批准独立修复：成功切换后只完成当前 owner，切换失败仍清理受保护的 outgoing；异常分支遵循相同所有权规则。`kvmem-lane-conversations-test` 确定性覆盖淘汰、后续重新占用、结束失败、切换失败及幂等结束。旧头文件副本在当前 committed rows 断言失败，修复后通过。测试注入的 finish error 是预期结果，不是正常服务错误。

## 失败记录与复现

首次完整 NInfer 4B 初始化被未登记的注意力几何拒绝，已补齐。新增注意力测试首次误用了 packed KV 的参考编码重载，导致主机崩溃；修正测试入口后九种编码通过，未修改数值标准。小规格 MTP fold 初测暴露 16 值头准入缺口，已补齐并通过独立状态测试。最终 Python 复测首次因未设置已有 gguf 包路径而无法收集测试，补齐路径后 52 项通过，未新增依赖。

本机 MSVC 增量构建漏识别会话头文件更新，第一轮标作 fixed 的服务测试实际上使用旧二进制；该轮已判无效，强制重建服务对象后重测通过。原始失败和无效记录均保留，未覆盖为成功。

可重复执行的数值入口：

```text
ninfer_dense_family_tests ninfer_softmax_attention_test --dense-family-only
ninfer_dense_family_tests ninfer_gdn_replay_fold_test --dense-family-only
ninfer_dense_family_tests ninfer_gdn_gating_proj_test
kvmem-gdn-replay-test
kvmem-lane-conversations-test
```

完整 llama 服务回归需要调用方提供已有模型和显卡 UUID，不自动下载权重：

```text
python tests/qwen35-dense-service-regression.py --binary <llama-kvmem-server> --model <complete-4B-MTP.gguf> --gpu <GPU-UUID> --out <results>
```

私有原始日志、模型来源和完整 NInfer 边界探针位于本机 `qwen35-dense-family-20261021/`。上述公开说明只主张明确列出的范围。
