# Local model inspection

The launch console inspects a selected model on the CPU, without starting a worker,
loading tensor payloads, initializing CUDA or allocating VRAM. All three backends
use the same report and UI: llama.cpp and PolyStrata inspect GGUF; NInfer inspects
its v3 artifact format. This is a static preflight, not a numerical model test or
a guarantee that the selected worker can load the model.

## Behavior

The card below the model selector shows file facts, included components, issues
for the current configuration and expandable check details. Changing the model,
backend, worker, vision or speculative mode invalidates the old report immediately.
Responses belonging to an earlier selection cannot replace a newer result.

Missing/truncated files, inconsistent shards, invalid object ranges and missing
enabled NInfer components block launch. Disabling vision or speculation through
the card edits only the draft and triggers another check. Malformed inactive vision
resources produce a warning. Unsupported versions, unknown formats, exhausted
limits or inspector failures remain unverified and permit an attempted launch.

`Controller.start` independently repeats inspection for the submitted configuration;
the browser's previous report is never an authority for launching. Normal launcher
validation and worker loading still run afterward. Files can change after inspection;
the loader remains responsible for validating the bytes it actually consumes.

## Checked scope

* GGUF v2/v3: header and metadata framing, duplicate names, tensor dimensions,
  alignment, known quantization extents, overlaps, truncation and named split files.
  Unknown quantization types leave extent validation incomplete. The directory is
  used to report tokenizer/template/MTP evidence; it does not prove their semantics.
* NInfer v3: the pinned native Python artifact reader validates the directory,
  object spans, known encodings, file lengths and all continuation UUIDs/indices,
  including otherwise unused continuations. Small text/vision resources are parsed;
  tokenizer vocabulary and tensor payloads are not read.
* Current configuration: NInfer text, vision, MTP/DFlash2 component presence;
  worker/custom-template/external-draft file existence; PolyStrata projector,
  vision worker and explicit exported tokenizer resources.
* Reference profiles: PolyStrata's launcher declares `qwen35moe`; Qwen3.5 vision
  geometry and normalization are compared with the vendored reader revision's
  frontend source. Differences are warnings with actual/expected values. These
  profiles are not proof about an arbitrary executable selected by the user.

Worker architecture/kernel support, required tensor binding completeness, projector
pairing, tokenizer correctness, Jinja execution, tool calling, numerical quality and
memory feasibility remain unverified. No model or resource is automatically repaired.
Weights are not checksummed, so payload bit corruption cannot be detected here.

## API and limits

`POST /api/launcher/inspect-model` uses the existing console token/origin checks and
configuration envelope (`schema_version: 1`, `parameters`). Only inspection-relevant
fields are passed to the subprocess; credentials are excluded. `/api/launcher/info`
advertises `model_inspection_version: 1`.

Reports include `status` (`ready`, `warning`, `blocked`, `unavailable`), `can_start`,
`model`, `features` and `checks`. Each check has a stable code, file/config/worker
layer, pass/warning/error/unknown status, bilingual label, evidence, and optionally
a draft-only recovery action.

The controller permits two inspectors concurrently and kills each subprocess after
10 seconds. Parsing has a five-second cooperative budget, at most 128 files and
200,000 tensors/objects; NInfer directory JSON is limited to 16 MiB, each inspected
resource to 1 MiB and total resource reads to 8 MiB. GGUF directory reads are bounded
at 64 MiB. No results are cached, so recheck and launch read the current files.

The packaged console requires Python 3.10 or later. Its standard-library-only NInfer
reader lives in `scripts/model_health_ninfer`; `PROVENANCE.json` records its upstream
revision and exact source hashes, with the Apache 2.0 license alongside it. Updating
the backend format requires reviewing/updating this pinned reader and its fixtures.

## Verification

`python -m unittest discover -s tests -p test_model_health.py` exercises small real
containers, corruption, future versions, untrusted sizes/paths, disabled components,
native encoding checks, timeout handling and the fresh launch gate.

`node ui/test-model-health.cjs` starts a disposable console and verifies the live API,
authentication, launch gating, recovery actions, stale responses, polling, language
changes, all backend selectors and mobile layout. Set `PLAYWRIGHT_MODULE`, `CHROMIUM`
and `PYTHON` as needed. It never launches an inference worker.
