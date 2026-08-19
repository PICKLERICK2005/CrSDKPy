// crsdkpy_host - hosts the vendor SDK outside the Python interpreter.
//
// Why this process exists
// -----------------------
// 1. The vendor SDK resolves its transport-adapter directory against the host
//    executable's directory. Measured: identical bridge code succeeds from an
//    executable beside CrAdapter/ and fails with an adaptor-create error under
//    an interpreter that is not, regardless of working directory or DLL search
//    path. Placing a first-party executable beside the runtime satisfies the
//    vendor's expectation without touching the user's Python installation.
// 2. Vendor code then runs in its own process, so a native fault takes down a
//    replaceable helper instead of the caller's interpreter.
//
// Transport: the inherited stdin/stdout pipes.
//   - local by construction, with no socket and no network surface at all;
//   - process death is a clean EOF on the pipe, needing no liveness protocol;
//   - identical on Windows and POSIX, so the future Linux/macOS host needs no
//     separate transport;
//   - a pure-Python fake host can speak it, so protocol and lifecycle tests
//     run in CI with no native build and no camera.
//
// The obvious hazard of using stdout is that vendor code may print to it and
// corrupt the frame stream. That is handled below: the real stdout handle is
// duplicated for exclusive protocol use, then the C-level stdout is redirected
// to the null device before the vendor library is touched.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#if defined(_WIN32)
#  include <fcntl.h>
#  include <io.h>
#  define CRSDKPY_NULL_DEVICE "NUL"
#  define crsdkpy_read _read
#  define crsdkpy_write _write
#  define crsdkpy_dup _dup
#  define crsdkpy_fileno _fileno
#else
#  include <unistd.h>
#  define CRSDKPY_NULL_DEVICE "/dev/null"
#  define crsdkpy_read read
#  define crsdkpy_write write
#  define crsdkpy_dup dup
#  define crsdkpy_fileno fileno
#endif

#include "crsdkpy_abi.h"
#include "ipc_protocol.h"

