/*
 * Self-test for the native bridge, exercising the C ABI from C.
 *
 * Useful for two things:
 *   - proving the bridge works independently of Python, which isolates
 *     ctypes and interpreter issues out of any bug hunt;
 *   - a quick smoke test on a machine with a camera attached.
 *
 * Build with the bridge (see native/CMakeLists.txt) and run it from the
 * directory holding the vendor runtime.
 */

#include <stdio.h>
#include <string.h>

#include "crsdkpy_abi.h"

static void print_error(void)
{
    char detail[512];
    detail[0] = '\0';
    crsdkpy_last_error(detail, (uint32_t)sizeof(detail));
    if (detail[0]) printf("    last_error: %s\n", detail);
}

int main(int argc, char** argv)
{
    const char* adapter_dir = (argc > 1) ? argv[1] : NULL;
    const int32_t version = crsdkpy_abi_version();
    printf("abi version: %d.%d\n", (version >> 16) & 0xFFFF, version & 0xFFFF);
    printf("adapter dir: %s\n", adapter_dir ? adapter_dir : "(none)");

    int32_t status = crsdkpy_init(adapter_dir);
    printf("init: %d\n", status);
    if (status != CRSDKPY_OK) {
        print_error();
        return 1;
    }

    uint32_t count = 0;
    status = crsdkpy_enumerate(3, &count);
    printf("enumerate: %d, cameras=%u\n", status, count);
    if (status != CRSDKPY_OK) print_error();

    for (uint32_t i = 0; i < count; ++i) {
        crsdkpy_camera_info info;
        memset(&info, 0, sizeof(info));
        if (crsdkpy_camera_at(i, &info) == CRSDKPY_OK) {
            printf("  [%u] model=%s transport=%s pid=0x%04X key=%s\n", i,
                   info.model, info.transport, info.usb_pid & 0xFFFF,
                   info.device_key);
        }
    }

    /* Connect only when a camera is actually present. */
    if (count > 0) {
        crsdkpy_camera_info info;
        memset(&info, 0, sizeof(info));
        crsdkpy_camera_at(0, &info);

        uint64_t handle = 0;
        status = crsdkpy_open_session(info.device_key, CRSDKPY_MODE_REMOTE, &handle);
        printf("open_session: %d handle=0x%llx\n", status,
               (unsigned long long)handle);
        if (status == CRSDKPY_OK) {
            int32_t state = -1;
            crsdkpy_connection_state(handle, &state);
            printf("  state: %d\n", state);

            uint32_t properties = 0;
            status = crsdkpy_property_count(handle, &properties);
            printf("  property_count: %d -> %u\n", status, properties);

            crsdkpy_event events[64];
            uint32_t produced = 0;
            crsdkpy_poll_events(handle, events, 64, &produced, 500);
            printf("  events drained: %u\n", produced);

            /* Read-only: this session is in Remote, which has no content
             * index, so the call must be refused rather than return nothing.
             * Nothing is shot and nothing on the card is touched. */
            uint32_t contents = 0;
            status = crsdkpy_list_content(handle, CRSDKPY_SLOT_1, 0, NULL, 0,
                                          &contents);
            printf("  list_content in remote: %d (expected %d)\n", status,
                   CRSDKPY_ERR_UNSUPPORTED);
            if (status != CRSDKPY_ERR_UNSUPPORTED) print_error();

            /* Nothing has been fetched, so nothing may be handed out. */
            uint32_t held = 0;
            status = crsdkpy_take_content_preview(handle, NULL, 0, &held);
            printf("  take with nothing held: %d (expected %d)\n", status,
                   CRSDKPY_ERR_NOT_FOUND);

            crsdkpy_close_session(handle);
            crsdkpy_close_session(handle); /* idempotent */
            printf("  closed twice, ok\n");
        } else {
            print_error();
        }
    }

    printf("shutdown: %d\n", crsdkpy_shutdown());
    printf("SELFTEST DONE\n");
    return 0;
}
