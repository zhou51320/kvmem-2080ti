# HTTP workers and host memory

The llama.cpp KVMem server runs inference on HTTP workers. Its automatic pool
is fixed at two workers per inference lane (`--parallel`), independent of the
host CPU count. Both omitted and nonpositive `--threads-http` values select
this default. A positive value selects that fixed number of workers; the pool
does not expand when requests queue. `/props` startup configuration reports
`http.threads` and `http.max_threads`.

This bounds the number of allocator arenas used by successive host-KV
allocations. With the previous hardware-sized/expanding pool, a glibc CPU
reproducer retained freed KV memory in different workers' arenas and RSS grew
with independent conversations, although live storage stayed constant. A
fixed pool reuses those arenas. It does not promise that RSS immediately falls
after releasing a conversation, or that all allocator fragmentation disappears.
The pool policy is portable; the demonstrated arena mechanism is specific to
glibc and is not evidence of identical Windows or musl behavior.

Two workers per lane leave capacity for a health/control request during one
active generation per lane. A fully occupied HTTP pool queues further requests,
including health requests. Increase `--threads-http` explicitly if the expected
concurrent HTTP load requires it, accounting for the extra memory high-water
marks. An explicit single worker can delay control requests during generation.

The fix does not change process-wide allocator settings, trim memory after
every request, restart the model, or change KV ownership or inference policy.
The model-free HTTP test checks queue bounds, health traffic and streaming.
The Linux/glibc memory test uses the real host KV store and includes an optional
`--unbounded-control` diagnostic to demonstrate that the old pool fails its
memory plateau invariant.
