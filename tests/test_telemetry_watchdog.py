"""Telemetry watchdog: the integration must notice when live data stops.

2026-11-27 the ESY cloud had an incident at 14:30. The MQTT feed went silent and
the integration never recovered: every sensor kept its last value for 7 hours
(looking live), so a controller kept acting on frozen numbers. These tests pin
the fix: remember when telemetry last arrived, reconnect (re-login, refetch
credentials) when it stops, mark MQTT-fed entities unavailable if it stays
silent, and never connect with generic fallback credentials.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esy_sunhome import coordinator as coordinator_module
from custom_components.esy_sunhome.const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_POLLING,
    CONF_MCU_VERSION,
    CONF_PV_POWER,
    CONF_TP_TYPE,
    DOMAIN,
)
from custom_components.esy_sunhome.coordinator import (
    TELEMETRY_RECONNECT_AFTER_S,
    TELEMETRY_UNAVAILABLE_AFTER_S,
    ESYSunhomeCoordinator,
)
from custom_components.esy_sunhome.esysunhome import ESYSunhomeAPI
from custom_components.esy_sunhome.number import ESYSunhomeSOCNumber
from custom_components.esy_sunhome.protocol_api import ProtocolDefinition
from custom_components.esy_sunhome.sensor import (
    LastTelemetrySensor,
    ScheduleSensor,
    StateOfChargeSensor,
)

T0 = datetime(2026, 11, 27, 14, 28, 50, tzinfo=UTC)


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
    api.reset_auth = MagicMock()
    api.close_session = AsyncMock()
    return ESYSunhomeCoordinator(
        hass=hass,
        api=api,
        device_sn="SN1",
        config_entry=entry,
        protocol=ProtocolDefinition(
            config_id=23, pv_power=20, tp_type=3, mcu_version=1146
        ),
    )


def _feed(coordinator, values=None):
    """Deliver one telemetry message that decodes to ``values``."""
    coordinator.parser.parse_message = MagicMock(
        return_value=values
        if values is not None
        else {"_configId": 23, "batterySoc": 87}
    )
    return coordinator._process_telemetry(b"x")


# --- remembering when telemetry last arrived -------------------------------


def test_fresh_coordinator_has_no_telemetry_time(coordinator):
    assert coordinator.last_telemetry_at is None
    assert coordinator.telemetry_age_s() is None
    assert coordinator.telemetry_stale is False


async def test_telemetry_records_aware_utc_time(coordinator, freezer):
    freezer.move_to(T0)
    await _feed(coordinator)
    assert coordinator.last_telemetry_at == T0
    assert coordinator.last_telemetry_at.utcoffset() == timedelta(0)


async def test_message_without_telemetry_does_not_count(coordinator, freezer):
    """A write acknowledgement is not live data."""
    freezer.move_to(T0)
    await _feed(coordinator, values={})
    assert coordinator.last_telemetry_at is None


async def test_dropped_config_id_mismatch_does_not_count(coordinator, freezer):
    freezer.move_to(T0)
    with patch.object(coordinator, "_config_id_matches", return_value=False):
        await _feed(coordinator)
    assert coordinator.last_telemetry_at is None


# --- age, baseline and staleness -------------------------------------------


async def test_age_counts_from_watch_start_when_nothing_ever_arrived(
    coordinator, freezer
):
    freezer.move_to(T0)
    coordinator.start_telemetry_watch()
    freezer.move_to(T0 + timedelta(seconds=90))
    assert coordinator.telemetry_age_s() == pytest.approx(90)


async def test_age_counts_from_last_telemetry_once_it_arrived(coordinator, freezer):
    freezer.move_to(T0)
    coordinator.start_telemetry_watch()
    freezer.move_to(T0 + timedelta(seconds=60))
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(seconds=100))
    assert coordinator.telemetry_age_s() == pytest.approx(40)


async def test_stale_only_after_the_unavailable_threshold(coordinator, freezer):
    freezer.move_to(T0)
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_UNAVAILABLE_AFTER_S - 1))
    assert coordinator.telemetry_stale is False
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_UNAVAILABLE_AFTER_S + 1))
    assert coordinator.telemetry_stale is True


async def test_polling_disabled_never_stale(hass, entry, coordinator, freezer):
    """With polling off the device is not asked for data, so silence is normal."""
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_POLLING: False})
    freezer.move_to(T0)
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(hours=7))
    assert coordinator.telemetry_stale is False


# --- the watchdog ----------------------------------------------------------


async def test_no_reconnect_before_the_threshold(coordinator, freezer):
    freezer.move_to(T0)
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_RECONNECT_AFTER_S - 1))
    with patch.object(coordinator, "_force_mqtt_reconnect", AsyncMock()) as force:
        await coordinator._check_telemetry_watchdog()
    force.assert_not_awaited()


async def test_reconnects_once_telemetry_is_silent_past_the_threshold(
    coordinator, freezer
):
    freezer.move_to(T0)
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_RECONNECT_AFTER_S + 1))
    with patch.object(coordinator, "_force_mqtt_reconnect", AsyncMock()) as force:
        await coordinator._check_telemetry_watchdog()
    force.assert_awaited_once()


async def test_reconnects_when_nothing_ever_arrived(coordinator, freezer):
    freezer.move_to(T0)
    coordinator.start_telemetry_watch()
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_RECONNECT_AFTER_S + 1))
    with patch.object(coordinator, "_force_mqtt_reconnect", AsyncMock()) as force:
        await coordinator._check_telemetry_watchdog()
    force.assert_awaited_once()


async def test_polling_disabled_means_no_reconnects(hass, entry, coordinator, freezer):
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_POLLING: False})
    freezer.move_to(T0)
    coordinator.start_telemetry_watch()
    freezer.move_to(T0 + timedelta(hours=1))
    with patch.object(coordinator, "_force_mqtt_reconnect", AsyncMock()) as force:
        await coordinator._check_telemetry_watchdog()
    force.assert_not_awaited()


async def test_repeat_attempts_back_off_and_reset_when_data_returns(
    coordinator, freezer
):
    """A cloud that stays down must not be hammered with logins every minute."""
    freezer.move_to(T0)
    await _feed(coordinator)
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_RECONNECT_AFTER_S + 1))
    now = coordinator.last_telemetry_at + timedelta(
        seconds=TELEMETRY_RECONNECT_AFTER_S + 1
    )

    calls = []

    async def fake_force(reason):
        calls.append(reason)

    with patch.object(coordinator, "_force_mqtt_reconnect", fake_force):
        await coordinator._check_telemetry_watchdog()  # attempt 1
        assert len(calls) == 1
        freezer.move_to(now + timedelta(seconds=60))
        await coordinator._check_telemetry_watchdog()  # too soon
        assert len(calls) == 1
        freezer.move_to(now + timedelta(seconds=301))
        await coordinator._check_telemetry_watchdog()  # attempt 2 after 5 min
        assert len(calls) == 2
        freezer.move_to(now + timedelta(seconds=301 + 400))
        await coordinator._check_telemetry_watchdog()  # attempt 3 needs 10 min
        assert len(calls) == 2
        freezer.move_to(now + timedelta(seconds=301 + 601))
        await coordinator._check_telemetry_watchdog()
        assert len(calls) == 3

        # Data comes back: the next outage starts again from the first threshold.
        await _feed(coordinator)
        freezer.move_to(
            coordinator.last_telemetry_at
            + timedelta(seconds=TELEMETRY_RECONNECT_AFTER_S + 1)
        )
        await coordinator._check_telemetry_watchdog()
        assert len(calls) == 4


async def test_force_reconnect_relogs_in_refetches_credentials_and_restarts(
    coordinator, freezer
):
    """The reconnect must drop auth state AND the cached MQTT credentials."""
    started = []

    async def fake_loop():
        started.append(1)
        await asyncio.sleep(3600)

    coordinator._mqtt_credentials = MagicMock()
    coordinator._mqtt_connected = True
    with patch.object(coordinator, "_mqtt_loop", fake_loop):
        coordinator._mqtt_task = asyncio.create_task(fake_loop())
        await asyncio.sleep(0)
        old_task = coordinator._mqtt_task
        await coordinator._force_mqtt_reconnect("test")
        await asyncio.sleep(0)

        coordinator.api.reset_auth.assert_called_once()
        assert coordinator._mqtt_credentials is None
        assert coordinator._mqtt_connected is False
        assert coordinator.watchdog_reconnects == 1
        assert coordinator._mqtt_task is not old_task
        assert old_task.cancelled() or old_task.cancelling()
        assert len(started) == 2
        coordinator._mqtt_task.cancel()


async def test_force_reconnect_does_nothing_after_shutdown(coordinator):
    coordinator._shutdown = True
    await coordinator._force_mqtt_reconnect("test")
    coordinator.api.reset_auth.assert_not_called()


# --- entities --------------------------------------------------------------


async def test_telemetry_entities_unavailable_when_stale_but_rest_ones_stay(
    coordinator, freezer
):
    freezer.move_to(T0)
    await _feed(coordinator)
    soc = StateOfChargeSensor(coordinator=coordinator)
    schedule = ScheduleSensor(coordinator=coordinator)
    cutoff = ESYSunhomeSOCNumber(
        coordinator, "soc_purchase_cutoff", "x", "chargeCutOff", "mdi:x"
    )
    last = LastTelemetrySensor(coordinator=coordinator)
    coordinator.last_update_success = True

    assert soc.available is True
    freezer.move_to(T0 + timedelta(seconds=TELEMETRY_UNAVAILABLE_AFTER_S + 1))
    assert soc.available is False
    assert schedule.available is True  # fed by REST, not MQTT
    assert cutoff.available is True
    assert last.available is True  # it must keep showing how stale the data is


async def test_last_telemetry_sensor_reports_time_and_watchdog_count(
    coordinator, freezer
):
    freezer.move_to(T0)
    sensor = LastTelemetrySensor(coordinator=coordinator)
    assert sensor.native_value is None
    await _feed(coordinator)
    coordinator.watchdog_reconnects = 2
    assert sensor.native_value == T0
    assert sensor.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.extra_state_attributes["watchdog_reconnects"] == 2
    assert sensor.extra_state_attributes["stale"] is False


# --- credentials: no silent fallback to the generic account ----------------


@pytest.fixture
def api():
    return ESYSunhomeAPI("u@example.com", "pw", "dev1")


def test_reset_auth_forgets_all_tokens(api):
    api.access_token = "a"
    api.refresh_token = "r"
    api.token_expiry = datetime.now(UTC)
    api.reset_auth()
    assert api.access_token is None
    assert api.refresh_token is None
    assert api.token_expiry is None


async def test_credentials_fetch_fails_when_device_info_fails(api, tmp_path):
    """Generic 'admin' credentials are rejected by the broker; connecting with
    them on a cloud error only produced 'Not authorized' and hid the real fault."""
    api.get_device_info = AsyncMock(side_effect=RuntimeError("cloud down"))
    api.get_mqtt_certs = AsyncMock(return_value={})
    with pytest.raises(RuntimeError, match="cloud down"):
        await api.get_mqtt_credentials(str(tmp_path))


async def test_credentials_fetch_fails_when_certs_fail(api, tmp_path):
    api.get_device_info = AsyncMock(
        return_value={"mqttUserName": "u", "mqttPassword": "p"}
    )
    api.get_mqtt_certs = AsyncMock(side_effect=RuntimeError("cert endpoint down"))
    with pytest.raises(RuntimeError, match="cert endpoint down"):
        await api.get_mqtt_credentials(str(tmp_path))


async def test_missing_username_field_still_uses_the_default(api, tmp_path):
    """A reply that works but lacks the fields keeps the old default behaviour."""
    api.get_device_info = AsyncMock(return_value={})
    api.get_mqtt_certs = AsyncMock(
        return_value={"mqttDomain": "broker.example", "port": 8883}
    )
    creds = await api.get_mqtt_credentials(str(tmp_path))
    assert creds.broker_url == "broker.example"
    assert creds.username  # the library default, not an exception


async def test_mqtt_loop_never_connects_with_generic_credentials(coordinator):
    """Credential fetch fails -> wait and refetch; do not connect at all."""
    good = MagicMock(use_tls=True, broker_url="b", port=8883, client_cert_path="c")
    fetches = []

    async def fake_fetch(_cert_dir):
        fetches.append(1)
        if len(fetches) < 3:
            raise RuntimeError("cloud down")
        return good

    connected_with = []

    async def fake_connect():
        connected_with.append(coordinator._mqtt_credentials)
        coordinator._shutdown = True

    coordinator.api.get_mqtt_credentials = fake_fetch
    with (
        patch.object(coordinator, "_connect_mqtt", fake_connect),
        patch.object(coordinator_module.asyncio, "sleep", AsyncMock()),
    ):
        await coordinator._mqtt_loop()

    assert len(fetches) == 3  # retried every round, not after three failed connects
    assert connected_with == [good]  # the only connection used real credentials


async def test_mqtt_connected_flag_cleared_when_the_stream_ends(coordinator):
    """A stream that ends normally (no exception) must not leave 'connected' stuck."""

    class _Messages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    client = MagicMock()
    client.subscribe = AsyncMock()
    client.publish = AsyncMock()
    client.messages = _Messages()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    coordinator._mqtt_credentials = MagicMock(
        use_tls=False,
        broker_url="b",
        port=1883,
        username="u",
        password="p",
        client_cert_path=None,
    )
    with patch.object(coordinator_module.aiomqtt, "Client", return_value=cm):
        await coordinator._connect_mqtt()
    assert coordinator._mqtt_connected is False
