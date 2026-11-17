#include "download_identity.h"
#include <cstdio>
int main() {
    using crsdkpy::download_callback_matches;
    using crsdkpy::download_identity_valid;
    const std::string name = "0000000000000002-123456789abcdef0123456789abcdef0.ARW";
    const std::string path = "/private/stage/" + name;
    if (!download_identity_valid(2, path)) return 1;
    if (download_identity_valid(1, path)) return 2;
    if (!download_callback_matches(2, path, path)) return 3;
    if (!download_callback_matches(2, path, name)) return 4;
    if (download_callback_matches(2, path, "/old/stage/" + name)) return 5;
    if (download_callback_matches(2, path, "0000000000000001.ARW")) return 6;
    if (download_callback_matches(2, path, "")) return 7;
    if (download_callback_matches(2, path, "DSC00001.ARW")) return 8;
    if (download_identity_valid(0, path)) return 9;
    if (download_identity_valid(uint64_t(1) << 63, path)) return 10;
    if (download_callback_matches(2, path,
        "0000000000000002-00000000000000000000000000000000.ARW")) return 11;
    if (download_callback_matches(2, path, name.substr(0, name.size() - 1))) return 12;
    if (download_callback_matches(2, path, name + ".extra")) return 13;
    if (download_callback_matches(2, path, "./" + name)) return 14;
    if (download_callback_matches(2, path,
            "0000000000000002-123456789ABCDEF0123456789ABCDEF0.ARW")) return 15;
    const std::string unicode = u8"/\u6444\u5f71/\u00e9/" + name;
    if (!download_callback_matches(2, unicode, unicode)) return 16;
    if (download_callback_matches(2, unicode, u8"/\u6444\u5f71/e\u0301/" + name)) return 17;
#if defined(_WIN32)
    if (!download_callback_matches(2, "C:/stage/" + name, "C:\\stage\\" + name)) return 18;
#else
    if (download_callback_matches(2, "/stage/" + name, "\\stage\\" + name)) return 18;
#endif
    std::puts("owned callback path/identity contract PASS");
    return 0;
}
