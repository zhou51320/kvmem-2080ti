// Independent integer-codebook and FP64 tests for the published Bonsai formats.
#include "ggml.h"
#include "ggml-alloc.h"
#include "ggml-backend.h"
#include "ggml-cpu.h"
#include "ggml-impl.h"
#ifdef KVMEM_TEST_CUDA
#include "ggml-cuda.h"
#endif

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <vector>

static void require(bool ok, const char * what) {
    if (!ok) { throw std::runtime_error(what); }
}

// Build bytes from base-3 integers, with a separately generated element array.
// No production packer/dequantizer participates in the expected result.
static void block(ggml_type type, unsigned seed, uint8_t * packed, float * values) {
    const float scale = std::ldexp(1.0f, -4 - int(seed % 5));
    const ggml_fp16_t d = ggml_fp32_to_fp16(scale);
    if (type == GGML_TYPE_PQ2_0) {
        std::memcpy(packed, &d, 2);
        for (int j = 0; j < 32; ++j) {
            const unsigned byte = (seed + 17 * j) & 255;
            packed[2 + j] = uint8_t(byte);
            for (int n = 0; n < 4; ++n) {
                values[4*j + n] = (int((byte >> (2*n)) & 3) - 1) * scale;
            }
        }
        return;
    }
    std::memcpy(packed + 26, &d, 2);
    for (int j = 0; j < 26; ++j) {
        const bool high = j >= 24;
        unsigned code = (seed + 23 * j) % (high ? 81 : 243);
        packed[j] = uint8_t(((high ? code * 3 : code) * 256 + 242) / 243);
        const int count = high ? 4 : 5;
        const int stride = high ? 2 : (j < 16 ? 16 : 8);
        const int base = high ? 120 + j - 24 : (j < 16 ? j : 80 + j - 16);
        for (int n = count - 1; n >= 0; --n) {
            values[base + n*stride] = (int(code % 3) - 1) * scale;
            code /= 3;
        }
    }
}

static void codec(ggml_type type) {
    std::vector<uint8_t> packed(ggml_type_size(type));
    float expected[128], actual[128];
    const auto * traits = ggml_get_type_traits(type);
    for (unsigned seed = 0; seed < 243 * 256; ++seed) {
        block(type, seed, packed.data(), expected);
        traits->to_float(packed.data(), actual, 128);
        require(std::memcmp(expected, actual, sizeof(actual)) == 0, "codec mismatch");
    }
    std::printf("codec %s: all codewords, element positions and five exact scales PASS\n", ggml_type_name(type));
}

