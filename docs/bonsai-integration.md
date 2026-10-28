# Bonsai 2 integration

This development change integrates Bonsai into the existing `llamacpp` and `ninfer`
workers. It adds no Prism backend, model-specific serving protocol, or ingestion state.
The initial model scope is Ternary Bonsai 2 27B text with an optional matching r3 MTP
head. Other Bonsai sizes, vision, ROCm, and multi-GPU are not qualified by this work.

## Source ownership and removable patches

The upstream gitlinks remain pinned. Bonsai layers are applied over the current
KVMem integration, including the native prefix admission repair and CPU vision
lifecycle. The ordered series in `backends/versions.json` is:

| Backend | Layers after the pinned upstream |
|---|---|
| llama.cpp | `kvmem`, `pq2`, `ptq1`, `ptq1-performance` |
| ninfer-all | `kvmem`, `ptq1`, `ptq1-performance`, `t5-performance` |

Every layer has a SHA-256, explicit dependencies and exact resulting Git tree.
`scripts/prepare-backends.py` validates the complete series in a temporary index
before applying it, preserves the real index, recognizes only the base and declared
intermediate trees, and refuses unknown local edits. Repeated preparation is a no-op.
Build metadata includes the complete source manifest rather than one patch hash.

llama.cpp retains upstream's Bonsai model graph, Hadamard transforms, embedding
inverse transform and MTP implementation. The format patches add the published
PQ2 (GGUF type 142, group 128, 34-byte block) and PTQ1 (type 143, group 128,
28-byte block) codecs, CPU references, CUDA conversion/get-rows/matvec/MMQ and
type registration. PQ2's file-type IDs 141 and legacy 142 remain readable; a
file-type ID and a tensor quantization ID are distinct namespaces.

The separate llama.cpp performance patch imports the previous Prism planar Q8
activation / PTQ1 matvec implementation, including its PDL producer-consumer
ordering fix. It is restricted to dense CUDA 2D calls with 1–4 columns and a
supported fusion. Other layouts retain the generic path. Set
`GGML_CUDA_PTQ1_FAST=0` for a diagnostic comparison against that path. No global
batch-invariant attention or unrelated kernel changes are imported.

ninfer-all already imports PQ2/PTQ1 GGUF through its T2 representation. Its new
PTQ1 patch preserves compact 24-byte low, 2-byte high and 2-byte scale planes
instead, including exact row selection/reordering and FP32 embedding decoding
before the final BF16 conversion. The performance layer enables PTQ1 A8 prefill
on SM120 without changing `NINFER_SM120_NATIVE` or other formats' policies.
The existing `--no-prefill-a8` setting still selects A16. PTQ1 uses independent
dispatch and `ptq1_i8_tile/<N>x<K>` calibration keys; T2 profiles and environment
overrides cannot select a PTQ1 packed-layout kernel. Decode and MTP widths below
33 retain the A16 path; wide PTQ1 calls use the compact INT8 tile when permitted.

The native prefix-summary repair was merged independently in PR #175 and is
already in the first KVMem layer. It is not applied twice. Format and optimization
patches can be retired separately.

## Models

Use ordinary GGUF input with the llama.cpp worker, or the existing converter's
`bonsai2_27b_ptq1` recipe with ninfer-all. The native artifact keeps its standard
container and optional MTP component. T2 artifacts remain supported separately.

`scripts/bonsai-mtp-graft.py` appends the pinned r3 head to the pinned PTQ1 GGUF.
It verifies both input hashes, checks all 851 base tensors byte-for-byte, writes
15 MTP tensors, and records output provenance. `--head-type Q4_0 --norm-type F32`
is the 8GB recipe; it quantizes only MTP matrices. The matching model/head license
and notice must accompany redistributed model artifacts. Model files are not
part of the source repository or worker package.

Prepare sources with `python scripts/prepare-backends.py --backend all`, then use
the existing [backend build wrappers](multi-backend-repository.md). Install the
local GGUF package with `python -m pip install ./backends/llamacpp/gguf-py` to obtain
its dependencies, including NumPy and PyYAML. The conversion environment qualified here is
Python 3.12.13. For the llama.cpp MTP artifact:

