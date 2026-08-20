// CrSDKPy native bridge implementation.
//
// Ownership rules enforced here, all of which come from observed vendor
// behaviour rather than theory:
//
//   * Vendor objects are owned by RAII wrappers and released exactly once.
//   * Vendor-owned arrays are copied out before the owning list is released.
//     Reusing a handle after releasing its list is an access violation.
//   * Vendor callbacks arrive on vendor threads. They only push into a
//     mutex-protected queue; nothing calls back into Python.
//   * Session handles carry a generation counter, so a stale handle from a
//     closed session is rejected instead of aliasing a new one.
//   * Close is idempotent.

#include "crsdkpy_abi.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32)
#  include <windows.h>
#  include <direct.h>
#else
#  include <unistd.h>
#endif

#include "CameraRemote_SDK.h"
#include "CrCommandData.h"
#include "CrDeviceProperty.h"
#include "IDeviceCallback.h"

namespace SDK = SCRSDK;

namespace {

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

std::mutex g_error_mutex;
std::string g_last_error;

void set_error(const std::string& message)
{
    std::lock_guard<std::mutex> lock(g_error_mutex);
    g_last_error = message;
}

void copy_string(char* dest, size_t capacity, const std::string& source)
{
    if (!dest || capacity == 0) return;
    const size_t n = source.size() < capacity - 1 ? source.size() : capacity - 1;
    std::memcpy(dest, source.data(), n);
    dest[n] = '\0';
}

// The vendor uses wchar_t on Windows and char elsewhere. Narrow without
// pulling in a locale dependency: model and adapter names are ASCII.
std::string narrow(const CrChar* text)
{
    if (!text) return std::string();
#if defined(_WIN32)
    std::string out;
    for (const CrChar* p = text; *p; ++p) {
        const unsigned int value = static_cast<unsigned int>(*p);
        out.push_back(value < 0x80 ? static_cast<char>(value) : '?');
    }
    return out;
#else
    return std::string(text);
#endif
}

// Vendor strings are UTF-16 on Windows. Converted to UTF-8 rather than
// narrowed, because a model or lens name is not guaranteed to be ASCII and
// replacing what does not fit would corrupt it silently. A leading byte-order
// mark is dropped: the camera includes one and it is not part of the value.
std::string to_utf8(const CrInt16u* text)
{
    if (!text) return std::string();
    std::string out;
    for (const CrInt16u* p = text; *p; ++p) {
        uint32_t cp = static_cast<uint32_t>(*p) & 0xFFFFu;
        if (p == text && cp == 0xFEFF) continue;  // byte-order mark
        if (cp >= 0xD800 && cp <= 0xDBFF && *(p + 1)) {
            const uint32_t low = static_cast<uint32_t>(*(p + 1)) & 0xFFFFu;
            if (low >= 0xDC00 && low <= 0xDFFF) {
                cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                ++p;
            }
        }
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }
    return out;
}

// The inverse of narrow(), for the few vendor calls that take a path. Paths
// are widened byte-for-byte, which covers ASCII; a non-ASCII path is rejected
// by the caller rather than silently mangled here.
std::vector<CrChar> widen(const std::string& text)
{
    std::vector<CrChar> out;
    out.reserve(text.size() + 1);
    for (unsigned char c : text) out.push_back(static_cast<CrChar>(c));
    out.push_back(static_cast<CrChar>(0));
    return out;
}

// Reads an environment variable. Uses the bounds-checked form on Windows,
// where the portable one is deprecated.
std::string environment(const char* name)
{
#if defined(_WIN32)
    char buffer[1024];
    size_t length = 0;
    if (getenv_s(&length, buffer, sizeof(buffer), name) != 0 || length == 0) {
        return std::string();
    }
    return std::string(buffer);
#else
    const char* value = std::getenv(name);
    return value ? std::string(value) : std::string();
#endif
}

bool is_ascii(const std::string& text)
{
    for (unsigned char c : text) {
        if (c >= 0x80) return false;
    }
    return true;
}

// Temporarily switches the process working directory, restoring it on scope
// exit.
//
// Note on adapter discovery, established experimentally: the vendor SDK
// resolves its transport adapter directory against the *host executable's*
// directory, not the working directory and not the directory of the DLL that
// calls it. Neither this scope nor SetDllDirectory changes that; both were
// tried and measured. It is kept because it costs nothing, keeps the vendor's
// own relative file handling predictable, and is the natural place to express
// "this is where the runtime lives" if a future SDK version honours it.
//
// The real constraint is handled at a higher level; see crsdkpy_init.
class ScopedWorkingDirectory
{
public:
    explicit ScopedWorkingDirectory(const char* target)
    {
        if (!target || !*target) return;
        char buffer[4096] = {0};
#if defined(_WIN32)
        if (!_getcwd(buffer, static_cast<int>(sizeof(buffer)))) return;
        previous_ = buffer;
        ok_ = (_chdir(target) == 0);
#else
        if (!getcwd(buffer, sizeof(buffer))) return;
        previous_ = buffer;
        ok_ = (chdir(target) == 0);
#endif
        changed_ = ok_;
    }

    ~ScopedWorkingDirectory()
    {
        if (!changed_) return;
#if defined(_WIN32)
        _chdir(previous_.c_str());
#else
        if (chdir(previous_.c_str()) != 0) { /* nothing useful to do here */ }
#endif
    }

    ScopedWorkingDirectory(const ScopedWorkingDirectory&) = delete;
    ScopedWorkingDirectory& operator=(const ScopedWorkingDirectory&) = delete;

    bool ok() const { return ok_; }

private:
    std::string previous_;
    bool changed_ = false;
    bool ok_ = true;  // no target requested is success
};

std::int64_t now_ms()
{
    using namespace std::chrono;
    static const steady_clock::time_point origin = steady_clock::now();
    return duration_cast<milliseconds>(steady_clock::now() - origin).count();
}

int32_t map_access(const SDK::CrDeviceProperty& property)
{
    const bool readable = property.IsGetEnableCurrentValue();
    const bool writable = property.IsSetEnableCurrentValue();
    if (readable && writable) return CRSDKPY_ACCESS_READ_WRITE;
    if (readable) return CRSDKPY_ACCESS_READ_ONLY;
    if (writable) return CRSDKPY_ACCESS_WRITE_ONLY;
    return CRSDKPY_ACCESS_UNKNOWN;
}

int32_t map_value_type(SDK::CrDataType type)
{
    switch (type) {
    case SDK::CrDataType_STR:
        return CRSDKPY_VTYPE_STRING;
    case SDK::CrDataType_UInt8:
    case SDK::CrDataType_UInt16:
    case SDK::CrDataType_UInt32:
    case SDK::CrDataType_UInt64:
    case SDK::CrDataType_Int8:
    case SDK::CrDataType_Int16:
    case SDK::CrDataType_Int32:
    case SDK::CrDataType_Int64:
        return CRSDKPY_VTYPE_INT;
    default:
        // Array and range types still carry a usable current value.
        return (type & SDK::CrDataType_ArrayBit) ? CRSDKPY_VTYPE_INT_ARRAY
                                                 : CRSDKPY_VTYPE_UNKNOWN;
    }
}

// ---------------------------------------------------------------------------
// RAII wrappers over vendor objects
// ---------------------------------------------------------------------------

// Releases a vendor property array exactly once. Every value must be copied
// out before this goes out of scope.
class PropertyList
{
public:
    PropertyList(SDK::CrDeviceHandle handle, SDK::CrDeviceProperty* list)
        : handle_(handle), list_(list) {}
    ~PropertyList()
    {
        if (list_) SDK::ReleaseDeviceProperties(handle_, list_);
    }
    PropertyList(const PropertyList&) = delete;
    PropertyList& operator=(const PropertyList&) = delete;

    SDK::CrDeviceProperty* get() const { return list_; }

private:
    SDK::CrDeviceHandle handle_;
    SDK::CrDeviceProperty* list_;
};

class EnumList
{
public:
    explicit EnumList(SDK::ICrEnumCameraObjectInfo* list) : list_(list) {}
    ~EnumList()
    {
        if (list_) list_->Release();
    }
    EnumList(const EnumList&) = delete;
    EnumList& operator=(const EnumList&) = delete;

    SDK::ICrEnumCameraObjectInfo* get() const { return list_; }
    void reset(SDK::ICrEnumCameraObjectInfo* list)
    {
        if (list_) list_->Release();
        list_ = list;
    }

private:
    SDK::ICrEnumCameraObjectInfo* list_;
};

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------

class Session;

class Callback final : public SDK::IDeviceCallback
{
public:
    explicit Callback(Session* session) : session_(session) {}

