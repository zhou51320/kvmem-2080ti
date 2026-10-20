# Eight-lane speculative configuration, 2026-10-19

The DFlash2 integration initially capped configuration at four lanes because its qualification stopped there. The 27B all-GPU capacity failure on a 16 GB card does not establish a general concurrency bound for other models or larger cards. This update separates the configured engine limit from a particular workload's memory feasibility.

## Configuration boundary

Both common launchers and the UI accept one through eight requests. llama.cpp's native parser and ordinary/MTP/DFlash2 capability reports now agree on eight independent lanes. Its admission and conversation containers are sized dynamically. NInfer retains its existing `kMaximumConcurrency = 8`; DFlash2's KVMem admission now uses that native bound, matching its existing round buffers and scheduler arrays. Parameter, actual child-argv and browser tests accept five/eight and reject nine. The eight-lane queue test holds every lane, queues a ninth request and releases exactly the eighth lane for reuse.

No proposal, target verification, sampling, KV, recurrent-state or CUDA algorithm is changed. DFlash2 remains single-GPU, fixed-width, and uses the same draft limits. At this checkpoint the launcher still blocked concurrent llama.cpp SSD; the subsequent [legacy SSD retirement](legacy-ssd-retirement-20261019.md) removes that overbroad gate and validates eight-way DFlash2 with real SSD I/O. The mixed-placement snapshots requirement remains in force. Main/draft weights can be shared while per-lane KV and workspaces still grow with concurrency. Loading a workload remains subject to its actual capacity; no automatic reduction of the requested lane count is introduced.

## Evidence and limits

The focused GPU checks use an RTX 5050 Laptop with CUDA 13.2/MSVC 19.44 and eight build jobs. These are functional checks, not performance measurements.

- llama.cpp uses the existing Qwen3.5-0.8B Q8_0 target and deterministic untrained DFlash2 fixture, five drafts, n-gram off, context 4096, B512/R256 and snapshots. All eight slots are simultaneously active. Twenty-five completed request records cover isolated/concurrent secret-word checks, historical prefix reuse, client cancellation and recovery. All isolated/concurrent outputs match. The log has one shared draft-model pointer, seven reuses and eight distinct draft contexts. Common/native counters and Prometheus checks pass.
- Host checks pass: 37 console tests, seven Windows child-argument tests, the PowerShell launcher suite, the browser launcher suite, native llama.cpp options/queue tests, NInfer serving-options/schema/metrics tests and both prepared backend patch/tree checks. The affected NInfer CPU units, including the current main branch's transfer units, are rebuilt against unchanged CUDA libraries.

NInfer cannot use the 0.8B target as the same runtime fixture: its existing input-projection geometry guard rejects that target before decoding. Instead, an execution-only artifact copies the first six target layers and original proposal/DFlash2 weight bytes from the existing 27B IQ3/Q4 artifact. Its five draft taps are retargeted to target layers 0 through 4. This truncated target and retargeted head are not a trained model pair and cannot establish natural-language quality, trained-head acceptance or full-model throughput.

The initial NInfer exact-output check observes eight active requests and seven of eight isolated/concurrent results equal. The differing request emits a space, a period and spaces in isolation, versus spaces in the concurrent run; its isolated DFlash2 result equals ordinary decoding. Ordinary decoding's isolated/concurrent control is equal for all eight requests. The truncated target predominantly emits spaces even without DFlash2. The difference is retained as unresolved; it is not attributed to a confirmed numerical effect, an inherited defect, state corruption or memory exhaustion. The user chose reasonable-output review rather than exact textual identity as the release gate. Since this artifact cannot answer the task meaningfully in either mode, the punctuation difference is not used to impose a four-lane configuration cap. It also is not counted as an eight-lane semantic-quality pass.

The subsequent NInfer run checks the narrower observable scope of eight-lane admission, sustained output budgets, native counter bounds, prefix reuse and cancellation recovery. Its result and the earlier failed identity check are recorded separately in the [machine-readable evidence](benchmarks/dflash2-eight-lanes-20261019.json).

The previous trained 27B C4 and all-GPU C2 results retain their original scope in the [earlier qualification](dflash2-concurrency-validation-20261019.md). Full trained-model C8 quality/performance, physical multi-GPU DFlash2 and the complete concurrent n-gram/vision matrix are not established by this change. In particular, the earlier 16 GB all-GPU C4 capacity failure remains a failure; increasing the configuration limit does not make that workload fit.

## Test-environment corrections

Private probes use the already installed browser executable and the repository's GGUF module. The static-library helper reconstructs Ninja's removed response file from the known object inventory. Native route calibration is explicitly disabled to avoid an unrelated calibration campaign. A first truncated artifact omitted the source model's proposal dependency; the corrected artifact preserves it. A proposed per-request logit-bias oracle is rejected by NInfer's existing protocol contract and is not used. These corrections do not change product inference behavior. No new model download, 256K run, RC3 run or throughput campaign is performed.

Initial Linux CI caught a second parameter-alias test that still expected five lanes to be rejected. Both aliases now test every supported count through eight and reject nine; the rebuilt native options test passes locally. This correction changes the test expectation only.
