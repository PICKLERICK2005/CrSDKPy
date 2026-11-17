#include "live_view_payload.h"
#include <array>
#include <cstring>
struct Block {
    const uint8_t* image;
    uint32_t size;
    uint32_t GetImageSize() const { return size; }
    const uint8_t* GetImageData() const { return image; }
    uint32_t GetFrameNo() const { return 13; }
    uint32_t GetTimeCode() const { return 42; }
};
int main() {
    std::array<uint8_t, 64> buffer{};
    std::array<uint8_t, 64> output{};
    const uint8_t jpeg[] = {255, 216, 1, 2, 255, 217};
    crsdkpy_frame_info info{};
    info.width = 640;
    info.height = 428;
    auto copy = [&](Block b, uint8_t* out, uint32_t capacity) {
        return crsdkpy::copy_live_view_payload(buffer.data(), buffer.size(), b,
                                             out, capacity, &info, 1234);
    };
    std::memcpy(buffer.data()+17, jpeg, sizeof(jpeg));
    if (copy({buffer.data()+17, sizeof(jpeg)}, output.data(), output.size()) != CRSDKPY_OK) return 1;
    if (std::memcmp(output.data(), jpeg, sizeof(jpeg)) || info.byte_length != sizeof(jpeg) ||
        info.frame_number != 13 || info.time_code != 42 || info.fetched_ms != 1234 ||
        info.width != 640 || info.height != 428) return 2;
    if (copy({buffer.data()+17, sizeof(jpeg)}, buffer.data()+16, 48) != CRSDKPY_OK ||
        std::memcmp(buffer.data()+16, jpeg, sizeof(jpeg))) return 3;
    std::memcpy(buffer.data(), jpeg, sizeof(jpeg));
    if (copy({buffer.data(), sizeof(jpeg)}, buffer.data(), buffer.size()) != CRSDKPY_OK ||
        std::memcmp(buffer.data(), jpeg, sizeof(jpeg))) return 4;
    if (copy({nullptr, 0}, output.data(), output.size()) != CRSDKPY_ERR_NOT_FOUND || info.byte_length) return 5;
    if (copy({nullptr, 1}, output.data(), output.size()) != CRSDKPY_ERR_INVALID_ARG) return 6;
    if (copy({output.data(), 1}, output.data(), output.size()) != CRSDKPY_ERR_INVALID_ARG) return 7;
    if (copy({buffer.data()+64, 1}, output.data(), output.size()) != CRSDKPY_ERR_INVALID_ARG) return 8;
    if (copy({buffer.data()+60, 5}, output.data(), output.size()) != CRSDKPY_ERR_INVALID_ARG) return 9;
    if (copy({buffer.data(), 65}, output.data(), output.size()) != CRSDKPY_ERR_INVALID_ARG) return 10;
    output.fill(99);
    if (copy({buffer.data(), 6}, output.data(), 5) != CRSDKPY_ERR_BUFFER_TOO_SMALL ||
        info.byte_length != 6 || output[0] != 99) return 11;
    if (copy({buffer.data(), 6}, nullptr, 6) != CRSDKPY_ERR_INVALID_ARG) return 12;
    if (crsdkpy::copy_live_view_payload(buffer.data(), 64, Block{buffer.data(), 6},
                                      output.data(), 64, nullptr, 1) != CRSDKPY_ERR_INVALID_ARG) return 13;
    std::memcpy(buffer.data(), jpeg, sizeof(jpeg));
    if (copy({buffer.data(), 6}, buffer.data()+1, 63) != CRSDKPY_OK ||
        std::memcmp(buffer.data()+1, jpeg, sizeof(jpeg))) return 14;
    const auto before = reinterpret_cast<const uint8_t*>(
        reinterpret_cast<uintptr_t>(buffer.data())-1);
    if (copy({before, 1}, output.data(), 64) != CRSDKPY_ERR_INVALID_ARG) return 15;
    return 0;
}
