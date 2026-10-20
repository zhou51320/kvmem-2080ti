#include "common.h"
#include "speculative.h"
#include "ggml-backend.h"

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <stdexcept>
#include <vector>

static void require(bool condition, const char * message) {
    if (!condition) throw std::runtime_error(message);
}

static std::vector<llama_token> propose(common_params params, llama_model * target,
                                      llama_context * context, common_speculative_init_result & draft) {
    params.speculative.draft.ctx_tgt = context;
    params.speculative.draft.ctx_dft = draft.context();
    common_speculative_ptr spec(common_speculative_init(params.speculative, 1));
    require(bool(spec), "native speculative init failed");
    const auto tokens = common_tokenize(context, "Continue this sequence: one two three four five", true, true);
    common_batch batch(context);
    for (size_t i = 0; i < tokens.size(); ++i) batch.add(tokens[i], (llama_pos) i, 0, i + 1 == tokens.size());
    require(llama_process(context, LLAMA_PROCESS_TYPE_DECODE, batch.get()) == 0, "target prefill failed");
    require(common_speculative_process(spec.get(), batch), "draft feature injection failed");
    common_speculative_begin(spec.get(), 0, tokens);
    std::vector<llama_token> result;
    auto & request = common_speculative_get_draft_params(spec.get(), 0);
    request.drafting = true;
    request.pos0 = (llama_pos) tokens.size();
    request.id_last = tokens.back();
    request.prompt = &tokens;
    request.result = &result;
    request.temp = 0;
    common_speculative_draft(spec.get());
    require(result.size() == 5, "native draft width changed");
    for (auto token : result) require(token >= 0 && token < llama_vocab_n_tokens(llama_model_get_vocab(target)), "invalid candidate token");
    return result;
}

int main(int argc, char ** argv) {
    const char * target_path = argc > 1 ? argv[1] : std::getenv("KVMEM_TEST_MODEL");
    const char * draft_path = argc > 2 ? argv[2] : std::getenv("KVMEM_TEST_DFLASH2_DRAFT");
    if (!target_path || !draft_path) return 77;
    try {
        common_init();
        ggml_backend_load_all();
        auto model_params = llama_model_default_params();
        model_params.n_gpu_layers = 99;
        llama_model_ptr target(llama_model_load_from_file(target_path, model_params));
        require(bool(target), "target model load failed");
        common_params params;
        params.n_ctx = 512;
        params.n_batch = params.n_ubatch = 128;
        params.n_gpu_layers = params.speculative.draft.n_gpu_layers = 99;
        params.speculative.types = {COMMON_SPECULATIVE_TYPE_DRAFT_DFLASH};
        params.speculative.draft.mparams.path = draft_path;
        params.speculative.draft.n_max = 5;
        auto context_params = common_context_params_to_llama(params);
        llama_context_ptr first_context(llama_init_from_model(target.get(), context_params));
        llama_context_ptr second_context(llama_init_from_model(target.get(), context_params));
        llama_context_ptr reference_context(llama_init_from_model(target.get(), context_params));
        require(first_context && second_context && reference_context, "target context init failed");
        auto draft_params = common_base_params_to_speculative(params);
        auto first = common_speculative_init_from_params(draft_params, target.get(), first_context.get());
        require(first && first->context(), "first draft context init failed");
        auto weights = first->shared_model();
        require(bool(weights), "draft weight owner missing");
        std::weak_ptr<llama_model> lifetime = weights;
        auto second = common_speculative_init_from_params(draft_params, target.get(), second_context.get(), weights);
        require(second && second->context(), "second draft context init failed");
        require(first->model() == second->model() && first->context() != second->context(), "draft weights or contexts have incorrect ownership");
        first.reset();
        first_context.reset();
        weights.reset();
        require(!lifetime.expired(), "first lane released live shared weights");
        const auto shared_result = propose(params, target.get(), second_context.get(), *second);
        auto reference = common_speculative_init_from_params(draft_params, target.get(), reference_context.get());
        require(reference && reference->context() && reference->model() != second->model(), "independent reference init failed");
        require(shared_result == propose(params, target.get(), reference_context.get(), *reference), "shared weights changed candidate output");
        second.reset();
        require(lifetime.expired(), "draft weights survived their last owning context");
        std::puts("PASS shared native draft survives first-lane release, matches independent load, and frees after the last owner");
    } catch (const std::exception & error) {
        std::fprintf(stderr, "FAIL %s\n", error.what());
        return 1;
    }
    return 0;
}
