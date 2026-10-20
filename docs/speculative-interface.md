# Speculative decoding across backends

The shared contract covers configuration, launcher capabilities and completed-request statistics. Each backend still owns proposal generation, target verification, sampling, accepted-prefix commits, KV storage and recurrent-state rollback. There is no shared speculative execution loop.

`kvmem/include/kvmem/speculative.hpp` defines the configuration and statistics schema without a JSON library dependency. `scripts/speculative-capabilities.json` defines the combinations offered by the common launcher and UI. These are launcher limits for compatible models, not a promise that every artifact contains the required trained head. Native model loading remains authoritative.

## Configuration and UI

The console offers Off, MTP and DFlash2, a draft length, a fixed/adaptive policy and a draft GGUF field where applicable. Unsupported methods and combinations are disabled according to backend, concurrency and selected GPU count. Disabled dependent fields remain in saved browser drafts but are omitted from launch requests. Old MTP templates are migrated in memory; existing `MtpDrafts` and `AdaptiveMtp` script parameters remain accepted. Conflicting explicitly supplied old and new settings are rejected.

| Common parameter | Meaning |
|---|---|
| `-SpeculativeMode none/mtp/dflash2` | Select a method; `none` disables drafts |
| `-DraftTokens N` | Fixed length or adaptive upper bound; defaults to MTP4/DFlash2-5 when enabling a method |
| `-DraftPolicy fixed/adaptive` | Adaptive is available only for NInfer MTP |
| `-DraftModel PATH` | Existing compatible GGUF; required for llama.cpp DFlash2, optional for llama.cpp MTP, absent for NInfer |

| Backend | MTP | DFlash2 |
|---|---|---|
| llama.cpp common launcher | Fixed 1-5; embedded or sidecar head | Fixed 1-5; separate GGUF; up to eight requests on one GPU; no n-gram |
| NInfer common launcher | Fixed/adaptive 1-15; embedded component; native n-gram options | Fixed 1-15; embedded component; up to eight requests on one GPU; native n-gram options |

Other existing restrictions still apply, including llama.cpp concurrency versus GPU split and cache settings. The common llama.cpp width limit of five follows the default ReplaySSM profile. Advanced native snapshot configurations have their own limits and are not enabled by increasing the common UI limit. Actual effective width can be smaller because of native model block limits. DFlash2 has no adaptive-width mode in this integration.

Example using a separately built llama.cpp worker:

```powershell
./scripts/windows/start-backend.ps1 -Backend llamacpp `
  -Worker 'D:/build/llama-kvmem-server.exe' -Model 'D:/models/target-IQ3.gguf' `
  -Gpu 'GPU-your-device-uuid' -SpeculativeMode dflash2 -DraftTokens 5 `
  -DraftPolicy fixed -DraftModel 'D:/models/target-DFlash2-Q4_K_M.gguf'
