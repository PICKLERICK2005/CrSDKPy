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