    void OnConnected(SDK::DeviceConnectionVersioin version) override;
    void OnDisconnected(CrInt32u error) override;
    void OnPropertyChanged() override {}
    void OnPropertyChangedCodes(CrInt32u count, CrInt32u* codes) override;
    void OnWarning(CrInt32u warning) override;
    void OnWarningExt(CrInt32u warning, CrInt32 p1, CrInt32 p2, CrInt32 p3) override;
    void OnError(CrInt32u error) override;
    void OnCompleteDownload(CrChar*, CrInt32u) override {}
    void OnNotifyContentsTransfer(CrInt32u, SDK::CrContentHandle, CrChar*) override {}
    void OnNotifyRemoteTransferContentsListChanged(CrInt32u notify,
                                                   CrInt32u slot,
                                                   CrInt32u added) override;
    void OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per,
                                      CrInt8u* data, CrInt64u size) override;
    // The vendor declares two overloads of this callback. The one above
    // carries bytes and serves the requests that deliver data; this one
    // carries the written path and serves the requests that write a file. They
    // are separate virtuals, so implementing only one compiles cleanly and
    // silently loses every result the other would have reported.
    void OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per,
                                      CrChar* filename) override;
    void OnNotifyPostViewImage(CrChar* filename, CrInt32u size) override;

private:
    Session* session_;
};

// A postview the camera has announced but nobody has pulled yet.
//
// The announcement carries the size, so the pull is sized exactly instead of
// guessed. Only the newest announcement is kept: an older one that was never
// pulled describes a frame the camera has already moved past.
struct PostviewPending
{
    bool        waiting = false;
    uint32_t    size = 0;
    int64_t     notified_ms = 0;
    std::string filename;
};

// One compressed-preview transfer.
//
// The vendor's completion callback carries no request identity at all: not the
// content id, not the file id, not the kind. Association is therefore
// structural rather than reported - exactly one transfer may be in flight per
// session, and a delivery arriving with nothing in flight is dropped instead of
// being handed to whoever asks next.
// How long to keep listening after a data-bearing callback that did not report
// completion. Measured transfers finish in well under a tenth of a second, so
// this is generous without making a fetch feel slow.
const int32_t kTransferSettleMs = 400;

struct PendingTransfer
{
    bool     in_flight = false;
    bool     complete = false;
    bool     failed = false;
    uint32_t slot = 0;
    uint32_t content_id = 0;
    uint32_t file_id = 0;
    int32_t  kind = 0;
    int32_t  notify = 0;
    uint32_t deliveries = 0;
    uint32_t last_percent = 0;
    int64_t  requested_ms = 0;
    int64_t  completed_ms = 0;
    std::vector<uint8_t> bytes;
};

class Session
{
public:
    Session(std::string device_key, int32_t mode)
        : device_key_(std::move(device_key)), mode_(mode), callback_(this) {}

    ~Session() { close(); }

    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;

    int32_t open(SDK::ICrCameraObjectInfo* info, const std::string& save_directory)
    {
        state_.store(CRSDKPY_STATE_CONNECTING);
        push(CRSDKPY_EVENT_CONNECTION, 0, CRSDKPY_STATE_CONNECTING, 0, 0, 0);

        const auto error = SDK::Connect(
            info, &callback_, &handle_,
            static_cast<SDK::CrSdkControlMode>(mode_), SDK::CrReconnecting_ON);
        if (error != SDK::CrError_None) {
            set_error("Connect failed");
            state_.store(CRSDKPY_STATE_CLOSED);
            return static_cast<int32_t>(error);
        }

        // Connect is asynchronous; wait for the callback rather than assuming.
        std::unique_lock<std::mutex> lock(mutex_);
        const bool ok = condition_.wait_for(
            lock, std::chrono::seconds(15),
            [this] { return state_.load() == CRSDKPY_STATE_CONNECTED
                          || state_.load() == CRSDKPY_STATE_CLOSED; });
        if (!ok || state_.load() != CRSDKPY_STATE_CONNECTED) {
            lock.unlock();
            close();
            set_error("timed out waiting for the connection callback");
            return CRSDKPY_ERR_CONNECT_FAILED;
        }
        lock.unlock();

        // The connection callback fires before the initial property load
        // finishes; a snapshot taken immediately contains only a handful of
        // codes. Wait for the burst to go quiet so that returning from here
        // genuinely means the session is usable, which is what the backend
        // contract promises.
        wait_for_property_quiet(kPropertyQuietMs, kPropertySettleCapMs);
        apply_save_info(save_directory);
        return CRSDKPY_OK;
    }

    // Tells the camera where a host-bound still may be written.
    //
    // The vendor's own sample calls this immediately after every successful
    // Connect, and hardware showed why it is not optional: with no save path
    // configured, a capture whose destination includes the host announces no
    // postview at all, and StillImageStoreDestination then reports itself as
    // not settable for the rest of the session, consistent with a transfer the
    // camera is still holding. A card-only session never notices.
    //
    // A failure here is reported as an event and does not fail the connect,
    // because a session that only ever writes to the card is still perfectly
    // usable.
    void apply_save_info(const std::string& directory)
    {
        if (directory.empty() || !is_ascii(directory)) {
            push(CRSDKPY_EVENT_WARNING, CRSDKPY_WARN_SAVE_PATH_UNUSABLE, 0, 0, 0, 0);
            return;
        }
        std::vector<CrChar> path = widen(directory);
        std::vector<CrChar> prefix = widen(std::string());
        // -1 keeps the camera's own file numbering rather than imposing one.
        const auto error =
            SDK::SetSaveInfo(handle_, path.data(), prefix.data(), -1);
        if (error != SDK::CrError_None) {
            push(CRSDKPY_EVENT_WARNING, CRSDKPY_WARN_SAVE_PATH_REFUSED,
                 static_cast<int32_t>(error), 0, 0, 0);
        }
    }

    // Returns once no property notification has arrived for quiet_ms, or
    // once cap_ms has elapsed overall.
    void wait_for_property_quiet(int64_t quiet_ms, int64_t cap_ms)
    {
        const int64_t deadline = now_ms() + cap_ms;
        for (;;) {
            const int64_t now = now_ms();
            if (now >= deadline) return;
            const int64_t last = last_property_ms_.load();
            if (last != 0 && now - last >= quiet_ms) return;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

    void close()
    {
        if (closed_.exchange(true)) return;  // idempotent
        state_.store(CRSDKPY_STATE_CLOSING);
        // Wake anything waiting on a transfer before the device goes away, so
        // a blocked fetch fails fast instead of waiting out its timeout.
        {
            std::lock_guard<std::mutex> lock(transfer_mutex_);
            transfer_.in_flight = false;
            transfer_.bytes.clear();
        }
        transfer_condition_.notify_all();
        if (handle_) {
            SDK::Disconnect(handle_);
            SDK::ReleaseDevice(handle_);
            handle_ = 0;
        }
        state_.store(CRSDKPY_STATE_CLOSED);
        condition_.notify_all();
    }

    bool closed() const { return closed_.load(); }
    int32_t state() const { return state_.load(); }
    int32_t mode() const { return mode_; }
    SDK::CrDeviceHandle handle() const { return handle_; }

    // -- compressed preview transfers -------------------------------------

    // Serialises whole fetch operations for this session; see PendingTransfer.
    std::mutex& transfer_gate() { return transfer_gate_; }

    void begin_transfer(uint32_t slot, uint32_t content_id, uint32_t file_id,
                        int32_t kind)
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        // Discarding here is what guarantees a caller can never be handed the
        // previous still's bytes.
        transfer_ = PendingTransfer();
        transfer_.in_flight = true;
        transfer_.slot = slot;
        transfer_.content_id = content_id;
        transfer_.file_id = file_id;
        transfer_.kind = kind;
        transfer_.requested_ms = now_ms();
    }

    // Returns true when the transfer finished within the timeout, filling
    // *out with a copy of its state. A false return leaves nothing in flight.
    //
    // A single request can produce several data-bearing callbacks. Returning
    // on the first one yields a JPEG that is valid but not final, which is
    // indistinguishable from success unless you fetch the same still twice and
    // compare. So the wait continues past the first delivery until the vendor
    // reports completion, or until settle_ms passes with nothing further.
    bool await_transfer(int32_t timeout_ms, int32_t settle_ms,
                        PendingTransfer* out)
    {
        const auto overall_deadline =
            std::chrono::steady_clock::now() +
            std::chrono::milliseconds(timeout_ms > 0 ? timeout_ms : 0);
        std::unique_lock<std::mutex> lock(transfer_mutex_);

        if (!transfer_condition_.wait_until(lock, overall_deadline, [this] {
                return transfer_.complete || !transfer_.in_flight;
            })) {
            transfer_.in_flight = false;  // refuse a late, unwanted delivery
            transfer_.bytes.clear();
            return false;
        }
        if (!transfer_.complete) {  // closed out from under us
            transfer_.in_flight = false;
            transfer_.bytes.clear();
            return false;
        }

        while (transfer_.last_percent < 100 && transfer_.in_flight) {
            const uint32_t seen = transfer_.deliveries;
            auto settle_until = std::chrono::steady_clock::now() +
                                std::chrono::milliseconds(settle_ms > 0 ? settle_ms
                                                                        : 0);
            if (settle_until > overall_deadline) settle_until = overall_deadline;
            const bool more = transfer_condition_.wait_until(
                lock, settle_until, [this, seen] {
                    return transfer_.deliveries != seen ||
                           transfer_.last_percent >= 100 || !transfer_.in_flight;
                });
            if (!more) break;  // quiet: the last delivery was the whole thing
            if (std::chrono::steady_clock::now() >= overall_deadline) break;
        }

        transfer_.in_flight = false;
        if (out) *out = transfer_;
        return !transfer_.bytes.empty() || transfer_.failed;
    }

    void abandon_transfer()
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        transfer_ = PendingTransfer();
    }

