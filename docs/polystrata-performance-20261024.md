# PolyStrata overhead optimization on RTX 5050, 2026-10-24

This report records the first optimization stage. Its measurements and binary
hashes are historical; see the [32K follow-up](polystrata-32k-performance-20261024.md)
for the subsequent kernel/graph changes and currently deployed worker.

Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL; RTX 5050 Laptop. Device identity is retained in local evidence. Tests follow the explicit 5050 request for this task.
Both paths use STRATA_FATTN=1 and STRATA_FDEC=1, Q8_0 KV, prompt batch 512, --stage-mib 2048 (stage-ahead remains the native default 1024 tokens), 16 CPU threads, 64 expert swaps, MTP3, CUDA graphs, and 256 MiB VRAM reserve. A 2048-input/512-output warmup is excluded from the measured table. Each measured request generates 512 tokens at temperature 0 with the exact same input IDs and no prefix reuse.
Native ceiling: 67584. KVMem logical ceiling: 262144; resident budget 32768, generation reserve 2048, Host payload quota 4096 MiB, retained sessions 4, Query max 512, replay max 2048. All measured prompts fit the resident capacity and report resident-only Query execution without replay. The harness explicitly disables native prompt/conversation caching in both commands; KVMem history reuse is separately enabled to preserve its configured history semantics. This compares the deployed KVMem feature set against upstream inference, not identical RAM layouts or retention functionality.

## Final worker comparison

Rates are tokens/s; delta is (KVMem / upstream - 1). Rows pool tokens and elapsed time across the selected runs. Results include history checkpoints and attention feedback, without subtracting their time.
Measured run directories: chunked-archive-pair1, chunked-archive-recheck.

| Input | Upstream prefill | KVMem prefill | Delta | Upstream decode | KVMem decode | Delta |
|---:|---:|---:|---:|---:|---:|---:|
| 2048 | 536.7 | 522.0 | -2.7% | 50.01 | 49.31 | -1.4% |
| 4096 | 657.8 | 638.8 | -2.9% | 48.87 | 44.83 | -8.2% |
| 8192 | 666.8 | 675.5 | +1.3% | 50.69 | 48.55 | -4.2% |
| 16384 | 664.7 | 656.6 | -1.2% | 47.34 | 47.55 | +0.4% |
| 32768 | 631.3 | 609.5 | -3.4% | 43.40 | 41.80 | -3.7% |

## Changes and evidence

- Replaced one-CTA-per-head full-window attention-feedback scans with page-parallel reductions and grouped-query key reuse. The complete causal normalization, accepted-row boundary, layer/head averaging, Q8 and F16 support remain. Scratch reuses the existing attention workspace instead of reserving more expert VRAM. Unsupported shapes and small scratch fall back to the old kernel.
- GPU-event microbenchmark at 32K: 1 sample 8.08302 -> 0.22843 ms; 16 samples 38.23118 -> 4.86106 ms. These are isolated capture-kernel measurements, not whole-model speedups. The previous actual-UI 32K prefill was 499.85 tok/s with attention acceleration already enabled; subsequent native changes address the remaining feedback and memory overhead. Historical and current runs have different page-cache state, so this is descriptive progress, not a clean causal A/B percentage.
- Native spill/restore buffers now allocate on demand. History archiving uses dirty 4096-row slices (46.75 MiB for this model) instead of a full B+R buffer (397.375 MiB each). Full-resident execution does not allocate the restore buffer. The 88 MiB bounded-Query host workspace is allocated only when needed. Compared with the old eager path, archive-only execution avoids about 836 MiB of pinned RAM; later view exchanges can still allocate the full buffers.
- No changes to the history quota, retention count, attention sampling rate, selection policy, KV precision, MTP acceptance or Query protocol were used to obtain these results.

## Validation

- CUDA build with 8 jobs. Capture tests compare against independent CPU causal softmax for F16/Q8, multiple shapes including 32K, partial pages, dynamic CUDA-graph coordinates, offsets and no-sample execution. Mean-K, pre-RoPE capture and packing tests pass.
- MTP selected acceptance case passes exact native KV bytes and recurrent/draft state and Mean-K comparisons, plus sparse view transfers, bounded Query replay and cancellation/reset. STRATA_FREEZE_EXPERTS=1 is used only for state-equivalence tests to hold CPU/GPU summation placement fixed; performance runs use ordinary adaptive experts.
- A separate 4161-token archive test crosses the slice boundary, compares every archived K/V plane to native GPU bytes, updates the partial tail, and verifies the pinned buffer remains 4096 rows. It passes.
- Versioned patch applies cleanly to the prepared repository tree using a separate temporary Git index. Launcher default-on and explicit-off assertions and Windows DryRun were validated in the preceding flag-enablement phase.

## Variability and remaining limitations

