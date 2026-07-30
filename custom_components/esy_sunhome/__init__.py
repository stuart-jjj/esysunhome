"""ESY Sunhome Integration - Dynamic Protocol Version."""

import json
import logging
import os
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DEVICE_ID,
    CONF_DEVICE_SN,
    CONF_PV_POWER,
    CONF_TP_TYPE,
    CONF_MCU_VERSION,
    DEFAULT_PV_POWER,
    DEFAULT_TP_TYPE,
    DEFAULT_MCU_VERSION,
    FC_READ_HOLDING,
    DATA_TYPE_SIGNED,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the ESY Sunhome component.
    
    This is called for YAML configuration (which we don't support).
    We only support config entry setup via the UI.
    """
    return True


def _import_aiomqtt():
    """Import aiomqtt in executor thread to avoid blocking warnings."""
    import aiomqtt  # noqa: F401
    return True


def _write_protocol_dump(
    config_dir: str,
    device_sn: str,
    params: dict,
    protocol_list,
    segment_list,
    protocol,
) -> str:
    """Write the raw + parsed protocol definition to a JSON file for offline
    analysis/troubleshooting.

    `protocol_list`/`segment_list` are the raw dicts ESY's server returned
    (before parsing into RegisterDefinition/SegmentDefinition, which only
    keep the fields our own code cares about) -- this is the only place the
    complete server payload is captured. Blocking file I/O; must be called
    via hass.async_add_executor_job, never directly from the event loop.
    """
    dump_dir = os.path.join(config_dir, ".storage", "esy_sunhome_protocol_dump")
    os.makedirs(dump_dir, exist_ok=True)
    path = os.path.join(
        dump_dir,
        f"protocol_{device_sn}_{params.get('pvPower')}_{params.get('tpType')}_"
        f"{params.get('mcuVersion')}.json",
    )
    payload = {
        "dumped_at": datetime.now(timezone.utc).isoformat(),
        "device_sn": device_sn,
        "params": params,
        "raw_protocol_list": protocol_list,
        "raw_segment_list": segment_list,
        "parsed_summary": (
            {
                "config_id": protocol.config_id,
                "num_input_registers": len(protocol.input_registers),
                "num_holding_registers": len(protocol.holding_registers),
                "num_segments": len(protocol.segments),
            }
            if protocol
            else None
        ),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True, default=str)
    return path


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry to new version.
    
    This handles migration from older config entry versions to the current version.
    """
    try:
        current_version = config_entry.version
        _LOGGER.info("Checking migration for config entry version %s (target: 2)", current_version)

        # Handle missing or 0 version (very old entries)
        if current_version is None or current_version == 0:
            current_version = 1
            _LOGGER.info("Config entry has no version, treating as version 1")

        if current_version > 2:
            # Future version - can't downgrade, but try to continue anyway
            _LOGGER.warning("Config entry has future version %s, attempting to use anyway", current_version)
            return True

        if current_version < 2:
            # Migration to v2: add protocol parameters
            _LOGGER.info("Migrating config entry from version %s to version 2", current_version)
            new_data = {**config_entry.data}
            
            # Add default protocol parameters if missing
            if CONF_PV_POWER not in new_data:
                new_data[CONF_PV_POWER] = DEFAULT_PV_POWER
            if CONF_TP_TYPE not in new_data:
                new_data[CONF_TP_TYPE] = DEFAULT_TP_TYPE
            if CONF_MCU_VERSION not in new_data:
                new_data[CONF_MCU_VERSION] = DEFAULT_MCU_VERSION
            
            hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
            _LOGGER.info("Migration to version 2 successful")
        else:
            _LOGGER.info("Config entry already at version %s, no migration needed", current_version)

        return True
        
    except Exception as e:
        _LOGGER.error("Migration failed with error: %s", e)
        # Return True anyway to allow loading - better to try than fail
        return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ESY Sunhome from a config entry."""
    _LOGGER.info("Setting up ESY Sunhome integration")
    
    # Pre-import aiomqtt in executor to avoid blocking call warnings
    await hass.async_add_executor_job(_import_aiomqtt)
    
    # Now import our modules (coordinator imports aiomqtt, but it's already cached)
    from .esysunhome import ESYSunhomeAPI
    from .protocol_api import get_protocol_api
    from .coordinator import ESYSunhomeCoordinator
    
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    device_id = entry.data.get(CONF_DEVICE_ID, "")
    device_sn = entry.data.get(CONF_DEVICE_SN, device_id)
    
    # Protocol parameters. Start from stored values / defaults, but the device
    # LIST record has pvPower=null and the config flow read versionMcu under the
    # wrong key, which can select a single-phase map for a three-phase unit
    # (telemetry then decodes at the wrong register addresses → garbage). The
    # authoritative source is /api/lsydevice/info, so re-detect from there at
    # startup (this is what the solaniq optimizer does to decode correctly).
    pv_power = entry.data.get(CONF_PV_POWER, DEFAULT_PV_POWER)
    tp_type = entry.data.get(CONF_TP_TYPE, DEFAULT_TP_TYPE)
    mcu_version = entry.data.get(CONF_MCU_VERSION, DEFAULT_MCU_VERSION)

    # Create API instance
    api = ESYSunhomeAPI(username, password, device_id)

    protocol = None
    try:
        # Authenticate
        await api.get_bearer_token()
        _LOGGER.info("Successfully authenticated with ESY API")

        # Re-detect protocol parameters from the authoritative device-info
        # endpoint (pvPower / tpType / versionMcu). Falls back to stored/default
        # values if the call fails so setup still proceeds.
        try:
            device_info = await api.get_device_info()
            det_pv = device_info.get("pvPower")
            det_tp = device_info.get("tpType")
            det_mcu = device_info.get("versionMcu") or device_info.get("mcuVersion")
            if det_pv:
                pv_power = int(det_pv)
            if det_tp:
                tp_type = int(det_tp)
            if det_mcu:
                mcu_version = int(det_mcu)
            _LOGGER.info(
                "Detected protocol params from device info: pvPower=%d, tpType=%d, mcuVersion=%d",
                pv_power, tp_type, mcu_version,
            )
            # Persist corrected params so they survive restarts.
            if (
                entry.data.get(CONF_PV_POWER) != pv_power
                or entry.data.get(CONF_TP_TYPE) != tp_type
                or entry.data.get(CONF_MCU_VERSION) != mcu_version
            ):
                hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PV_POWER: pv_power,
                        CONF_TP_TYPE: tp_type,
                        CONF_MCU_VERSION: mcu_version,
                    },
                )
                _LOGGER.info("Updated stored protocol params from device info")
        except Exception as e:
            _LOGGER.warning(
                "Could not read device info for protocol params (%s); using "
                "pvPower=%d tpType=%d mcuVersion=%d", e, pv_power, tp_type, mcu_version,
            )

        # Load protocol definition from API
        protocol_api = get_protocol_api(api.access_token)
        protocol = await protocol_api.get_protocol_definition(
            pv_power=pv_power,
            tp_type=tp_type,
            mcu_version=mcu_version,
        )

        if protocol:
            _LOGGER.info("Loaded protocol: %d input regs, %d holding regs, %d segments",
                        len(protocol.input_registers),
                        len(protocol.holding_registers),
                        len(protocol.segments))
        else:
            _LOGGER.warning("Failed to load protocol, using fallback")

        # Best-effort dump of the raw + parsed protocol definition to disk
        # (.storage/esy_sunhome_protocol_dump/) for offline analysis --
        # never fails setup, since this is purely a troubleshooting aid.
        try:
            dump_path = await hass.async_add_executor_job(
                _write_protocol_dump,
                hass.config.config_dir,
                device_sn,
                protocol_api.last_fetch_params
                or {"pvPower": pv_power, "tpType": tp_type, "mcuVersion": mcu_version},
                protocol_api.last_raw_protocol_list,
                protocol_api.last_raw_segment_list,
                protocol,
            )
            _LOGGER.info("Dumped protocol definition to %s", dump_path)
        except Exception as e:
            _LOGGER.warning("Failed to dump protocol definition: %s", e)

    except Exception as e:
        _LOGGER.error("Failed to set up ESY Sunhome: %s", e)
        raise
    
    # Create coordinator with protocol
    coordinator = ESYSunhomeCoordinator(
        hass=hass,
        api=api,
        device_sn=device_sn,
        config_entry=entry,
        protocol=protocol,
    )
    
    # Start coordinator
    await coordinator.async_config_entry_first_refresh()
    
    # Store coordinator
    entry.runtime_data = coordinator
    
    # Register debug dump service
    async def async_dump_debug(call):
        """Service to dump debug info to logs."""
        _LOGGER.info("=" * 60)
        _LOGGER.info("ESY SUNHOME DEBUG DUMP")
        _LOGGER.info("=" * 60)
        
        # Config info
        _LOGGER.info("Config: device_sn=%s, pv_power=%s, tp_type=%s, mcu_version=%s",
                    device_sn, pv_power, tp_type, mcu_version)
        
        # MQTT status
        _LOGGER.info("MQTT: connected=%s, last_message=%s",
                    coordinator._mqtt_connected,
                    coordinator._last_mqtt_time)
        _LOGGER.info("Topics: UP=%s, DOWN=%s",
                    coordinator._topic_up, 
                    getattr(coordinator, '_topic_down', 'N/A'))
        
        # Protocol info
        if coordinator.protocol:
            _LOGGER.info("Protocol: %d registers defined",
                        len(getattr(coordinator.protocol, '_registers', [])))
        
        # Raw values
        raw = getattr(coordinator, '_last_raw_values', {})
        _LOGGER.info("Raw values (%d keys):", len(raw))
        for key, value in sorted(raw.items()):
            _LOGGER.info("  %s = %s", key, value)
        
        # Parsed data
        if coordinator.data and hasattr(coordinator.data, 'data'):
            parsed = coordinator.data.data
            _LOGGER.info("Parsed values (%d keys):", len(parsed))
            for key, value in sorted(parsed.items()):
                if not key.startswith('_'):  # Skip internal keys
                    _LOGGER.info("  %s = %s", key, value)
        
        _LOGGER.info("=" * 60)
        _LOGGER.info("END DEBUG DUMP")
        _LOGGER.info("=" * 60)
    
    hass.services.async_register(DOMAIN, "dump_debug", async_dump_debug)

    # Register raw-register write service, for testing a register's real
    # effect before deciding whether it's worth wiring up as a proper
    # control entity (e.g. antiBackflowPower -- see CLAUDE.md/project notes
    # on the antiBackflowPowerPercentage key mismatch). Deliberately not a
    # bypass: refuses to write unless the register exists in this device's
    # live protocol map and is flagged settable (canSet) there, same gate
    # number.py's CONTROLS entries go through.
    async def async_write_raw_register(call) -> None:
        """Service to write a raw value directly to a holding register."""
        address = call.data["address"]
        value = call.data["value"]

        reg = (
            coordinator.protocol.get_register(address, FC_READ_HOLDING)
            if coordinator.protocol else None
        )
        if reg is None:
            _LOGGER.error(
                "write_raw_register: refusing to write -- holding register "
                "%d not found in this device's live protocol map", address,
            )
            return
        if not reg.can_set:
            _LOGGER.error(
                "write_raw_register: refusing to write -- register %d "
                "(dataKey=%s) is not flagged settable (canSet) on this "
                "device", address, reg.data_key,
            )
            return

        # The wire format is always a 16-bit unsigned word (protocol.py packs
        # it with struct.pack(">H", ...)); signed registers just reinterpret
        # that word as two's complement on the read side (raw_unsigned - 65536
        # when > 32767). Mirror that here instead of passing the user's value
        # straight through -- otherwise a negative value or one outside 16
        # bits raises struct.error deep inside publish_command instead of
        # giving the caller a clear reason.
        value = int(value)
        if reg.data_type == DATA_TYPE_SIGNED:
            if not -32768 <= value <= 32767:
                _LOGGER.error(
                    "write_raw_register: refusing to write -- value %d out "
                    "of range for signed register %d (dataKey=%s); must be "
                    "-32768..32767", value, address, reg.data_key,
                )
                return
        elif not 0 <= value <= 65535:
            _LOGGER.error(
                "write_raw_register: refusing to write -- value %d out of "
                "range for unsigned register %d (dataKey=%s); must be "
                "0..65535", value, address, reg.data_key,
            )
            return
        raw_value = value & 0xFFFF

        _LOGGER.info(
            "write_raw_register: writing raw value %s to addr=%d "
            "(dataKey=%s, coefficient=%s, unit=%s)",
            value, address, reg.data_key, reg.coefficient, reg.unit,
        )
        ok = await coordinator.write_register(address, raw_value)
        _LOGGER.info(
            "write_raw_register: %s",
            "command sent" if ok else "FAILED to send (MQTT not connected?)",
        )

    hass.services.async_register(DOMAIN, "write_raw_register", async_write_raw_register)

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    _LOGGER.info("ESY Sunhome integration setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading ESY Sunhome integration")
    
    # Unregister services
    hass.services.async_remove(DOMAIN, "dump_debug")
    
    # Stop coordinator
    coordinator = entry.runtime_data
    if coordinator:
        await coordinator.async_shutdown()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
