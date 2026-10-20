# 旧磁盘接口退役与公共 SSD 回归（2026-10-19）

旧在线 NVMe、raw-K NVMe、独立会话磁盘缓存已由公共 RAM/SSD 分层替代。
原先 UI 和 Windows 启动器对 llama 多路 SSD 的整体拒绝也已解除；两后端允许配置 1–8 路，
实际 H/D 和显存仍需容纳相应负载。未修改推测提案、验证、采样或 GPU 内核算法。

## 迁移

服务 worker 和独立 `llama-kvmem-cli` 统一使用：

```text
--kvmem-host-mib H --kvmem-disk-mib D --kvmem-disk-path PATH
```

H/D 单位是 MiB。D=0 关闭 SSD 且不传目录；正数 D 与目录必须配对。H/D 是活动和非活动
历史共享的进程级预算，不能按同值替换旧每会话/仅闲置预算。H 至少容纳所有并发工作集。
CLI 启用 KVMem 时 H 默认 16384 MiB，其原有实验 B/R 参数语义保留。

以下参数仅保留明确的退役报错及迁移提示，不接受静默别名：
`--kvmem-nvme-gb`、`--kvmem-nvme-dir`、`--kvmem-raw-k-nvme`、
`--kvmem-session-nvme-gb`、`--kvmem-session-cache-dir`，包括等号写法。
独立旧 CPU arena 及旧 RAM 会话计数/软预算不在这次退役范围；CPU arena 仍不可与公共 SSD 混用。
llama 公共 SSD 仍要求 Flash Attention；DFlash2 的单 GPU、固定草稿宽度等限制保留。

已删除 POSIX NVMe 类、Windows 不可用占位类、raw-K 专属写入线程、在线 NVMe 预取/换页分支、
独立会话目录清理器及专属构建开关 `KVMEM_ENABLE_NVME`。`RawKvStore` 的原生 KV、mean-K、
截断与快照能力保留。会话状态附件改为必须持有公共 `SpillFile`，与 KV 共用 D；未发布写入
失败直接释放自己的区间，已发布副本在读取/删除失败时继续有效。

公共 SSD 使用跨平台的缓冲位置 I/O；旧 POSIX 路径的 direct-I/O/page-cache 调优接口不再保留。
本次没有性能等价或加速结论。文件限当前进程，正常退出或强制终止后由 OS 清理，不提供重启恢复。
旧目录不会再被新程序扫描、复用或清理；既有用户文件不受影响。

## 必要验证

GPU 功能检查使用 RTX 5050 Laptop；没有使用 RTX 5060 Ti 或运行速度比较。

- Windows 公共层 18 项通过：原生 KV、mean-K、尾部回滚、共享预算、冷恢复、快照校验和、附件交换及故障恢复。
- 新的进程检查通过正常退出、写入中强杀和用户文件保留。WSL 缺少 CMake，使用已有 GCC、8 个并行编译任务完成 8 项必要存储/附件/进程检查；这是 WSL 主机证据，不是原生 Linux GPU 验收。
- 实际 llama worker、独立 CLI 和参数测试编译通过；108 项模型无关 CLI 兼容检查通过，20 种实际二进制旧参数及等号写法明确拒绝。
- 37 项控制台、7 项 Windows 子进程参数、PowerShell 启动器、Windows 打包和浏览器启动页套件通过。启动器覆盖两后端 C2/C8 的公共 SSD 参数；浏览器覆盖 C8 下保留 SSD 配置与预览。
- llama C8 使用既有 0.8B Q8_0 目标和未训练 DFlash2 夹具、固定 5 草稿、B512/R256、H64/D1024 MiB。25 条完成请求记录覆盖独立/并发秘密词、精确输出对照、历史复用、取消与恢复。真实峰值 8 路，读取 10,027,008 bytes、写入 77,778,944 bytes，配额检查和 I/O 错误检查通过。
- 独立 CLI 对同一输入比较 RAM 与公共 SSD，32 个生成 token 完全一致。SSD H8/D256 MiB，读取 2,506,752 bytes、写入 45,434,880 bytes，配额与退出清理通过。

首个 C8 样本只用了约 44.6 MiB 原生 RAM、未触发 SSD，因此其“真实读写”检查失败；
该结果保留，不能计作 SSD 通过。加长输入后才完成上述压力资格，未改变产品预算或推理逻辑。
上一轮八路开放遗漏了一个手工 CLI 脚本的五路拒绝断言，本轮改为接受五/八路、拒绝九路，原失败记录保留。
本轮不建立完整 27B 八路、训练草稿接受率、NInfer 新 GPU 矩阵或图像/SSD/DFlash2 全组合的资格。
以前的容量失败、少层 NInfer 标点差异和旧版本性能数据均保留原范围。

机器可读证据见 [公共 SSD 清理回归](benchmarks/legacy-ssd-retirement-20261019.json)。
当前手工入口为 `scripts/ssd_spill.py` 和支持公共 SSD/DFlash2 的
`scripts/test_server_lane_conversations.py`；旧独立文件缓存的专用脚本已移除，历史记录保留。
