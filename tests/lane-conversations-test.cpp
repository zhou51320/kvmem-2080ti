// Deterministic catalog interleavings; no model or GPU is needed.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "llama-kvmem-driver.h"
#include "llama-kvmem-diag.h"
#include "llama-kvmem-execution.h"
#include "kvmem-session-files.h"
#include "log.h"
#include "nlohmann/json.hpp"
#include <cassert>
#include <iostream>
#include <limits>
#include <set>

using json = nlohmann::json;
struct ServerState : LlamaEngineState {
    std::mutex mu;
    bool fail_finish = false;
};
static void llama_driver_finish(ServerState& state) {
    if (state.fail_finish) throw std::runtime_error("injected finish failure");
    llama_driver_finish(static_cast<LlamaEngineState&>(state));
}
#include "kvmem-lane-conversations.h"

struct kvmem_lane_conversations_test_access {
    static kvmem_lane_conversations::operation handover(kvmem_lane_conversations& catalog) {
        // Exact published state of attach: lane 0 switched from owner 0 to 2;
        // the outgoing owner is idle and can be reclaimed by lane 1.
        auto& incoming = catalog.entries_[2];
        incoming.lane = 0;
        incoming.busy = true;
        catalog.entries_.at(0).lane = -1;
        catalog.resident_[0] = 2;
        catalog.next_id_ = 3;
        kvmem_lane_conversations::operation op;
        op.lane = 0; op.id = 2; op.outgoing = 0;
        op.added = op.attached = op.ready = true;
        return op;
    }
    static void finish_other_lane(kvmem_lane_conversations& catalog, ServerState& state) {
        kvmem_lane_conversations::operation op;
        op.lane = op.id = op.outgoing = 1;
        op.attached = op.ready = true;
        catalog.entries_.at(1).busy = true;
        catalog.finish(op, state);
    }
    static bool exists(const kvmem_lane_conversations& c, int id) { return c.entries_.count(id); }
    static auto& entry(kvmem_lane_conversations& c, int id) { return c.entries_.at(id); }
};

int main() {
    using access = kvmem_lane_conversations_test_access;
    {
        ServerState a, b;
        kvmem_lane_conversations catalog({&a, &b}, 8, 0, true);
        auto op = access::handover(catalog);
        access::finish_other_lane(catalog, b);
        assert(!access::exists(catalog, 0));
        a.cached_tokens = {11, 12, 13};
        catalog.finish(op, a);
        assert(access::entry(catalog, 2).committed.match.rows == 3);
        assert(!access::entry(catalog, 2).busy);
        assert(catalog.status()["retained_sessions"] == 1);
        catalog.finish(op, a); // completion remains idempotent
        assert(access::entry(catalog, 2).committed.match.rows == 3);
    }
    for (bool fail : {false, true}) {
        ServerState a, b;
        kvmem_lane_conversations catalog({&a, &b}, 8, 0);
        auto op = access::handover(catalog);
        // A newer operation has reserved the old owner. A's successful or
        // exceptional completion must not release that operation's protection.
        auto& outgoing = access::entry(catalog, 0);
        outgoing.switching = true;
        outgoing.committed.match.rows = 7;
        a.cached_tokens = {21, 22};
        a.fail_finish = fail;
        catalog.finish(op, a);
        assert(outgoing.switching && outgoing.committed.match.rows == 7);
        assert(!access::entry(catalog, 2).busy);
        assert(access::entry(catalog, 2).committed.match.rows == (fail ? 0 : 2));
    }
    {
        ServerState a, b;
        kvmem_lane_conversations catalog({&a, &b}, 8, 0);
        auto op = access::handover(catalog);
        op.ready = false;
        auto& outgoing = access::entry(catalog, 0);
        outgoing.switching = true;
        outgoing.committed.match.rows = 7;
        a.cached_tokens = {31};
        catalog.finish(op, a);
        assert(!outgoing.switching && outgoing.committed.match.rows == 0);
        assert(!access::entry(catalog, 2).busy);
        assert(access::entry(catalog, 2).committed.match.rows == 0);
    }
    for(int protection=0;protection<3;++protection) {
        ServerState a, b;
        const auto account=a.mm_checkpoint_accounting;
        {
            kvmem_lane_conversations catalog({&a,&b},8,0);
            auto op=access::handover(catalog);
            auto data=std::make_shared<MultimodalCheckpointData>();
            data->recurrent.resize(128);
            const auto charged=data->resident_bytes();
            account->limit=charged;
            data->accounting=account;account->add(charged);
            auto& outgoing=access::entry(catalog,0);
            outgoing.payload.mm_live_checkpoint=std::move(data);
            outgoing.busy=protection==0;outgoing.switching=protection==1;outgoing.refs=protection==2;
            a.mm_checkpoint_reclaim(account,64);
            assert(access::exists(catalog,0) && account->live_bytes==charged);
            outgoing.busy=outgoing.switching=false;outgoing.refs=0;
            a.mm_checkpoint_reclaim(account,64);
            assert(!access::exists(catalog,0) && account->live_bytes==0);
            catalog.finish(op,a); // evicting the old owner cannot invalidate handover completion
            assert(!access::entry(catalog,2).busy);
        }
        assert(!a.mm_checkpoint_reclaim && !b.mm_checkpoint_reclaim);
    }
    std::cout << "PASS lane conversation eviction, ownership, checkpoint admission and failed handover\n";
}