    // Copies the held bytes out and releases them, so the same delivery can
    // never be served twice.
    bool take_transfer_bytes(std::vector<uint8_t>* out)
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        if (!transfer_.complete || transfer_.failed) return false;
        if (out) out->swap(transfer_.bytes);
        transfer_.bytes.clear();
        transfer_.complete = false;
        return true;
    }

    size_t held_transfer_size()
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        return (transfer_.complete && !transfer_.failed) ? transfer_.bytes.size()
                                                         : 0;
    }

    // -- postview ----------------------------------------------------------

    void on_postview_announced(const std::string& filename, uint32_t size)
    {
        std::lock_guard<std::mutex> lock(postview_mutex_);
        postview_.waiting = true;
        postview_.size = size;
        postview_.notified_ms = now_ms();
        postview_.filename = filename;
    }

    // Claims the announcement so exactly one caller pulls it.
    bool take_postview_pending(PostviewPending* out)
    {
        std::lock_guard<std::mutex> lock(postview_mutex_);
        if (!postview_.waiting) return false;
        if (out) *out = postview_;
        postview_ = PostviewPending();
        return true;
    }

    void hold_postview(std::vector<uint8_t>&& bytes, int64_t pulled_ms)
    {
        std::lock_guard<std::mutex> lock(postview_mutex_);
        postview_bytes_ = std::move(bytes);
        postview_pulled_ms_ = pulled_ms;
    }

    size_t held_postview_size()
    {
        std::lock_guard<std::mutex> lock(postview_mutex_);
        return postview_bytes_.size();
    }

    bool take_postview_bytes(std::vector<uint8_t>* out)
    {
        std::lock_guard<std::mutex> lock(postview_mutex_);
        if (postview_bytes_.empty()) return false;
        if (out) out->swap(postview_bytes_);
        postview_bytes_.clear();
        return true;
    }

    void on_transfer_result(CrInt32u notify, CrInt32u percent, CrInt8u* data,
                            CrInt64u size)
    {
        {
            std::lock_guard<std::mutex> lock(transfer_mutex_);
            if (!transfer_.in_flight) return;  // nothing asked for this
            transfer_.notify = static_cast<int32_t>(notify);
            transfer_.last_percent = static_cast<uint32_t>(percent);
            if (data && size) {
                ++transfer_.deliveries;
                transfer_.bytes.assign(data, data + size);
                transfer_.completed_ms = now_ms();
                transfer_.complete = true;
            } else if (percent >= 100) {
                // Finished without delivering anything: a failure, not a wait.
                transfer_.completed_ms = now_ms();
                transfer_.complete = true;
                transfer_.failed = true;
            } else {
                return;  // progress only
            }
        }
        transfer_condition_.notify_all();
    }

    void set_state(int32_t value)
    {
        state_.store(value);
        condition_.notify_all();
    }

    // -- file-writing transfers --------------------------------------------

    void on_transfer_file_result(int32_t outcome, uint32_t notify,
                                 uint32_t percent, const std::string& path)
    {
        if (!path.empty()) {
            std::lock_guard<std::mutex> lock(transfer_mutex_);
            transfer_path_ = path;
        }
        push(CRSDKPY_EVENT_TRANSFER, notify, static_cast<int32_t>(percent),
             outcome, path.empty() ? 0 : 1, 0);
        transfer_condition_.notify_all();
    }

    // Non-destructive, so a caller can size a buffer without consuming the
    // result it is about to ask for.
    size_t transfer_path_size()
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        return transfer_path_.size();
    }

    bool take_transfer_path(std::string* out)
    {
        std::lock_guard<std::mutex> lock(transfer_mutex_);
        if (transfer_path_.empty()) return false;
        if (out) *out = transfer_path_;
        transfer_path_.clear();
        return true;
    }

    void note_property_activity() { last_property_ms_.store(now_ms()); }
    int64_t last_property_ms() const { return last_property_ms_.load(); }

    void push(int32_t kind, uint32_t code, int32_t i0, int32_t i1, int32_t i2,
              int64_t i3)
    {
        crsdkpy_event event;
        std::memset(&event, 0, sizeof(event));
        event.kind = kind;
        event.timestamp_ms = now_ms();
        event.code = code;
        event.i0 = i0;
        event.i1 = i1;
        event.i2 = i2;
        event.i3 = i3;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            // Bound the queue: a client that stops polling must not grow it
            // without limit. Oldest events are dropped first.
            if (queue_.size() >= kMaxQueued) queue_.pop_front();
            queue_.push_back(event);
        }
        condition_.notify_all();
    }

    uint32_t drain(crsdkpy_event* out, uint32_t capacity, int32_t timeout_ms)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (queue_.empty() && timeout_ms > 0) {
            condition_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                                [this] { return !queue_.empty(); });
        }
        uint32_t produced = 0;
        while (produced < capacity && !queue_.empty()) {
            out[produced++] = queue_.front();
            queue_.pop_front();
        }
        return produced;
    }

private:
    static const size_t kMaxQueued = 4096;
    static const int64_t kPropertyQuietMs = 400;
    static const int64_t kPropertySettleCapMs = 5000;

    std::string device_key_;
    int32_t mode_;
    Callback callback_;
    SDK::CrDeviceHandle handle_ = 0;
    std::atomic<int32_t> state_{CRSDKPY_STATE_CLOSED};
    std::atomic<bool> closed_{false};
    std::atomic<int64_t> last_property_ms_{0};
    std::mutex mutex_;
    std::condition_variable condition_;
    std::deque<crsdkpy_event> queue_;

    std::mutex transfer_gate_;
    std::mutex transfer_mutex_;
    std::condition_variable transfer_condition_;
    PendingTransfer transfer_;
    std::string transfer_path_;

    std::mutex postview_mutex_;
    PostviewPending postview_;
    std::vector<uint8_t> postview_bytes_;
    int64_t postview_pulled_ms_ = 0;
};

void Callback::OnConnected(SDK::DeviceConnectionVersioin version)
{
    // A second OnConnected after a recovery is normal and must not be treated
    // as an error or as a new session.
    session_->set_state(CRSDKPY_STATE_CONNECTED);
    // i1 says whether this was a recovery and i2 carries the connection
    // version. They are separate slots because a first connect reports a
    // non-zero version, and putting the two in one field made every fresh
    // session look like it had recovered from something.
    session_->push(CRSDKPY_EVENT_CONNECTION, 0, CRSDKPY_STATE_CONNECTED,
                   0 /* not a recovery */, static_cast<int32_t>(version), 0);
}

void Callback::OnDisconnected(CrInt32u error)
{
    session_->set_state(CRSDKPY_STATE_CLOSED);
    session_->push(CRSDKPY_EVENT_CONNECTION, error, CRSDKPY_STATE_CLOSED, 0, 0, 0);
}

