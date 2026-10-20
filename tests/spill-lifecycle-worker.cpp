#include "kvmem-session-files.h"
#include <iostream>
#include <vector>

// Real process: forced termination bypasses C++ destruction.
int main(int argc, char ** argv) {
    try {
        if (argc != 3) throw std::runtime_error("usage: worker ROOT hold|partial");
        std::string command;
        if (!std::getline(std::cin, command) || command != "start") return 2;
        const auto root = std::filesystem::u8path(argv[1]);
        kvmem_session_files files(root, std::make_shared<kvmem::SpillFile>(root, 4 * 1024 * 1024));
        const std::vector<uint8_t> payload(4096, 0xa7);
        files.save(1, payload.size(), [&](kvmem::SnapshotWriter & out) { out.write(payload.data(), payload.size()); });
        if (std::string(argv[2]) == "partial") {
            files.save(2, 2 * payload.size(), [&](kvmem::SnapshotWriter & out) {
                out.write(payload.data(), payload.size());
                std::cout << "READY" << std::endl;
                std::getline(std::cin, command);
                throw std::runtime_error("partial writer must be terminated");
            });
        } else {
            std::vector<uint8_t> restored(payload.size());
            files.load(1, [&](kvmem::SnapshotReader & in) { in.read(restored.data(), restored.size()); });
            if (restored != payload) throw std::runtime_error("attachment changed");
            std::cout << "READY" << std::endl;
            if (!std::getline(std::cin, command) || command != "quit") return 2;
        }
    } catch (const std::exception & error) { std::cerr << error.what() << '\n'; return 1; }
}
