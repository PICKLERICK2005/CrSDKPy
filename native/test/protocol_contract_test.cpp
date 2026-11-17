// No SDK initialization, discovery, camera connection or capture.
#include "crsdkpy_abi.h"
#include "ipc_protocol.h"

#include <cstdint>
#include <cstdio>

static_assert(sizeof(crsdkpy_ipc_header) == 24, "header ABI changed");
static_assert(sizeof(crsdkpy_ipc_request) == 232, "request ABI changed");
static_assert(sizeof(crsdkpy_ipc_response) == 544, "response ABI changed");
static_assert(sizeof(crsdkpy_event) == 40, "event ABI changed");

int main()
{
    static_assert(CRSDKPY_IPC_VERSION_MINOR == 4, "owned transfers need IPC 1.4");
    static_assert(CRSDKPY_OP_START_OWNED_CONTENT_DOWNLOAD == 29, "owned opcode");
    crsdkpy_ipc_content_args owned = {};
    const uint64_t identity = 0x12345678ABCDEF01ULL;
    owned.reserved = static_cast<uint32_t>(identity);
    owned.reserved2 = static_cast<uint32_t>(identity >> 32);
    if ((uint64_t(owned.reserved) | (uint64_t(owned.reserved2) << 32)) != identity)
        return 3;
    const uint32_t codes[] = {0x0109u, 0x0751u, 0x020Eu, 0xF1234567u};
    for (const uint32_t code : codes) {
        crsdkpy_ipc_request request = {};
        request.u32_arg = code;
        request.i32_arg = 0; // The original native dispatcher incorrectly read this.
        if (crsdkpy_ipc_property_code(&request) != code) return 1;
        request.i32_arg = 0x1234;
        if (crsdkpy_ipc_property_code(&request) != code) return 2;
    }
    std::puts("metadata u32 contract and fixed ABI layouts: PASS");
    return 0;
}
