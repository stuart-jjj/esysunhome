"""Runtime configId resync after an ESY firmware update.

Every MQTT message header carries the device's own configId. If it differs
from the loaded ProtocolDefinition's config_id, every register address means
something else: reads decode garbage and writes land on the wrong register.
That happened on 2026-10-30 (firmware 1146 -> 1155, configId 23 -> 29) and
stayed broken until the next Home Assistant restart, because mcuVersion was
only re-detected in async_setup_entry. The coordinator now detects the
mismatch on the wire and re-fetches the register map itself.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esy_sunhome.const import (
    CONF_MCU_VERSION,
    CONF_PV_POWER,
    CONF_TP_TYPE,
    DOMAIN,
)
from custom_components.esy_sunhome.coordinator import ESYSunhomeCoordinator
from custom_components.esy_sunhome.protocol_api import ProtocolDefinition


def _protocol(config_id: int, mcu: int) -> ProtocolDefinition:
    return ProtocolDefinition(config_id=config_id, pv_power=20, tp_type=3, mcu_version=mcu)


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PV_POWER: 20, CONF_TP_TYPE: 3, CONF_MCU_VERSION: 1146},
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def coordinator(hass, entry):
    api = MagicMock()
    api.access_token = "token"
    api.get_device_info = AsyncMock(return_value={"versionMcu": 1155})
    c = ESYSunhomeCoordinator(
        hass=hass, api=api, device_sn="SN1", config_entry=entry,
        protocol=_protocol(23, 1146),
    )
    c.publish_command = AsyncMock(return_value=True)
    return c


def _feed(coordinator, values):
    coordinator.parser.parse_message = MagicMock(return_value=dict(values))
    return coordinator._process_telemetry(b"x")


async def test_matching_config_id_updates_data(coordinator):
    with patch.object(coordinator, "_async_resync_protocol", AsyncMock()) as resync:
        await _feed(coordinator, {"_configId": 23, "batterySoc": 80})
    assert coordinator._last_data["batterySoc"] == 80
    assert coordinator.config_id_mismatch is None
    resync.assert_not_called()


async def test_mismatch_drops_message_and_starts_resync(hass, coordinator):
    coordinator._last_data = {"batterySoc": 80}
    with patch.object(coordinator, "_async_resync_protocol", AsyncMock()) as resync:
        await _feed(coordinator, {"_configId": 29, "batterySoc": 0})
        await hass.async_block_till_done()
    # Garbage decoded under the wrong map must not reach the cache.
    assert coordinator._last_data == {"batterySoc": 80}
    assert coordinator.config_id_mismatch == 29
    resync.assert_awaited_once_with(29)


async def test_resync_saves_mcu_and_reloads_entry(hass, coordinator, entry):
    # Entities (e.g. number.py power controls) cache their register
    # definition at setup, so a matching map is applied by reloading the
    # entry, not by swapping the coordinator's map in place.
    coordinator.config_id_mismatch = 29
    protocol_api = MagicMock()
    protocol_api.get_protocol_definition = AsyncMock(return_value=_protocol(29, 1155))
    with patch(
        "custom_components.esy_sunhome.coordinator.get_protocol_api",
        return_value=protocol_api,
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator._async_resync_protocol(29)

    protocol_api.get_protocol_definition.assert_awaited_once_with(
        pv_power=20, tp_type=3, mcu_version=1155, force_refresh=True,
    )
    assert entry.data[CONF_MCU_VERSION] == 1155
    reload.assert_called_once_with(entry.entry_id)
    # Writes stay blocked until the reloaded entry is running.
    assert coordinator.protocol.config_id == 23
    assert coordinator.config_id_mismatch == 29


async def test_resync_discards_superseded_result(hass, coordinator, entry):
    protocol_api = MagicMock()

    async def fetch(**kwargs):
        # The device goes back to the loaded configId while the REST
        # request is in flight.
        await _feed(coordinator, {"_configId": 23})
        return _protocol(29, 1155)

    protocol_api.get_protocol_definition = fetch
    coordinator.config_id_mismatch = 29
    with patch(
        "custom_components.esy_sunhome.coordinator.get_protocol_api",
        return_value=protocol_api,
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator._async_resync_protocol(29)

    reload.assert_not_called()
    assert entry.data[CONF_MCU_VERSION] == 1146
    # A later mismatch may start a fresh attempt straight away.
    assert coordinator._last_resync_attempt is None


async def test_shutdown_cancels_running_resync(hass, coordinator):
    coordinator.api.close_session = AsyncMock()
    started = asyncio.Event()

    async def slow_resync(_wire):
        started.set()
        await asyncio.sleep(3600)

    with patch.object(coordinator, "_async_resync_protocol", slow_resync):
        await _feed(coordinator, {"_configId": 29})
        await started.wait()
        task = coordinator._resync_task
        await coordinator.async_shutdown()
    assert task.cancelled()


async def test_resync_without_match_keeps_protocol_and_waits(hass, coordinator, entry):
    coordinator.config_id_mismatch = 29
    protocol_api = MagicMock()
    protocol_api.get_protocol_definition = AsyncMock(return_value=_protocol(15, 1155))
    with patch(
        "custom_components.esy_sunhome.coordinator.get_protocol_api",
        return_value=protocol_api,
    ):
        await coordinator._async_resync_protocol(29)

    assert coordinator.protocol.config_id == 23
    assert coordinator.config_id_mismatch == 29
    assert entry.data[CONF_MCU_VERSION] == 1146

    # A further mismatched message inside the retry window must not hammer
    # ESY's API with another fetch.
    with patch.object(coordinator, "_async_resync_protocol", AsyncMock()) as resync:
        await _feed(coordinator, {"_configId": 29})
        await hass.async_block_till_done()
    resync.assert_not_called()


async def test_resync_survives_api_error(coordinator, entry):
    coordinator.config_id_mismatch = 29
    coordinator.api.get_device_info = AsyncMock(side_effect=Exception("ESY API down"))
    await coordinator._async_resync_protocol(29)
    assert coordinator.protocol.config_id == 23
    assert coordinator.config_id_mismatch == 29
    assert entry.data[CONF_MCU_VERSION] == 1146


async def test_writes_refused_while_mismatched(coordinator):
    coordinator.config_id_mismatch = 29
    assert await coordinator.write_register(72, 3) is False
    assert await coordinator.write_registers([(72, 3)]) is False
    assert await coordinator.set_mode_mqtt(3) is False
    coordinator.publish_command.assert_not_called()


async def test_mismatch_clears_when_wire_matches_again(coordinator):
    coordinator.config_id_mismatch = 29
    await _feed(coordinator, {"_configId": 23, "batterySoc": 70})
    assert coordinator.config_id_mismatch is None
    assert coordinator._last_data["batterySoc"] == 70