```sh
python scripts/bonsai-mtp-graft.py --base /models/Ternary-Bonsai-2-27B-PTQ1_0.gguf \
  --head /models/Bonsai2-r3-MTP/model_mtp.safetensors \
  --head-type Q4_0 --norm-type F32 --output /models/Bonsai2-PTQ1-r3-Q4-MTP.gguf
```

The qualified GGUF is 6,185,633,120 bytes, SHA-256
`4b0e0f9b252d1b77ace4e197f913dbb8729a13c2a0174d4eb8b2b80c22918832`.
For native artifacts, follow the existing
[Bonsai converter instructions](../backends/ninfer/docs/weight-conversion.md#ternary-bonsai-2-27b)
with `bonsai2_27b_ptq1`; select the matching head explicitly.

## Historical development qualification

The capacity and performance tables below record the original local development
worker, whose KVMem layer also contained the separate NVFP4 fork. The merge port
uses the current mainline KVMem layer and excludes that fork. These historical
measurements are not new measurements of the merged combination. Merge validation
and any corrections found during review are recorded in the merge PR.

## 8GB qualification

Device: RTX 5050 Laptop, UUID `GPU-A`,
8,151 MiB reported total, approximately 7,964 MiB initially available. Capacity
runs use this device exclusively. Timings are functional diagnostics, not a
cross-GPU performance comparison. Performance comparisons use the RTX 5060 Ti.

Shared settings: one active lane, PTQ1 main weights, r3 Q4 MTP loaded and enabled,
three draft tokens, at least four-bit K and V, prefill 256, logical context 131,072,
explicit selected-history budget B and separate append reserve R=2,048. KVMem's
ordinary Query/Pressure policy and replay remain unchanged. No CPU model-layer
offload is requested. Host history storage is a separate 4GiB quota.

| Backend / configuration | Status |
|---|---|
| llama.cpp, B=20,480, Q4_0 K/V and draft K/V | Complete long-request pass: 22,011 prompt tokens, neutral acknowledgement, separate recall question, then 235-token continuation. Peak dedicated GPU memory about 7,100 MiB. |
| llama.cpp, B=49,152, same configuration | Complete long-request pass: 50,690 prompt tokens, separate recall question and 256-token continuation. About 7,840 MiB dedicated after long requests. |
| llama.cpp, B=53,248, same configuration | Complete long-request pass: 54,787 prompt tokens, separate recall question and 253-token continuation. Peak `nvidia-smi` usage 7,890 MiB. |
| llama.cpp, B=65,536, same configuration | Startup and short generation only; about 7,908 MiB dedicated after the short request. Insufficient evidence for a long-request recommendation. |
| ninfer-all, B=20,480, RK4V4, FP16 GDN state | Complete long-request pass: 22,011 prompt tokens, neutral acknowledgement, separate recall question and 253-token continuation. Peak `nvidia-smi` usage 6,630 MiB. |
| ninfer-all, B=45,056, same configuration | Complete long-request pass: 46,593 prompt tokens, separate recall question and 241-token continuation. Peak `nvidia-smi` usage 7,080 MiB. |
| ninfer-all, B=49,152, same configuration | Startup rejected by the memory planner: required runtime 1,327,859,968 bytes versus available 1,297,674,240 bytes (28.8 MiB short). |

The 20K question reused 22,013 prefix tokens, and native tracing reports its own
31-token Query feature span plus 39 replay rows. All three reference codes were
recovered. The document and question are ordinary separate requests; the later
continuation is a correlated probe, not an independent quality sample. MTP
accepted 16 of 18 drafted tokens during the question and 140 of 288 during the
continuation. B excludes R: the 20K main pool is 22,528 cells (396 MiB), and the
draft pool has the same cell count (24.75 MiB).

Windows dedicated/shared GPU counters are recorded along with `nvidia-smi`.
Shared usage includes driver/host mappings and cannot by itself prove paging;
the 20K run reported about 432 MiB shared after long requests. A successful
allocation or short answer alone is not treated as a fully resident long-context
upper bound. Record loading, filled-pool execution, OOM and paging observations
separately when increasing B. For this model and Q4_0, main plus MTP KV costs
19,584 bytes per additional physical token, before workspaces and allocator growth.
At 52K the Windows counters reported 7,889 MiB dedicated and 576 MiB shared. Thus
52K is the highest completed long-request configuration in this measurement, not
a proof that every workload is resident at that budget. Use additional headroom
for a practical default. The interrupted first 48K attempt overlapped an external
GPU service and is excluded; the successful rerun monitored process exclusivity.

ninfer's 44K question reused 46,595 prefix tokens and recovered all three codes.
The native planner recorded Query span [46600,46630), 30 tokens, on that separate
question; this was an `all_resident` request with zero replay rows and no Query
capture. It validates the request boundary and capacity, not cold-history recall
quality. The later continuation accepted 131/333 MTP draft tokens. Windows reported
about 7,081 MiB dedicated and 770 MiB shared after the question. FP16 recurrent
GDN state is part of this recipe; the same budget is not claimed for FP32 state.
44K is the highest completed native budget; 48K failed under the same policy.
Intermediate budgets were not exhaustively searched. Both backends exceed the
20K goal with the MTP head loaded and active and four-bit K/V.

The completed native capacity measurements precede the independent non-KVMem
summary fix. Extra 20K runs with the final worker were excluded: two were stopped
by the exclusivity guard when unrelated Polystrata test processes appeared, and
one exited during startup. They are not additional capacity successes or budget
bounds. The final worker separately passes native-cache restoration and the
paired A16/A8 comparison. The original port-18200 Polystrata service was restored
with an identical saved configuration and a successful health response. Port
18500 exited independently and was never stopped by this task.

Requests, responses, source/model identities and rejected/contaminated attempts
are indexed in [the measurement record](benchmarks/bonsai-20261023.json). B is a
selected-history budget, not total context or B+R physical capacity. Keep margin
below the measured upper configuration for larger prefill batches, concurrent
lanes, or other applications. These runs do not establish general recall quality.

## Inherited native prefix-cache defect

With KVMem disabled, an ordinary follow-up that reused a native hybrid prefix
returned HTTP 500, `runtime Begin summary differs from committed admission`.
The same unchanged T2 artifact reproduced it in the pre-Bonsai worker and in the
new worker. It originates in the existing KVMem integration: prefill now reports
`reported_reuse_path`, and ordinary materialization initializes that field, but
hybrid materialization left its default `Root` despite admitting a Device/Host
reuse. Thus the engine correctly rejected inconsistent admission metadata.

The user authorized repair on 2026-10-23. The independent patch initializes the
reported path from the committed quote and retains the strict engine invariant.
The original T2 reproduction now returns the correct access code with 2,594
cached tokens. PTQ1 Device-only turns and Device/Host restoration with both normal
decoding and the ordinary full MTP head pass. The regression checks a nonzero,
block-aligned frontier inside the actual common prefix, matching Device/Host
frontiers, real Host image/block restore counters and generated tokens.

The older `restore-exact` fixture assumed frontier 1,024 but this artifact/runtime
captured 1,152; its MTP variant explicitly required an absent optimized head.
Those failures are preserved as fixture incompatibilities. A new
`restore-admission` scenario exercises the applicable invariants with the normal
head; the older fixture tests are unchanged. No native cache placement policy or
KVMem Query/Pressure behavior is altered by the fix.

## Measured optimization

On RTX 5060 Ti `GPU-B`, `llama-bench` ran
PTQ1 main weights, all GPU layers, Q4_0 K/V, flash attention, batch/microbatch 256,
four CPU threads and polling disabled. Each run repeated PP512/TG128 three times;
the order was generic, optimized, optimized, generic. This tests main-model
decoding without MTP or KVMem. ninfer compilation ran concurrently on the CPU.

| PTQ1 matvec | TG128 mean (token/s) | Individual run means |
|---|---:|---|
| Generic (`GGML_CUDA_PTQ1_FAST=0`) | 26.00 | 25.90, 26.10 |
| Planar optimization (default) | 46.32 | 45.97, 46.66 |

The measured decode increase is about 78%. PP512 varied from 466 to 492 token/s
in both paths; this experiment establishes no prefill improvement. Do not apply
these rates to the 5050 capacity runs or to MTP throughput.

On the same 5060 Ti, the native worker compared A16 (`--no-prefill-a8`) against
the new A8 path in the order A16, A8, A8, A16, with three document/question pairs
per run. PTQ1 main weights, Q4 MTP with three drafts, RK4V4, FP16 GDN state,
prefill 256, capacity 8,192, host cache zero and device profile off were fixed;
KVMem was disabled. Each document computed 2,603 tokens from a fresh prefix,
and the separate follow-up reused its prefix and returned the correct code.

| Native PTQ1 prefill | Mean token/s (six documents) | Mean prefill seconds |
|---|---:|---:|
| A16 | 345.04 | 7.547 |
| A8 | 667.88 | 3.902 |

This is a 94% increase in measured prefill rate. Excluding each process's first
document gives 349.63 versus 684.26 token/s. Rates use the worker's computed-token
count and prefill timer, not client HTTP time. No decode improvement is claimed
from these short follow-ups. GPU process checks found no competing 5060 process;
other CPU compilation and some 5050 functional work overlapped, so this is a
local paired measurement rather than an isolated system-wide benchmark.

## Correctness and retirement gates

`kvmem-bonsai-quant-test` independently constructs format bytes and expected
elements, exercises all canonical trit codewords, checks exact GET_ROWS and FP64
matrix products, and covers row/K tails and token-width boundaries. Its CUDA
mode is `--cuda`; changing inputs between repeated graph executions checks reuse
and ordering. Separate fused SwiGLU cases check the FP64 composed operation.
CPU, generic CUDA and optimized CUDA checks pass, including full model dimensions.
CUDA memcheck reports zero memory errors. API diagnostics separately reported
`cudaErrorGraphExecUpdateFailure` when graph topology changed; the existing
upstream path clears that status and reinstantiates the graph. The memory check
therefore disables API-error reporting after verifying that fallback.
ninfer's PTQ1 bundle includes artifact, A16/A8 linear, residual,
SwiGLU, attention/GDN projection and Hadamard tests; Python conversion tests
cover exact bytes, scale bits and selected/reordered rows.

When upstream implements support, do not silently skip a patch because a symbol
has the same name. Adopt a deliberately pinned commit, verify wire IDs/layout,
Hadamard/sign semantics, MTP, numerical oracles, KVMem state and 8GB behavior, then
remove only the superseded layers. Rebase later layers against that exact tree
and update dependencies/hashes in the same change. Upstream format support may
replace the format patch while a measured local optimization remains useful;
upstream faster kernels may replace the optimization independently. A fresh
base-to-final replay and a build without the removed layer are required. This
makes conflicts explicit during preparation rather than hiding them at runtime.


## Optional JGamboa T5 acceleration layer

This extension ports the T5 implementation from
[JGamboa/ninfer-4090-windows](https://github.com/JGamboa/ninfer-4090-windows/tree/e905787f067de89ef6ef28c52d65a2ce91d425ca),
pinned at `e905787f067de89ef6ef28c52d65a2ce91d425ca`, into the existing native
backend. It introduces neither a Prism engine nor a new KVMem protocol. The
existing llama.cpp PTQ1 optimization and native PTQ1 fallback remain available.

T5 uses the same ternary integers and FP16 group scales as PTQ1: 26 code bytes
plus two scale bytes per 128 weights (1.75 bits/weight). Its byte order improves
parallel base-3 decoding. The new native artifact explicitly allows per-group
INT8 activation quantization. Consequently weights are preserved exactly while
activation rounding and generated text can differ from PTQ1's BF16 decode path.
Do not call this an equal-arithmetic kernel replacement or infer quality parity
from speed alone.

Integration follows existing ownership: artifact code defines packing and
conversion; ops own activation scratch and CUDA routes; model Uses retain
Hadamard signs and execution permission. The two-parent projections preserve the
T5 permission, write attention destinations directly, and support GDN's strided
qkv destinations. Existing RMSNorm/rotation and SwiGLU/rotation producers retain
their BF16 boundary. The donor's fully fused FP32 producer/rotation/quantization
path and its 4090 wave-cost model are not copied. All new weight storage stays
compact; no full-model expansion or load-time repacking occurs.

Decode widths 1..4 use dp4a, widths 5..32 use integer tensor cores, and larger
batches use prefill GEMM tiles. This covers ordinary decode and the token counts
used by MTP verification. The donor's shape thresholds and prefetch settings are
starting defaults; this qualification does not claim optimal tuning on other
architectures. Only the recorded SM120 builds/devices are runtime-qualified.

From the prepared native backend directory, use the existing conversion Python
environment and run:

```powershell
python -m tools.convert.repack_t5 INPUT-PTQ1.ninfer OUTPUT-T5.ninfer
```

The command creates a distinct artifact and a `.ninfer.repack.json` verification
report. It preserves bindings, signs, components and all non-PTQ1 objects
(including the existing Q4 MTP) and verifies the persisted output. The
`bonsai2_27b_t5` recipe also supports conversion from original Bonsai sources.
Select the new artifact in the existing `ninfer-all` backend. T5 is A8-only;
rollback selects the original PTQ1 artifact. No automatic migration or default
switch is implied.

The optional layer is exported separately as `ninfer-t5-performance.patch` after
`ptq1-performance` and the current KVMem layer. It is pinned to the exact preceding source tree and
must not silently skip conflicts. If upstream later adds T5, compare byte layout,
activation/rotation semantics, MTP behavior and tests, then replace this layer
and update the recorded tree/hash. Keeping the original PTQ1 artifact makes the
runtime rollback independent of that upstream migration.

### T5 qualification and throughput, 2026-10-23

The existing PTQ1/Q4-MTP artifact was repacked with 322 T5 objects and 472
byte-identical copied objects. Persisted ternary integers and FP16 scale bits
match the source hashes. Both files occupy 6,182,359,808 bytes. The shared target
vocabulary head is converted to T5, so MTP computation can change even though
its separate Q4 weight objects are unchanged.

The SM120a build used CUDA 13.2 and eight compilation jobs. The independent
FP64 CUDA oracle passed real model dimensions, route boundaries, graph input
changes, split attention/GDN destinations, residual/SwiGLU fusion, embeddings
and a 248,320-row vocabulary head. A8 projection criteria are relative L2
0.04 and gross absolute/relative-to-maximum-reference bounds of 1/256 and 0.06;
these are acceptance criteria, not measured maximum errors. Compute Sanitizer
memcheck reported zero errors. All 12 existing PTQ1/T2 C++ suites passed;
56 Python artifact/conversion tests passed under Python 3.11.15. Only these
SM120 devices are runtime-qualified.

The performance comparison uses the RTX 5060 Ti
(`GPU-B`) and the **same new worker** for
both artifacts. Thus the baseline is the preceding native PTQ1 route with its
A8 prefill optimization, not upstream ninfer or llama.cpp. Capacity/context
8,192, concurrency one, host cache zero, prefill chunk 256, RK4V4, FP16 GDN,
device profile off, greedy sampling, no thinking and no KVMem are fixed.

| Mode | Original PTQ1 decode | T5 decode | Increase | PTQ1 prefill | T5 prefill |
|---|---:|---:|---:|---:|---:|
| MTP, three drafts | 66.53 tok/s | 109.80 tok/s | 65.0% | 664.38 tok/s | 1,042.02 tok/s |
| No MTP | 26.09 tok/s | 61.67 tok/s | 136.4% | 670.40 tok/s | 1,045.78 tok/s |

MTP runs used PTQ1, T5, T5, PTQ1 order, with three repetitions per run (six
256-token generations per artifact). Individual decode rates ranged from
64.62 to 67.89 for PTQ1 and 106.29 to 111.68 for T5. The secondary no-MTP
comparison used PTQ1 then T5, three repetitions each. Decode is
`sum(completion_tokens - 1) / sum(native decode_seconds)` because prefill
emits the first output token. Prefill uses 2,603 computed document tokens per
request, separately from the question turn. The MTP prefill increase is 56.8%.
Acceptance was 72.14% versus 72.78%, with 2.161 versus 2.174 accepted tokens
per round. No other compute process occupied the measured GPU.

All 18 short document-first recall probes returned the correct code, with
2,594 prefix tokens reused on each question turn. The three matched MTP decode
texts were identical across PTQ1/T5. No-MTP texts differed, and some MTP/plain
texts differed even within the original PTQ1 path. Numerical oracles, rather
than legacy token identity, establish kernel correctness. These few probes
do not establish general quality parity after activation quantization.

The JSON report is `docs/benchmarks/bonsai-t5-20261023.json`; raw commands,
worker hash, requests, responses, native timings and process checks are retained
under `performance-t5-5060ti` and `performance-t5-no-mtp-5060ti` in the report's
artifact root. A failed no-MTP startup that incorrectly supplied `--draft-tokens`
is excluded; the corrected no-MTP runs are recorded separately. The rates above
are short-context results and are not predictions for the 8GB long-context runs.

### T5 on the 8GB RTX 5050 Laptop

Capacity qualification used `GPU-A`, the
same T5/Q4-MTP artifact, RK4V4, FP16 GDN, prefill 256, host cache 4,096 MiB,
MTP three drafts, one lane and a separate append reserve of 2,048. These are
functional capacity runs; their timings are diagnostic, not performance comparisons.

| Selected-history budget B | Result |
|---|---|
| 20,480 (20K) | Full pass: 22,011-token document, neutral acknowledgement, independent three-code question, then a 242-token continuation. |
| 47,104 (46K) | Full pass: 48,650-token document, neutral acknowledgement, independent three-code question, then a 233-token continuation. |
| 48,128 (47K) | Planner rejected startup: runtime reservation short by 10,586,368 bytes (10.10 MiB). |
| 49,152 (48K) | Planner rejected startup: runtime reservation short by 30,185,728 bytes (28.79 MiB). |

Both completed runs recovered all three codes. The 20K question reused 22,013
prefix tokens and recorded native Query span [22018,22048). The 46K question
reused 48,652 tokens and recorded [48657,48687). Both were `all_resident` with
zero replay rows and no Query capture, so this establishes the separate-question
boundary and filled-pool execution, not cold-history recall quality.

At 46K, physical KV capacity was 49,152 including reserve. Startup recorded
85.6 MiB free and only 8.60 MiB planner slack; the highest observed `nvidia-smi`
usage was 7,116 MiB. Windows reported about 7,117 MiB dedicated and 770 MiB shared
after the long requests (20K: about 6,631 MiB dedicated and the same shared
amount). Shared counters include driver and pinned-host mappings; these readings
alone cannot prove or disprove WDDM paging. The measured ceiling under this
planner/configuration lies between 47,104 and 48,128 selected tokens. Intermediate
budgets were not searched, and the 46K result is a boundary sample, not a default
with generous headroom. The requested 20K target is met with MTP active and
four-bit K/V. Earlier PTQ1 capacity numbers above remain historical baseline data.

The original Polystrata service on port 18200 was restored with an exact match
to its saved configuration and a successful health check. Its package was missing
the `model_health.py` module referenced by the launcher; the corresponding source
module was copied into the package to make restoration work. The separate 18500
service was not running at the start and was not touched.

The historical development series replayed from its pinned bases with the real
indexes preserved. The historical patch-preparation and whitespace checks passed.
