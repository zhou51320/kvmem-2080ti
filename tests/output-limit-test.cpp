#include "kvmem-webui.h"
#include <cstdlib>

static void check(bool condition) { if (!condition) std::abort(); }
int main() {
    using nlohmann::json;
    for (const char * key : {"max_tokens", "max_completion_tokens"}) {
        for (auto n : {json(65536), json(uint64_t{4294967297}), json(UINT64_MAX)}) {
            int value = 128; std::string error;
            check(kvmem_output_limit(json{{key, n}}, 16384, value, error));
            check(value == 16384);
        }
        for (auto n : {json(-1), json(0)}) {
            int value = 128; std::string error;
            check(kvmem_output_limit(json{{key, n}}, 16384, value, error));
            check(value == (n == -1 ? -1 : 128));
        }
        for (auto n : {json(-2), json(1.5), json(nullptr), json("64"), json(true)}) {
            int value = 128; std::string error;
            check(!kvmem_output_limit(json{{key, n}}, 16384, value, error));
        }
    }
    int value = -1; std::string error;
    check(kvmem_output_limit(json::object(), 4096, value, error) && value == -1);
    check(kvmem_resolve_output_limit(4096, 66, value, error) && value == 4030);
    // Neither a short prompt nor one exceeding a B+R window creates a fixed output cap.
    for (size_t prompt : {size_t{66}, size_t{1024}, size_t{4095}}) {
        value = -1;
        check(kvmem_resolve_output_limit(4096, prompt, value, error));
        check(value == 4096 - static_cast<int>(prompt));
    }
    value = -1;
    check(!kvmem_resolve_output_limit(4096, 4096, value, error));
    check(!kvmem_resolve_output_limit(4096, SIZE_MAX, value, error));
    value = 16;
    check(kvmem_resolve_output_limit(4096, 66, value, error) && value == 16);
    value = 4096;
    check(!kvmem_resolve_output_limit(4096, 66, value, error));
    check(kvmem_output_limit(json{{"max_tokens", 8}, {"max_completion_tokens", 32}}, 4096, value, error));
    check(value == 8);
}
