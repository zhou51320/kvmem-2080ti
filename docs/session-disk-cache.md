# 会话的公共 RAM/SSD 缓存

活动和非活动历史共用原生 KV 的 H/D 预算、文件和所有权；恢复仍检查本后端的前缀、
状态及已提交 token 边界。完整说明见[活跃 KV 分层](active-kv-tiering.md)。

```text
--kvmem --kvmem-host-mib H --kvmem-disk-path PATH --kvmem-disk-mib D
--kvmem-sessions N
```

H/D 单位是 MiB。H 需容纳全部并发工作集；D 包括持有的原生 KV、状态附件和未发布写入。
N 只限制非活动历史数量。公共层支持并发，llama 需要 Flash Attention；SSD 文件限当前进程，
最后一个所有者退出或进程被终止时由操作系统清理，不提供重启恢复。

旧在线 NVMe、raw-K NVMe、独立会话磁盘接口和旧目录清理器已移除；旧参数报错提示迁移，
不自动把原每会话/闲置预算换成共享全局预算。旧 CPU arena 不是本次退役范围，不能与公共
SSD 混用。[迁移与验证](legacy-ssd-retirement-20261019.md)列出具体接口、范围和证据。

公共 `SpillFile` 以区间所有权计费；未完成写入不能发布或释放唯一源副本。状态附件沿用
有界序列化与校验和，和原生 KV 使用同一磁盘额度。写入、读取、删除失败及取消保留有效
副本，恢复后的原生字节与状态仍由后端验证。

必要的回归入口包括公共存储/状态附件单元测试、真实进程退出/强杀清理测试、
`scripts/ssd_spill.py` 的独立 CLI RAM/SSD token 对照，以及
`scripts/test_server_lane_conversations.py` 的多路/可选图像、MTP 或 DFlash2 检查。
早期单独快照文件的专用手工脚本已退役；[历史验收](multi-session-validation-2026-09-27.md)
及其性能记录保留原始版本范围，不作为当前共享 H/D 的等价性能保证。