void Callback::OnPropertyChangedCodes(CrInt32u count, CrInt32u* codes)
{
    // One event per code. Python coalesces; the bridge does not editorialise.
    session_->note_property_activity();
    for (CrInt32u i = 0; i < count; ++i) {
        session_->push(CRSDKPY_EVENT_PROPERTY_CHANGED, codes[i],
                       static_cast<int32_t>(i), static_cast<int32_t>(count), 0, 0);
    }
}

void Callback::OnWarning(CrInt32u warning)
{
    if (warning == SDK::CrWarning_Connect_Reconnecting) {
        session_->set_state(CRSDKPY_STATE_RECONNECTING);
        session_->push(CRSDKPY_EVENT_CONNECTION, warning,
                       CRSDKPY_STATE_RECONNECTING, 0, 0, 0);
        return;
    }
    if (warning == SDK::CrWarning_Connect_Reconnected) {
        session_->set_state(CRSDKPY_STATE_CONNECTED);
        session_->push(CRSDKPY_EVENT_CONNECTION, warning, CRSDKPY_STATE_CONNECTED,
                       1 /* recovered */, 0 /* version unchanged */, 0);
        return;
    }
    if (warning == SDK::CrNotify_Captured_Event) {
        session_->push(CRSDKPY_EVENT_CAPTURE, warning, 0, 0, 0, 0);
        return;
    }
    session_->push(CRSDKPY_EVENT_WARNING, warning, 0, 0, 0, 0);
}

void Callback::OnWarningExt(CrInt32u warning, CrInt32 p1, CrInt32 p2, CrInt32 p3)
{
    if (warning == SDK::CrWarningExt_AFStatus) {
        // The second focus channel. It uses its own enumeration and may lead
        // or trail the property channel; Python decides what it means.
        session_->push(CRSDKPY_EVENT_FOCUS, warning, p1,
                       CRSDKPY_FOCUS_SRC_WARNING, p2, p3);
        return;
    }
    session_->push(CRSDKPY_EVENT_WARNING, warning, p1, p2, p3, 0);
}

void Callback::OnError(CrInt32u error)
{
    session_->push(CRSDKPY_EVENT_ERROR, error, 0, 0, 0, 0);
}

void Callback::OnNotifyRemoteTransferContentsListChanged(CrInt32u notify,
                                                         CrInt32u slot,
                                                         CrInt32u added)
{
    // The vendor says the list changed but not what appeared, so this carries
    // no content id. Python treats it as a hint to re-read the index rather
    // than as the identity of a new item.
    session_->push(CRSDKPY_EVENT_CONTENT, notify, static_cast<int32_t>(slot),
                   static_cast<int32_t>(added), 0, 0);
}

// Maps a vendor notify code onto the normalized outcome. Anything unrecognised
// stays visible: the caller still receives the raw code.
static int32_t transfer_outcome(CrInt32u notify)
{
    switch (notify) {
    case SDK::CrNotify_RemoteTransfer_InProgress:
        return CRSDKPY_TRANSFER_IN_PROGRESS;
    case SDK::CrNotify_RemoteTransfer_Result_OK:
        return CRSDKPY_TRANSFER_OK;
    case SDK::CrNotify_RemoteTransfer_Result_NG:
        return CRSDKPY_TRANSFER_FAILED;
    case SDK::CrNotify_RemoteTransfer_Result_DeviceBusy:
        return CRSDKPY_TRANSFER_BUSY;
    case SDK::CrWarning_File_StorageFull:
        return CRSDKPY_TRANSFER_STORAGE_FULL;
    case SDK::CrNotify_RemoteTransfer_Control_Stopped:
        return CRSDKPY_TRANSFER_STOPPED;
    case SDK::CrNotify_RemoteTransfer_Control_Canceled:
        return CRSDKPY_TRANSFER_CANCELED;
    default:
        return CRSDKPY_TRANSFER_UNKNOWN;
    }
}

void Callback::OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per,
                                            CrChar* filename)
{
    // Copied here: the string belongs to the vendor and is not valid once this
    // returns.
    session_->on_transfer_file_result(transfer_outcome(notify), notify, per,
                                      narrow(filename));
}

void Callback::OnNotifyRemoteTransferResult(CrInt32u notify, CrInt32u per,
                                            CrInt8u* data, CrInt64u size)
{
    // Copied immediately: the buffer belongs to the vendor and is not valid
    // once this returns.
    session_->on_transfer_result(notify, per, data, size);
}

void Callback::OnNotifyPostViewImage(CrChar* filename, CrInt32u size)
{
    // Only the announcement is recorded here. The bytes are pulled on the
    // caller's thread, so no vendor call is made from inside a vendor
    // callback.
    session_->on_postview_announced(narrow(filename), size);
    session_->push(CRSDKPY_EVENT_CONTENT, 0, 0, 0, 1 /* postview */,
                   static_cast<int64_t>(size));
}

// ---------------------------------------------------------------------------
// process-wide state
// ---------------------------------------------------------------------------

// Sessions are shared rather than uniquely owned so that a call which must
// release the global lock while it blocks - the compressed-preview fetch waits
// on a vendor callback for the better part of a second - can hold the session
// alive for the duration. A concurrent close then makes the wait fail rather
// than freeing the object under it.
struct Slot
{
    std::shared_ptr<Session> session;
    uint32_t generation = 0;
};

std::mutex g_mutex;
bool g_initialized = false;
std::string g_adapter_dir;
// Where a host-bound still may be written. Empty means "use the adapter
// directory", which is where the host process already runs.
std::string g_save_dir;
EnumList g_cameras(nullptr);
std::vector<std::string> g_camera_keys;
std::vector<Slot> g_slots;

uint64_t make_handle(uint32_t index, uint32_t generation)
{
    return (static_cast<uint64_t>(generation) << 32) | index;
}

// Rejects a handle from a closed session instead of aliasing a newer one.
std::shared_ptr<Session> resolve(uint64_t handle)
{
    const uint32_t index = static_cast<uint32_t>(handle & 0xFFFFFFFFu);
    const uint32_t generation = static_cast<uint32_t>(handle >> 32);
    if (index >= g_slots.size()) return nullptr;
    Slot& slot = g_slots[index];
    if (!slot.session || slot.generation != generation) return nullptr;
    return slot.session;
}

// Copies one vendor content record into plain data. Every pointer the vendor
// owns is dereferenced here, while its list is still alive, and never stored.
void copy_content(const SDK::CrContentsInfo& source, uint32_t slot,
                  crsdkpy_content& target)
{
    std::memset(&target, 0, sizeof(target));
    target.content_id = source.contentId;
    target.file_number = source.fileNumber;
    target.dir_number = source.dirNumber;
    target.content_type = static_cast<uint32_t>(source.contentType);
    target.slot = slot;
    target.file_count = source.filesNum;
    target.file_size = -1;

    const SDK::CrCaptureDate& created = source.creationDatetimeLocaltime;
    target.created_year = created.year;
    target.created_month = created.month;
    target.created_day = created.day;
    target.created_hour = created.hour;
    target.created_minute = created.minute;
    target.created_second = created.sec;
    target.created_millisecond = created.msec;

    if (source.filesNum == 0 || !source.files) return;
    const SDK::CrContentsFile& file = source.files[0];
    target.file_id = file.fileId;
    target.file_format = static_cast<uint32_t>(file.fileFormat);
    target.file_size = static_cast<int64_t>(file.fileSize);
    if (file.isImageParamExsist) {
        target.image_width = file.imageParam.imagePixWidth;
        target.image_height = file.imageParam.imagePixHeight;
    }
    if (file.filePath) {
        // filePathLength is a byte count; stop at the first NUL either way so
        // a length expressed in some other unit cannot run past the string.
        const char* text = reinterpret_cast<const char*>(file.filePath);
        size_t limit = static_cast<size_t>(file.filePathLength);
        if (limit == 0 || limit > sizeof(target.path) - 1) {
            limit = sizeof(target.path) - 1;
        }
        size_t length = 0;
        while (length < limit && text[length] != '\0') ++length;
        std::memcpy(target.path, text, length);
        target.path[length] = '\0';
    }
}

}  // namespace

// ---------------------------------------------------------------------------
// exported ABI
// ---------------------------------------------------------------------------

extern "C" {

int32_t crsdkpy_abi_version(void)
{
    return (CRSDKPY_ABI_VERSION_MAJOR << 16) | CRSDKPY_ABI_VERSION_MINOR;
}

int32_t crsdkpy_last_error(char* buffer, uint32_t capacity)
{
    if (!buffer || capacity == 0) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_error_mutex);
    copy_string(buffer, capacity, g_last_error);
    return CRSDKPY_OK;
}