- This is synthetic coding throughput, not a recall-quality comparison. Generated tokens and MTP accepted/offered counts can differ because adaptive CPU/GPU expert execution changes numerical paths. Raw output IDs and counters are saved.
- System RAM is 32 GiB and is shared with other applications. One pre-slice 32K KVMem recheck reached 14.7 MiB available RAM and 8.41 GiB of system disk reads, with decode falling to 31.72 tok/s. Disk counters are system-wide and are evidence of memory/IO pressure, not proof that every byte came from this worker. No valid Nsight trace was obtained; attribution uses code inspection, CUDA-event microbenchmarks and saved telemetry.
- Final run telemetry (one-second samples) is retained below. The 2K first measured prompt remains sensitive to initial file-cache/retained-history costs. Do not present the best run or a normalized MTP-step rate as the overall user-visible rate.
- The early baseline/optimized-pair1 diagnostics used --stage-mib 512, swaps 2 and a short warmup, with some build overlap. They are excluded from final comparisons. ui-settings-pair1 predates lazy host staging; lazy-staging-pair1/recheck predate bounded archive slices. These remain diagnostic history, not pooled into final numbers.
- 64K and 128K were measured in the preceding attention-switch sweep; the new native optimizations are not requalified there. Past results are in ../attention-acceleration/REPORT.md. No cross-backend KV transfer or additional architecture is introduced.

Remaining measured points slower by more than 5%: [{"input_tokens": 4096, "prefill_percent": -2.894346978557516, "decode_percent": -8.248429256332235}]

| Run | Input | Mode | Minimum free RAM MiB | System disk read GiB | MTP accepted/offered |
|---|---:|---|---:|---:|---:|
| chunked-archive-pair1 | 2048 | managed | 174.4 | 3.14 | 281/365 |
| chunked-archive-pair1 | 4096 | managed | 116.9 | 0.77 | 280/375 |
| chunked-archive-pair1 | 8192 | managed | 123.8 | 0.22 | 293/362 |
| chunked-archive-pair1 | 16384 | managed | 91.8 | 0.28 | 270/332 |
| chunked-archive-pair1 | 32768 | managed | 109.2 | 1.83 | 290/360 |
| chunked-archive-pair1 | 2048 | native | 208.2 | 2.14 | 286/348 |
| chunked-archive-pair1 | 4096 | native | 229.5 | 0.45 | 277/339 |
| chunked-archive-pair1 | 8192 | native | 443.7 | 0.26 | 303/364 |
| chunked-archive-pair1 | 16384 | native | 976.7 | 0.69 | 278/352 |
| chunked-archive-pair1 | 32768 | native | 527.5 | 0.77 | 237/337 |
| chunked-archive-recheck | 2048 | native | 231.8 | 1.31 | 239/306 |
| chunked-archive-recheck | 4096 | native | 209.0 | 0.26 | 305/384 |
| chunked-archive-recheck | 32768 | native | 65.3 | 1.62 | 245/326 |
| chunked-archive-recheck | 2048 | managed | 203.4 | 1.42 | 252/318 |
| chunked-archive-recheck | 4096 | managed | 261.9 | 0.28 | 274/337 |
| chunked-archive-recheck | 32768 | managed | 427.0 | 0.79 | 267/352 |

First-use warmups (not OS-cold tests, and not included in the primary rates):

| Run | Mode | Prefill tok/s | Decode tok/s |
|---|---|---:|---:|
| chunked-archive-pair1 | managed | 315.8 | 37.92 |
| chunked-archive-pair1 | native | 296.2 | 36.03 |
| chunked-archive-recheck | native | 306.3 | 40.09 |
| chunked-archive-recheck | managed | 256.7 | 38.60 |

The reverse-order recheck also enables the existing KVMEM_POLY_PROFILE phase timer; its native logs preserve those timings. This lightweight logging is not a CUDA trace.

## 4K diagnostic with MTP disabled in both paths

This does not replace the MTP3 results or change the launcher defaults. It removes speculative acceptance counts as a source of variation, while also removing draft-block work and allocations; it is a diagnostic, not an exact isolation of all adapter cost.

| Mode | Prefill tok/s | Decode tok/s |
|---|---:|---:|
| managed | 657.2 | 38.42 |
| native | 648.9 | 38.78 |

## Actual console service validation

The optimized binary is deployed locally and the original 5050 console configuration is restored, including MTP3, thinking defaults, history retention, 32K budget and 2K generation reserve. The worker environment confirms both attention switches enabled. One 32K request is run through the loaded HTTP service after a 2K warmup; this is an integration check, not another paired upstream comparison.

| Input | Prefill tok/s | Decode tok/s | Reused tokens | Extra native reads |
|---:|---:|---:|---:|---:|
| 32768 | 607.6 | 42.09 | 0 | 0 |

## Provenance

- Current worker SHA256: `1eba7a0f79a56ae8b1dfecb0b2ceddf25d3407be532da8ffeb90b6c4b24b3f4f`.
- Previous deployed worker SHA256: `ea92dbcd059d0ddfa37b42a942f7b97d56291bd604052a1a2e866f8492d882cd`.
- Repository performance patch SHA256: `de5e3bb2b74032b6879e549bf63a5080fe008100c0045469e8e590fd49a9a71b`; prepared tree `f0a715ade6cc649cba69c0e8c254b9bd0adbf5e8`.
- The runtime was rebuilt from the existing deployed native source plus this performance delta. That source predates two unrelated portable Query API adjustments in the repository prepared tree. The delta is verified against the newer prepared tree, but the runtime is not claimed to be a rebuild of that entire tree. Both API-baseline differences are checked explicitly by prepare_attention_patch.py.
- The upstream reference binary is unchanged. The repository changes were uncommitted at measurement time. Input IDs, raw results, native command lines, RAM/GPU telemetry, tests and scripts are retained with the local provenance.
