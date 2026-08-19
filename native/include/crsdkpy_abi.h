/*
 * CrSDKPy native bridge - strict C ABI.
 *
 * This header is the entire contract between the native bridge and Python.
 * It deliberately contains no vendor types, no C++ constructs and no pointers
 * to anything the caller does not own, so that:
 *
 *   - the bridge is independent of the CPython ABI (loaded with ctypes);
 *   - vendor C++ complexity stays entirely on the native side;
 *   - no vendor-owned memory is ever visible to Python.
 *
 * Conventions
 * -----------
 *  - Every function returns int32_t: 0 on success, a negative CRSDKPY_ERR_*
 *    value for bridge-level failures, or a positive value carrying the vendor
 *    error code verbatim.
 *  - Handles are opaque uint64_t values with an embedded generation counter,
 *    so a stale handle is rejected rather than silently reused.
 *  - Strings are fixed-size UTF-8 buffers inside POD structs, always NUL
 *    terminated. Nothing is heap-shared across the boundary.
 *  - Array getters take (buffer, capacity) and write the number of items
 *    actually produced to out_count. Call with capacity 0 to size first.
 *
 * The Sony Camera Remote SDK is NOT distributed with CrSDKPy. Building this
 * bridge requires a user-supplied copy.
 */

#ifndef CRSDKPY_ABI_H
#define CRSDKPY_ABI_H

#include <stdint.h>

#if defined(_WIN32)
#  if defined(CRSDKPY_BUILDING)
#    define CRSDKPY_API __declspec(dllexport)
#  else
#    define CRSDKPY_API __declspec(dllimport)
#  endif
#else
#  define CRSDKPY_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Bump the major component on any incompatible layout or signature change.
 * Python refuses to load a library whose major version it does not know. */
#define CRSDKPY_ABI_VERSION_MAJOR 1
#define CRSDKPY_ABI_VERSION_MINOR 1

/* ---- status codes ---------------------------------------------------- */
#define CRSDKPY_OK                    0
#define CRSDKPY_ERR_UNKNOWN          (-1)
#define CRSDKPY_ERR_NOT_INITIALIZED  (-2)
#define CRSDKPY_ERR_ALREADY_INIT     (-3)
#define CRSDKPY_ERR_INVALID_ARG      (-4)
#define CRSDKPY_ERR_INVALID_HANDLE   (-5)   /* unknown or stale handle */
#define CRSDKPY_ERR_BUFFER_TOO_SMALL (-6)
#define CRSDKPY_ERR_NOT_FOUND        (-7)
#define CRSDKPY_ERR_SDK_INIT_FAILED  (-8)
#define CRSDKPY_ERR_CONNECT_FAILED   (-9)
#define CRSDKPY_ERR_TIMEOUT          (-10)
#define CRSDKPY_ERR_UNSUPPORTED      (-11)
#define CRSDKPY_ERR_NOT_CONNECTED    (-12)

/* ---- enumerations ----------------------------------------------------- */
/* Control mode, matching the vendor's own ordering. */
#define CRSDKPY_MODE_REMOTE            0
#define CRSDKPY_MODE_CONTENTS_TRANSFER 1
#define CRSDKPY_MODE_REMOTE_TRANSFER   2

/* Connection state. Note that RECONNECTING may be followed directly by
 * CONNECTED with no DISCONNECTED in between; that is normal. */
#define CRSDKPY_STATE_CONNECTING   0
#define CRSDKPY_STATE_CONNECTED    1
#define CRSDKPY_STATE_RECONNECTING 2
#define CRSDKPY_STATE_CLOSING      3
#define CRSDKPY_STATE_CLOSED       4

/* Event kinds. Anything the bridge cannot classify arrives as RAW so that
 * vendor functionality newer than this bridge is still visible to Python. */