int32_t crsdkpy_init(const char* adapter_dir)
{
    set_error("");  // never inherit a previous failure's text
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_initialized) return CRSDKPY_OK;  // idempotent

    // The vendor SDK looks for its transport adapters relative to the process
    // working directory and provides no way to override that. Scope a chdir
    // around the init call and put it back, so the caller's working directory
    // is not permanently changed by loading a camera library.
    g_adapter_dir = adapter_dir ? adapter_dir : "";
    g_save_dir = environment("CRSDKPY_SAVE_DIR");
    ScopedWorkingDirectory scoped(g_adapter_dir.c_str());
    if (!scoped.ok()) {
        set_error("could not switch to the adapter directory");
        g_adapter_dir.clear();
        return CRSDKPY_ERR_INVALID_ARG;
    }

    if (!SDK::Init()) {
        set_error("SDK::Init() returned false");
        return CRSDKPY_ERR_SDK_INIT_FAILED;
    }
    g_initialized = true;
    return CRSDKPY_OK;
}

int32_t crsdkpy_shutdown(void)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_initialized) return CRSDKPY_OK;
    for (Slot& slot : g_slots) {
        if (slot.session) {
            slot.session->close();
            slot.session.reset();
        }
    }
    g_slots.clear();
    g_camera_keys.clear();
    g_cameras.reset(nullptr);
    SDK::Release();
    g_adapter_dir.clear();
    g_initialized = false;
    return CRSDKPY_OK;
}

int32_t crsdkpy_enumerate(int32_t timeout_sec, uint32_t* out_count)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_count) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_initialized) return CRSDKPY_ERR_NOT_INITIALIZED;

    ScopedWorkingDirectory scoped(g_adapter_dir.c_str());
    SDK::ICrEnumCameraObjectInfo* found = nullptr;
    const auto error = SDK::EnumCameraObjects(
        &found, static_cast<CrInt8u>(timeout_sec > 0 ? timeout_sec : 3));
    if (error != SDK::CrError_None || !found) {
        g_cameras.reset(nullptr);
        g_camera_keys.clear();
        *out_count = 0;
        if (error != SDK::CrError_None) {
            if ((error & 0xFF00) == 0x8700) {
                set_error(
                    "the vendor SDK could not create a transport adapter. Its "
                    "adapter directory is resolved against the host "
                    "executable's directory, so CrAdapter must be reachable "
                    "from the directory containing the running interpreter or "
                    "application binary.");
            } else {
                set_error("EnumCameraObjects failed");
            }
            return static_cast<int32_t>(error);
        }
        return CRSDKPY_OK;  // no cameras is not an error
    }

    g_cameras.reset(found);
    g_camera_keys.clear();
    const uint32_t count = found->GetCount();
    for (uint32_t i = 0; i < count; ++i) {
        const auto* info = found->GetCameraObjectInfo(i);
        // A stable key: model plus the vendor id bytes. Survives reopen and
        // control-mode changes, which is what Python's Camera relies on.
        std::string key = narrow(info->GetModel());
        key.push_back(':');
        const CrInt8u* id = info->GetId();
        const CrInt32u id_size = info->GetIdSize();
        static const char* kHex = "0123456789abcdef";
        for (CrInt32u b = 0; b < id_size && b < 32; ++b) {
            key.push_back(kHex[(id[b] >> 4) & 0xF]);
            key.push_back(kHex[id[b] & 0xF]);
        }
        g_camera_keys.push_back(key);
    }
    *out_count = count;
    return CRSDKPY_OK;
}

int32_t crsdkpy_camera_at(uint32_t index, crsdkpy_camera_info* out)
{
    set_error("");  // never inherit a previous failure's text
    if (!out) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_initialized) return CRSDKPY_ERR_NOT_INITIALIZED;
    if (!g_cameras.get() || index >= g_cameras.get()->GetCount()) {
        return CRSDKPY_ERR_NOT_FOUND;
    }
    const auto* info = g_cameras.get()->GetCameraObjectInfo(index);
    std::memset(out, 0, sizeof(*out));
    copy_string(out->device_key, sizeof(out->device_key), g_camera_keys[index]);
    copy_string(out->model, sizeof(out->model), narrow(info->GetModel()));
    copy_string(out->serial, sizeof(out->serial), g_camera_keys[index]);
    copy_string(out->transport, sizeof(out->transport),
                narrow(info->GetConnectionTypeName()));
    copy_string(out->adapter, sizeof(out->adapter), narrow(info->GetAdaptorName()));
    out->usb_pid = static_cast<int32_t>(info->GetUsbPid());
    return CRSDKPY_OK;
}

int32_t crsdkpy_status_is_busy(int32_t status)
{
    switch (static_cast<CrInt32u>(status)) {
    // Observed on hardware: the first content listing after opening a
    // RemoteTransfer session can land while the camera is still building its
    // index. It fails in about a millisecond and the next call succeeds.
    case SDK::CrError_RemoteTransfer_GetContentsInfoListProcessing:
    case SDK::CrError_Adaptor_DeviceBusy:
    case SDK::CrError_Connect_FailBusy:
        return 1;
    default:
        return 0;
    }
}

int32_t crsdkpy_open_session(const char* device_key, int32_t mode,
                             uint64_t* out_handle)
{
    set_error("");  // never inherit a previous failure's text
    if (!device_key || !out_handle) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_initialized) return CRSDKPY_ERR_NOT_INITIALIZED;
    if (!g_cameras.get()) return CRSDKPY_ERR_NOT_FOUND;

    const std::string wanted(device_key);
    uint32_t index = 0;
    bool found = false;
    for (uint32_t i = 0; i < g_camera_keys.size(); ++i) {
        if (g_camera_keys[i] == wanted) {
            index = i;
            found = true;
            break;
        }
    }
    if (!found) {
        set_error("device key not present in the last enumeration");
        return CRSDKPY_ERR_NOT_FOUND;
    }

    ScopedWorkingDirectory scoped(g_adapter_dir.c_str());
    auto session = std::make_shared<Session>(wanted, mode);
    auto* raw_info = const_cast<SDK::ICrCameraObjectInfo*>(
        g_cameras.get()->GetCameraObjectInfo(index));
    const std::string save_directory =
        g_save_dir.empty() ? g_adapter_dir : g_save_dir;
    // A failed open has already disconnected and released the device by the
    // time this returns, so nothing is left for the caller to clean up. That
    // is what makes retrying the callback timeout worthwhile, and the decision
    // to retry belongs to the caller rather than here: this function holds the
    // global lock, and spending two full connect deadlines under it would
    // block every other call into the bridge.
    const int32_t status = session->open(raw_info, save_directory);
    if (status != CRSDKPY_OK) return status;

    // Reuse a free slot, bumping its generation so old handles stay invalid.
    uint32_t slot_index = static_cast<uint32_t>(g_slots.size());
    for (uint32_t i = 0; i < g_slots.size(); ++i) {
        if (!g_slots[i].session) {
            slot_index = i;
            break;
        }
    }
    if (slot_index == g_slots.size()) g_slots.push_back(Slot());
    g_slots[slot_index].generation += 1;
    g_slots[slot_index].session = std::move(session);
    *out_handle = make_handle(slot_index, g_slots[slot_index].generation);
    return CRSDKPY_OK;
}

int32_t crsdkpy_close_session(uint64_t handle)
{
    set_error("");  // never inherit a previous failure's text
    std::lock_guard<std::mutex> lock(g_mutex);
    const uint32_t index = static_cast<uint32_t>(handle & 0xFFFFFFFFu);
    const uint32_t generation = static_cast<uint32_t>(handle >> 32);
    if (index >= g_slots.size()) return CRSDKPY_OK;  // already gone
    Slot& slot = g_slots[index];
    if (!slot.session || slot.generation != generation) return CRSDKPY_OK;
    slot.session->close();
    slot.session.reset();
    return CRSDKPY_OK;
}

int32_t crsdkpy_connection_state(uint64_t handle, int32_t* out_state)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_state) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) {
        *out_state = CRSDKPY_STATE_CLOSED;
        return CRSDKPY_OK;
    }
    *out_state = session->state();
    return CRSDKPY_OK;
}

int32_t crsdkpy_poll_events(uint64_t handle, crsdkpy_event* out, uint32_t capacity,
                            uint32_t* out_count, int32_t timeout_ms)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_count) return CRSDKPY_ERR_INVALID_ARG;
    *out_count = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        session = resolve(handle);
        if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    }
    // Drain outside the global lock: this call blocks, and holding the global
    // lock here would stall every other session.
    *out_count = session->drain(out, capacity, timeout_ms);
    return CRSDKPY_OK;
}

