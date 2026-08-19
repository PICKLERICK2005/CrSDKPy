/*
 * CrSDKPy host IPC protocol.
 *
 * Framing
 * -------
 * Every message is a fixed 24-byte header followed by two optional payloads:
 *
 *     [ header 24 bytes ][ meta meta_len bytes ][ blob blob_len bytes ]
 *
 * meta carries a fixed-layout POD struct for the message type. blob carries a
 * homogeneous array of POD items, or raw bytes. Keeping the two lengths
 * separate is what lets image buffers travel as themselves rather than encoded
 * into a text payload.
 *
 * A few operations need more than the fixed struct holds. Those append a
 * second POD immediately after it, which both sides read only when the
 * declared meta_len covers it. Extending this way keeps the struct layouts
 * frozen, so the protocol version does not move for an additive change.
 *
 * There is no JSON anywhere: the wire format for cameras, properties and
 * events is the same POD already defined in crsdkpy_abi.h, so both sides
 * describe it once and neither needs a parser.
 *
 * Transport
 * ---------
 * The host speaks the protocol over its inherited stdin/stdout pipes. See
 * native/host/main.cpp for why, and for how the C-level stdout is redirected
 * away so that vendor printf output cannot corrupt the frame stream.
 */

#ifndef CRSDKPY_IPC_PROTOCOL_H
#define CRSDKPY_IPC_PROTOCOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 'C''R''P''Y'. Guards against a desynchronised or foreign stream. */
#define CRSDKPY_IPC_MAGIC 0x43525059u

/* Bump major on any incompatible change; both sides reject a mismatch. */
#define CRSDKPY_IPC_VERSION_MAJOR 1
#define CRSDKPY_IPC_VERSION_MINOR 0

/* Refuse absurd frames rather than attempting a huge allocation. */
#define CRSDKPY_IPC_MAX_META (1u << 16)
#define CRSDKPY_IPC_MAX_BLOB (64u << 20)

/* Message types. */
#define CRSDKPY_MSG_HELLO     1  /* client -> host */
#define CRSDKPY_MSG_HELLO_ACK 2  /* host -> client */
#define CRSDKPY_MSG_REQUEST   3  /* client -> host */
#define CRSDKPY_MSG_RESPONSE  4  /* host -> client, matches request_id */
#define CRSDKPY_MSG_EVENT     5  /* host -> client, unsolicited, request_id 0 */
#define CRSDKPY_MSG_BYE       6  /* client -> host, graceful shutdown */

/* Operations. Unknown ops are refused explicitly rather than ignored. */
#define CRSDKPY_OP_PING              1
#define CRSDKPY_OP_INIT              2
#define CRSDKPY_OP_SHUTDOWN          3
#define CRSDKPY_OP_ENUMERATE         4
#define CRSDKPY_OP_CAMERA_AT         5
#define CRSDKPY_OP_OPEN_SESSION      6
#define CRSDKPY_OP_CLOSE_SESSION     7
#define CRSDKPY_OP_CONNECTION_STATE  8
#define CRSDKPY_OP_POLL_EVENTS       9
#define CRSDKPY_OP_LIST_PROPERTIES  10
#define CRSDKPY_OP_GET_PROPERTY     11
#define CRSDKPY_OP_SET_PROPERTY     12
#define CRSDKPY_OP_SEND_COMMAND     13
#define CRSDKPY_OP_LIST_CONTENT     14
#define CRSDKPY_OP_CONTENT_PREVIEW  15
#define CRSDKPY_OP_CONFIGURE_POSTVIEW 16
#define CRSDKPY_OP_PULL_POSTVIEW      17
#define CRSDKPY_OP_LIVE_VIEW_INFO     18
#define CRSDKPY_OP_LIVE_VIEW_FRAME    19
/* Test-only: makes the host terminate abruptly so process-death handling can
 * be exercised without provoking a real native fault. */
#define CRSDKPY_OP_TEST_CRASH      900

/* Error categories, so Python can rebuild its exception hierarchy without
 * pattern-matching on message text. */
#define CRSDKPY_CAT_NONE          0
#define CRSDKPY_CAT_VENDOR        1  /* raw code is a vendor error */
#define CRSDKPY_CAT_INVALID_ARG   2
#define CRSDKPY_CAT_STALE_HANDLE  3
#define CRSDKPY_CAT_NOT_STARTED   4
#define CRSDKPY_CAT_UNSUPPORTED   5
#define CRSDKPY_CAT_SDK_MISSING   6
#define CRSDKPY_CAT_ADAPTER_PATH  7  /* the known adaptor-create failure */
#define CRSDKPY_CAT_NOT_CONNECTED 8
#define CRSDKPY_CAT_TIMEOUT       9
#define CRSDKPY_CAT_NOT_FOUND    10

#pragma pack(push, 1)

typedef struct crsdkpy_ipc_header {
    uint32_t magic;
    uint16_t version_major;
    uint16_t message_type;
    uint32_t request_id;   /* 0 for events */
    uint32_t meta_len;
    uint32_t blob_len;
    uint32_t reserved;
} crsdkpy_ipc_header;      /* 24 bytes */

typedef struct crsdkpy_ipc_hello {
    uint16_t version_major;
    uint16_t version_minor;
    uint32_t reserved;
} crsdkpy_ipc_hello;

typedef struct crsdkpy_ipc_hello_ack {
    uint16_t protocol_major;
    uint16_t protocol_minor;
    uint16_t abi_major;
    uint16_t abi_minor;
    uint32_t host_version;      /* (major << 16) | minor */
    int32_t  sdk_available;     /* 1 when the vendor library loaded */
    char     host_build[64];
    char     sdk_note[192];
} crsdkpy_ipc_hello_ack;

typedef struct crsdkpy_ipc_request {
    uint16_t op;
    uint16_t reserved;
    uint32_t u32_arg;   /* property code, camera index */
    int32_t  i32_arg;   /* control mode, timeout_ms, enumerate timeout */
    int32_t  i32_arg2;
    uint64_t handle;    /* session handle, 0 when not applicable */
    char     text[208]; /* device key or adapter directory, NUL terminated */
} crsdkpy_ipc_request;

typedef struct crsdkpy_ipc_response {
    int32_t  status;      /* 0 ok; negative bridge code; positive vendor code */
    int32_t  category;    /* CRSDKPY_CAT_* */
    uint32_t count;       /* items in blob */
    uint32_t item_size;   /* bytes per blob item, 0 when none */
    uint64_t handle;      /* opened session handle */
    int32_t  i32_result;  /* connection state and similar scalars */
    int32_t  reserved;
    char     message[512];
} crsdkpy_ipc_response;

/* Extra arguments for the content operations.
 *
 * Sent as a tail on the request meta, so the fixed request struct keeps its
 * layout and its size and no protocol version bump is needed. Both sides
 * already tolerate a meta payload longer than the struct they read. */
typedef struct crsdkpy_ipc_content_args {
    uint32_t slot;
    uint32_t content_id;
    uint32_t file_id;
    uint32_t after_content_id;
    int32_t  kind;
    int32_t  timeout_ms;
    uint32_t reserved;
    uint32_t reserved2;
} crsdkpy_ipc_content_args;

#pragma pack(pop)

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CRSDKPY_IPC_PROTOCOL_H */