#define CRSDKPY_EVENT_CONNECTION       0
#define CRSDKPY_EVENT_PROPERTY_CHANGED 1
#define CRSDKPY_EVENT_FOCUS            2
#define CRSDKPY_EVENT_CAPTURE          3
#define CRSDKPY_EVENT_CONTENT          4
#define CRSDKPY_EVENT_WARNING          5
#define CRSDKPY_EVENT_ERROR            6
#define CRSDKPY_EVENT_RAW              7

/* Which channel reported a focus state. The two use different vendor
 * enumerations and neither is reliably first. */
#define CRSDKPY_FOCUS_SRC_PROPERTY 0
#define CRSDKPY_FOCUS_SRC_WARNING  1

/* Property access. */
#define CRSDKPY_ACCESS_UNKNOWN    0
#define CRSDKPY_ACCESS_READ_ONLY  1
#define CRSDKPY_ACCESS_WRITE_ONLY 2
#define CRSDKPY_ACCESS_READ_WRITE 3

/* Property value type. */
#define CRSDKPY_VTYPE_UNKNOWN   0
#define CRSDKPY_VTYPE_INT       1
#define CRSDKPY_VTYPE_STRING    2
#define CRSDKPY_VTYPE_INT_ARRAY 3

/* Compressed preview forms carried by the content index. Both are guaranteed
 * by the vendor to depict the still identified by (content_id, file_id). */
#define CRSDKPY_PREVIEW_THUMBNAIL  1
#define CRSDKPY_PREVIEW_SCREENNAIL 2

/* Media slot. The vendor numbers these from one. */
#define CRSDKPY_SLOT_1 1
#define CRSDKPY_SLOT_2 2

/* Upper bound on any single image the bridge will allocate for. A postview is
 * a full-resolution JPEG and runs to several megabytes; this is generous
 * enough for that and small enough that a nonsense size is refused instead of
 * attempted. */
#define CRSDKPY_MAX_IMAGE_BYTES (64u * 1024u * 1024u)

/* ---- POD structures --------------------------------------------------- */
/* Fixed layout. Do not reorder without bumping the ABI major version. */

typedef struct crsdkpy_camera_info {
    char    device_key[192]; /* opaque, stable across reconnects */
    char    model[64];
    char    serial[64];
    char    firmware[32];
    char    transport[32];
    char    adapter[64];
    int32_t usb_pid;         /* -1 when not applicable */
    int32_t reserved;
} crsdkpy_camera_info;

typedef struct crsdkpy_property {
    uint32_t code;
    int32_t  value_type;
    int32_t  access;
    int32_t  reserved;
    int64_t  value;          /* meaningful when value_type is INT */
    uint32_t allowed_count;  /* number of permitted values available */
    uint32_t reserved2;
} crsdkpy_property;

/* One durable item on the camera's media.
 *
 * Every field is copied out of the vendor's list while that list is still
 * alive; no vendor pointer is retained. Identifiers are monotonic but not
 * contiguous, so callers detect new content with id > baseline. */
typedef struct crsdkpy_content {
    uint32_t content_id;
    uint32_t file_id;
    uint32_t file_number;
    uint32_t dir_number;
    uint32_t content_type;   /* vendor CrContentsInfo_ContentType */
    uint32_t file_format;    /* vendor CrContentsFile_FileFormat */
    uint32_t image_width;    /* of the original still, 0 when absent */
    uint32_t image_height;
    int64_t  file_size;      /* -1 when the camera did not report one */
    uint32_t slot;
    uint32_t file_count;     /* files under this content id */
    /* Creation time as the camera reported it, in its own local calendar.
     * Kept as separate fields rather than an epoch value because the camera
     * states no timezone and inventing one would be a guess. */
    uint16_t created_year;
    uint16_t created_month;
    uint16_t created_day;
    uint16_t created_hour;
    uint16_t created_minute;
    uint16_t created_second;
    uint16_t created_millisecond;
    uint16_t reserved;
    char     path[256];
} crsdkpy_content;

/* Result of one compressed-preview transfer. The bytes themselves are held
 * inside the bridge until crsdkpy_take_content_preview copies them out. */
