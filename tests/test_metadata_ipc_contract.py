"""Nonzero property metadata codes cross the hosted boundary unchanged."""

import pytest

import crsdkpy
from crsdkpy.backend import _ipc
from test_host_ipc import make_backend


@pytest.mark.parametrize("code,value", [(0x0751, "9.99"), (0x0765, "SIM 100mm F2.0")])
def test_nonzero_string_codes_recover_the_actual_string(code, value):
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        with sdk.discover()[0].open("remote") as session:
            assert session.properties.get(code).value == value


def test_nonzero_value_codes_recover_enum_and_range_and_ignore_i32():
    backend = make_backend()
    with crsdkpy.SDK(backend=backend) as sdk:
        with sdk.discover()[0].open("remote") as session:
            assert session.properties.get(0x0109).allowed_values == (1, 2, 3)
            r = session.properties.get(0x020E).value_range
            assert (r.minimum, r.maximum, r.step) == (0, 65535, 1)
            handle = backend._handle(session._id, "test")
            response, _ = backend._call(
                _ipc.OP_PROPERTY_STRING,
                handle=handle,
                u32=0x0751,
                i32=0x0109,
                operation="test",
            )
            assert response.message.decode() == "9.99"
