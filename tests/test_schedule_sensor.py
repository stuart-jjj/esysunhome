"""ScheduleSensor: the ESY BEM schedule as a Home Assistant sensor (ADO 351)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esy_sunhome.const import (
    CONF_DEVICE_ID,
    CONF_MCU_VERSION,
    CONF_PV_POWER,
    CONF_TP_TYPE,
    DOMAIN,
)
from custom_components.esy_sunhome.coordinator import ESYSunhomeCoordinator
from custom_components.esy_sunhome.protocol_api import ProtocolDefinition
from custom_components.esy_sunhome.sensor import ScheduleSensor

SYDNEY = ZoneInfo("Australia/Sydney")
FETCHED_AT = datetime(2026, 11, 25, 0, 5, 0, tzinfo=UTC)
UPDATE_TIME = 1795405320


def _live_schedule(**overrides):
    """The live 2026-11-23 schedule: Regular 08:00-15:00, Sell 15:00-08:00."""
    schedule = {
        "releaseTimeQuantum": [
            {"start": 480, "end": 900, "soc": 20, "sort": 1, "power": 5, "electric": 0}
        ],
        "dischargeTimeQuantum": [
            {"start": 900, "end": 1439, "soc": 10},
            {"start": 0, "end": 480, "soc": 10},
        ],
        "chargeTimeQuantum": [],
        "releaseSwitch": 1,
        "dischargeSwitch": 1,
        "chargeSwitch": 0,
        "updateTime": UPDATE_TIME,
    }
    schedule.update(overrides)
    return schedule


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DEVICE_ID: "e08cfe52ae74",
            CONF_PV_POWER: 20,
            CONF_TP_TYPE: 3,
            CONF_MCU_VERSION: 1155,
        },
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def coordinator(hass, entry):
    api = MagicMock()
    api.access_token = "token"
    api.device_id = "e08cfe52ae74"
    api.get_device_info = AsyncMock(return_value={"versionMcu": 1155})
    return ESYSunhomeCoordinator(
        hass=hass,
        api=api,
        device_sn="SN1",
        config_entry=entry,
        protocol=ProtocolDefinition(
            config_id=29, pv_power=20, tp_type=3, mcu_version=1155
        ),
    )


def _sensor(coordinator, schedule, hour, minute=0, fetched_at=FETCHED_AT):
    coordinator.schedule_data = schedule
    coordinator.schedule_fetched_at = fetched_at
    sensor = ScheduleSensor(coordinator)
    now = datetime(2026, 11, 25, hour, minute, tzinfo=SYDNEY)
    return sensor, patch(
        "custom_components.esy_sunhome.sensor.dt_util.now", return_value=now
    )


def _read(coordinator, schedule, hour, minute=0, **kwargs):
    sensor, patcher = _sensor(coordinator, schedule, hour, minute, **kwargs)
    with patcher:
        return sensor.native_value, sensor.extra_state_attributes


def test_identity(coordinator):
    sensor = ScheduleSensor(coordinator)
    assert sensor._attr_translation_key == "schedule"
    assert sensor.unique_id == "e08cfe52ae74_schedule"
    assert sensor.icon == "mdi:calendar-clock"


def test_regular_window_midday(coordinator):
    state, attrs = _read(coordinator, _live_schedule(), 10)
    assert state == "regular"
    assert attrs["regular_periods"] == [{"start": 480, "end": 900, "soc": 20}]
    assert attrs["regular_enabled"] is True
    assert attrs["sell_periods"] == [
        {"start": 900, "end": 1439, "soc": 10},
        {"start": 0, "end": 480, "soc": 10},
    ]
    assert attrs["sell_enabled"] is True
    assert attrs["purchase_periods"] == []
    assert attrs["purchase_enabled"] is False
    assert attrs["next_mode"] == "sell"
    nxt = datetime.fromisoformat(attrs["next_transition"])
    assert nxt.utcoffset() is not None
    assert nxt == datetime(2026, 11, 25, 15, 0, tzinfo=SYDNEY)
    fetched = datetime.fromisoformat(attrs["fetched_at"])
    assert fetched.utcoffset() is not None
    assert fetched == FETCHED_AT
    updated = datetime.fromisoformat(attrs["updated_at"])
    assert updated.utcoffset() is not None
    assert updated == datetime.fromtimestamp(UPDATE_TIME, tz=UTC)


def test_attribute_types_are_plain_json(coordinator):
    _, attrs = _read(coordinator, _live_schedule(), 10)
    for key in ("regular_enabled", "sell_enabled", "purchase_enabled"):
        assert type(attrs[key]) is bool
    for key in ("regular_periods", "sell_periods", "purchase_periods"):
        for p in attrs[key]:
            assert type(p["start"]) is int and type(p["end"]) is int
    for key in ("fetched_at", "updated_at", "next_transition", "next_mode"):
        assert isinstance(attrs[key], str)


def test_sell_window_evening_next_is_regular_tomorrow(coordinator):
    state, attrs = _read(coordinator, _live_schedule(), 16)
    assert state == "sell"
    assert attrs["next_mode"] == "regular"
    assert datetime.fromisoformat(attrs["next_transition"]) == datetime(
        2026, 11, 26, 8, 0, tzinfo=SYDNEY
    )


def test_boundary_minute(coordinator):
    state, _ = _read(coordinator, _live_schedule(), 14, 59)
    assert state == "regular"
    state, _ = _read(coordinator, _live_schedule(), 15, 0)
    assert state == "sell"


def test_regular_switched_off(coordinator):
    state, attrs = _read(coordinator, _live_schedule(releaseSwitch=0), 10)
    assert state == "none"
    assert attrs["regular_enabled"] is False
    assert attrs["regular_periods"] == [{"start": 480, "end": 900, "soc": 20}]
    assert attrs["next_mode"] == "sell"
    assert datetime.fromisoformat(attrs["next_transition"]) == datetime(
        2026, 11, 25, 15, 0, tzinfo=SYDNEY
    )


def test_all_day_regular_has_no_transition(coordinator):
    schedule = _live_schedule(
        releaseTimeQuantum=[{"start": 0, "end": 1439, "soc": 20}],
        dischargeSwitch=0,
    )
    state, attrs = _read(coordinator, schedule, 10)
    assert state == "regular"
    assert "next_transition" not in attrs
    assert "next_mode" not in attrs


def test_nothing_enabled(coordinator):
    schedule = _live_schedule(releaseSwitch=0, dischargeSwitch=0, chargeSwitch=0)
    state, attrs = _read(coordinator, schedule, 10)
    assert state == "none"
    assert "next_transition" not in attrs
    assert "next_mode" not in attrs
    assert attrs["regular_enabled"] is False
    assert attrs["sell_enabled"] is False


def test_purchase_window(coordinator):
    schedule = _live_schedule(
        releaseSwitch=0,
        dischargeSwitch=0,
        chargeSwitch=1,
        chargeTimeQuantum=[{"start": 600, "end": 660, "soc": 90}],
    )
    state, attrs = _read(coordinator, schedule, 10, 30)
    assert state == "purchase"
    assert attrs["purchase_enabled"] is True
    assert attrs["next_mode"] == "none"


def test_before_first_fetch(coordinator):
    coordinator.schedule_data = None
    coordinator.schedule_fetched_at = None
    sensor = ScheduleSensor(coordinator)
    assert sensor.native_value is None
    assert not sensor.extra_state_attributes


@pytest.mark.parametrize("update_time", [None, "yesterday", True, [1], float("nan")])
def test_updated_at_absent_for_bad_update_time(coordinator, update_time):
    schedule = _live_schedule(updateTime=update_time)
    _, attrs = _read(coordinator, schedule, 10)
    assert "updated_at" not in attrs
    assert attrs["regular_enabled"] is True


def test_updated_at_missing_key(coordinator):
    schedule = _live_schedule()
    del schedule["updateTime"]
    _, attrs = _read(coordinator, schedule, 10)
    assert "updated_at" not in attrs


def test_updated_at_milliseconds(coordinator):
    _, attrs = _read(coordinator, _live_schedule(updateTime=UPDATE_TIME * 1000), 10)
    assert datetime.fromisoformat(attrs["updated_at"]) == datetime.fromtimestamp(
        UPDATE_TIME, tz=UTC
    )


def test_null_lists_and_string_switch_degrade(coordinator):
    schedule = {
        "releaseTimeQuantum": None,
        "dischargeTimeQuantum": None,
        "chargeTimeQuantum": None,
        "releaseSwitch": "1",
        "dischargeSwitch": "on",
        "chargeSwitch": None,
    }
    state, attrs = _read(coordinator, schedule, 10)
    assert state == "none"
    for name in ("regular", "sell", "purchase"):
        assert attrs[f"{name}_periods"] == []
        assert attrs[f"{name}_enabled"] is False
    assert "updated_at" not in attrs
    assert "next_transition" not in attrs


def test_fetched_at_follows_coordinator(coordinator):
    later = datetime(2026, 11, 25, 0, 8, 0, tzinfo=UTC)
    _, attrs = _read(coordinator, _live_schedule(), 10, fetched_at=later)
    assert datetime.fromisoformat(attrs["fetched_at"]) == later
    _, attrs = _read(coordinator, _live_schedule(), 10, fetched_at=None)
    assert "fetched_at" not in attrs


def test_never_raises(coordinator):
    sensor, patcher = _sensor(coordinator, _live_schedule(), 10)
    with patcher:
        with patch(
            "custom_components.esy_sunhome.sensor.scheduled_mode",
            side_effect=RuntimeError("boom"),
        ):
            assert sensor.native_value is None
        with patch(
            "custom_components.esy_sunhome.sensor.next_transition",
            side_effect=RuntimeError("boom"),
        ):
            assert sensor.extra_state_attributes is None


def test_non_dict_schedule_never_raises(coordinator):
    state, attrs = _read(coordinator, ["unexpected"], 10)
    assert state == "none"
    assert attrs["regular_periods"] == []


def test_attributes_pass_controller_shape_rules(coordinator):
    """Re-state the consumer's shape rules and check the live schedule against them.

    The rules mirror ``parse_regular_periods`` in the homeassistant-config repo's
    pyscript/esy_export_controller.py: integer (not bool) start/end within
    0..1440, ``start < end``, ``regular_enabled is True`` and an aware ISO
    ``fetched_at``.
    """
    _, attrs = _read(coordinator, _live_schedule(), 10)
    assert attrs["regular_enabled"] is True
    for period in attrs["regular_periods"]:
        for key in ("start", "end"):
            value = period[key]
            assert isinstance(value, int) and not isinstance(value, bool)
            assert 0 <= value <= 1440
        assert period["start"] < period["end"]
    assert datetime.fromisoformat(attrs["fetched_at"]).utcoffset() is not None


def test_naive_fetched_at_is_omitted(coordinator):
    _, attrs = _read(
        coordinator, _live_schedule(), 10, fetched_at=datetime(2026, 11, 25, 0, 5)
    )
    assert "fetched_at" not in attrs


def test_sell_window_early_morning_next_is_regular_same_day(coordinator):
    state, attrs = _read(coordinator, _live_schedule(), 3)
    assert state == "sell"
    assert attrs["next_mode"] == "regular"
    assert datetime.fromisoformat(attrs["next_transition"]) == datetime(
        2026, 11, 25, 8, 0, tzinfo=SYDNEY
    )


def test_sell_window_last_minute_next_is_regular_next_day(coordinator):
    state, attrs = _read(coordinator, _live_schedule(), 23, 59)
    assert state == "sell"
    assert attrs["next_mode"] == "regular"
    assert datetime.fromisoformat(attrs["next_transition"]) == datetime(
        2026, 11, 26, 8, 0, tzinfo=SYDNEY
    )