namespace {

const uint32_t kHostVersion = (1u << 16) | 0u;

int g_in_fd = -1;
int g_out_fd = -1;   // private duplicate; never the same as C stdout

// Claim the protocol stream, then point C stdout somewhere harmless so vendor
// printf output cannot interleave with frames.
bool claim_streams()
{
    g_in_fd = crsdkpy_fileno(stdin);
    g_out_fd = crsdkpy_dup(crsdkpy_fileno(stdout));
    if (g_in_fd < 0 || g_out_fd < 0) return false;
#if defined(_WIN32)
    _setmode(g_in_fd, _O_BINARY);
    _setmode(g_out_fd, _O_BINARY);
#endif
    std::freopen(CRSDKPY_NULL_DEVICE, "w", stdout);
    return true;
}

bool read_exact(void* buffer, uint32_t length)
{
    char* cursor = static_cast<char*>(buffer);
    uint32_t remaining = length;
    while (remaining > 0) {
        const int got = crsdkpy_read(g_in_fd, cursor, remaining);
        if (got <= 0) return false;  // EOF or error: the client is gone
        cursor += got;
        remaining -= static_cast<uint32_t>(got);
    }
    return true;
}

bool write_exact(const void* buffer, uint32_t length)
{
    const char* cursor = static_cast<const char*>(buffer);
    uint32_t remaining = length;
    while (remaining > 0) {
        const int put = crsdkpy_write(g_out_fd, cursor, remaining);
        if (put <= 0) return false;
        cursor += put;
        remaining -= static_cast<uint32_t>(put);
    }
    return true;
}

bool send_frame(uint16_t type, uint32_t request_id, const void* meta,
                uint32_t meta_len, const void* blob, uint32_t blob_len)
{
    crsdkpy_ipc_header header;
    std::memset(&header, 0, sizeof(header));
    header.magic = CRSDKPY_IPC_MAGIC;
    header.version_major = CRSDKPY_IPC_VERSION_MAJOR;
    header.message_type = type;
    header.request_id = request_id;
    header.meta_len = meta_len;
    header.blob_len = blob_len;
    if (!write_exact(&header, sizeof(header))) return false;
    if (meta_len && !write_exact(meta, meta_len)) return false;
    if (blob_len && !write_exact(blob, blob_len)) return false;
    return true;
}

void fill(crsdkpy_ipc_response& response, int32_t status, int32_t category,
          const char* message)
{
    std::memset(&response, 0, sizeof(response));
    response.status = status;
    response.category = category;
    if (message) {
        std::strncpy(response.message, message, sizeof(response.message) - 1);
    }
}

// Turn a bridge/vendor status into a category Python can map to an exception
// without parsing text.
int32_t categorise(int32_t status)
{
    if (status == CRSDKPY_OK) return CRSDKPY_CAT_NONE;
    if (status > 0) {
        // The adaptor family is the known executable-directory failure.
        return ((status & 0xFF00) == 0x8700) ? CRSDKPY_CAT_ADAPTER_PATH
                                             : CRSDKPY_CAT_VENDOR;
    }
    switch (status) {
    case CRSDKPY_ERR_INVALID_HANDLE:   return CRSDKPY_CAT_STALE_HANDLE;
    case CRSDKPY_ERR_NOT_INITIALIZED:  return CRSDKPY_CAT_NOT_STARTED;
    case CRSDKPY_ERR_UNSUPPORTED:      return CRSDKPY_CAT_UNSUPPORTED;
    case CRSDKPY_ERR_SDK_INIT_FAILED:  return CRSDKPY_CAT_SDK_MISSING;
    case CRSDKPY_ERR_NOT_CONNECTED:    return CRSDKPY_CAT_NOT_CONNECTED;
    case CRSDKPY_ERR_TIMEOUT:          return CRSDKPY_CAT_TIMEOUT;
    case CRSDKPY_ERR_NOT_FOUND:        return CRSDKPY_CAT_NOT_FOUND;
    case CRSDKPY_ERR_INVALID_ARG:      return CRSDKPY_CAT_INVALID_ARG;
    default:                           return CRSDKPY_CAT_VENDOR;
    }
}

void attach_bridge_error(crsdkpy_ipc_response& response)
{
    char detail[512];
    detail[0] = '\0';
    crsdkpy_last_error(detail, static_cast<uint32_t>(sizeof(detail)));
    if (detail[0]) {
        std::strncpy(response.message, detail, sizeof(response.message) - 1);
    }
}

// Handles one request.
//
// blob_out is only populated for array- or bytes-returning ops. meta_tail is
// only populated by ops whose result does not fit the fixed response struct;
// it is appended to the response meta.
void dispatch(const crsdkpy_ipc_request& request,
              const crsdkpy_ipc_content_args& content,
              crsdkpy_ipc_response& response, std::vector<char>& blob_out,
              std::vector<char>& meta_tail)
{
    blob_out.clear();
    meta_tail.clear();
    fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");

    switch (request.op) {
    case CRSDKPY_OP_PING:
        return;

    case CRSDKPY_OP_INIT: {
        const char* adapter = request.text[0] ? request.text : nullptr;
        const int32_t status = crsdkpy_init(adapter);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) attach_bridge_error(response);
        return;
    }

    case CRSDKPY_OP_SHUTDOWN: {
        const int32_t status = crsdkpy_shutdown();
        fill(response, status, categorise(status), "");
        return;
    }

    case CRSDKPY_OP_ENUMERATE: {
        uint32_t count = 0;
        const int32_t status = crsdkpy_enumerate(request.i32_arg, &count);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) {
            attach_bridge_error(response);
            return;
        }
        // Return every camera in one frame: the caller always wants the set.
        blob_out.resize(sizeof(crsdkpy_camera_info) * count);
        uint32_t produced = 0;
        for (uint32_t i = 0; i < count; ++i) {
            crsdkpy_camera_info info;
            std::memset(&info, 0, sizeof(info));
            if (crsdkpy_camera_at(i, &info) == CRSDKPY_OK) {
                std::memcpy(blob_out.data() + sizeof(info) * produced, &info,
                            sizeof(info));
                ++produced;
            }
        }
        blob_out.resize(sizeof(crsdkpy_camera_info) * produced);
        response.count = produced;
        response.item_size = sizeof(crsdkpy_camera_info);
        return;
    }