typedef struct crsdkpy_preview_info {
    uint32_t content_id;
    uint32_t file_id;
    int32_t  kind;           /* CRSDKPY_PREVIEW_* */
    int32_t  vendor_notify;  /* completion code from the vendor callback */
    uint32_t byte_length;
    uint32_t slot;
    /* The vendor may call back more than once for a single request. These
     * record what actually happened, because "it returned a valid JPEG" is
     * not the same as "it returned the whole JPEG". */
    uint32_t deliveries;     /* data-bearing callbacks seen */
    uint32_t last_percent;   /* progress reported by the final callback */
    int64_t  requested_ms;   /* bridge monotonic clock */
    int64_t  completed_ms;
} crsdkpy_preview_info;

/* Result of one postview delivery.
 *
 * Postview is announced by the camera and then pulled; the two are separate
 * events and the announcement carries the size, so the pull can be sized
 * exactly rather than guessed. */
typedef struct crsdkpy_postview_info {
    uint32_t byte_length;
    uint32_t reserved;
    int64_t  notified_ms;   /* when the camera announced it */
    int64_t  pulled_ms;     /* when the bytes finished arriving */
    char     filename[256]; /* as named by the camera, may be empty */
} crsdkpy_postview_info;

/* What the camera reports about its live-view stream.
 *
 * info_ok and a usable buffer are separate facts. A camera has been observed
 * answering this call successfully while reporting a zero-byte buffer, with
 * the frame fetch then failing outright. Reporting success is not the same as
 * being able to deliver a frame. */
typedef struct crsdkpy_live_view_info {
    int32_t  info_ok;       /* the vendor accepted the query */
    int32_t  vendor_error;  /* its error code when it did not */
    uint32_t width;
    uint32_t height;
    uint32_t buffer_size;   /* zero means no frame can be delivered */
    uint32_t reserved;
} crsdkpy_live_view_info;

/* One live-view frame. */
typedef struct crsdkpy_frame_info {
    uint32_t byte_length;
    uint32_t frame_number;  /* vendor sequence; repeats when nothing is new */
    uint32_t width;         /* as reported by the info call */
    uint32_t height;
    uint32_t time_code;
    uint32_t reserved;
    int64_t  fetched_ms;    /* bridge monotonic clock */
} crsdkpy_frame_info;

typedef struct crsdkpy_event {
    int32_t  kind;
    int32_t  reserved;
    int64_t  timestamp_ms;   /* bridge monotonic clock */
    uint32_t code;           /* property code, warning code, or 0 */
    int32_t  i0;             /* state / focus value / source */
    int32_t  i1;
    int32_t  i2;
    int64_t  i3;             /* content id and other wide payloads */
} crsdkpy_event;

/* ---- library ---------------------------------------------------------- */

/* Returns the ABI version as (major << 16) | minor. Safe to call at any
 * time; this is the only function guaranteed to exist across versions. */
CRSDKPY_API int32_t crsdkpy_abi_version(void);

/* Human-readable description of the last failure on the calling thread.
 * Copies into caller memory; never returns vendor-owned storage. */
CRSDKPY_API int32_t crsdkpy_last_error(char* buffer, uint32_t capacity);

/* Initialise the vendor SDK. Idempotent per process.
 *
 * adapter_dir may be NULL. When given, the bridge changes the working
 * directory to it for the duration of the vendor init call and restores it
 * afterwards. This is necessary because the vendor SDK resolves its transport
 * adapter directory relative to the process working directory and offers no
 * API to point it elsewhere; without this, initialisation fails with an
 * adaptor-create error unless the host application happens to have been
 * started from the right folder. Callers should pass the directory holding
 * the vendor runtime and its adapter subdirectory. */
CRSDKPY_API int32_t crsdkpy_init(const char* adapter_dir);

/* Close every session and release the vendor SDK. Idempotent. */
CRSDKPY_API int32_t crsdkpy_shutdown(void);

/* ---- discovery -------------------------------------------------------- */

