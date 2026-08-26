import pytest

import crsdkpy
from crsdkpy.simulator import Scenario

ISO = 0x0104
F_NUMBER = 0x0100
COMPENSATION = 0x0101
WHITE_BALANCE = 0x0108
DRIVE_MODE = 0x010E


def _open(scenario=None):
    sdk = crsdkpy.SDK(backend="simulator", profile="fx3a", scenario=scenario)
    session = sdk.discover()[0].open("remote")
    return sdk, session


def test_setting_reads_live_value_and_writes_through_property_layer() -> None:
    sdk, session = _open()
    try:
        setting = session.exposure.iso
        assert setting.available
        assert setting.value == 100
        assert setting.writable
        assert setting.allowed_values == (100, 125, 200, 400, 0xFFFFFF)
        setting.set(125)
        assert setting.value == 125
        assert session.properties.get(ISO).value == 125
    finally:
        sdk.close()


def test_read_only_setting_rejects_write_explicitly() -> None:
    scenario = Scenario(read_only_property_codes=(F_NUMBER,))
    sdk, session = _open(scenario)
    try:
        setting = session.exposure.f_number
        assert setting.value == 140
        assert not setting.writable
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="read-only"):
            setting.set(200)
    finally:
        sdk.close()


def test_allowed_list_constrains_writes_not_current_camera_value() -> None:
    scenario = Scenario(property_value_overrides=((ISO, 777),))
    sdk, session = _open(scenario)
    try:
        setting = session.exposure.iso
        assert setting.value == 777
        assert 777 not in setting.allowed_values
        assert not setting.accepts(777)
        with pytest.raises(ValueError):
            setting.set(777)
        setting.set(200)
        assert setting.value == 200
    finally:
        sdk.close()


def test_range_and_step_validation_use_decoded_property_metadata() -> None:
    sdk, session = _open()
    try:
        setting = session.exposure.compensation
        assert setting.value_range == crsdkpy.PropertyRange(-3000, 3000, 1000)
        assert setting.accepts(2000)
        assert not setting.accepts(2500)
        assert not setting.accepts(4000)
        with pytest.raises(ValueError):
            setting.set(2500)
        setting.set(-1000)
        assert setting.value == -1000
    finally:
        sdk.close()


def test_unavailable_property_is_represented_and_rejects_write() -> None:
    scenario = Scenario(unavailable_property_codes=(WHITE_BALANCE,))
    sdk, session = _open(scenario)
    try:
        setting = session.white_balance.mode
        assert not setting.available
        assert setting.value is None
        assert setting.allowed_values == ()
        assert setting.value_range is None
        assert not setting.accepts(0)
        with pytest.raises(crsdkpy.UnsupportedOperationError, match="does not expose"):
            setting.set(0)
    finally:
        sdk.close()


def test_setting_observes_runtime_disappearance_and_session_close() -> None:
    sdk, session = _open()
    setting = session.drive.mode
    try:
        assert setting.available
        session._backend.simulate_property_unavailable(session._id, DRIVE_MODE)
        assert not setting.available
        assert setting.value is None
        session.close()
        with pytest.raises(crsdkpy.SessionClosedError):
            _ = setting.value
    finally:
        sdk.close()


def test_undecoded_values_remain_raw_without_fake_validation() -> None:
    scenario = Scenario(
        property_value_overrides=((DRIVE_MODE, 0xDEADBEEF),),
        values_undecoded_property_codes=(DRIVE_MODE,),
    )
    sdk, session = _open(scenario)
    try:
        setting = session.drive.mode
        assert setting.value == 0xDEADBEEF
        assert setting.values_undecoded
        assert not setting.validation_available
        assert setting.allowed_values == ()
        assert setting.value_range is None
        assert setting.accepts(0x12345678)
    finally:
        sdk.close()


def test_exposure_facade_composes_documented_settings() -> None:
    sdk, session = _open()
    try:
        assert session.exposure.f_number.code == F_NUMBER
        assert session.exposure.compensation.code == COMPENSATION
        assert session.exposure.shutter_speed.code == 0x0103
        assert session.exposure.iso.code == ISO
        assert session.exposure.program.code == 0x0105
        assert session.exposure.shutter_speed.value == 0x0001007D
    finally:
        sdk.close()


def test_white_balance_and_drive_facades_preserve_raw_modes() -> None:
    scenario = Scenario(
        property_value_overrides=((WHITE_BALANCE, 0x7777), (DRIVE_MODE, 0x8888))
    )
    sdk, session = _open(scenario)
    try:
        assert session.white_balance.mode.value == 0x7777
        assert session.drive.mode.value == 0x8888
        assert not session.white_balance.mode.accepts(0x7777)
        assert not session.drive.mode.accepts(0x8888)
    finally:
        sdk.close()
