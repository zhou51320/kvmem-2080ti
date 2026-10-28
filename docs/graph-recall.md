# Graph recall C

Current source builds default to C for KVMem retrieval in both llama.cpp and NInfer; the scoring options below also expose Mean-Q cosine and normalized GQA max.
The resource recipe in `start-ninfer.ps1` still follows 0.18: context 204800,
B=36864, R=16384, host KV=12288 MiB, prefill=256, sink/recent=12288/1024,
adaptive MTP4 and ngram31/minimum12. Published 0.18 binaries retain their old
LRFU/max ranking. Rebuild both backend patches when using this source.

The implementation follows the C configuration of
[kvmem-vllm at 8560331](https://github.com/Di-Chai/kvmem-vllm/tree/8560331de4d9a889a1e884fe238b91e957a5958d).
This reference is an algorithm source; its model precision, batching and MTP
implementation are not copied into the two native engines.

## Query recall

For every actual query token, active full-attention layer and query head,
compute scaled dot products against the corresponding KV head's Mean-K pages,
then softmax over the eligible page domain. Average layers/heads and sum token
scores. Q and Mean-K have an explicit FP16 operand boundary. Recurrent layers
are absent, not zero-valued attention layers. This is the default mean-head
configuration; optional scoring variants are defined under Startup scoring options.

The denominator mask and graph transport mask are separate. The latter removes
the exact fixed set reserved by the picker, including mandatory media groups.
Mandatory blocks do not automatically disappear from the softmax denominator.
Sink/recent denominator masking follows the reference capacity conditions.
Both backends bound the query span to its last 512 tokens by default.
The native option is `--kvmem-query-max-tokens`.

After the initial score, perform one simultaneous propagation step with
alpha=0.2: at most 0.05 to each adjacent block and 0.1 through outgoing attention
edges. The remaining mass stays at the source. Missing local destinations keep
their mass at the source; surviving attention destinations are renormalized.
There is no second hop and no replacement of filtered top-four destinations.
Cross-message local edges receive a coefficient of 0.25; unknown boundaries
use 1. Message boundaries come from structured request/template metadata. Text
inside a user message cannot introduce a trusted boundary. Unsupported llama
templates and media use the unknown-boundary fallback.

An attention edge **source -> target** means sampled query tokens in the source
block attended to K tokens in the target block. All sampled rows of a source
are aggregated before selecting its four strongest past destinations. Self and
future edges are removed. A source row is sealed only once the complete source
block has been accepted. An unfinished source retains its raw accumulation.
Node identities are original logical block starts, never recycled native slots.

## Unified request and replay policy

Both backends distinguish new user Query recall from ordinary tool continuation.
Tool continuation (including a cold/restored session) appends normally and uses
resident-only Pressure when necessary. It does not import old Q to recall cold
history, probe tool text as Q, or replay the user-to-tool history. There is no
document-ingestion mode or new protocol field.

Query scoring binds the checkpoint immediately before the bounded Q: its exact
Mean-K boundary (including a partial page), graph and recoverable history domain.
The probe suffix is append workspace, not an additional history candidate domain.
Both backends share range/admission rules in `kvmem/query_policy.hpp`.

Continuation classification uses the verified input boundary, independently of
cached Q features. Q capture is enabled only for an admitted Query consumer;
tools, a proved complete resident view and refused replay do not copy/store
unused per-token Q. Mean-K and native attention feedback remain enabled.
NInfer includes native draft workspace and prior compaction in the resident
proof, so a prompt smaller than B+R can still require Query recall.

NInfer recycles at most two released checkpoint StateImage buffers per Program.
Live/aliased snapshots retain their own leases, each reused buffer is cleared
before capture, and idle capacity is included in `checkpoint_bytes`. The cache
reuses only existing buffers; it does not preallocate two additional snapshots.

`--kvmem-query-max-tokens` defaults to 512. Independently,
`--kvmem-query-replay-max-tokens` defaults to 2048 and bounds the complete causal
suffix from the actual recovery point, including template/media alignment.
Admission also checks selected prefix pages plus appended pages and native MTP
workspace against actual device capacity. Output length is not a replay budget.

Plan and admit before changing native selection or recurrent/MTP state. A proved
resident/unchanged view retains the first pass. A refused replay preserves its
complete first-pass view, state and logits. Admitted replay uses a fixed historical
selection: batching is allowed, Pressure during Query replay is forbidden. Mean-K
is rebuilt from the prefix and the probe's graph/feedback suffix is discarded;
replay advances accepted frontiers without publishing duplicate observations.
Failures/cancellation restore a verified checkpoint or invalidate the cache.

Quality comparisons use ordinary document-first requests followed by a separate
user question. Preserve the frozen original master; comparisons that share the new
workflow but replace the scorer must be labelled separately.

## Pressure and acceptance

`Query`, `PrefillPressure` and `DecodePressure` are explicit reasons. Only Query
can recall cold history or propagate through the graph. Pressure ranks the
accepted resident working set, including Query-selected old history and newly
generated blocks, by accumulated attention. Hard requirements stay mandatory.
Selection shrinks toward B and frees R for appending. R is not a generation
limit; the configured output/context limits still apply.

When there are N pending accepted output samples, pressure previews
`0.5 * previous_output_history + pending_mass / N`; with N=0, history is
unchanged. Preview neither commits nor decays stored history. If any output
score is positive across the candidate domain, output feedback is used;
otherwise prefill mass/exposure is used. This decision precedes fixed-set
filtering. Pressure never brings a cold block back into the resident domain.

Output samples occur every 16 accepted, actually consumed, non-thinking tokens.
Thinking delimiters are excluded. Prefill samples use interval 1024, stride32,
half-life8192 and the reference near-capacity condition (two chunks plus one
block of slack). Native Q is retained at the first/middle/last full-attention
layers, deduplicated for short layer slices. Page attention is token-log-sum-exp
followed by normalization across visible pages and head averaging. It retains
causal visibility and the actual native K representation/scales; it does not
apply softmax to Mean-K when measuring attention feedback.

Draft, rejected, replayed and cancelled suffixes do not publish reusable graph
or feedback contributions. A sampled emitted token only contributes when a
target forward has consumed it. Pending feedback, partial graph rows, parser
scope, accepted frontiers and sampling watermarks travel with checkpoints.
Restore validates these against the native accepted state before publication.

An NInfer reconstruction boundary inside an accepted MTP span first reads the
current verification K columns, then captures the temporary prefix. Returning
to the base restores graph, pending feedback and output sampling scope together
with native state and Mean-K, on success and on exceptions. The full span is
then committed once. Its temporary graph snapshot shares immutable chunks and
is charged to the existing statistics budget. llama.cpp complete sequence
removal resets the same conversation statistics as clear; internal Query replay
retains its explicit exemption from that full-history reset.

## Cost and ownership

- No full token-attention matrix is copied to the CPU. Native K stays in its
  backend layout. The new CUDA operations return only sampled page masses.
- NInfer reserves Q capture, logical maps, page LSE, normalizers and Query
  scratch in its startup memory plan. The Query scorer borrows that workspace;
  it does not allocate device buffers during a request. llama.cpp reuses a
  device-owned sampler/scorer workspace, with a generic GGML fallback.
- NInfer page-softmax captures the original normalized BF16 Q rows, packed from
  the intersection of the current chunk and the admitted Query span. Per-layer
  capture capacity is `min(prefill_chunk, max_context, query_max_tokens)`;
  the pinned host mirror uses the same BF16 layout. Expansion to FP32 on the
  host preserves represented values before the existing FP16 scoring boundary.
  This changes storage, not the configured Query range or per-token scoring.
  Mean-Q cosine instead retains one FP32 sum per layer/head/channel, with
  chronological additions across chunks and accepted-state reseeding before
  execution; it allocates neither token rows nor page-softmax scratch.
  Native post-RoPE attention-feedback capture remains separate.
- For 16 full-attention layers, 24 Q heads and head dimension 256, the default
  512-token Query limit requires 48 MiB of device Q capture at chunk 256 and
  96 MiB at chunks 512 or larger. The previous FP32 chunk-sized capture used
  96, 192 and 768 MiB at chunks 256, 512 and 2048 respectively. Mean-Q uses
  0.375 MiB for its device sum. These are Q-buffer sizes, not total process
  VRAM; native attention, KV, graph and execution workspaces have separate costs.
- Query logits are tiled over at most 64 rows and at most 64 MiB of scratch.
  The llama attention sampler bounds its tile estimate at 32 MiB. Sampling
  reads native K, so its cost still grows with visible tokens and sample count.
  This first delivery adds kernels; reusing native attention reductions remains
  a possible later optimization.
- Graph and feedback statistics remain process-local RAM metadata even when
  native KV spills. Each conversation budget is 32 MiB and the parent budget
  is 256 MiB across active lanes and retained histories. Every live snapshot
  is charged its full logical footprint, even for shared immutable chunks;
  this deliberately may reject earlier than unique physical memory usage.
  Statistics are not part of the native KV payload quota. Budget failure is
  explicit; the engine does not silently revert to another ranking policy.
- Query fixed blocks include the complete overlapping media group. Pressure
  requires its mandatory set to be resident/incoming and cannot fetch cold KV
  to satisfy a ranking accident.

### Startup scoring options

Both native servers accept the same options. They affect Query scoring only;
Pressure, bounded replay, tool append, accepted-token accounting and rollback use
the shared execution policy described above. Options are fixed for the engine's
lifetime; histories are engine-local and llama lane transfer checks the scorer,
aggregation, graph alpha and LRFU configuration before attaching a conversation.

| Option | Values and default |
| --- | --- |
| `--kvmem-query-scorer` | `page-softmax` (default), `meanq-cosine` |
| `--kvmem-score-agg` | `mean`, `max`; omitted: page-softmax mean, meanq-cosine max |
| `--kvmem-lrfu` / `--no-kvmem-lrfu` | omitted: enabled only for meanq-cosine; explicit override wins regardless of scorer argument order |
| `--kvmem-lrfu-lambda` | [0,1], default 0.5; requires enabled LRFU |
| `--kvmem-lrfu-top-frac` | (0,1], default 0.9; requires enabled LRFU |
| `--kvmem-graph-alpha` | [0,1]; omitted: page-softmax 0.2, meanq-cosine 0; zero bypasses propagation |

Page-softmax max takes each head's page probabilities, the maximum within each
KV head's GQA group, then renormalizes that group over eligible pages. It averages
KV groups/layers and sums Query tokens. Mean-Q cosine first sums Query rows and
uses the existing cosine scorer: max compares individual Query heads; mean folds
heads within a GQA group before cosine.

Scorer, graph propagation and LRFU are independent controls. Both scorers follow
the same order: current Query scores, one-hop graph propagation, LRFU preview
(when enabled), then selection. History is committed once at accepted prefill,
not on probes, tool continuations or speculative rejected rows. Repeated probes
derive from the same committed history. Accumulated history is never a graph seed.

For signed cosine scores `s`, propagate only `p = max(s, 0)` and retain the
negative residual locally: `r = min(s, 0) + G_alpha(p)`. Thus negative evidence
cannot spread as negative attention mass, but its ordering is not erased by a
global clamp or offset. A negative page can receive positive evidence from
neighbors. Alpha zero returns `s` exactly; an all-nonpositive vector also remains
unchanged. Nonnegative page-softmax scores use the existing propagation formula.
Mandatory pages remain excluded from transport. LRFU consumes `r` using its
hit-fraction and decay rules. The fraction gates positive reinforcement only;
negative evidence is retained for every page, including non-hits. In symbols,
`H_t = lambda * H_(t-1) + min(r,0) + hit(r) * max(r,0)`.
Threshold ties are included. This avoids promoting an unselected negative page
above a selected less-negative page solely by replacing its increment with zero.
History represents relevance to previous queries, not just the current one.
Page-softmax still sums Query tokens, so
longer queries can contribute more history mass; enabling LRFU does not add a
new per-query normalization or make scores comparable between scorers.

Selecting a scorer alone does not enable KVMem; use `--kvmem` or a positive budget.
Startup diagnostics print the resolved configuration.

```text
--kvmem-query-scorer meanq-cosine --kvmem-score-agg max --kvmem-lrfu
--kvmem-query-scorer meanq-cosine --kvmem-score-agg max --no-kvmem-lrfu
--kvmem-query-scorer page-softmax --kvmem-score-agg mean --kvmem-graph-alpha 0.2
--kvmem-query-scorer page-softmax --kvmem-score-agg max --kvmem-graph-alpha 0.2
--kvmem-query-scorer page-softmax --kvmem-graph-alpha 0.2 --kvmem-lrfu
--kvmem-query-scorer meanq-cosine --kvmem-graph-alpha 0.2 --kvmem-lrfu
```

Windows launchers expose `-QueryScorer`, `-ScoreAggregation`, `-GraphAlpha`,
`-Lrfu`/`-NoLrfu`, `-LrfuLambda` and `-LrfuTopFraction` with the same defaults.
The graphical launcher's Cache & history advanced section exposes these controls
and the Query/replay limits. Its default recipe remains C; selecting another
scorer does not toggle the independently editable graph and LRFU settings.
Legacy signed-cosine templates migrate to explicit controls, including disabled
LRFU. Preview checks the worker's model-free `--kvmem-capabilities` contract;
runtime details and monitoring show the native startup report separately from
the editable draft. Published performance tables describe their original
release, not these variants.

## Focused qualification

### NInfer Query storage, 2026-10-24

Compared against PR171 (`0512b6d`, its pinned NInfer integration patch), using
Windows, MSVC 19.44, CUDA 13.2, Release sm120a and RTX 5060 Ti
(device UUID retained in local evidence). The 27B GSQ-RCO IQ3 model used
NVFP4 KV, context 204800, B=36864, R=16384, chunk 2048, Query limit 512,
one lane and no speculation. Scorer settings were identical within each pair.

| Scorer | Native runtime reservation, before / after | Saving | Driver startup VRAM, before / after |
| --- | --- | --- | --- |
| page-softmax | 2430.61 / 1758.61 MiB | 672 MiB | 13756 / 13084 MiB |
| Mean-Q cosine | 2430.61 / 1637.21 MiB | 793.40 MiB | 13756 / 12964 MiB |

After an eight-token-limit request, driver usage was 2 MiB higher in all four
runs, retaining the same before/after deltas. These are same-configuration
startup/short-request resource measurements, not long-request peak memory,
throughput or answer-quality benchmarks. The default scorer remains page-softmax.

The production `ninfer_token_sums` test passed 636 cases, including exact BF16
copy, FP64 sum references, chronological FP32 additions, subnormal values,
compact/partial/empty ranges, output guards, Graph replay, interleaved seeds and
discarded accumulation. The four-layer MTP fixture passed 270 HTTP assertions
per version across both scorers at chunk/Query-limit pairs 128/512 and 2048/64.
All 20 compared native Query page selections and the saved responses matched.
The HTTP cases cover document-first requests, fresh Query rows, native prefix
reuse, tools, session changes, edits, cancellation and resident-only Pressure.

The first large-reserve test did not trigger Pressure: its original tool text
fit inside R=2048. Both versions were rerun with `--tool-repeats 320` (the normal
R=1024 workload keeps 160); the original failure remains in the local evidence.
Windows checkout CRLF normalization, a standalone-test architecture definition
and preserved copy timestamps were corrected before final qualification. The
baseline tree and final versioned patch both passed `prepare-backends.py --check`.

### Native Query lifecycle regression

`tests/ninfer-query-lifecycle-regression.py` exercises the real HTTP service with
chunk 2048, Query limit 512, two lanes and MTP. Build the test service with
`-DBUILD_TESTING=ON -DNINFER_KVMEM_QUERY_TEST_HOOKS=ON`, then run:

```text
python tests/ninfer-query-lifecycle-regression.py --argv-json <server-command.json> --output <new-evidence-directory> --gpu <GPU-UUID>
```

Run once for each scorer. The argv JSON supplies the executable and existing
four-layer model fixture; the script records all effective options, requests,
responses, native traces, comparisons and failures. Each reference document is
sent in an ordinary first request, followed by a separate question. The test
aligns the question using the native tokenizer so its 512-row Query crosses a
2048-token execution boundary.

KVMem currently rejects `--concurrent-prefill`. Two simultaneous Query captures
are therefore outside the supported service contract; the test verifies that
rejection instead of bypassing it. The supported concurrency case proves
`A partial Query -> B accepted decode -> A complete Query` from native lane
events, and compares Query row counts, accepted-feature fingerprints, both
pre-replay and post-replay score fingerprints, and selected pages against
independent serial requests. These are lifecycle checks, not a replacement for
the Op's independent FP64/exact-copy numerical references.

The test-only rendezvous pauses after a normal prefill step has accepted a
strict Query prefix. The client disconnects there, before any Query score or
generated response, then releases the worker to its ordinary cancellation
boundary. Native prefill-cancellation metrics and traces must confirm that the
remaining Query rows were never committed/scored. Both retrying the original
request and editing its question must restore a native prefix and match their
independent controls. A peer request must remain correct through cancellation.

Each scenario starts a fresh service: a previously completed identical question
can legitimately skip Query capture. The peer uses a different document so it
cannot lease the control's same cached endpoint and force an incomparable cold
recomputation. No timing-based assumption is accepted as proof of interleaving;
the native event order must demonstrate it.

The build option is OFF by default and changes only three translation units.
It adds fingerprints and the bounded filesystem rendezvous selected by
`NINFER_QUERY_TEST_GATE`, with no request protocol fields. Production builds
contain neither that rendezvous nor its per-step test traces. Test-gated runs
do not establish latency or throughput.

On 2026-10-24, the four-layer MTP2 fixture on the RTX 5060 Ti passed all 69
lifecycle assertions for each scorer. Both cancellation cases stopped at
265/512 accepted Query rows, emitted no completed Query score or response, and
recovered to the control's 512-row features and selected pages. All six
feature/score/page comparisons per scorer matched. The uninstrumented service
also passed 68 ordinary Query/Pressure assertions per scorer at 2048/512.
Test hooks were disabled again for the default executable after qualification;
the instrumented executable was retained separately.

Fixture development exposed two incomparable setups: completed identical
questions can skip capture, and concurrent use of the same cached document can
force cold recomputation in one lane. Fresh processes and distinct peer
documents resolved those setups; the earlier failures are retained locally.
The HTTP readiness wait and Windows reader-close ordering were also corrected
before final qualification. These tests also exposed an inherited request-summary
bug: `computed_prefill_tokens` was derived from the entire prompt minus reported
reuse, while cancellation lost the admitted reuse summary and partial prefill
duration. The initial lifecycle conclusions used native accepted rows and absence
of scoring, independently of that incorrect summary.

The user authorized correcting cancellation accounting in the same round. Engine
results now carry accepted logical prefill progress and the admitted reuse choice;
request logs, console totals and terminal timing rates consume that count. Partial
prefill stage time is snapshotted before cancellation retires the request. Logical
input usage still reports the full prompt; computed work excludes internal replay.
The lifecycle test cross-checks the request count against the native restored and
accepted frontiers and the Engine cancellation counter. It also cancels a third
HTTP request while both lanes are occupied and requires zero computed/reused
tokens for that queued request. Host tests cover the corresponding JSON, console
and protocol timing renderers, including normal completed requests.

The accounting follow-up passed 99 native lifecycle assertions and all six
feature/score/page comparisons per scorer, plus 68 ordinary Query/Pressure checks
per scorer with test hooks disabled (334 native assertions total). The same
four-layer MTP2 fixture and RTX 5060 Ti were used. All four partial cancellations
reported 4,224 reused and 1,920 computed tokens at accepted frontier 6,144 of
6,400, retaining nonzero partial prefill duration. Both queued cancellations
reported zero computed/reused tokens. Four host test groups for request logs,
console totals, Chat timings and text completion passed.

Two fixture issues were corrected before the final runs: the deferred gauge is
not refreshed while the worker is paused, so queue synchronization uses the
post-submit request-start event; and ordinary Pressure checks use the prior
320-repeat tool workload to exceed the 2,048-token reserve. The initial failed
runs remain in local evidence. All 205 affected C++ module/test units were
rebuilt and linked using CMake-generated commands, reusing the unchanged,
previously qualified CUDA archives after a costly Ninja cache rebuild was
stopped. Qwen4Exp and Gemma4 result producers were compiled for the new count;
real-model execution in this round covers the Qwen3.5 fixture only. These runs
make no performance or long-context quality claim.

### Earlier graph and policy qualification


Baseline: KVMem master `61569ff4bddcad60e8fad838e02f77a6190126d1` with its
pinned backend patches. Candidate and baseline use the same device/settings.
Windows, CUDA13.2, Release, sm120a; performance uses RTX5060Ti and correctness
uses RTX5050 Laptop. Exact device UUIDs are retained in the local test records.

Core checks cover numerical softmax/GQA, more than 32768 pages, separate masks,
message-local edges, sealing/rollback, pending feedback, resident-only pressure,
fixed pickers and bounded snapshot ownership. CUDA results are checked against
independent CPU/dense references. Native NInfer attention covers BF16 and all
eight quantized K formats (worst absolute page-mass error about 1.3e-7 in the
tested geometries); llama covers f16/f32/q8_0/q5_0/q4_0 on the small model
(worst about 1.3e-7 on the CUDA route).

Small-model functional checks use the existing Qwen3.5-0.8B Q8_0 GGUF and the
existing four-layer NInfer slice (3 GDN +1 full attention +MTP). They cover Query,
warm reuse, rewritten requests and multiple decode-pressure events; NInfer
also covers cancellation and subsequent recovery. The slice validates native
state and control flow, not model quality. No new model download is required.
The complete IQ3 GGUF additionally passes MTP4 with B=768/R=128:512 output
tokens cross three DecodePressure events, followed by warm reuse and rewrite
recovery. Both the normal Qwen prompt and one containing literal role markers
produce only the five structured message spans, without extra trusted edges.

Initial, pre-audit measurements used Qwen3.8-27B GSQ-RCO IQ3_S, one warmup plus three
measured repetitions, cold approximately59K-token input and short-input512-token
output. The acceptance threshold is at most5% median throughput loss. The
NInfer 0.18 resource recipe is used unchanged; only ranking differs.

| NInfer workload | Fixed master median (range), tokens/s | C median (range), tokens/s | C change |
|---|---:|---:|---:|
| Long-input prefill | 570.03 (567.79–570.78) | 547.00 (545.23–547.53) | -4.04% |
| 512-token decode | 55.81 (49.40–57.34) | 55.42 (49.29–57.83) | -0.71% |
| Short-input prefill | 112.54 (84.24–128.63) | 107.07 (96.46–111.39) | -4.87%, noisy |

The short-input prefill result has substantial variation and does not establish
a precise regression size. The long-input prefill and decode medians satisfy
the threshold in this measured workload. A separately measured attempt to
group native sampler CTAs by page increased Op latency from about47.53ms to
49.27ms at the tested53K-token geometry, so it was discarded. This isolated
measurement is not an end-to-end baseline comparison.

llama.cpp uses the same context/B/R/H/prefill/sink/recent values, q8_0 KV,
CPU vision (1024-token cap) and fixed MTP4. Its server does not expose the same
adaptive MTP/ngram options as NInfer. These are comparisons within each backend,
not a comparison of the backends against each other. Requests use greedy
sampling, seed42 and thinking disabled in both baseline and candidate.

| llama.cpp workload | Fixed master median (range), tokens/s | C median (range), tokens/s | C change |
|---|---:|---:|---:|
| Long-input prefill | 490.06 (489.90–490.61) | 484.65 (482.25–485.38) | -1.10% |
| 512-token decode | 37.75 (37.20–39.29) | 37.91 (37.06–39.28) | +0.42% |
| Short-input prefill | 113.89 (113.84–114.48) | 113.62 (112.22–114.13) | -0.24% |

All three measured llama.cpp medians meet the threshold. NInfer's noisiest
short-prefill paired repetition changed from128.63 to107.07 tokens/s (-16.76%);
the three-run median is -4.87%. This is retained as uncertainty, not erased by
the stable long-input result. Neither set of three measurements establishes
performance for every request length or model.

The focused C launcher UI check passes old-draft ranking omission, preset
resource values and output-preview budgets above R. The common PowerShell
launcher and worker configuration-conflict tests pass. The broader pre-existing
UI harness stops at a stale `[data-default-worker]` assertion; no full UI-suite
pass is claimed. Backend patch hashes and patched trees are verified both in
place and by applying each patch to a fresh copy of its pinned base.

Qualification found and fixed a shared-memory maximum-to-sum synchronization
race in the new CUDA softmax reductions. The initially intermittent native
attention oracle failure passed ten repetitions after adding the barriers;
the final all-format native oracle and shared Query oracle also pass.

A subsequent boundary audit found two C integration defects: NInfer temporary
reconstruction rollback omitted graph/feedback/scope, and llama.cpp full
sequence removal retained them. It also found an inherited NInfer ordering
defect: temporary checkpoint Mean-K was committed before the current candidate
K was read. These paths now restore/reset the complete statistical state and
read current candidate K before any commit. The native NInfer regression uses
the existing four-layer fixture with ngram acceptance and a diagnostic-only
legal interior split; it checks normal and injected-exception rollback and
saved partial-page sums against current GPU candidate K. It does not establish
natural thinking-boundary behavior or model quality for the untrained fixture.
The extended `kvmem-llama-commit-order-test` checks accepted sampling, full
sequence removal and shorter-history checkpointing with synchronous and queued
GDN Fold on the existing 27B IQ3 model (RTX5060Ti). Its GDN replay prerequisite
excludes the 0.8B model; the separate small-model reset/marker check and both
backends' HTTP reuse/rewrite/pressure checks run on RTX5050.
The throughput tables above describe the earlier candidate. The final acceptance
section below includes these corrections and the legacy removal.

Other architectures, GPU vendors, multi-GPU pipelines, all media combinations
and broad model-quality benchmarks are outside this focused qualification.
The generic llama GGML fallback is not numerically qualified to the same CUDA
tolerance. The supported default speculative widths fit the native ubatch;
custom speculation split across several tiny ubatches is not newly qualified.

## Query policy consolidation (2026-10-21)

The llama.cpp server uses the `user` request/checkpoint flow for both text and media.
The old `legacy` query-policy executor and its private GDN snapshots/session fields
have been removed. `--kvmem-query-policy user` remains a compatibility option;
`--kvmem-query-policy legacy` fails at argument parsing with a migration message.
This resolves audit A4 by removing the path that rewound native history without
restoring graph/attention state. User-query suffix fallback and the independent
`--kvmem-query-replay legacy|auto` setting remain supported. Historical milestone
reports describe their original versions and do not define current CLI support.

## Historical post-audit performance acceptance before request unification (2026-10-21)

This earlier candidate includes the accepted-state/rollback corrections and the old
Query-policy executor removal. It predates the unified request/replay policy;
these measurements do not qualify the current unification build. It is compared with the same fixed master and
0.18 resource recipes on the same RTX 5060 Ti UUID recorded above. Product sources
and binaries are fixed during measurement. Initial runs use one warmup and three
measured repetitions per workload/version. Initial losses of at least 4% receive
one further warmup plus two measured repetitions per version; all five measured
samples are retained. Warmups are excluded throughout.

NInfer:

| Workload | Samples per version | Fixed master median (range), tokens/s | Final C median (range), tokens/s | Change |
|---|---:|---:|---:|---:|
| Long-input prefill | 5 | 569.32 (566.80–572.10) | 545.54 (542.41–546.95) | -4.18% |
| 512-token decode | 3 | 55.66 (51.74–58.07) | 55.38 (49.96–57.75) | -0.50% |
| Short-input prefill | 3 | 133.67 (133.06–133.81) | 131.25 (131.19–132.53) | -1.81% |

llama.cpp:

| Workload | Samples per version | Fixed master median (range), tokens/s | Final C median (range), tokens/s | Change |
|---|---:|---:|---:|---:|
| Long-input prefill | 3 | 486.95 (486.27–487.90) | 482.41 (480.30–482.50) | -0.93% |
| 512-token decode | 3 | 37.53 (36.65–38.90) | 37.42 (36.65–38.59) | -0.31% |
| Short-input prefill | 3 | 111.53 (109.82–111.62) | 110.83 (110.76–110.88) | -0.63% |

All final medians satisfy the maximum 5% loss gate for these workloads. Prefill
uses the actual processed prompt-token count; the approximately 59K-token history
may reuse up to six opening template tokens. Every long-input request returns
GRAPH-OK and every output workload completes 512 tokens. Decode uses the complete
speculative generation-loop timing, rather than a native single-step sub-timer.
The backends retain their own existing generation timing scopes and are compared
only against their respective baselines.

The 512-token output remains below R=16384, so it measures ordinary decode.
DecodePressure correctness was separately exercised at smaller B/R; its speed,
other request lengths, model quality and broad media/multi-GPU combinations are
not established by this focused performance run.


## Request/replay unification qualification (2026-10-21)

The current source passes the focused two-backend Query/Pressure qualification.
The existing 0.8B llama model and four-layer native NInfer fixture exercise real
prefix reuse, warm/cold tool continuation, session restore, rewriting, stream
cancellation, independent Query/replay caps and all-resident B<length<B+R.
Cancellation is also injected after replay has entered a nonzero prefix; a
subsequent same-session rewritten request succeeds. The NInfer fixture checks
native control/state, not semantic quality. Full IQ3 smoke checks separately
retain an actual document fact, then use a new tool correction in both backends.
Native accepted-prefix checks cover zero/partial/full MTP acceptance and native
state/Mean-K/graph/feedback checkpoint consistency.

An additional NInfer regression sets B768/R128 and MTP2/ngram31. Draft workspace
already causes Pressure while logical length is below896. A subsequent true
Query at884 tokens must score/replay that compressed prefix. It then completes
512 output tokens and crosses multiple DecodePressure events, proving that fixed
Query replay ends before ordinary generation pressure resumes. The pooled-buffer
test separately checks aliases, snapshot independence, bounded idle storage and
outstanding-lease lifetime after the pool owner is released.

Performance uses the unchanged master61569ff4bddcad60e8fad838e02f77a6190126d1,
the existing IQ3/0.18 resource recipes and RTX5060Ti. Each backend is compared only to its own
baseline. One warmup plus three measured repetitions cover approximately59K
cold input and512-token output. NInfer long input received one further warmup
plus two measurements under the predefined near-threshold rule; its fixed master
already has five long-input samples. Actual processed token counts and complete
speculative generation-loop timing are used, as in the historical baseline.

| Backend | Workload | Master median, tokens/s | Unified C median, tokens/s | Change | Samples master/C |
|---|---|---:|---:|---:|---:|
| ninfer | Long prefill | 569.32 | 543.81 | -4.48% | 5/5 |
| ninfer | 512-token decode | 55.66 | 55.47 | -0.34% | 3/3 |
| ninfer | Short prefill | 133.67 | 152.72 | +14.25% | 3/3 |
| llamacpp | Long prefill | 486.95 | 484.79 | -0.44% | 3/3 |
| llamacpp | 512-token decode | 37.53 | 37.66 | +0.34% | 3/3 |
| llamacpp | Short prefill | 111.53 | 115.43 | +3.49% | 3/3 |

All six medians satisfy the maximum5% loss gate in these workloads. All long-input
requests return GRAPH-OK and all output requests complete512 tokens. NInfer long
prefill is close to the limit; these samples do not establish a universal bound.

Earlier failures remain recorded: the initial candidate short-prefill median
lost10.05%; a matched short control lost about35.5%, also reproduced by an older
C execution path. Skipping unused Q capture alone still lost7.32%; caching one
released StateImage did not stabilize the result. A separate instrumented build
located large pinned allocation spikes and is excluded from acceptance. The
current pair cache covers the input/endpoint buffers released by anchor restore;
with conditional Q collection the final short-prefill median is152.72 tokens/s.
These build groups are kept separate. A fresh baseline supplement with mixed
69/63 processed-token cache states is excluded from the historical baseline pool.
No slow measured sample is removed from its recorded group.

Local evidence is under graph-design-20261020/unify-20261021: results.json,
comparison-final.json, adverse-evidence.json, validation-final.json, finish-run.json
and supplement-final-checks.json, plus full request/response/native logs. Backend
patches are independently applied to their pinned bases and checked against the
manifest. Fixed baseline executable hashes and expected backend patch trees are
unchanged. No commit or PR was created.

The512-token default output workload remains belowR16384. DecodePressure
correctness is qualified separately at smallR; its throughput is not measured
here. This work does not restart RULER or add broad quality, media, vendor or
multi-GPU qualification. The optional idle pair holds at most293.6MiB on this
IQ3 Program, appears in checkpoint_bytes and is released with the Program.

### NInfer assistant-prefill regression

Run `tests/ninfer-assistant-prefill-regression.py --argv-json <server-command.json>
--output <new-results-directory> --gpu <CUDA-device-UUID>` with an existing local
model. The JSON contains the server argv array, optionally under `argv`. Use
KVMem B=1024, R=1024, prefill chunks of 256, max-context at least 8192 and one
concurrent request. The runner starts a private loopback server, enables
`--assistant-prefill`, disables thinking, and records requests, responses and
per-request native traces. It always shuts down its own server.

The test first evaluates an assistant prefix, then submits the same user question
with a longer assistant prefix. It requires an exact cached restore that includes
the old assistant text, `same_query=1`, no Query rescoring/replay, new prefill
attention samples strictly within the uncached accepted suffix, and a real
prefill Pressure boundary. EOS is a valid completion: this checks cache and
attention-feedback behavior, not answer quality or decode throughput. Models
with different tokenizers may need a separately adapted workload; failures to
reach the required restore, sampling or Pressure boundaries are not passes.

On 2026-10-22, RTX 5060 Ti with the existing Qwen3.8 27B IQ3 model, INT8 KV,
MTP4 and C defaults passed twice: 46 cached tokens, 2471 input tokens, three
prefill batches totaling 24 attention samples, and Pressure at token 2048.
A temporary build restoring only the old `query_replayed` sampling guard failed
specifically on missing samples (zero), confirming that the regression detects
the defect. The fixed source and executable were restored afterwards.
This diagnostic uses transfer verification and a small window: extended-request
wall times were 8.92 and 9.61 seconds with the fix versus 4.70 seconds in the
single negative-control run. These single runs were not warmed throughput
measurements. The model returned
EOS for these supplied assistant prefixes, so answer quality is not qualified.

Follow-up attribution on the same GPU/model used one diagnostic binary with a
temporary old-guard switch: fixed/old/fixed process order, three identical
base-plus-extension cycles per process, with the first cycle reported separately.
All extensions restored 46 tokens and evaluated the same 2471-token prompt.
Cold first requests took 9.99/8.84/8.90 seconds; subsequent fixed requests took
4.15-4.57 seconds and old-guard requests 4.15-4.16 seconds. The ordinary model
prefill work was 3.353-3.366 seconds in both variants. Accepted attention feedback
(including copies, synchronization, graph and Pressure updates) totaled
13.1-13.4 milliseconds with the fix versus about 0.12 milliseconds with the old
guard. Warm restore times varied from 375 to 735 milliseconds and explain much
of the residual end-to-end variation; this small series is not a general
no-regression performance qualification.

The several-second delay occurs before attention feedback in the first
210-token model prefill chunk, which took 4.77-5.72 seconds versus about
328 milliseconds on later cycles. An independent empty CUDA cache reproduced a
10.05-second first request, with 5.775 seconds inside layer-index 13's FFN host
call, while its final GPU wait was only 1.3 milliseconds; the next request took
4.51 seconds. This supports model-operator first-use compilation/loading cache
cost, not graph sampling, as the cause of the large gap. The exact CUDA internal
API responsible was not isolated. The server's built-in warmup only submits
"hi" with four output tokens, so it does not exercise this large-prefill path.

Nsight tracing changed cold-start timing and is excluded from the latency
comparison. Forcing `CUDA_MODULE_LOADING=EAGER` did not solve the issue: its first
request took 27.49 seconds and its second 4.20 seconds; the setting was not adopted.
All temporary timers and diagnostic switches were removed and the normal server
rebuilt. Future timing comparisons must warm the relevant prompt/chunk shape and
report cold-start latency separately from steady-state inference.
