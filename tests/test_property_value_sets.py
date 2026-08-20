"""The wire format of a property's advertised value set.

The camera describes what each property accepts, and the bridge decodes that
using the vendor's own data type rather than guessing from the byte count. This
pins the format itself against bytes a real body reported, so a change to the
element width or the range layout has something concrete to fail against.

It exercises the format contract, not the native code path: the decoder lives in
C++ and only runs with a camera attached. What it guarantees is that the rules
the C++ implements are the rules the hardware actually uses.
"""

from __future__ import annotations

import json
import os
import struct

import pytest

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "property_value_sets.json")

# Vendor data-type bits.
SIGN_BIT = 0x1000
ARRAY_BIT = 0x2000
RANGE_BIT = 0x4000
WIDTHS = {0x0001: 1, 0x0002: 2, 0x0003: 4, 0x0004: 8}


def decode(data_type: int, blob: bytes):
    """The same rules the bridge applies, expressed independently."""
    width = WIDTHS.get(data_type & 0x0FFF, 0)
    ranged = bool(data_type & RANGE_BIT)
    arrayed = bool(data_type & ARRAY_BIT)
    if width == 0 or not (ranged or arrayed) or len(blob) % width:
        return "raw", []
    if ranged and len(blob) // width != 3:
        return "raw", []
    code = {1: "b", 2: "h", 4: "i", 8: "q"}[width]
    if not data_type & SIGN_BIT:
        code = code.upper() if width < 8 else "Q"
    values = list(struct.unpack(f"<{len(blob) // width}{code}", blob))
    return ("range" if ranged else "enum"), values


def load():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)["properties"]


def test_the_fixture_carries_no_identifying_data() -> None:
    """Snapshots from a real body must not leak into tracked fixtures."""
    with open(FIXTURE, encoding="utf-8") as handle:
        text = handle.read().lower()
    for leaked in ("serial", "dcim", "c:/", "d:/", "a:/", "users"):
        assert leaked not in text


@pytest.mark.parametrize("entry", load(), ids=lambda e: e["name"] or e["code"])
def test_every_advertised_set_decodes_to_a_known_shape(entry) -> None:
    blob = bytes.fromhex(entry["values_hex"])
    assert len(blob) == entry["value_size"]
    shape, values = decode(entry["data_type"], blob)
    assert shape in ("range", "enum")
    assert values
    if shape == "range":
        minimum, maximum, step = values
        assert minimum <= maximum
        assert step >= 1  # a zero step would make a range unusable


def test_a_range_is_three_values_in_minimum_maximum_step_order() -> None:
    """Two properties whose real ranges are independently known.

    The focus-position range is the full 16-bit space, and the interval-recording
    shot count is 1 to 9999 -- a figure the camera's own menu shows, which is
    what makes it usable as a check on the byte order.
    """
    by_code = {e["code"]: e for e in load()}

    focus = by_code["0x020E"]
    shape, (minimum, maximum, step) = decode(
        focus["data_type"], bytes.fromhex(focus["values_hex"])
    )
    assert shape == "range"
    assert (minimum, maximum, step) == (0, 65535, 1)

    shots = by_code["0x01FF"]
    shape, (minimum, maximum, step) = decode(
        shots["data_type"], bytes.fromhex(shots["values_hex"])
    )
    assert shape == "range"
    assert (minimum, maximum, step) == (1, 9999, 1)


def test_an_enumerated_set_is_a_list_of_permitted_values() -> None:
    by_code = {e["code"]: e for e in load()}
    focus_mode = by_code["0x0109"]
    shape, values = decode(
        focus_mode["data_type"], bytes.fromhex(focus_mode["values_hex"])
    )
    assert shape == "enum"
    # The measured focus-mode values on this body: MF, AF-S and AF-C.
    for expected in (1, 2, 3):
        assert expected in values


def test_a_range_type_with_the_wrong_element_count_is_left_raw() -> None:
    """Four numbers is not a range, and must not be read as one."""
    shape, values = decode(RANGE_BIT | 0x0002, struct.pack("<4H", 1, 2, 3, 4))
    assert shape == "raw"
    assert values == []


def test_an_unknown_base_type_is_left_raw() -> None:
    shape, _ = decode(RANGE_BIT | 0x0FFF, b"\x01\x02\x03\x04\x05\x06")
    assert shape == "raw"
