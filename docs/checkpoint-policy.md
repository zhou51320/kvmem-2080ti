# Checkpoint coverage and RAM/SSD placement

The experimental `spread` policy adds historical coverage to the llama.cpp and
NInfer Qwen3.5 KVMem paths. The default remains `legacy`. PolyStrata keeps its own
native checkpoint policy. This feature does not add a request mode or persistence
across process restarts.

## Configuration

Set these environment variables before starting the worker:

| Variable | Default in spread mode | Meaning |
|---|---:|---|
| `KVMEM_CHECKPOINT_POLICY` | `legacy` | `legacy` or `spread` |
| `KVMEM_CHECKPOINT_INTERVAL` | 16384 | Approximate token spacing of optional captures |
| `KVMEM_CHECKPOINT_MIN_GAP` | 0 | Minimum spacing of ordinary retained points; 0 disables nearby thinning; 64 is the tested experimental setting |
| `KVMEM_CHECKPOINT_COUNT` | 24 | Retained records per conversation, 1–256 |
| `KVMEM_CHECKPOINT_MIB` | 4096 | Logical retained payload and native metadata budget per conversation |
| `KVMEM_CHECKPOINT_HOT` | min(4, count) | Target number of images in RAM; at most count |
| `KVMEM_CHECKPOINT_HOST_MIB` | 1024 | Checkpoint Host budget per lane |

Numeric values must be positive except MIN_GAP, which also accepts zero. The extra variables are ignored in legacy
mode. These defaults are experimental starting points, not tuned recommendations.
The interval is not a bound on recomputation after pruning or skipped captures.

Use the existing `--kvmem-host-mib`, `--kvmem-disk-mib` and `--kvmem-disk-path`
options for native KV storage and SSD capacity. Checkpoints and KV share D;
checkpoint payloads do not get a second disk allowance. H remains a KV budget.
See [active KV tiering](active-kv-tiering.md) for quota and file lifetime rules.

The checkpoint Host account includes live native images, captured attachments,
read/capture buffers, outstanding writer leases and idle pooled images. NInfer
uses one worker account of `HOST_MIB × max_concurrency`, including a reserved
single-image read buffer when SSD is enabled; its idle pinned pool retains at
most one image. Llama charges each image to the lane account that created it,
including after conversation migration. Neither limit is a process RSS limit:
GPU state, model/workspace, allocator overhead, unrelated metadata and OS page
cache have separate lifetimes.
Native image bytes are exact. Aliases of one checkpoint payload are charged
once, while metadata shared by distinct snapshots is charged conservatively
per snapshot; the checkpoint counter is not an exclusive-allocation RSS meter.
NInfer charges retained vector capacity, including token types, all three
position axes, nested media identity, rewrite frontiers and resident page IDs.
Truncating an identity does not release its capacity. Capture admits a conservative
bound from the source capacities before copying metadata or allocating the native
image; the completed metadata must fit that admission. Small Host budgets can
therefore skip more optional captures than builds that omitted these allocations.

## Capture, retention and selection

The capture planner uses the structured system-prefix boundary, observed LCP
anchor, periodic completed prefill chunks, the boundary before the last mutable
message, and native committed endpoints. Exact semantic boundaries can split a
text batch. Periodic captures follow actual completed chunks. A checkpoint never
splits an indivisible media group or names a frontier not yet evaluated. Optional
long-generation sampling is not enabled in this version.

Before constructing optional coverage or reuse-anchor images, a metadata-only
lookup checks for a valid earlier archive point less than MIN_GAP tokens away.
If found, capture is skipped; restoring that earlier point later performs the
extra prefill normally. Backend prefix/state proofs still govern reuse. No SSD
image is loaded for this check, and equal frontiers never prove state identity.

The historical directory also thins close points even below capacity: retain the
earlier representative and one latest endpoint. Older endpoints follow ordinary
spacing when a new endpoint arrives. Comparisons use actual retained ordinary
points, so skipped neighbors cannot move the representative forward indefinitely.
Required Query/replay, rollback and MTP snapshots remain exact execution leases.
When those leases end, their images compete under the same historical spacing.
MIN_GAP=0 disables both the capture gate and nearby directory thinning. This
simple rule has no cost predictor, usage estimator or background control loop.
It defaults off: the 64-token experiment reduced some retention/write work but
regressed several tail-edit and switch-back requests. The validation record
reports the complete paired results. Enabling it trades retained images for
extra prefill and can also require rebuilding exact execution snapshots.

Native Query replay, reconstruction and cancellation snapshots remain execution
leases. Optional captures reserve room before allocating their native image and
skip when there is no room. Required captures first reclaim idle buffers, demote
eligible images, or evict optional retention; if admission still fails, they
report capacity exhaustion before allocating the image. Required snapshots are
never silently replaced by an approximate periodic point.
When parked owners consume the creator lane's remaining Host quota, required
admission can evict an older idle owner. Busy, switching, queued and resident
owners remain protected. A later request for an evicted owner recomputes its
prefix. A request rejected after prefill began uses its existing rollback lease.

