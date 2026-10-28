# PolyStrata 32K follow-up on RTX 5050, 2026-10-24

This follows the earlier attention-overhead optimization. The primary comparison includes all three same-input pairs, including the slow decode in pairs1 and3. No run is dropped to meet the target. Rates pool tokens divided by total elapsed time; they are not arithmetic averages of displayed speeds.

## Configuration

RTX 5050 Laptop; Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL. Device identity is retained in local evidence. The explicit 5050 request for this task determines the test device. STRATA_FATTN=1, STRATA_FDEC=1, Q8_0 KV, MTP3, CUDA graphs, prompt batch 512, 16 CPU threads, 64 expert swaps, stage-mib 2048, native stage-ahead default 1024, and VRAM reserve 256 MiB.
Every process receives a 2048-input/512-output warmup and then the same 32768 input token IDs with 512 generated tokens, temperature 0. Warmups are excluded. Every measured request has zero prefix reuse. There is no expert freezing or phase profiling during these comparisons.
Native ceiling is 67584. KVMem logical ceiling is 262144, resident budget 32768, generation reserve 2048, Host KV payload quota 4096 MiB and four retained histories. Both commands disable native prompt/conversation caching; KVMem history reuse remains enabled. Query execution is resident-only, with no replay. These compare configured KVMem functionality with upstream inference, not identical host-memory layouts or history semantics.
Pair1 runs KVMem then upstream; pairs2 and3 run upstream then KVMem. The previous deployed KVMem worker is measured before the pairs and after the pairs, with the same warmup/request protocol. This differs from the earlier multi-length sweep, which accumulated several histories before reaching 32K.

## Whole-model throughput

| Version | Prefill tok/s | Decode tok/s |
|---|---:|---:|
| Upstream, three runs | 622.64 | 38.643 |
| New KVMem, three runs | 609.79 | 38.121 |
| Previous KVMem, two control runs | 610.39 | 34.125 |

New KVMem versus upstream: prefill -2.06%, decode -1.35%. Positive means faster.
New versus previous KVMem controls: prefill -0.10%, decode +11.71%.

The previous-worker controls show essentially unchanged whole-model prefill. They do not establish an end-to-end prefill improvement from this patch; the smaller gap against upstream can partly reflect run variability. The large pooled decode improvement over the previous worker is also not a causal estimate: its late control slowed sharply as the run progressed.

Individual measurements, retained to show variability:

| Pair | Mode | Prefill tok/s | Decode tok/s | Minimum free RAM MiB | System disk read GiB | MTP accepted/offered |
|---|---|---:|---:|---:|---:|---:|
| pair1 | managed | 602.55 | 42.900 | 252.7 | 6.16 | 241/321 |
| pair1 | native | 625.94 | 38.524 | 61.9 | 3.13 | 270/367 |
| pair2 | native | 617.88 | 43.181 | 51.2 | 6.73 | 255/320 |
| pair2 | managed | 616.20 | 41.188 | 175.9 | 5.39 | 272/332 |
| pair3 | native | 624.16 | 35.066 | 233.8 | 7.15 | 250/311 |
| pair3 | managed | 610.77 | 32.146 | 257.1 | 7.29 | 250/305 |

Previous-worker controls:

| Run | Prefill tok/s | Decode tok/s | MTP accepted/offered |
|---|---:|---:|---:|
| previous | 611.82 | 41.930 | 263/333 |
| previous-recheck | 608.98 | 28.770 | 249/303 |

## Implementation and correctness

- The Qwen35 GQA8 feedback kernel assigns each physical page and its group of query heads to one warp. Warps no longer wait at block-wide barriers between page reductions; QK sums are broadcast only where consumed. The kernel still uses FP32 QK accumulation and a stable softmax over the entire causal window. Reduction ordering may differ slightly. Unsupported shapes retain the earlier paths.
- Sampled and unsampled decode use distinct CUDA-graph variants. Unsampled steps omit attention-feedback nodes and redundant coordinate updates. Switching back to a sampled step uploads the complete coordinates. The sampling rate and accepted/rejected MTP boundary remain unchanged.
- No changes to model precision, KV precision, MTP acceptance, history retention, sampling cadence, Query/Pressure policy, prompt budget or generation reserve are used to obtain the results.
- The independent CPU softmax tests pass for F16 and Q8, partial causal pages, offsets, empty samples and 32K. The final kernel also passes Mean-K packing and pre-RoPE capture tests.
- All seven MTP state cases pass exact KV byte checks and recurrent/draft-state and Mean-K comparisons. A new test compares 32 serial accepted tokens with partially rejected MTP windows across the output sampling boundary; both publish two samples with matching page masses. Sampled/unsampled graph reuse, bounded Query replay and cancellation/reset pass.
- Two-lane native concurrency and isolation pass; both follow-ups reuse their own prefix (361 and 385 tokens). Functional state tests freeze expert placement; throughput tests do not. Tests use the 5050. No new architecture or other backend is changed.
- Build parallelism is eight. The final combined patch is checked against the canonical prepared tree with a separate temporary Git index.

