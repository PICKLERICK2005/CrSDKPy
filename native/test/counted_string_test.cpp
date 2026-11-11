#include "sony_counted_string.h"
#include <cstdlib>
#include <initializer_list>

static void check(std::initializer_list<uint16_t> data, bool valid,
                  const std::string& expected = "")
{
    std::string text = "previous";
    if (sony_counted_string(data.begin(), data.size(), text) != valid ||
        text != expected) std::abort();
}

int main()
{
    check({5, '6', '.', '0', '1', 0}, true, "6.01");
    check({3, '0', '2', 0}, true, "02");
    check({2, 5, 0}, true, "\x05"); // content is never blindly stripped
    check({16, 'F','E',' ','2','0','-','7','0','m','m',' ','F','4',' ','G',0},
          true, "FE 20-70mm F4 G");
    check({0}, true);
    check({1, 0}, true);
    check({2, 0x00e9, 0}, true, "\xc3\xa9");
    check({3, 0xd83d, 0xde00, 0}, true, "\xf0\x9f\x98\x80");
    check({}, false);
    check({65535, 'x', 0}, false);
    check({2, 'x'}, false);
    check({2, 'x', 'y'}, false);
    check({3, 'x', 0, 0}, false);
    check({2, 0xd800, 0}, false);
    check({2, 0xdc00, 0}, false);
}