```

For a NInfer artifact already containing the matching DFlash2 component, use `-Backend ninfer` with the same mode, width and policy and omit `-DraftModel`. `start-dflash2.ps1` retains the previously qualified NInfer 36K+10K, FP16 GDN, n-gram-off profile. General 0.18 launch defaults remain unchanged.

The mappings preserve native names: llama.cpp receives `--spec-type draft-dflash --spec-draft-n-max N --spec-draft-model PATH`; NInfer receives `--spec dflash2 --draft-tokens N`. Do not duplicate named speculative options in `WorkerArgs`. Updated scripts require workers built from this integration; existing release binaries do not gain DFlash2 support from a new UI alone.

## Native llama.cpp integration

The pinned upstream already implements DFlash2 convolution and selector execution through `COMMON_SPECULATIVE_TYPE_DRAFT_DFLASH`. KVMem selects that implementation and loads its independent draft GGUF. The main model does not load its MTP head for DFlash2. Startup validates draft metadata, target feature width and target layer indices; structural compatibility does not establish training provenance, so users must still choose the matching trained companion.

Lanes share one read-only draft model through a native shared owner, while each lane retains its own context, KV cache, sampler, graphs and rollback state. Each context keeps its model alive; the target weight owner outlives all companion contexts and weights because native companions can borrow target tensors. Logical token rows are carried separately from M-RoPE positions through feature injection and cache removal. Native full sequence-state serialization supplies historical draft checkpoints and restores; ordinary ReplaySSM rounds avoid copying the full draft cache. MTP-only raw-token replay is not used for the feature-conditioned DFlash2 head. Proposal, selector, sampler and attention kernels are unchanged.

Upstream refreshes therefore remain localized to the native speculative adapter, context hooks and state serialization interfaces. The shared configuration header does not depend on upstream speculative implementation types. Changes remain versioned backend patches against pinned commits, checked by `scripts/prepare-backends.py --check`.

## Capability and statistics interfaces

`start-backend.ps1 -Describe` returns `speculative.schema_version` and `speculative.modes`. The console exposes that catalog as `/api/launcher/info.speculative_capabilities`. Both workers expose the same top-level `/props.speculative` object:

```json
{
  "schema_version": 1,
  "enabled": true,
  "algorithm": "dflash2",
  "draft_tokens": 5,
  "adaptive": false,
  "ngram_draft_tokens": 0,
  "capabilities": [],
  "statistics": {
    "scope": "completed_requests",
    "requests": 1,
    "rounds": 1,
    "drafted_tokens": 5,
    "accepted_tokens": 1,
    "generated_tokens": 2,
    "fallback_steps": 0,
    "acceptance_rate": 0.2
  }
}
```

The real `capabilities` array describes MTP and DFlash2 launcher profiles, including width, adaptive/n-gram support, concurrency, GPU count and draft asset type. Configuration fields describe the running engine; they do not modify it. NInfer's existing non-KVMem DFlash mode is reported as `dflash` rather than being mistaken for disabled speculation, but is not offered by this common launcher.

Statistics accumulate when generation finishes, including cancellation paths that produce a native completion record; they are not live progress counters. Admission failures are excluded. `rounds` maps to native target verification steps with proposals; `fallback_steps` counts ordinary decode steps when no proposal is available. `drafted_tokens` counts native proposal candidates, excluding internal replay proposals; `accepted_tokens` counts committed accepted candidates and excludes correction/bonus and replay tokens. `generated_tokens` counts output tokens, including correction/bonus tokens. Acceptance is accepted/drafted, or JSON null before any drafts. Existing backend-specific per-response and Prometheus metrics remain available, including their separate generation-round and timing fields. Short metadata locks avoid holding an inference lock while polling `/props`.

## Validation

The focused [2026-10-19 validation record](speculative-validation-20261019.md) covers launcher/UI behavior, real IQ3+Q4 text/image requests, synthetic native DFlash2 rejection, SWA rollover and historical state restoration on the RTX 5060 Ti. It establishes functional integration, not a new throughput or 256K quality result. Earlier NInfer benchmark records retain their original scope.

The initial [concurrency qualification](dflash2-concurrency-validation-20261019.md) tested up to C4 on one GPU for both backends. The current configuration limit is C8; model capacity and the tested workload remain separate from this limit. llama.cpp shares draft weights and selects storage-aware layout/capture paths; mixed captures synchronize their producers and drain older captures before reading host state. The trained IQ3+Q4 C4 profile on the 16 GB 5060 Ti uses `-ngl 40 --kvmem-mtp-state snapshots`, with the remaining target layers on CPU. It passes four-active-request isolation, history reuse and cancellation recovery. This does not establish that the same 27B C4 profile fits entirely on the GPU; the recorded all-GPU run exhausts memory. CPU recurrent layers require the existing snapshots mode, configured through native worker arguments or PowerShell `WorkerArgs`. NInfer already shares the embedded draft weights. Same-card pipeline protocol checks do not qualify physical multi-GPU operation.

The [eight-lane follow-up](dflash2-eight-lane-validation-20261019.md) separates this configuration limit from workload capacity. It records llama.cpp C8 functional checks, NInfer execution-fixture protocol coverage and the unresolved punctuation difference in the truncated NInfer target. It does not claim trained 27B C8 semantic or performance qualification.