/* Enumerate cameras and cache the result. Writes the number found. */
CRSDKPY_API int32_t crsdkpy_enumerate(int32_t timeout_sec, uint32_t* out_count);

/* Read one entry from the cached enumeration. */
CRSDKPY_API int32_t crsdkpy_camera_at(uint32_t index, crsdkpy_camera_info* out);

/* ---- sessions --------------------------------------------------------- */

/* Open a session. device_key comes from crsdkpy_camera_at. The mode cannot be
 * changed afterwards; reopen to switch. */
CRSDKPY_API int32_t crsdkpy_open_session(const char* device_key,
                                         int32_t mode,
                                         uint64_t* out_handle);

/* Close a session. Safe to call repeatedly and safe on a stale handle. */
CRSDKPY_API int32_t crsdkpy_close_session(uint64_t handle);

CRSDKPY_API int32_t crsdkpy_connection_state(uint64_t handle, int32_t* out_state);

/* ---- events ----------------------------------------------------------- */

/* Drain queued events into caller memory, waiting up to timeout_ms for the
 * first one. Vendor callbacks stay on vendor threads and only ever push into
 * an internal queue; this is the sole point where Python sees them. */
CRSDKPY_API int32_t crsdkpy_poll_events(uint64_t handle,
                                        crsdkpy_event* out,
                                        uint32_t capacity,
                                        uint32_t* out_count,
                                        int32_t timeout_ms);

/* ---- commands --------------------------------------------------------- */

/* Send a command by numeric vendor id with an up/down parameter.
 *
 * Acceptance proves only that the command was delivered. It is never evidence
 * that the camera acted: an accepted release in an autofocus mode with no
 * half-press produces no exposure at all. Wait for a capture event instead. */
#define CRSDKPY_PARAM_UP   0
#define CRSDKPY_PARAM_DOWN 1

CRSDKPY_API int32_t crsdkpy_send_command(uint64_t handle,
                                         uint32_t command_id,
                                         int32_t parameter);

/* ---- properties ------------------------------------------------------- */

/* Number of properties currently reported. This is a live figure and varies
 * with control mode; it must never be treated as a health assertion. */
CRSDKPY_API int32_t crsdkpy_property_count(uint64_t handle, uint32_t* out_count);

CRSDKPY_API int32_t crsdkpy_list_properties(uint64_t handle,
                                            crsdkpy_property* out,
                                            uint32_t capacity,
                                            uint32_t* out_count);

/* Read one property by numeric code, including codes the vendor's own
 * enumeration does not name. */
CRSDKPY_API int32_t crsdkpy_get_property(uint64_t handle,
                                         uint32_t code,
                                         crsdkpy_property* out);

/* Write one property by numeric code.
 *
 * The value type is never guessed: the bridge reads the property first and
 * writes back the object the camera itself described, so the vendor's own code
 * and value type are preserved. A property the camera reports as not settable
 * fails with CRSDKPY_ERR_UNSUPPORTED rather than being attempted. */
CRSDKPY_API int32_t crsdkpy_set_property(uint64_t handle,
                                         uint32_t code,
                                         int64_t value);

/* ---- content index ---------------------------------------------------- */

/* Enumerate durable content, newest last, restricted to items whose id is
 * strictly greater than after_content_id (pass 0 for everything in range).
 *
 * The range queried is the camera's most recent captured date. That is a
 * deliberate bound: asking for the whole card costs time proportional to its
 * contents, and every caller of this so far wants "what is new". A shot that
 * crosses midnight is still found, because the newest date is re-read on each
 * call rather than remembered.
 *
 * Call with capacity 0 to learn the count first. Note that the count can grow
 * between the two calls if the camera is still writing, so a caller that must
 * not miss an item should size generously rather than assume the figure is
 * stable.
 *
 * Only available in the RemoteTransfer control mode; other modes fail with
 * CRSDKPY_ERR_UNSUPPORTED rather than returning an empty list, because empty
 * and unavailable mean very different things to a caller waiting for a shot. */
