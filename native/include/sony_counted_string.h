#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

// Sony CurrentStr: uint16 count, then count UTF-16 units including NUL.
// available is the allocation size in uint16 units, including the count word.
inline bool sony_counted_string(const uint16_t* data, size_t available,
                                std::string& output)
{
    output.clear();
    if (!data || available == 0) return false;
    const size_t count = data[0];
    if (count == 0) return true; // PTP empty string has no payload.
    if (count >= available || data[count] != 0) return false;
    std::string text;
    for (size_t i = 1; i < count; ++i) {
        uint32_t cp = data[i];
        if (cp == 0) return false; // embedded NUL contradicts the count
        if (cp >= 0xd800 && cp <= 0xdbff) {
            if (++i >= count || data[i] < 0xdc00 || data[i] > 0xdfff)
                return false;
            cp = 0x10000 + ((cp - 0xd800) << 10) + (data[i] - 0xdc00);
        } else if (cp >= 0xdc00 && cp <= 0xdfff) {
            return false;
        }
        if (cp < 0x80) text += static_cast<char>(cp);
        else if (cp < 0x800) {
            text += static_cast<char>(0xc0 | (cp >> 6));
            text += static_cast<char>(0x80 | (cp & 0x3f));
        } else if (cp < 0x10000) {
            text += static_cast<char>(0xe0 | (cp >> 12));
            text += static_cast<char>(0x80 | ((cp >> 6) & 0x3f));
            text += static_cast<char>(0x80 | (cp & 0x3f));
        } else {
            text += static_cast<char>(0xf0 | (cp >> 18));
            text += static_cast<char>(0x80 | ((cp >> 12) & 0x3f));
            text += static_cast<char>(0x80 | ((cp >> 6) & 0x3f));
            text += static_cast<char>(0x80 | (cp & 0x3f));
        }
    }
    output = text;
    return true;
}