int32_t crsdkpy_property_count(uint64_t handle, uint32_t* out_count)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_count) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrDeviceProperty* raw = nullptr;
    CrInt32 count = 0;
    const auto error = SDK::GetDeviceProperties(session->handle(), &raw, &count);
    PropertyList guard(session->handle(), raw);
    if (error != SDK::CrError_None) return static_cast<int32_t>(error);
    *out_count = static_cast<uint32_t>(count < 0 ? 0 : count);
    return CRSDKPY_OK;
}

int32_t crsdkpy_list_properties(uint64_t handle, crsdkpy_property* out,
                                uint32_t capacity, uint32_t* out_count)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_count) return CRSDKPY_ERR_INVALID_ARG;
    *out_count = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrDeviceProperty* raw = nullptr;
    CrInt32 count = 0;
    const auto error = SDK::GetDeviceProperties(session->handle(), &raw, &count);
    PropertyList guard(session->handle(), raw);
    if (error != SDK::CrError_None) return static_cast<int32_t>(error);
    if (count < 0) count = 0;

    *out_count = static_cast<uint32_t>(count);
    if (capacity == 0) return CRSDKPY_OK;             // sizing call
    if (static_cast<uint32_t>(count) > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;

    // Copy every value out while the vendor list is still alive.
    for (CrInt32 i = 0; i < count; ++i) {
        SDK::CrDeviceProperty& source = raw[i];
        crsdkpy_property& target = out[i];
        std::memset(&target, 0, sizeof(target));
        target.code = source.GetCode();
        target.value = static_cast<int64_t>(source.GetCurrentValue());
        target.value_type = map_value_type(source.GetValueType());
        target.access = map_access(source);
        target.allowed_count = source.GetValueSize() / 2;
    }
    return CRSDKPY_OK;
}