After a turn, reusable execution snapshots enter the bounded archive and release
their execution slots. Shared payload identities are counted once. A new payload
at the same frontier replaces the directory entry; equality of frontier alone
never aliases recurrent or Query-replay state.

Retention softly protects the root, latest anchor and newest frontier. Among
other records, remove the smallest
`(next_frontier - previous_frontier) / max(interval, newest - previous_frontier)`;
ties prefer the least recently used record. This keeps denser recent coverage
and coarser old coverage. Under tiny budgets the soft protections yield to the
count/byte limits. For example, two recent retained points can both lie beyond a
future prompt's legal restore frontier, requiring a full prefill.

Selection considers metadata from RAM and SSD together, chooses the deepest
legal frontier and preserves native prefix, accepted-token, media, KV-coverage,
recurrent, MTP, mean-K and retrieval-history checks. It does not read every image
to find a candidate. Restoring a prefix invalidates the edited suffix.

In llama multi-conversation selection, spread mode permits an explicit, already
bound `kvmem.conversation_id` to edit that owner's earlier history using a valid
checkpoint. This replaces that owner's suffix, rather than retaining an extra
branch. Inferred/unknown conversation IDs retain the existing continuation gate;
legacy mode also keeps that gate for explicit IDs. This is an intentional policy
change required for middle-edit reuse, not a correction of the old fork policy.

NInfer's existing Responses route binds `prompt_cache_key` to a session key
(`store: false` is sufficient); that explicit owner can also replace an earlier
suffix. In Chat Completions, `prompt_cache_key` is only a prompt-anchor hint,
not a session binding. Spread protects anonymous histories from consumptive
restores by unrelated requests sharing only the system prefix: a continuation
must reach the previous accepted input boundary, allowing a 64-token template
tail margin. Earlier edits without a session key start a separate history. This
keeps a short unrelated conversation from truncating the retained long history.

Ordinary tool continuation still appends with resident-only Pressure. A saved Q
does not authorize cold-history recall. Checkpoint policy does not change the
Query/Pressure algorithm or add document-ingestion protocol fields.

## SSD publication and failure

One checkpoint writer per owning llama lane, or one for the NInfer Program,
borrows a retained immutable source. It reserves D, writes native segments with
identity, sizes and checksum, and publishes only after the engine revalidates
the owner and source. The worker does not call CUDA or mutate the directory.
Cancelled, replaced or failed writes keep a valid source until the task drains;
unpublished destinations release their quota. Cold records share the existing
process-local spill-file lifetime and cannot be restored after a restart.

Before a cold restore, reserve Host space and validate the complete image before
publishing its bytes to native state. Llama can reject an unreadable candidate
and try an earlier legal point. NInfer's already-admitted materialization
transaction falls back to a clean full prefill on an image-read failure; it does
not change frontier midway through that transaction. Partial native restore
failure follows the backend's clean reset/rollback path.

RAM-only and full/unavailable SSD use the same bounded directory. A hot-record
target can be temporarily exceeded by execution leases or a pending write;
the Host byte limit governs admission. Optional captures can be skipped, so
larger directories do not guarantee more points under a small Host budget.

## Verification

See the [validation record](checkpoint-policy-validation-20261025.md) for current
results, fixed failures and qualification limits.

`kvmem-checkpoint-policy-test` checks retention, identity aliases, tiny budgets,
fixed leases, truncation and exact capture boundaries. `kvmem-checkpoint-image-test`
checks framing, checksum, partial reads, full disk/queue, cancellation, source
lifetime and reservation refund. The native checkpoint-tail test compares exact
KV/recurrent bytes and rejects incomplete coverage.

`tests/checkpoint-coverage-regression.py` exercises ordinary document-first and
separate-question turns, tail/middle/front edits, session switches and actual SSD
reads. `tests/session-restore-regression.py --checkpoint-policy spread` covers
MTP switching and cancellation; `--checkpoint-disk-mib` enables its SSD case.
`--gpu-layers 4 --attention-verify --expect-attention-stage` qualifies the mixed
CPU/GPU sampler on the small fixture against dense FP64. Partial-offload MTP
uses `--mtp-state snapshots`; the default replay path requires GPU recurrent
layers. These options do not change the ordinary request protocol.
These diagnostic runs retain requests, responses, logs, GPU UUID and policy
settings. Their traced timings are not controlled throughput measurements.
NInfer may retain its admitted, quoted reuse count after an optional restore
fails and the prefix is recomputed. Actual restore evidence is
`KVMEM_HISTORY_RESTORE`; an API cache-hit counter alone is insufficient to prove
that a cold checkpoint was used.

The implementation borrows coverage ideas from Strata and the local PolyStrata
v0.1.45-poly.2 source review (release commit
`974552f562aecb3e86cdd2679e76e4515b52b9d2`, `conv_cache.hpp` and `generate.cpp`);
payloads and restore proofs remain native to each
KVMem backend. Changing the default requires separate representative performance
and quality qualification, including 27B and longer contexts.
