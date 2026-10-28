# Native NInfer prefix admission

With KVMem disabled, a normal follow-up using the native hybrid prefix cache could
fail with HTTP 500, `runtime Begin summary differs from committed admission`.
The ordinary materialization path initialized `reported_reuse_path`, but hybrid
activation left it at `Root` while admitting a Device or Host reuse. The runtime
correctly rejected that inconsistent summary.

Hybrid activation now reports the reuse path from its committed admission quote.
The strict runtime check, cache placement policy, and KVMem Query/Pressure policy
remain intact. This repair is independent of Bonsai formats or kernel changes.

The real-artifact regression has a `restore-admission` scenario for ordinary MTP
heads. It requires a nonzero, block-aligned frontier within the common prefix,
equal Device/Host frontiers, actual Host image and block restores, and identical
continuation tokens from the restored state. It retains the original exact-frontier
fixtures instead of weakening their assertions.

Build `ninfer_prefix_cache_tests`. Set `NINFER_TEST_ARTIFACT` to an explicit artifact
with an ordinary MTP head, select the device by `CUDA_VISIBLE_DEVICES`, and run:

```text
ctest --test-dir build-ninfer -R ^ninfer_hybrid_prefix_admission_test$ --output-on-failure
```

Without an artifact, this model-dependent check returns skip code 77. The focused
executable also accepts `NINFER_HYBRID_REAL_SCENARIO=turns-device-only` to check
ordinary follow-up reuse without Host pressure. Sliced artifacts validate native
execution and state restoration; they are not a model-quality oracle.
