/* C compatibility/layout only. Never loads the SDK or touches a camera. */
#include "crsdkpy_abi.h"

int main(void)
{
    return sizeof(crsdkpy_event) != 40 ||
           sizeof(crsdkpy_event_boundary) != 16 ||
           CRSDKPY_ABI_VERSION_MAJOR != 1 || CRSDKPY_ABI_VERSION_MINOR != 6;
}
