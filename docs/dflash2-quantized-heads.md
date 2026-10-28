# Q3, Q4 and NVFP4 DFlash2 companions

The NInfer backend can import the Qwen3.8-27B DFlash2 Q3_K_M/Q4_K_M companions
without converting their attention output and MLP down projections to Q8. The
dynamic grouped convolution finish path materializes these actual GGUF block
formats through Linear, including the Q6_K tensors inside the Q4_K_M file. The
workspace capacity includes Linear's temporary storage for each layer's actual
projection format. Q8 and NVFP4 do not reserve the GGUF fallback workspace, and
Q3/Q4/Q6 matrix routes do not reserve IQ1_M's 16 MiB dequantization scratch.

NVFP4 imports use the existing ModelOpt source adapter. The runtime additionally
registers DFlash2's feature, QKV, attention output, convolution and selector matrix
geometries in both NVFP4 A16 dispatch tables. QKV remains on its native Q8 fused
route, convolution preparation and norms remain BF16, and the supported feature
and selector matrices are quantized from BF16 to weight-only NVFP4. These are
mixed-format companions; the filename does not describe every stored tensor.

The implementation adapts
[melis2023/ninfer-kvmem-actkv at 664cb6d](https://github.com/melis2023/ninfer-kvmem-actkv/commit/664cb6daa8cba30dfe39b0d10113a72b20587853).
It retains the current backend's INT8 routing and Blackwell scale handling.
The two ordered backend patch layers are `dflash2-gguf-blocks` and `dflash2-nvfp4`.
The restored draft readable-range behavior and launcher defaults are unchanged.
The subsequent `cuda-graph-probe` layer adds [CUDA Graph startup calibration](cuda-graph-probe.md)
with a shared 256 MiB floor for MTP, DFlash2 and ordinary decode.

## Companion sources

The upstream release links the following repositories. The tested files were
downloaded from HF-Mirror at immutable revisions and checked against LFS SHA-256.

| Companion | Repository / revision | File SHA-256 |
|---|---|---|
| Q3_K_M | [Anbeeld/Qwen3.8-27B-DFlash2-GGUF](https://hf-mirror.com/Anbeeld/Qwen3.8-27B-DFlash2-GGUF/tree/2f2814d13dcd6fe6c9cfa29f6882b0bc1267dc46), `2f2814d13dcd6fe6c9cfa29f6882b0bc1267dc46` | `72809a53e89cb959a3149c9b3752ad181b640164b51a3b9f445712520beec5dc` |
| Q4_K_M | Same revision | `ebd3a84f4fd056af9870401ef3b7ae8cd78020ffb78393fb0c3acd2273725d12` |
| NVFP4 RTNcal | [maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal](https://hf-mirror.com/maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal/tree/bd7a934213c47a9e7ef69eef36bb3325f47fd1f1), `bd7a934213c47a9e7ef69eef36bb3325f47fd1f1` | `2228b9b22e93a88d84556419c879448ab6c490ae65c4c0b166f4962190ddbf26` (`model.safetensors`) |

Keep `config.json` and `hf_quant_config.json` beside the NVFP4 safetensors file.
The downloaded GGUF files are 916,702,560 and 1,143,006,560 bytes; the NVFP4
safetensors file is 1,550,153,248 bytes. Retain their model cards and licenses.

## Conversion

Run from the prepared `backends/ninfer` directory. Supply the target checkpoint
metadata/tokenizer directory and the original target GGUF. Add the vision source
and `vision` component when creating a multimodal artifact.

```powershell
python -m tools.convert --model D:/models/target-metadata `
  --recipe qwen3_5_dense_gguf --source gguf=D:/models/target.gguf `
  --source dflash2=D:/models/Qwen3.8-27B-DFlash2-Q3_K_M.gguf `
  --override tools/convert/recipes/dflash2_gguf_blocks.py `
  --components text,mtp,dflash2 --device cpu --out D:/models/target-dflash2-q3.ninfer
```

For an already quantized ModelOpt NVFP4 checkpoint, change `--source dflash2` to
its directory and select `tools/convert/recipes/dflash2_import_nvfp4.py`. This
preserves the source NVFP4 codes and block scales; the source's multiplicative
global scale is represented as the equivalent native divisor. The optional
`dflash2_nvfp4.py` override instead requantizes a BF16/FP8 source with bounded CPU
chunks and round-to-nearest-even E2M1 codes. Its supported activation policy is
`A16Only`. This override explicitly packs each MLP gate/up pair into one NVFP4
parent with a shared weight divisor, as required by the native SwiGLU binding.

The serving interface remains `--spec dflash2 --draft-tokens 5`, using an artifact
that contains the draft component. The original MTP component can coexist in the
same artifact and remains selectable with `--spec mtp`.

## Validation

The conversion fixtures check exact retained Q3/Q4 blocks and independent NVFP4
rounding/decoding, including ties, signs, finite inputs and unsupported policies.
Real-checkpoint read-back additionally verifies attention output/down codes and
scales, and all 1,183 existing target/Vision/MTP objects are byte-identical in the
three prepared artifacts. Native focused validation is available through the
`ninfer_dflash2_format_tests` bundle: dynamic convolution composition and the
registered NVFP4 matrix geometries use independent numerical references, allocation
guards, preserved-input checks and CUDA Graph replay.

The original integration passed 11 draft conversion tests and 6 existing NVFP4
source/recipe tests. Audit fixes add BF16/FP8 gate/up packing and chunk-boundary
round trips plus rejection of activation-quantized policies (16 draft tests total).
The original focused native bundle passes both programs under default routing and
the scoped `NINFER_LINEAR_ROUTES=a16:unified` override on the RTX 5060 Ti. Native
NVFP4 geometry tests include widths 1, 2, 3, 5, 6, 16, 32 and 33 and graph replay;
dynamic convolution checks sweep widths 2 through 16 and batches 1 through 8.

After the audit fixes, the 16 draft conversion tests pass and the server, focused
test bundle and dynamic-convolution benchmark target rebuild successfully. The
dynamic-convolution suite passes again under default and `a16:unified` routing on
an idle RTX 5050 Laptop, including exact per-format allocations, guards and graph
replay. These are correctness/resource checks on a different GPU, not new speed
measurements; the Task 1 results below retain their original binary and settings.

## Task 1 comparison, 2026-10-26

The comparison uses the existing IQ3_S target, resident Vision and original MTP
objects. Each variant runs in a fresh process, with one complete warmup conversation
and one measured conversation. The order is original MTP, rebuilt MTP, Q3, Q4,
NVFP4, then the reverse order for a second repeat. All launches check that the
specified RTX 5060 Ti is idle before using it.

Common settings are logical context 262144, history budget 16384, generation
reserve 4096, INT8 KV, FP16 GDN state, prefill 512, one lane/session, resident Vision
with 1024 merged tokens, 12 GiB host payload, LRFU, sink/recent 5120/5120 and no
n-gram drafting. CUDA Graphs use the existing 128 MiB planning allowance and no
probe. All three drafts complete startup at this budget without a further reduction.
MTP uses three drafts and DFlash2 uses five.

The ordinary requests are a 12K-token background with a short acknowledgement,
then the existing 896 by 896 red-square/blue-circle/green-triangle fixture and its
question, then a request to generate HTML/SVG with thinking. Each question carries
the actual preceding assistant messages. Requests use seed 42, temperature 1,
top-p 0.95, top-k 20, thinking budget 128 and a 512-token output cap.

Reported decode rates are `sum(output_tokens - 1) / sum(decode_seconds)` from the
compatibility response timings, over the two measured repeats. Cold document
ingestion and prefix hits on the two follow-ups are checked separately. Short
answers and sampled histories differ across draft variants, so whole-conversation
latency and short-answer rates include those differences. The capped code segment
measures throughput and does not establish complete HTML correctness. This workload
does not establish long-context recall quality or a general speedup.

| Variant | Long-text response, tok/s | Image response, tok/s | Code with thinking, tok/s | Code vs original MTP | Peak VRAM, MiB |
|---|---:|---:|---:|---:|---:|
| Original MTP3 | 37.84 | 61.88 | 67.80 | baseline | 13468 |
| Rebuilt MTP3 | 40.66 | 61.92 | 69.01 | +1.8% | 13468 |
| DFlash2-5 Q3_K_M | 44.49 | 55.46 | 86.54 | +27.6% | 14426 |
| DFlash2-5 Q4_K_M | 64.02 | 55.15 | 88.26 | +30.2% | 14600 |
| DFlash2-5 NVFP4 | 50.55 | 63.15 | 76.70 | +13.1% | 14556 |

All ten measured image answers name the correct color/shape pairs. The complete
assistant messages from the original and rebuilt MTP are identical across both
repeats; rebuilt MTP has no observed code-throughput regression in this workload.
The original MTP code rates span 66.65 to 68.99 tok/s, so its two-run variation also
matters when interpreting the 1.8% control difference.

Q4 has the highest code throughput. Against MTP in the **same rebuilt binary**, the
code-rate improvements are 25.4% for Q3, 27.9% for Q4 and 11.1% for NVFP4.
Code draft acceptance is 76.0% for MTP, 74.7% for Q3, 76.5% for Q4 and 69.5% for
NVFP4; widths differ, so acceptance percentage alone does not rank speed.
The two Q4 code rates are 88.27/88.25 tok/s and NVFP4 is 76.70/76.70 tok/s.
NVFP4's weight-only A16 route does not beat the GGUF companions on this code stage.

Q3/Q4 image-response decode rates are 10.4%/10.9% below original MTP. NVFP4's
image decode rate is 2.1% higher, but its longer reasoning/output makes the image
request take 3.48 seconds versus MTP's 3.08. Median total measured conversation
times are 29.34 seconds for original MTP, 29.15 for rebuilt MTP, 28.42 for Q3,
27.92 for Q4 and 29.37 for NVFP4. Cold long-text prefill takes about 16.8 seconds
with MTP, 16.9–17.1 with GGUF drafts and 17.6–17.7 with NVFP4; this dominates the
whole-conversation timing. NVFP4 therefore has no measured whole-conversation
latency improvement despite its higher code decode rate.

All launches stay within the 16 GB card; Q4 peaks at approximately 14.26 GiB.
[Task 1 records](benchmarks/dflash2-quantized-task1-20261026.json) retain both
repeats, per-stage timings, acceptance, memory peaks, model revisions/checksums,
artifact identities, preserved-weight checks, backend trees and binary hashes.
Physical device identifiers are anonymized in the public copy; the complete local
requests, responses, native logs and sampled GPU telemetry remain alongside the
private task evidence.
