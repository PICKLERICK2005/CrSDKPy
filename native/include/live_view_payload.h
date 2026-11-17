#ifndef CRSDKPY_LIVE_VIEW_PAYLOAD_H
#define CRSDKPY_LIVE_VIEW_PAYLOAD_H
#include "crsdkpy_abi.h"
#include <cstdint>
#include <cstring>

namespace crsdkpy {
// The caller owns the receive allocation. Sony's image pointer may identify
// a subrange, valid only while that allocation and image block remain alive.
template<class Block>
int32_t copy_live_view_payload(const uint8_t* receive, uint32_t receive_size,
                              const Block& block, uint8_t* out, uint32_t capacity,
                              crsdkpy_frame_info* info, uint64_t fetched_ms) {
    if (!info) return CRSDKPY_ERR_INVALID_ARG;
    info->byte_length = 0;
    const uint32_t produced = block.GetImageSize();
    if (!produced) return CRSDKPY_ERR_NOT_FOUND;
    const uint8_t* image = block.GetImageData();
    if (!receive || !image || !out) return CRSDKPY_ERR_INVALID_ARG;
    const uintptr_t base = reinterpret_cast<uintptr_t>(receive);
    const uintptr_t source = reinterpret_cast<uintptr_t>(image);
    // Integer subtraction after ordering avoids undefined subtraction of
    // pointers from different allocations; subtraction-based bounds avoid wrap.
    if (source < base || source - base > receive_size ||
        produced > receive_size - (source - base)) return CRSDKPY_ERR_INVALID_ARG;
    if (produced > capacity) {
        info->byte_length = produced;
        return CRSDKPY_ERR_BUFFER_TOO_SMALL;
    }
    std::memmove(out, image, produced);
    info->byte_length = produced;
    info->frame_number = block.GetFrameNo();
    info->time_code = block.GetTimeCode();
    info->fetched_ms = fetched_ms;
    return CRSDKPY_OK;
}
}
#endif