    case CRSDKPY_OP_OPEN_SESSION: {
        uint64_t handle = 0;
        const int32_t status =
            crsdkpy_open_session(request.text, request.i32_arg, &handle);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) {
            attach_bridge_error(response);
            return;
        }
        response.handle = handle;
        return;
    }

    case CRSDKPY_OP_CLOSE_SESSION: {
        const int32_t status = crsdkpy_close_session(request.handle);
        fill(response, status, categorise(status), "");
        return;
    }

    case CRSDKPY_OP_CONNECTION_STATE: {
        int32_t state = CRSDKPY_STATE_CLOSED;
        const int32_t status = crsdkpy_connection_state(request.handle, &state);
        fill(response, status, categorise(status), "");
        response.i32_result = state;
        return;
    }

    case CRSDKPY_OP_POLL_EVENTS: {
        const uint32_t capacity = 256;
        std::vector<crsdkpy_event> events(capacity);
        uint32_t produced = 0;
        const int32_t status = crsdkpy_poll_events(
            request.handle, events.data(), capacity, &produced, request.i32_arg);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) {
            attach_bridge_error(response);
            return;
        }
        blob_out.resize(sizeof(crsdkpy_event) * produced);
        if (produced) {
            std::memcpy(blob_out.data(), events.data(), blob_out.size());
        }
        response.count = produced;
        response.item_size = sizeof(crsdkpy_event);
        return;
    }

    case CRSDKPY_OP_LIST_PROPERTIES: {
        uint32_t count = 0;
        int32_t status = crsdkpy_list_properties(request.handle, nullptr, 0, &count);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        std::vector<crsdkpy_property> properties(count ? count : 1);
        uint32_t produced = 0;
        if (count) {
            status = crsdkpy_list_properties(request.handle, properties.data(),
                                             count, &produced);
            if (status != CRSDKPY_OK) {
                fill(response, status, categorise(status), "");
                attach_bridge_error(response);
                return;
            }
        }
        fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
        blob_out.resize(sizeof(crsdkpy_property) * produced);
        if (produced) {
            std::memcpy(blob_out.data(), properties.data(), blob_out.size());
        }
        response.count = produced;
        response.item_size = sizeof(crsdkpy_property);
        return;
    }

    case CRSDKPY_OP_GET_PROPERTY: {
        crsdkpy_property property;
        std::memset(&property, 0, sizeof(property));
        const int32_t status =
            crsdkpy_get_property(request.handle, request.u32_arg, &property);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) {
            attach_bridge_error(response);
            return;
        }
        blob_out.resize(sizeof(property));
        std::memcpy(blob_out.data(), &property, sizeof(property));
        response.count = 1;
        response.item_size = sizeof(crsdkpy_property);
        return;
    }

    case CRSDKPY_OP_SET_PROPERTY: {
        // The value rides in the 64-bit handle-sized slot alongside the
        // session handle, so a full vendor value survives the boundary.
        const int32_t status = crsdkpy_set_property(
            request.handle, request.u32_arg,
            (static_cast<int64_t>(request.i32_arg2) << 32) |
                (static_cast<uint32_t>(request.i32_arg)));
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) attach_bridge_error(response);
        return;
    }

    case CRSDKPY_OP_SEND_COMMAND: {
        const int32_t status = crsdkpy_send_command(
            request.handle, request.u32_arg, request.i32_arg);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) attach_bridge_error(response);
        return;
    }

    case CRSDKPY_OP_LIST_CONTENT: {
        uint32_t count = 0;
        int32_t status = crsdkpy_list_content(request.handle, content.slot,
                                              content.after_content_id, nullptr, 0,
                                              &count);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        // Headroom: the camera may still be writing, so the list can grow
        // between the sizing call and this one. Without it a shot landing at
        // exactly the wrong moment turns into a buffer-too-small failure.
        const uint32_t capacity = count + 8;
        std::vector<crsdkpy_content> items(capacity);
        uint32_t produced = 0;
        if (count) {
            status = crsdkpy_list_content(request.handle, content.slot,
                                          content.after_content_id, items.data(),
                                          capacity, &produced);
            if (status != CRSDKPY_OK) {
                fill(response, status, categorise(status), "");
                attach_bridge_error(response);
                return;
            }
        }
        fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
        blob_out.resize(sizeof(crsdkpy_content) * produced);
        if (produced) {
            std::memcpy(blob_out.data(), items.data(), blob_out.size());
        }
        response.count = produced;
        response.item_size = sizeof(crsdkpy_content);
        return;
    }

    case CRSDKPY_OP_CONTENT_PREVIEW: {
        // Fetch and take in one round trip: the two-step split exists on the C
        // ABI only because the byte count is unknown until the transfer ends,
        // and the host is the side that can size a buffer without a second
        // pipe exchange.
        crsdkpy_preview_info info;
        std::memset(&info, 0, sizeof(info));
        int32_t status = crsdkpy_fetch_content_preview(
            request.handle, content.slot, content.content_id, content.file_id,
            content.kind, content.timeout_ms, &info);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        std::vector<char> bytes(info.byte_length ? info.byte_length : 1);
        uint32_t produced = 0;
        status = crsdkpy_take_content_preview(
            request.handle, reinterpret_cast<uint8_t*>(bytes.data()),
            info.byte_length, &produced);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
        blob_out.assign(bytes.begin(), bytes.begin() + produced);
        response.count = produced;
        response.item_size = 1;
        meta_tail.resize(sizeof(info));
        std::memcpy(meta_tail.data(), &info, sizeof(info));
        return;
    }

    case CRSDKPY_OP_CONFIGURE_POSTVIEW: {
        const int32_t status = crsdkpy_configure_postview(
            request.handle, request.i32_arg, request.i32_arg2);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) attach_bridge_error(response);
        return;
    }

    case CRSDKPY_OP_PULL_POSTVIEW: {
        crsdkpy_postview_info info;
        std::memset(&info, 0, sizeof(info));
        int32_t status = crsdkpy_pull_postview(request.handle, &info);
        if (status == CRSDKPY_ERR_NOT_FOUND) {
            // Nothing announced yet. An empty success, not an error: the
            // caller polls this while waiting for the camera.
            fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
            response.count = 0;
            return;
        }
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        std::vector<char> bytes(info.byte_length ? info.byte_length : 1);
        uint32_t produced = 0;
        status = crsdkpy_take_postview(request.handle,
                                       reinterpret_cast<uint8_t*>(bytes.data()),
                                       info.byte_length, &produced);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
        blob_out.assign(bytes.begin(), bytes.begin() + produced);
        response.count = produced;
        response.item_size = 1;
        meta_tail.resize(sizeof(info));
        std::memcpy(meta_tail.data(), &info, sizeof(info));
        return;
    }

    case CRSDKPY_OP_LIVE_VIEW_INFO: {
        crsdkpy_live_view_info view;
        std::memset(&view, 0, sizeof(view));
        const int32_t status = crsdkpy_get_live_view_info(request.handle, &view);
        fill(response, status, categorise(status), "");
        if (status != CRSDKPY_OK) {
            attach_bridge_error(response);
            return;
        }
        meta_tail.resize(sizeof(view));
        std::memcpy(meta_tail.data(), &view, sizeof(view));
        return;
    }

    case CRSDKPY_OP_LIVE_VIEW_FRAME: {
        crsdkpy_frame_info frame;
        std::memset(&frame, 0, sizeof(frame));
        int32_t status =
            crsdkpy_get_live_view_frame(request.handle, nullptr, 0, &frame);
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        std::vector<char> bytes(frame.byte_length ? frame.byte_length : 1);
        status = crsdkpy_get_live_view_frame(
            request.handle, reinterpret_cast<uint8_t*>(bytes.data()),
            frame.byte_length, &frame);
        if (status == CRSDKPY_ERR_NOT_FOUND) {
            // No new frame. Ordinary around an exposure, so it travels as an
            // empty success rather than an error the caller has to catch.
            fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
            response.count = 0;
            return;
        }
        if (status != CRSDKPY_OK) {
            fill(response, status, categorise(status), "");
            attach_bridge_error(response);
            return;
        }
        fill(response, CRSDKPY_OK, CRSDKPY_CAT_NONE, "");
        blob_out.assign(bytes.begin(), bytes.begin() + frame.byte_length);
        response.count = frame.byte_length;
        response.item_size = 1;
        meta_tail.resize(sizeof(frame));
        std::memcpy(meta_tail.data(), &frame, sizeof(frame));
        return;
    }

    case CRSDKPY_OP_TEST_CRASH:
        // Deliberate abrupt exit so the client's process-death handling can be
        // exercised without provoking a genuine native fault.
        std::_Exit(97);

    default:
        fill(response, CRSDKPY_ERR_UNSUPPORTED, CRSDKPY_CAT_UNSUPPORTED,
             "operation is not implemented by this host; it covers discovery, "
             "sessions, events, properties, commands and the content index");
        return;
    }
}