## Isolated feedback-kernel timing

GPU-event timings below compare the previous deployed kernel with the final kernel; 20 calls after three warmups. These do not represent whole-model throughput.

| Context | Samples | Previous ms | New ms | Change |
|---:|---:|---:|---:|---:|
| 2048 | 1 | 0.02780 | 0.04639 | +66.9% |
| 2048 | 16 | 0.29545 | 0.23718 | -19.7% |
| 8192 | 1 | 0.06510 | 0.06879 | +5.7% |
| 8192 | 16 | 1.19969 | 0.97457 | -18.8% |
| 16384 | 1 | 0.11820 | 0.10724 | -9.3% |
| 16384 | 16 | 3.28097 | 2.76811 | -15.6% |
| 32768 | 1 | 0.23591 | 0.18845 | -20.1% |
| 32768 | 16 | 4.79880 | 4.00694 | -16.5% |

The one-sample 2K kernel becomes slower in isolation; the 32K path is the target of this change. The absolute extra time is small and only applies to sampled steps, but no complete short-context performance sweep is claimed. Four-head and eight-lane-per-head experiments were slower at 32K and were discarded; their diagnostic logs remain separate.

## Interpretation and limits

The pooled result is a measurement, not a guarantee that every individual request remains within 3%. The individual pairs show larger fluctuations. The machine has 32 GiB RAM shared with other applications; minimum available RAM can fall below 100 MiB and system-wide disk reads reach several GiB. Disk counters do not identify which process/file caused all reads. MTP accepted/offered counts and generated output IDs also differ between runs because adaptive CPU/GPU expert placement changes numerical paths. These factors limit attribution of small whole-model speed differences.
The kernel microbenchmark establishes a local reduction in feedback cost. The whole-model table uses the unadjusted native prompt/decode timers, with KVMem capture, history and checkpoints enabled. Model loading and HTTP/tokenization time are excluded. No time is subtracted and no MTP-normalized rate replaces generated-token throughput. Earlier 3.4%/3.7% gaps belong to the prior sweep and are not a fixed baseline for a new run.
This is synthetic throughput, not long-context recall quality. No 64K/128K performance or recall requalification is claimed here. CUDA on the 5050 is validated; HIP/SYCL are not validated by this local run. No valid Nsight trace is claimed.

An independently running ninfer CUDA build was confirmed during final inspection. Its Ninja process started at 10:00:58 and remained active, with eight compilation jobs, in pr171-memory-review-20261024/build. This overlaps pair3, the late previous-worker control and the UI check. The observation is a process lifetime snapshot, not a continuous CPU trace; it cannot quantify how much of the slowdown the build caused. No other task was stopped.
As a sensitivity view, pair1+pair2 (completed before that build started) pool to prefill -2.02% and decode +3.21% versus upstream. These are reported alongside all three pairs, not substituted to select a favorable outcome. Neither view is sufficient to certify a stable 3% bound under the observed RAM/IO variation.

## Loaded console validation

The tested binary is deployed locally and the original 5050 console configuration is restored. A fresh HTTP 32K request after a 2K warmup checks the real service and monitor. This is an integration check, not an additional paired comparison.

| Input | Prefill tok/s | Decode tok/s | Reused tokens | Extra native reads | System disk read GiB | Minimum free RAM MiB |
|---:|---:|---:|---:|---:|---:|---:|
| 32768 | 585.10 | 20.951 | 0 | 0 | 13.54 | 361.7 |

The loaded-service check is substantially slower than the raw paired workers and records high system-wide disk traffic. It establishes service correctness, not performance parity. No claim that the UI service consistently meets a 3% overhead bound follows from the pooled raw-worker comparison.

## Provenance

- Tested worker SHA256: `2967dee2c9e898f602dcf067979d4bea29be647ee02d50ae8eaa48124ee60ab8`.
- Previous worker SHA256: `1eba7a0f79a56ae8b1dfecb0b2ceddf25d3407be532da8ffeb90b6c4b24b3f4f`.
- Patch SHA256: `28a7743a2b355872d03e52124c23a8b1203b97e34944f2677a49ecf0c67f7dfc`; prepared tree `abd050da76aea2c55c7ad71396186cc2b4db73d6`.
- The upstream worker is unchanged. The runtime is rebuilt from the deployed native source plus the performance delta. That base predates two unrelated portable Query API adjustments in the canonical prepared tree; the delta applies cleanly to that newer tree, but this runtime is not claimed to rebuild that entire tree.
- The earlier report and packaged provenance are retained as historical evidence. Raw tokens, timings, commands, telemetry, final native sources and test logs accompany the local report. The repository changes were uncommitted at measurement time.