CRSDKPY_API int32_t crsdkpy_list_content(uint64_t handle,
                                         uint32_t slot,
                                         uint32_t after_content_id,
                                         crsdkpy_content* out,
                                         uint32_t capacity,
                                         uint32_t* out_count);

/* Fetch a compressed preview of one specific still and block until the vendor
 * finishes delivering it.
 *
 * The vendor delivers asynchronously through a callback that identifies
 * neither the content nor the request, so the bridge allows one transfer per
 * session at a time and discards any delivery arriving with nothing in flight.
 * That is what makes the returned bytes provably those of the requested
 * (content_id, file_id) rather than a leftover from an earlier fetch.
 *
 * On success the bytes are held in the session until taken. Any previously
 * held bytes are discarded when a new fetch starts. */
CRSDKPY_API int32_t crsdkpy_fetch_content_preview(uint64_t handle,
                                                  uint32_t slot,
                                                  uint32_t content_id,
                                                  uint32_t file_id,
                                                  int32_t kind,
                                                  int32_t timeout_ms,
                                                  crsdkpy_preview_info* out_info);

/* Copy the held preview bytes into caller memory and release them.
 *
 * Call with capacity 0 to size. Taking clears the holding buffer, so the same
 * bytes can never be served twice; a second take with nothing held fails with
 * CRSDKPY_ERR_NOT_FOUND. */
CRSDKPY_API int32_t crsdkpy_take_content_preview(uint64_t handle,
                                                 uint8_t* out,
                                                 uint32_t capacity,
                                                 uint32_t* out_size);

/* ---- postview --------------------------------------------------------- */

/* Enable or disable postview and choose where it is delivered.
 *
 * Being allowed to configure this and actually receiving postview bytes are
 * independent: a camera has been observed rejecting this call outright while
 * still delivering postview once its still destination included the host. So
 * a rejection here says nothing about delivery, and callers must not infer
 * one from the other. Rejection is reported as CRSDKPY_ERR_UNSUPPORTED. */
CRSDKPY_API int32_t crsdkpy_configure_postview(uint64_t handle,
                                               int32_t enabled,
                                               int32_t transfer_to_ram);

/* Pull an announced postview into the session's holding buffer.
 *
 * Returns CRSDKPY_ERR_NOT_FOUND when the camera has not announced one, which
 * is an ordinary "nothing yet" and not a failure. */
CRSDKPY_API int32_t crsdkpy_pull_postview(uint64_t handle,
                                          crsdkpy_postview_info* out_info);

/* Copy the held postview bytes out and release them. Call with capacity 0 to
 * size. Taking clears the buffer, so the same delivery cannot be served
 * twice. */
CRSDKPY_API int32_t crsdkpy_take_postview(uint64_t handle,
                                          uint8_t* out,
                                          uint32_t capacity,
                                          uint32_t* out_size);

/* ---- live view -------------------------------------------------------- */

/* Query the stream. Always returns CRSDKPY_OK for a live session: whether the
 * vendor accepted the query is reported inside the struct, because "the
 * camera says it cannot stream" is an answer, not an error. */
CRSDKPY_API int32_t crsdkpy_get_live_view_info(uint64_t handle,
                                               crsdkpy_live_view_info* out);

/* Fetch the newest frame into caller memory.
 *
 * Call with capacity 0 to learn the size the camera currently wants. No
 * vendor-owned buffer is involved at any point: the bridge hands the vendor a
 * buffer it owns, so nothing the vendor allocated is ever visible to, or
 * outlives the call into, the caller.
 *
 * Returns CRSDKPY_ERR_NOT_FOUND when the camera produced no frame, which is
 * ordinary around an exposure and must not be treated as a fault. */
CRSDKPY_API int32_t crsdkpy_get_live_view_frame(uint64_t handle,
                                                uint8_t* out,
                                                uint32_t capacity,
                                                crsdkpy_frame_info* out_info);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CRSDKPY_ABI_H */