bool handle_hello(uint32_t request_id, const std::vector<char>& meta)
{
    crsdkpy_ipc_hello hello;
    std::memset(&hello, 0, sizeof(hello));
    if (meta.size() >= sizeof(hello)) std::memcpy(&hello, meta.data(), sizeof(hello));

    crsdkpy_ipc_hello_ack ack;
    std::memset(&ack, 0, sizeof(ack));
    ack.protocol_major = CRSDKPY_IPC_VERSION_MAJOR;
    ack.protocol_minor = CRSDKPY_IPC_VERSION_MINOR;
    const int32_t abi = crsdkpy_abi_version();
    ack.abi_major = static_cast<uint16_t>((abi >> 16) & 0xFFFF);
    ack.abi_minor = static_cast<uint16_t>(abi & 0xFFFF);
    ack.host_version = kHostVersion;
    ack.sdk_available = 1;  // the bridge linked, so the vendor library loaded
    std::strncpy(ack.host_build, "crsdkpy_host " __DATE__, sizeof(ack.host_build) - 1);
    std::strncpy(ack.sdk_note,
                 "vendor SDK linked; adapter directory is resolved against this "
                 "executable's directory",
                 sizeof(ack.sdk_note) - 1);

    // Reject an incompatible peer loudly instead of limping along.
    if (hello.version_major != CRSDKPY_IPC_VERSION_MAJOR) {
        std::strncpy(ack.sdk_note, "protocol major version mismatch",
                     sizeof(ack.sdk_note) - 1);
        send_frame(CRSDKPY_MSG_HELLO_ACK, request_id, &ack, sizeof(ack), nullptr, 0);
        return false;
    }
    return send_frame(CRSDKPY_MSG_HELLO_ACK, request_id, &ack, sizeof(ack), nullptr, 0);
}

}  // namespace

