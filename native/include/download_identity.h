#ifndef CRSDKPY_DOWNLOAD_IDENTITY_H
#define CRSDKPY_DOWNLOAD_IDENTITY_H
#include <cstdint>
#include <cstdio>
#include <string>

namespace crsdkpy {
inline std::string download_normalize(std::string path) {
    for (char& c : path) {
#if defined(_WIN32)
        if (c == '\\') c = '/';
#else
        (void)c;
#endif
    }
    return path;
}
inline std::string download_basename(const std::string& path) {
    const auto position = path.find_last_of('/');
    return position == std::string::npos ? path : path.substr(position + 1);
}
inline bool download_identity_valid(uint64_t id, const std::string& expected) {
    if (!id || id >= (uint64_t(1) << 63)) return false;
    char prefix[17];
    std::snprintf(prefix, sizeof(prefix), "%016llx",
                  static_cast<unsigned long long>(id));
    const auto name = download_basename(download_normalize(expected));
    if (name.size() < 49 || name.compare(0, 16, prefix) != 0 || name[16] != '-')
        return false;
    for (size_t i = 17; i < 49; ++i)
        if (!((name[i] >= '0' && name[i] <= '9') ||
              (name[i] >= 'a' && name[i] <= 'f'))) return false;
    return name.size() == 49 || name[49] == '.';
}
inline bool download_callback_matches(uint64_t id, const std::string& expected,
                                      const std::string& reported) {
    if (!download_identity_valid(id, expected) || reported.empty()) return false;
    const auto want = download_normalize(expected);
    const auto got = download_normalize(reported);
    // Full paths must match. A filename-only callback is accepted only for
    // the operation-unique basename validated above; never arbitrary old names.
    return got == want || (got.find('/') == std::string::npos
                           && got == download_basename(want));
}
}
#endif
