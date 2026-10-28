#include "kvmem/monitor.hpp"
#include "kvmem/monitor_rate.hpp"
#include "kvmem/monitor_hardware.hpp"
#include <atomic>
#include <cstdlib>
#include <iostream>
#include <thread>

static void check(bool condition) { if (!condition) std::abort(); }
int main() {
    kvmem::monitor_rate_window fast, slow;
    check(fast.sample(1, 0, 1000)["decode_tps_live"].is_null());
    check(slow.sample(1, 0, 1000)["decode_tps_live"].is_null());
    check(fast.sample(40, 4000000000ULL, 5000)["decode_tps_live"].is_null());
    check(fast.sample(101, 5000000000ULL, 6000)["decode_tps_live"] == 20.0);
    check(slow.sample(26, 5000000000ULL, 6000)["decode_tps_live"] == 5.0);
    check(fast.sample(111, 6000000000ULL, 7000)["decode_tps_sampled_at_ms"] == 6000);
    check(fast.sample(1, 0, 8000)["decode_tps_live"].is_null());
    kvmem::monitor monitor;
    auto a = monitor.begin("/v1/chat/completions");
    auto b = monitor.begin("/v1/responses");
    monitor.update(a, {{"prompt_tokens", 80}, {"cached_tokens", 40}, {"generated_tokens", 2}});
    check(monitor.snapshot()["active"].size() == 2);
    monitor.update(a, {{"finish_reason", "stop"}});
    monitor.finish(a, "completed");
    monitor.finish(a, "disconnected"); // failed delivery after generation: one row, corrected outcome
    auto data = monitor.snapshot();
    check(data["recent"].size() == 1 && data["recent"][0]["status"] == "disconnected");
    check(data["recent"][0]["finish_reason"] == "disconnect");
    check(data["active"][0]["generated_tokens"].is_null());
    check(data["recent"][0]["cached_tokens"] == 40);
    monitor.finish(b, "error");
    std::atomic<bool> done{false};
    std::thread reader([&] { while (!done) check(monitor.snapshot()["recent"].size() <= 100); });
    for (int i = 0; i < 250; ++i) {
        const auto id = monitor.begin("test");
        monitor.update(id, {{"generated_tokens", i}});
        monitor.finish(id, "completed");
    }
    done = true; reader.join();
    check(monitor.snapshot()["recent"].size() == 100);
    for (int i = 0; i < 513; ++i) monitor.begin("queued");
    check(monitor.snapshot()["active"].size() == 512);
    check(monitor.snapshot()["dropped_records"] == 1);
    check(monitor.snapshot()["server_instance"] != kvmem::monitor{}.snapshot()["server_instance"]);
    kvmem::monitor_hardware hardware;
    for (int i = 0; i < 50 && hardware.snapshot()["sampled_at_ms"].is_null(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    check(!hardware.snapshot()["sampled_at_ms"].is_null());
    const auto sampled = hardware.snapshot();
    for (const auto & gpu : sampled["gpus"]) {
        check(gpu.contains("uuid") && gpu.contains("pci_bus_id"));
    }
    std::cout << hardware.snapshot().dump() << '\n';
}
