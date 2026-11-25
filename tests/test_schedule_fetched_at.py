"""The coordinator records when the BEM schedule was last fetched (ADO 351).

The schedule sensor publishes ``fetched_at`` so a consumer can tell stale data
from fresh. A failed fetch must therefore leave BOTH the cached schedule and its
timestamp untouched: the timestamp only ever means "the last good fetch".
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
from custom_components.esy_sunhome.number import ESYSunhomeSOCNumber
from custom_components.esy_sunhome.protocol_api import ProtocolDefinition

SCHEDULE = {"chargeCutOff": 90, "dischargeCutOff": 20, "releaseCutOff": 10}


def _assert_recent_utc(value):
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - value) < timedelta(seconds=5)


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PV_POWER: 20,
            CONF_TP_TYPE: 3,
            CONF_MCU_VERSION: 1146,
            CONF_DEVICE_ID: "dev1",
        },
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def coordinator(hass, entry):
    api = MagicMock()
    api.access_token = "token"
    api.device_id = "dev1"
    api.get_device_info = AsyncMock(return_value={"code": 1})
    api.get_schedule = AsyncMock(side_effect=lambda: dict(SCHEDULE))
    api.save_schedule = AsyncMock()
    return ESYSunhomeCoordinator(
        hass=hass,
        api=api,
        device_sn="SN1",
        config_entry=entry,
        protocol=ProtocolDefinition(
            config_id=23, pv_power=20, tp_type=3, mcu_version=1146
        ),
    )


def test_fresh_coordinator_has_no_fetch_time(coordinator):
    assert coordinator.schedule_data is None
    assert coordinator.schedule_fetched_at is None


async def test_successful_fetch_records_aware_utc_time(coordinator):
    await coordinator._check_bem_state()
    assert coordinator.schedule_data == SCHEDULE
    _assert_recent_utc(coordinator.schedule_fetched_at)


async def test_failed_refetch_keeps_last_good_data_and_time(coordinator):
    await coordinator._check_bem_state()
    data = coordinator.schedule_data
    fetched_at = coordinator.schedule_fetched_at

    coordinator.api.get_schedule.side_effect = RuntimeError("cloud down")
    await coordinator._check_bem_state()

    assert coordinator.schedule_data is data
    assert coordinator.schedule_fetched_at == fetched_at


async def test_first_fetch_failing_leaves_both_none(coordinator):
    coordinator.api.get_schedule.side_effect = RuntimeError("cloud down")
    await coordinator._check_bem_state()
    assert coordinator.schedule_data is None
    assert coordinator.schedule_fetched_at is None


async def test_soc_number_save_records_fetch_time(coordinator):
    entity = ESYSunhomeSOCNumber(
        coordinator,
        "soc_purchase_cutoff",
        "SOC Purchase Cutoff",
        "chargeCutOff",
        "mdi:x",
    )
    with patch.object(entity, "async_write_ha_state"):
        await entity.async_set_native_value(40)
    coordinator.api.save_schedule.assert_awaited_once()
    assert coordinator.schedule_data["chargeCutOff"] == 40
    _assert_recent_utc(coordinator.schedule_fetched_at)


async def test_soc_number_failed_save_keeps_data_and_time(coordinator):
    await coordinator._check_bem_state()
    data = coordinator.schedule_data
    fetched_at = coordinator.schedule_fetched_at
    coordinator.api.save_schedule.side_effect = RuntimeError("save failed")
    entity = ESYSunhomeSOCNumber(
        coordinator,
        "soc_purchase_cutoff",
        "SOC Purchase Cutoff",
        "chargeCutOff",
        "mdi:x",
    )
    with patch.object(entity, "async_write_ha_state"), pytest.raises(RuntimeError):
        await entity.async_set_native_value(40)
    assert coordinator.schedule_data is data
    assert coordinator.schedule_fetched_at == fetched_at


async def test_soc_number_save_drops_stale_update_time(coordinator):
    coordinator.api.get_schedule.side_effect = lambda: {
        **SCHEDULE,
        "updateTime": 1795405320,
    }
    entity = ESYSunhomeSOCNumber(
        coordinator,
        "soc_purchase_cutoff",
        "SOC Purchase Cutoff",
        "chargeCutOff",
        "mdi:x",
    )
    with patch.object(entity, "async_write_ha_state"):
        await entity.async_set_native_value(40)
    assert "updateTime" not in coordinator.schedule_data