int32_t crsdkpy_get_property(uint64_t handle, uint32_t code, crsdkpy_property* out)
{
    set_error("");  // never inherit a previous failure's text
    if (!out) return CRSDKPY_ERR_INVALID_ARG;
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrDeviceProperty* raw = nullptr;
    CrInt32 count = 0;
    CrInt32u wanted = code;
    const auto error =
        SDK::GetSelectDeviceProperties(session->handle(), 1, &wanted, &raw, &count);
    PropertyList guard(session->handle(), raw);
    // Same normalisation as the write path: for a property lookup, the vendor's
    // invalid-call result means the camera does not expose the code.
    if (error == SDK::CrError_Api_InvalidCalled || !raw || count < 1) {
        set_error("the camera does not expose that property code");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    if (error != SDK::CrError_None) return static_cast<int32_t>(error);

    std::memset(out, 0, sizeof(*out));
    out->code = raw[0].GetCode();
    out->value = static_cast<int64_t>(raw[0].GetCurrentValue());
    out->value_type = map_value_type(raw[0].GetValueType());
    out->access = map_access(raw[0]);
    out->allowed_count = raw[0].GetValueSize() / 2;
    return CRSDKPY_OK;
}


// Width in bytes of one element of a vendor data type, or 0 when the base type
// is not one this bridge decodes.
static uint32_t element_width(SDK::CrDataType type)
{
    switch (type & 0x0FFF) {
    case SDK::CrDataType_UInt8:  return 1;
    case SDK::CrDataType_UInt16: return 2;
    case SDK::CrDataType_UInt32: return 4;
    case SDK::CrDataType_UInt64: return 8;
    default: return 0;
    }
}

// Reads one element, sign-extending when the vendor says the type is signed.
static int64_t read_element(const uint8_t* data, uint32_t width, bool is_signed)
{
    uint64_t raw = 0;
    for (uint32_t i = 0; i < width; ++i) {
        raw |= static_cast<uint64_t>(data[i]) << (8 * i);  // vendor is little-endian
    }
    if (!is_signed) return static_cast<int64_t>(raw);
    switch (width) {
    case 1: return static_cast<int8_t>(raw);
    case 2: return static_cast<int16_t>(raw);
    case 4: return static_cast<int32_t>(raw);
    default: return static_cast<int64_t>(raw);
    }
}

int32_t crsdkpy_property_values(uint64_t handle, uint32_t code, int64_t* out,
                                uint32_t capacity, uint32_t* out_count,
                                int32_t* out_kind)
{
    set_error("");
    if (!out_count || !out_kind) return CRSDKPY_ERR_INVALID_ARG;
    *out_count = 0;
    *out_kind = CRSDKPY_VALUES_NONE;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrDeviceProperty* raw = nullptr;
    CrInt32 count = 0;
    CrInt32u wanted = code;
    const auto error =
        SDK::GetSelectDeviceProperties(session->handle(), 1, &wanted, &raw, &count);
    PropertyList guard(session->handle(), raw);
    if (error == SDK::CrError_Api_InvalidCalled || !raw || count < 1) {
        set_error("the camera does not expose that property code");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    if (error != SDK::CrError_None) return static_cast<int32_t>(error);

    const SDK::CrDataType type = raw[0].GetValueType();
    const uint8_t* values = reinterpret_cast<const uint8_t*>(raw[0].GetValues());
    const uint32_t bytes = raw[0].GetValueSize();
    if (!values || bytes == 0) return CRSDKPY_OK;  // advertises nothing

    const uint32_t width = element_width(type);
    const bool ranged = (type & SDK::CrDataType_RangeBit) != 0;
    const bool arrayed = (type & SDK::CrDataType_ArrayBit) != 0;
    // A shape this bridge cannot take apart is reported as raw rather than
    // sliced on a guess: a wrong value set is worse than none.
    if (width == 0 || (!ranged && !arrayed) || bytes % width != 0 ||
        (ranged && bytes / width != 3)) {
        *out_kind = CRSDKPY_VALUES_RAW;
        return CRSDKPY_OK;
    }

    const bool is_signed = (type & SDK::CrDataType_SignBit) != 0;
    const uint32_t total = bytes / width;
    *out_kind = ranged ? CRSDKPY_VALUES_RANGE : CRSDKPY_VALUES_ENUM;
    *out_count = total;
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (total > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;
    for (uint32_t i = 0; i < total; ++i) {
        out[i] = read_element(values + (i * width), width, is_signed);
    }
    return CRSDKPY_OK;
}

int32_t crsdkpy_property_string(uint64_t handle, uint32_t code, char* out,
                               uint32_t capacity, uint32_t* out_length)
{
    set_error("");
    if (!out_length) return CRSDKPY_ERR_INVALID_ARG;
    *out_length = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrDeviceProperty* raw = nullptr;
    CrInt32 count = 0;
    CrInt32u wanted = code;
    const auto error =
        SDK::GetSelectDeviceProperties(session->handle(), 1, &wanted, &raw, &count);
    PropertyList guard(session->handle(), raw);
    if (error == SDK::CrError_Api_InvalidCalled || !raw || count < 1) {
        set_error("the camera does not expose that property code");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    if (error != SDK::CrError_None) return static_cast<int32_t>(error);

    // Copied before the owning list is released, like every other read here.
    const std::string text = to_utf8(raw[0].GetCurrentStr());
    *out_length = static_cast<uint32_t>(text.size());
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (text.size() + 1 > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;
    copy_string(out, capacity, text);
    return CRSDKPY_OK;
}

int32_t crsdkpy_set_property(uint64_t handle, uint32_t code, int64_t value)
{
    set_error("");  // never inherit a previous failure's text
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    // Read-modify-write. The vendor value type is never guessed: the camera
    // describes the property, we change only its current value, and we copy
    // the object out before the owning list is released.
    SDK::CrDeviceProperty property;
    {
        SDK::CrDeviceProperty* raw = nullptr;
        CrInt32 count = 0;
        CrInt32u wanted = code;
        const auto error =
            SDK::GetSelectDeviceProperties(session->handle(), 1, &wanted, &raw, &count);
        PropertyList guard(session->handle(), raw);
        if (error == SDK::CrError_Api_InvalidCalled || !raw || count < 1) {
            set_error("the camera does not expose that property code");
            return CRSDKPY_ERR_NOT_FOUND;
        }
        if (error != SDK::CrError_None) return static_cast<int32_t>(error);
        property = raw[0];
    }

    if (!property.IsSetEnableCurrentValue()) {
        set_error("the camera reports this property as not settable");
        return CRSDKPY_ERR_UNSUPPORTED;
    }

    property.SetCurrentValue(static_cast<CrInt64u>(value));
    const auto error = SDK::SetDeviceProperty(session->handle(), &property);
    if (error != SDK::CrError_None) {
        set_error("the camera rejected the property write");
        return static_cast<int32_t>(error);
    }
    return CRSDKPY_OK;
}

int32_t crsdkpy_send_command(uint64_t handle, uint32_t command_id, int32_t parameter)
{
    set_error("");
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;
    if (parameter != CRSDKPY_PARAM_UP && parameter != CRSDKPY_PARAM_DOWN) {
        set_error("command parameter must be up or down");
        return CRSDKPY_ERR_INVALID_ARG;
    }

    const auto error = SDK::SendCommand(
        session->handle(), static_cast<CrInt32u>(command_id),
        static_cast<SDK::CrCommandParam>(parameter));
    if (error != SDK::CrError_None) {
        set_error("the camera rejected the command");
        return static_cast<int32_t>(error);
    }
    return CRSDKPY_OK;
}

int32_t crsdkpy_list_content(uint64_t handle, uint32_t slot,
                             uint32_t after_content_id, crsdkpy_content* out,
                             uint32_t capacity, uint32_t* out_count)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_count) return CRSDKPY_ERR_INVALID_ARG;
    *out_count = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;
    if (slot == 0) slot = CRSDKPY_SLOT_1;

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;
    if (session->mode() != CRSDKPY_MODE_REMOTE_TRANSFER) {
        set_error(
            "the content index is only exposed in the RemoteTransfer control "
            "mode; reopen the camera in that mode to read it");
        return CRSDKPY_ERR_UNSUPPORTED;
    }

    const auto device = session->handle();
    const auto vendor_slot = static_cast<SDK::CrSlotNumber>(slot);

    // Newest captured date first. Re-read every call: remembering it would go
    // stale the moment a shot crosses midnight.
    SDK::CrCaptureDate* dates = nullptr;
    CrInt32u date_count = 0;
    auto error = SDK::GetRemoteTransferCapturedDateList(device, vendor_slot, &dates,
                                                        &date_count);
    if (error != SDK::CrError_None) {
        if (dates) SDK::ReleaseRemoteTransferCapturedDateList(device, dates);
        set_error("the camera could not list captured dates");
        return static_cast<int32_t>(error);
    }
    if (!dates || date_count == 0) {
        if (dates) SDK::ReleaseRemoteTransferCapturedDateList(device, dates);
        return CRSDKPY_OK;  // empty media is not a failure
    }

    CrInt32u newest = 0;
    for (CrInt32u i = 1; i < date_count; ++i) {
        const SDK::CrCaptureDate& a = dates[i];
        const SDK::CrCaptureDate& b = dates[newest];
        const bool later =
            a.year > b.year ||
            (a.year == b.year && (a.month > b.month ||
                                  (a.month == b.month && a.day > b.day)));
        if (later) newest = i;
    }

    SDK::CrContentsInfo* list = nullptr;
    CrInt32u list_count = 0;
    // maxNums is 0 for "everything on that day". Asking for a bounded number
    // returns the *oldest* records, which is the opposite of what a caller
    // waiting for a fresh shot wants.
    error = SDK::GetRemoteTransferContentsInfoList(
        device, vendor_slot, SDK::CrGetContentsInfoListType_Range_Day,
        &dates[newest], 0, &list, &list_count);
    SDK::ReleaseRemoteTransferCapturedDateList(device, dates);
    if (error != SDK::CrError_None) {
        if (list) SDK::ReleaseRemoteTransferContentsInfoList(device, list);
        set_error("the camera could not list content");
        return static_cast<int32_t>(error);
    }
    if (!list || list_count == 0) {
        if (list) SDK::ReleaseRemoteTransferContentsInfoList(device, list);
        return CRSDKPY_OK;
    }

    // Copy everything of interest out while the vendor list is still alive,
    // then sort our own copies. Identifiers are monotonic but not contiguous,
    // and the vendor does not promise an order.
    std::vector<crsdkpy_content> selected;
    selected.reserve(list_count);
    for (CrInt32u i = 0; i < list_count; ++i) {
        if (list[i].contentId <= after_content_id) continue;
        if (list[i].dummyContent) continue;
        crsdkpy_content item;
        copy_content(list[i], slot, item);
        selected.push_back(item);
    }
    SDK::ReleaseRemoteTransferContentsInfoList(device, list);

    for (size_t i = 1; i < selected.size(); ++i) {
        crsdkpy_content key = selected[i];
        size_t j = i;
        while (j > 0 && selected[j - 1].content_id > key.content_id) {
            selected[j] = selected[j - 1];
            --j;
        }
        selected[j] = key;
    }

    *out_count = static_cast<uint32_t>(selected.size());
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (selected.size() > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;
    for (size_t i = 0; i < selected.size(); ++i) out[i] = selected[i];
    return CRSDKPY_OK;
}

int32_t crsdkpy_fetch_content_preview(uint64_t handle, uint32_t slot,
                                      uint32_t content_id, uint32_t file_id,
                                      int32_t kind, int32_t timeout_ms,
                                      crsdkpy_preview_info* out_info)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_info) return CRSDKPY_ERR_INVALID_ARG;
    std::memset(out_info, 0, sizeof(*out_info));
    if (slot == 0) slot = CRSDKPY_SLOT_1;
    if (kind != CRSDKPY_PREVIEW_THUMBNAIL && kind != CRSDKPY_PREVIEW_SCREENNAIL) {
        set_error("preview kind must be thumbnail or screennail");
        return CRSDKPY_ERR_INVALID_ARG;
    }

    // Resolve under the global lock, then release it: the wait below runs for
    // most of a second and holding the lock would stall every other session.
    // The shared_ptr keeps this session alive even if it is closed meanwhile.
    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        session = resolve(handle);
        if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
        if (session->state() != CRSDKPY_STATE_CONNECTED) {
            return CRSDKPY_ERR_NOT_CONNECTED;
        }
        if (session->mode() != CRSDKPY_MODE_REMOTE_TRANSFER) {
            set_error(
                "compressed previews are only available in the RemoteTransfer "
                "control mode; reopen the camera in that mode to fetch them");
            return CRSDKPY_ERR_UNSUPPORTED;
        }
    }

    // One transfer per session at a time. This is not throughput tuning: the
    // vendor's completion callback carries no request identity, so overlapping
    // fetches would make association guesswork.
    std::lock_guard<std::mutex> gate(session->transfer_gate());
    session->begin_transfer(slot, content_id, file_id, kind);

    const auto error = SDK::GetRemoteTransferContentsCompressedData(
        session->handle(), static_cast<SDK::CrSlotNumber>(slot), content_id,
        file_id, static_cast<SDK::CrGetContentsCompressedDataType>(kind));
    if (error != SDK::CrError_None) {
        session->abandon_transfer();
        set_error("the camera rejected the preview request");
        return static_cast<int32_t>(error);
    }

    PendingTransfer result;
    if (!session->await_transfer(timeout_ms > 0 ? timeout_ms : 10000,
                                 kTransferSettleMs, &result)) {
        set_error(session->closed()
                      ? "the session closed while the preview was transferring"
                      : "the camera did not deliver the preview before the "
                        "timeout");
        return CRSDKPY_ERR_TIMEOUT;
    }
    if (result.failed || result.bytes.empty()) {
        session->abandon_transfer();
        set_error("the camera ended the preview transfer without any data");
        return result.notify ? result.notify : CRSDKPY_ERR_UNKNOWN;
    }

    out_info->content_id = result.content_id;
    out_info->file_id = result.file_id;
    out_info->kind = result.kind;
    out_info->vendor_notify = result.notify;
    out_info->byte_length = static_cast<uint32_t>(result.bytes.size());
    out_info->slot = result.slot;
    out_info->deliveries = result.deliveries;
    out_info->last_percent = result.last_percent;
    out_info->requested_ms = result.requested_ms;
    out_info->completed_ms = result.completed_ms;
    return CRSDKPY_OK;
}

int32_t crsdkpy_configure_postview(uint64_t handle, int32_t enabled,
                                   int32_t transfer_to_ram)
{
    set_error("");  // never inherit a previous failure's text
    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    // Transferring type first: enabling postview without having said where it
    // should go would leave the camera on whatever it used last.
    const CrInt32u transferring =
        transfer_to_ram ? SDK::CrPostViewTransferring_UserSelect_RAM
                        : SDK::CrPostViewTransferring_UserSelect_File;
    auto error = SDK::SetDeviceSetting(
        session->handle(), SDK::Setting_Key_PostViewTransferringType, transferring);
    if (error == SDK::CrError_None) {
        error = SDK::SetDeviceSetting(session->handle(),
                                      SDK::Setting_Key_EnablePostView,
                                      enabled ? 1u : 0u);
    }
    if (error == SDK::CrError_None) return CRSDKPY_OK;

    if (error == SDK::CrError_Api_InvalidCalled) {
        // Observed on real hardware in one control mode. It says nothing
        // about whether postview will still be delivered.
        set_error(
            "the camera refused to configure postview in this control mode; "
            "this does not mean postview will not be delivered, which depends "
            "on the still destination and must be observed separately");
        return CRSDKPY_ERR_UNSUPPORTED;
    }
    set_error("the camera rejected the postview configuration");
    return static_cast<int32_t>(error);
}

int32_t crsdkpy_pull_postview(uint64_t handle, crsdkpy_postview_info* out_info)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_info) return CRSDKPY_ERR_INVALID_ARG;
    std::memset(out_info, 0, sizeof(*out_info));

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    PostviewPending pending;
    if (!session->take_postview_pending(&pending)) {
        // Ordinary: the camera has not announced one. Not a failure.
        return CRSDKPY_ERR_NOT_FOUND;
    }
    if (pending.size == 0) {
        set_error("the camera announced a postview of zero bytes");
        return CRSDKPY_ERR_INVALID_ARG;
    }
    if (pending.size > CRSDKPY_MAX_IMAGE_BYTES) {
        set_error("the camera announced an implausibly large postview");
        return CRSDKPY_ERR_INVALID_ARG;
    }

    std::vector<uint8_t> buffer(pending.size);
    const auto error =
        SDK::PullPostViewImage(session->handle(), buffer.data(), pending.size);
    if (error != SDK::CrError_None) {
        set_error("the camera rejected the postview pull");
        return static_cast<int32_t>(error);
    }

    const int64_t pulled = now_ms();
    out_info->byte_length = pending.size;
    out_info->notified_ms = pending.notified_ms;
    out_info->pulled_ms = pulled;
    copy_string(out_info->filename, sizeof(out_info->filename), pending.filename);
    session->hold_postview(std::move(buffer), pulled);
    return CRSDKPY_OK;
}