static void graph_case(ggml_backend_t backend, ggml_type type, int k, int rows, int tokens, bool swiglu = false) {
    ggml_context * ctx = ggml_init({ggml_tensor_overhead()*32 + ggml_graph_overhead(), nullptr, true});
    require(ctx != nullptr, "context allocation");
    auto * w = ggml_new_tensor_2d(ctx, type, k, rows);
    auto * a = ggml_new_tensor_2d(ctx, GGML_TYPE_F32, k, tokens);
    auto * out = ggml_mul_mat(ctx, w, a);
    auto * up = swiglu ? ggml_new_tensor_2d(ctx, type, k, rows) : nullptr;
    if (up) { out = ggml_swiglu_split(ctx, out, ggml_mul_mat(ctx, up, a)); }
    auto * indices = ggml_new_tensor_1d(ctx, GGML_TYPE_I32, 3);
    auto * gathered = ggml_get_rows(ctx, w, indices);
    auto * graph = ggml_new_graph(ctx);
    ggml_build_forward_expand(graph, out);
    ggml_build_forward_expand(graph, gathered);
    require(ggml_backend_supports_op(backend, out), "MUL_MAT unsupported");
    require(ggml_backend_supports_op(backend, gathered), "GET_ROWS unsupported");
    auto * buffer = ggml_backend_alloc_ctx_tensors(ctx, backend);
    require(buffer != nullptr, "backend allocation");
    std::vector<uint8_t> packed(ggml_nbytes(w));
    std::vector<float> weights(size_t(k)*rows), input(size_t(k)*tokens), actual(size_t(rows)*tokens);
    std::vector<float> up_weights(swiglu ? weights.size() : 0);
    for (int b = 0; b < k*rows/128; ++b) {
        block(type, unsigned(b * 103 + 47), packed.data() + b*ggml_type_size(type), weights.data() + b*128);
    }
    // Every Q8 activation group has exact scale 1/8; this removes activation
    // rounding as an excuse for an indexing, tail, or integer-accumulation error.
    for (size_t i = 0; i < input.size(); ++i) {
        input[i] = (i % 32 == 0 ? 127 : int((i*19 + i/k*11) % 255) - 127) * 0.125f;
    }
    const int32_t ids[] = {rows-1, 0, rows/2};
    ggml_backend_tensor_set(w, packed.data(), 0, packed.size());
    if (up) {
        for (int b = 0; b < k*rows/128; ++b) {
            block(type, unsigned(b * 71 + 113), packed.data() + b*ggml_type_size(type), up_weights.data() + b*128);
        }
        ggml_backend_tensor_set(up, packed.data(), 0, packed.size());
    }
    ggml_backend_tensor_set(a, input.data(), 0, input.size()*sizeof(float));
    ggml_backend_tensor_set(indices, ids, 0, sizeof(ids));
    // Repeated computation also exercises graph reuse and asynchronous ordering.
    for (int repetition = 0; repetition < 3; ++repetition) {
        for (float & value : input) { value = -value; }
        ggml_backend_tensor_set(a, input.data(), 0, input.size()*sizeof(float));
        require(ggml_backend_graph_compute(backend, graph) == GGML_STATUS_SUCCESS, "graph compute");
        ggml_backend_tensor_get(out, actual.data(), 0, actual.size()*sizeof(float));
        double worst = 0;
        for (int t = 0; t < tokens; ++t) {
            // Check every row for boundary cases; sample actual large model shapes.
            for (int r = 0; r < rows; r += rows > 257 ? 97 : 1) {
                double expected = 0;
                for (int j = 0; j < k; ++j) {
                    expected += double(weights[size_t(r)*k + j]) * input[size_t(t)*k + j];
                }
                if (up) {
                    double up_value = 0;
                    for (int j = 0; j < k; ++j) {
                        up_value += double(up_weights[size_t(r)*k + j]) * input[size_t(t)*k + j];
                    }
                    expected = expected / (1.0 + std::exp(-expected)) * up_value;
                }
                worst = std::max(worst, std::abs(double(actual[size_t(t)*rows+r]) - expected));
            }
        }
        if (worst > (swiglu ? 0.001 : 0.0001)) {
            std::fprintf(stderr, "%s %s K=%d N=%d T=%d max_abs=%.9g\n", ggml_backend_name(backend), ggml_type_name(type), k, rows, tokens, worst);
            throw std::runtime_error("FP64 dot-product oracle mismatch");
        }
    }
    std::vector<float> got(size_t(k)*3);
    ggml_backend_tensor_get(gathered, got.data(), 0, got.size()*sizeof(float));
    for (int i = 0; i < 3; ++i) {
        require(std::memcmp(got.data() + i*k, weights.data() + size_t(ids[i])*k, k*sizeof(float)) == 0, "GET_ROWS mismatch");
    }
    std::printf("%s %s K=%d N=%d T=%d: FP64 %s / exact GET_ROWS PASS\n", ggml_backend_name(backend), ggml_type_name(type), k, rows, tokens,
                swiglu ? "MUL_MAT+SWIGLU" : "MUL_MAT");
    ggml_backend_buffer_free(buffer);
    ggml_free(ctx);
}

int main(int argc, char ** argv) {
    try {
        bool cuda = false, boundary_only = false;
        for (int i = 1; i < argc; ++i) {
            if (std::strcmp(argv[i], "--cuda") == 0) { cuda = true; }
            else if (std::strcmp(argv[i], "--boundary-only") == 0) { boundary_only = true; }
            else { throw std::runtime_error("unknown argument"); }
        }
        ggml_backend_t backend = nullptr;
        if (cuda) {
#ifdef KVMEM_TEST_CUDA
            backend = ggml_backend_cuda_init(0);
#endif
        } else {
            backend = ggml_backend_cpu_init();
            ggml_backend_cpu_set_n_threads(backend, 8);
        }
        require(backend != nullptr, "requested backend unavailable");
        for (const auto type : {GGML_TYPE_PQ2_0, GGML_TYPE_PTQ1_0}) {
            codec(type);
            for (int t : {1, 2, 3, 4, 8, 16, 17, 31, 32, 33, 64, 65, 128, 256}) {
                graph_case(backend, type, 384, 129, t);
            }
            for (int t : {1, 4}) { graph_case(backend, type, 384, 129, t, true); }
            if (!boundary_only) for (int t : {1, 4, 33, 128}) {
                graph_case(backend, type, 6144, 2048, t);
                graph_case(backend, type, 5120, 7168, t);
            }
            if (!boundary_only) { graph_case(backend, type, 17408, 5120, 4); }
        }
        ggml_backend_free(backend);
        return 0;
    } catch (const std::exception & error) {
        std::fprintf(stderr, "FAIL: %s\n", error.what());
        return 1;
    }
}