int main(int argc, char** argv)
{
    (void)argc;
    (void)argv;
    if (!claim_streams()) return 2;

    std::vector<char> meta;
    std::vector<char> blob;
    for (;;) {
        crsdkpy_ipc_header header;
        if (!read_exact(&header, sizeof(header))) break;  // client gone
        if (header.magic != CRSDKPY_IPC_MAGIC) return 3;  // desynchronised
        if (header.meta_len > CRSDKPY_IPC_MAX_META) return 4;
        if (header.blob_len > CRSDKPY_IPC_MAX_BLOB) return 4;

        meta.assign(header.meta_len, 0);
        if (header.meta_len && !read_exact(meta.data(), header.meta_len)) break;
        blob.assign(header.blob_len, 0);
        if (header.blob_len && !read_exact(blob.data(), header.blob_len)) break;

        if (header.message_type == CRSDKPY_MSG_HELLO) {
            if (!handle_hello(header.request_id, meta)) return 5;
            continue;
        }
        if (header.message_type == CRSDKPY_MSG_BYE) {
            crsdkpy_shutdown();
            return 0;
        }
        if (header.message_type != CRSDKPY_MSG_REQUEST) {
            crsdkpy_ipc_response response;
            fill(response, CRSDKPY_ERR_INVALID_ARG, CRSDKPY_CAT_INVALID_ARG,
                 "unknown message type");
            if (!send_frame(CRSDKPY_MSG_RESPONSE, header.request_id, &response,
                            sizeof(response), nullptr, 0)) {
                break;
            }
            continue;
        }

        crsdkpy_ipc_request request;
        std::memset(&request, 0, sizeof(request));
        if (meta.size() >= sizeof(request)) {
            std::memcpy(&request, meta.data(), sizeof(request));
        } else {
            crsdkpy_ipc_response response;
            fill(response, CRSDKPY_ERR_INVALID_ARG, CRSDKPY_CAT_INVALID_ARG,
                 "truncated request payload");
            if (!send_frame(CRSDKPY_MSG_RESPONSE, header.request_id, &response,
                            sizeof(response), nullptr, 0)) {
                break;
            }
            continue;
        }
        request.text[sizeof(request.text) - 1] = '\0';

        // Optional tail carrying the arguments that do not fit the fixed
        // struct. Absent for every operation that does not need it.
        crsdkpy_ipc_content_args content;
        std::memset(&content, 0, sizeof(content));
        if (meta.size() >= sizeof(request) + sizeof(content)) {
            std::memcpy(&content, meta.data() + sizeof(request), sizeof(content));
        }

        crsdkpy_ipc_response response;
        std::vector<char> out_blob;
        std::vector<char> out_meta_tail;
        dispatch(request, content, response, out_blob, out_meta_tail);

        std::vector<char> out_meta(sizeof(response) + out_meta_tail.size());
        std::memcpy(out_meta.data(), &response, sizeof(response));
        if (!out_meta_tail.empty()) {
            std::memcpy(out_meta.data() + sizeof(response), out_meta_tail.data(),
                        out_meta_tail.size());
        }
        if (!send_frame(CRSDKPY_MSG_RESPONSE, header.request_id, out_meta.data(),
                        static_cast<uint32_t>(out_meta.size()),
                        out_blob.empty() ? nullptr : out_blob.data(),
                        static_cast<uint32_t>(out_blob.size()))) {
            break;
        }
    }

    crsdkpy_shutdown();
    return 0;
}