int32_t crsdkpy_take_transfer_path(uint64_t handle, char* out,
                                  uint32_t capacity, uint32_t* out_length)
{
    set_error("");
    if (!out_length) return CRSDKPY_ERR_INVALID_ARG;
    *out_length = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        session = resolve(handle);
        if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    }
    const size_t held = session->transfer_path_size();
    if (held == 0) {
        set_error("no transfer path is being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    *out_length = static_cast<uint32_t>(held);
    if (capacity == 0) return CRSDKPY_OK;  // sizing call, nothing consumed
    if (held + 1 > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;

    std::string path;
    if (!session->take_transfer_path(&path)) {
        set_error("no transfer path is being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    *out_length = static_cast<uint32_t>(path.size());
    copy_string(out, capacity, path);
    return CRSDKPY_OK;
}

int32_t crsdkpy_take_postview(uint64_t handle, uint8_t* out, uint32_t capacity,
                              uint32_t* out_size)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_size) return CRSDKPY_ERR_INVALID_ARG;
    *out_size = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        session = resolve(handle);
        if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    }

    const size_t held = session->held_postview_size();
    if (held == 0) {
        set_error("no postview bytes are being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    *out_size = static_cast<uint32_t>(held);
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (held > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;

    std::vector<uint8_t> bytes;
    if (!session->take_postview_bytes(&bytes)) {
        set_error("no postview bytes are being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    std::memcpy(out, bytes.data(), bytes.size());
    *out_size = static_cast<uint32_t>(bytes.size());
    return CRSDKPY_OK;
}

int32_t crsdkpy_get_live_view_info(uint64_t handle, crsdkpy_live_view_info* out)
{
    set_error("");  // never inherit a previous failure's text
    if (!out) return CRSDKPY_ERR_INVALID_ARG;
    std::memset(out, 0, sizeof(*out));

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrImageInfo info;
    const auto error = SDK::GetLiveViewImageInfo(session->handle(), &info);
    // A camera saying it cannot stream is an answer, not a failure of this
    // call, so the verdict travels in the struct.
    out->info_ok = (error == SDK::CrError_None) ? 1 : 0;
    out->vendor_error = static_cast<int32_t>(error);
    if (error == SDK::CrError_None) {
        out->width = info.GetWidth();
        out->height = info.GetHeight();
        out->buffer_size = info.GetBufferSize();
    }
    return CRSDKPY_OK;
}

int32_t crsdkpy_get_live_view_frame(uint64_t handle, uint8_t* out,
                                    uint32_t capacity,
                                    crsdkpy_frame_info* out_info)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_info) return CRSDKPY_ERR_INVALID_ARG;
    std::memset(out_info, 0, sizeof(*out_info));
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::lock_guard<std::mutex> lock(g_mutex);
    auto session = resolve(handle);
    if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    if (session->state() != CRSDKPY_STATE_CONNECTED) return CRSDKPY_ERR_NOT_CONNECTED;

    SDK::CrImageInfo info;
    auto error = SDK::GetLiveViewImageInfo(session->handle(), &info);
    if (error != SDK::CrError_None) {
        set_error("the camera would not describe its live-view stream");
        return static_cast<int32_t>(error);
    }
    const uint32_t needed = info.GetBufferSize();
    if (needed == 0) {
        // Measured: one control mode answers the info call successfully and
        // still cannot produce a frame. Refuse here rather than let the fetch
        // fail with a generic vendor code.
        set_error(
            "the camera reports a zero-byte live-view buffer, so no frame can "
            "be delivered in this control mode");
        return CRSDKPY_ERR_UNSUPPORTED;
    }
    if (needed > CRSDKPY_MAX_IMAGE_BYTES) {
        set_error("the camera reports an implausibly large live-view buffer");
        return CRSDKPY_ERR_INVALID_ARG;
    }

    out_info->byte_length = needed;
    out_info->width = info.GetWidth();
    out_info->height = info.GetHeight();
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (capacity < needed) return CRSDKPY_ERR_BUFFER_TOO_SMALL;

    // The vendor writes into the caller's buffer, so no vendor-owned
    // allocation exists at any point and nothing needs copying out of one.
    SDK::CrImageDataBlock block;
    block.SetSize(needed);
    block.SetData(out);
    error = SDK::GetLiveViewImage(session->handle(), &block);
    if (error != SDK::CrError_None) {
        set_error("the camera did not return a live-view frame");
        return static_cast<int32_t>(error);
    }
    const uint32_t produced = block.GetImageSize();
    if (produced == 0) {
        // Nothing new. Ordinary around an exposure; the stream resumes.
        return CRSDKPY_ERR_NOT_FOUND;
    }
    out_info->byte_length = produced > needed ? needed : produced;
    out_info->frame_number = block.GetFrameNo();
    out_info->time_code = block.GetTimeCode();
    out_info->fetched_ms = now_ms();
    return CRSDKPY_OK;
}

int32_t crsdkpy_take_content_preview(uint64_t handle, uint8_t* out,
                                     uint32_t capacity, uint32_t* out_size)
{
    set_error("");  // never inherit a previous failure's text
    if (!out_size) return CRSDKPY_ERR_INVALID_ARG;
    *out_size = 0;
    if (capacity > 0 && !out) return CRSDKPY_ERR_INVALID_ARG;

    std::shared_ptr<Session> session;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        session = resolve(handle);
        if (!session) return CRSDKPY_ERR_INVALID_HANDLE;
    }

    const size_t held = session->held_transfer_size();
    if (held == 0) {
        set_error("no preview bytes are being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    *out_size = static_cast<uint32_t>(held);
    if (capacity == 0) return CRSDKPY_OK;  // sizing call
    if (held > capacity) return CRSDKPY_ERR_BUFFER_TOO_SMALL;

    std::vector<uint8_t> bytes;
    if (!session->take_transfer_bytes(&bytes)) {
        set_error("no preview bytes are being held for this session");
        return CRSDKPY_ERR_NOT_FOUND;
    }
    std::memcpy(out, bytes.data(), bytes.size());
    *out_size = static_cast<uint32_t>(bytes.size());
    return CRSDKPY_OK;
}

}  // extern "C"
