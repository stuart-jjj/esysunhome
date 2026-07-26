"""ESY Sunhome number entities.

Two groups:
  * Power-control registers (charge/discharge power, export %, SOC limits) —
    settable inverter registers, written over the MQTT register-write path,
    with addresses resolved per-model from the dynamic register map.
  * BEM SOC cutoffs (purchase/sale/use) — part of the server-side BEM schedule,
    written via the schedule REST API.

Power-control entities are only created for registers present on the device's
model.

Charge/discharge are exposed as a *percentage* slider for a friendly UX, but —
matching the ESY mobile app's normal user control — they are written as
*watts* to the ``batteryChargePower`` / ``batteryDischargePower`` registers
(the app sends raw watts to these; it is the installer pages that use the
current/percent registers). The percentage is converted to watts against the
unit's rated AC power (5kW per phase). The export limit is kept as a
percentage (it maps to a grid feed-in cap, e.g. 5kW) and written to
``antiBackflowPowerPercentage``.

``antiBackflowPowerPercentage`` (Export Power Limit) and
``maxOutputPowerPercent`` (Max Output Power) are themselves percent-native
registers on the wire (unlike batteryChargePower/DischargePower, which are
genuinely watt-native) — there is no separate raw-watts register for these.
The watts-input variants below write to the *same* registers, converting
watts <-> percent against the unit's rated AC power, purely as a convenience
UI for users who'd rather enter an absolute watt figure than a percentage.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EsySunhomeEntity
from .const import ESY_PER_PHASE_RATED_W, ATTR_SYSTEM_RUN_MODE
from .protocol_api import FC_READ_HOLDING

if TYPE_CHECKING:
    from .coordinator import ESYSunhomeCoordinator

_LOGGER = logging.getLogger(__name__)

# systemRunMode value for Electricity Sell / Export mode. Confirmed on real
# hardware that antiBackflowPowerPercentage (Export Power Limit) latches on
# mode entry and ignores live writes while selling — hence unavailable in
# Sell mode by default. maxOutputPowerPercent is NOT confirmed to behave the
# same way (it's reported to function as a live power setpoint while
# selling); PowerControlDescriptor.available_in_sell lets specific controls
# opt out of this blanket gating so that can be tested/relied on per-register
# rather than assumed for all of them.
SELL_MODE_CODE = 3

# How long to wait for a power-control write to be confirmed by telemetry
# before retrying, and how many retries to attempt before giving up.
NUMBER_CONFIRM_TIMEOUT = 20  # seconds
NUMBER_MAX_RETRIES = 2  # total attempts = 1 + NUMBER_MAX_RETRIES

# How a control's slider value maps onto the register write:
#   "raw"           — write the value directly (scaled by the register coeff).
#   "watt_from_pct" — slider is a 0-100% of rated power; convert to watts
#                     (pct/100 * rated_w) before writing. native_value reads the
#                     watt register back and converts to a percentage.
#   "pct_from_watt" — register is a 0-100% value; input is watts of rated
#                     power, converted to a percentage before writing.
#                     native_value reads the percent register back and
#                     converts to watts.
WRITE_RAW = "raw"
WRITE_WATT_FROM_PCT = "watt_from_pct"
WRITE_PCT_FROM_WATT = "pct_from_watt"


# ---------------------------------------------------------------------------
# Power-control registers (MQTT register writes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PowerControlDescriptor:
    """Describes a settable power-control register exposed as a number."""

    data_key: str          # register dataKey to resolve + write
    translation_key: str   # unique-id suffix
    name: str
    unit: str
    min_value: float
    max_value: float
    step: float
    icon: str
    write_mode: str = WRITE_RAW
    slider: bool = True
    dynamic_max: bool = False  # max_value/step computed from rated watts at runtime
    available_in_sell: bool = False  # opt out of the blanket Sell-mode unavailability


# Candidate controls. Only those whose register exists on the device's model
# (per the fetched register map) are actually created.
CONTROLS: list[PowerControlDescriptor] = [
    # Charge/discharge: % slider UI, written as watts to the app's normal-user
    # power registers (batteryChargePower / batteryDischargePower).
    PowerControlDescriptor(
        "batteryChargePower", "battery_charge_power_pct",
        "Battery Charge Power", "%", 0, 100, 1, "mdi:battery-arrow-up",
        write_mode=WRITE_WATT_FROM_PCT,
    ),
    PowerControlDescriptor(
        "batteryDischargePower", "battery_discharge_power_pct",
        "Battery Discharge Power", "%", 0, 100, 1, "mdi:battery-arrow-down",
        write_mode=WRITE_WATT_FROM_PCT,
    ),
    # Export limit: kept as a percentage (grid feed-in cap, e.g. 5kW @ ~84%).
    PowerControlDescriptor(
        "antiBackflowPowerPercentage", "export_limit_percent",
        "Export Power Limit", "%", 0, 100, 1, "mdi:transmission-tower-export",
    ),
    PowerControlDescriptor(
        "maxOutputPowerPercent", "max_output_power_percent",
        "Max Output Power", "%", 0, 100, 1, "mdi:flash",
        available_in_sell=True,
    ),
    # Watts-input variants of the two percent registers above — same
    # registers, entered/displayed in watts instead of percent.
    PowerControlDescriptor(
        "antiBackflowPowerPercentage", "export_limit_watts",
        "Export Power Limit (W)", "W", 0, 100, 100, "mdi:transmission-tower-export",
        write_mode=WRITE_PCT_FROM_WATT, slider=False, dynamic_max=True,
    ),
    PowerControlDescriptor(
        "maxOutputPowerPercent", "max_output_power_watts",
        "Max Output Power (W)", "W", 0, 100, 100, "mdi:flash",
        write_mode=WRITE_PCT_FROM_WATT, slider=False, dynamic_max=True,
        available_in_sell=True,
    ),
    PowerControlDescriptor(
        "onGridSocLimit", "on_grid_soc_limit",
        "On-Grid SOC Limit", "%", 0, 100, 1, "mdi:battery-charging-50",
    ),
    PowerControlDescriptor(
        "offGridSocLimit", "off_grid_soc_limit",
        "Off-Grid SOC Limit", "%", 0, 100, 1, "mdi:battery-charging-10",
    ),
]


# ---------------------------------------------------------------------------
# BEM SOC cutoffs (schedule REST API) — from the phmarc fork
# (translation_key, name, schedule field, icon)
# ---------------------------------------------------------------------------
SOC_CUTOFFS = [
    ("soc_purchase_cutoff", "SOC Purchase Cutoff", "chargeCutOff", "mdi:battery-charging-60"),
    ("soc_sale_cutoff", "SOC Sale Cutoff", "dischargeCutOff", "mdi:battery-minus-outline"),
    ("soc_use_cutoff", "SOC Use Cutoff", "releaseCutOff", "mdi:battery-outline"),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up power-control and BEM SOC-cutoff number entities."""
    coordinator = entry.runtime_data
    protocol = coordinator.protocol

    entities: list[NumberEntity] = []

    # Power-control registers — only those present + settable on this model.
    for desc in CONTROLS:
        reg = (
            protocol.get_register_by_key(desc.data_key, FC_READ_HOLDING)
            if protocol else None
        )
        if reg is None:
            _LOGGER.debug(
                "Skipping number %s: register not in this model's map", desc.data_key
            )
            continue
        if not getattr(reg, "can_set", False):
            _LOGGER.debug("Skipping number %s: register not settable", desc.data_key)
            continue
        entities.append(ESYPowerControlNumber(coordinator, desc, reg))

    # BEM SOC cutoffs (schedule API).
    entities.extend(
        ESYSunhomeSOCNumber(coordinator, key, name, field, icon)
        for key, name, field, icon in SOC_CUTOFFS
    )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d number entities", len(entities))


class ESYPowerControlNumber(EsySunhomeEntity, NumberEntity):
    """A settable power-control register exposed as a number entity."""

    def __init__(self, coordinator, desc: PowerControlDescriptor, reg) -> None:
        # translation_key must be set before super().__init__ (used for unique_id)
        self._attr_translation_key = desc.translation_key
        super().__init__(coordinator)
        self._desc = desc
        self._reg = reg
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_native_min_value = desc.min_value
        self._attr_native_max_value = desc.max_value
        self._attr_native_step = desc.step
        self._attr_icon = desc.icon
        self._attr_mode = NumberMode.SLIDER if desc.slider else NumberMode.BOX
        self._optimistic: Optional[float] = None
        self._pending_value: Optional[float] = None
        self._pending_raw: Optional[int] = None
        self._pending_detail: Optional[str] = None
        self._retry_count = 0
        self._confirm_timeout_handle = None
        if desc.dynamic_max:
            # Watts-based range depends on the unit's rated power (5kW per
            # phase), only known once the coordinator's phase_count is set.
            self._attr_native_max_value = self._rated_watts

    @property
    def available(self) -> bool:
        """Unavailable while in Sell/Export mode, unless the descriptor opts out.

        Confirmed on real hardware for antiBackflowPowerPercentage (Export
        Power Limit): the inverter only applies it when the mode is
        (re-)entered, so live changes during Sell mode silently do nothing.
        Greying the control out signals that and points to the
        set-then-switch workflow. Controls with available_in_sell=True (e.g.
        Max Output Power, reported to function as a live setpoint while
        selling) skip this gating.
        """
        if not super().available:
            return False
        if self._desc.available_in_sell:
            return True
        try:
            mode = self.coordinator.data.get(ATTR_SYSTEM_RUN_MODE)
        except Exception:  # noqa: BLE001 - coordinator data may be missing early
            mode = None
        return mode != SELL_MODE_CODE

    @property
    def _rated_watts(self) -> float:
        """Rated AC power (W) used as the 100% basis for % power controls.

        Prefers the device's own reported `outputRatedPower` register over
        the ESY_PER_PHASE_RATED_W * phase_count guess. Confirmed live on
        hardware that these can disagree substantially: the guess gives
        15000W for a 3-phase unit, but this unit's own outputRatedPower
        reports 10000W — a 1.5x overestimate that silently inflated every
        %<->W conversion, making the watts-input entities display/target
        setpoints the device could never actually reach (e.g. commanding
        86% showed as "12900W" when the device's real ceiling at 86% of its
        true rating is ~8600W, matching the observed plateau almost
        exactly). Falls back to the guess only if telemetry hasn't reported
        outputRatedPower yet (e.g. very early before first poll).
        """
        try:
            reported = self.coordinator.data.get("outputRatedPower")
        except Exception:  # noqa: BLE001 - coordinator data may be missing early
            reported = None
        if reported:
            return float(reported)
        phases = getattr(self.coordinator, "phase_count", 1) or 1
        return ESY_PER_PHASE_RATED_W * phases

    def _telemetry_value(self) -> Optional[float]:
        """Convert the current raw telemetry reading to display units, if present."""
        try:
            val = self.coordinator.data.get(self._desc.data_key)
        except Exception:  # noqa: BLE001 - coordinator data may be missing early
            val = None
        if val is None:
            return None
        if self._desc.write_mode == WRITE_WATT_FROM_PCT:
            # Telemetry reports watts; present it back as a percentage of rated.
            rated = self._rated_watts or 1
            pct = round(val / rated * 100)
            return max(0, min(100, pct))
        if self._desc.write_mode == WRITE_PCT_FROM_WATT:
            # Telemetry reports a percentage; present it back as watts of rated.
            rated = self._rated_watts or 1
            watts = round(val / 100 * rated)
            return max(0, min(rated, watts))
        return val

    @property
    def native_value(self) -> Optional[float]:
        """Pending write value while unconfirmed, else live telemetry, else last guess.

        Telemetry for a given key is essentially never None once the device
        has reported it at least once (coordinator._last_data accumulates and
        is never cleared), so without prioritising _pending_value here this
        would keep showing the stale pre-write reading until confirmed rather
        than the value the user just requested.
        """
        if self._pending_value is not None:
            return self._pending_value
        telem = self._telemetry_value()
        if telem is not None:
            return telem
        return self._optimistic

    def _compute_raw(self, value: float) -> tuple[int, str]:
        """Convert a display-unit value to a raw register write + log detail."""
        coef = self._reg.coefficient or 1
        if self._desc.write_mode == WRITE_WATT_FROM_PCT:
            watts = round(value / 100 * self._rated_watts)
            raw = int(round(watts / coef))
            detail = f"{value:.0f}% -> {watts}W"
        elif self._desc.write_mode == WRITE_PCT_FROM_WATT:
            rated = self._rated_watts or 1
            pct = max(0.0, min(100.0, value / rated * 100))
            raw = int(round(pct / coef))
            detail = f"{value:.0f}W -> {pct:.1f}%"
        else:
            raw = int(round(value / coef))
            detail = f"{value}{self._desc.unit}"
        return raw, detail

    def _fire_event(self, event_type: str, **extra) -> None:
        """Fire a custom event describing a power-control write's lifecycle.

        Lets external automations (e.g. a pyscript controller) react to a
        confirmed write instead of guessing with a fixed sleep.
        """
        self.hass.bus.async_fire(event_type, {
            "device_id": self.coordinator.api.device_id,
            "entity_id": self.entity_id,
            "data_key": self._desc.data_key,
            "translation_key": self._desc.translation_key,
            "name": self._desc.name,
            "unit": self._desc.unit,
            **extra,
        })

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the register, then track it for confirmation.

        For % power controls the slider value is converted to watts against the
        rated power before scaling by the register coefficient; other controls
        write their value directly (scaled by the coefficient). The write is
        optimistic (UI updates immediately) but also tracked as "pending" —
        once telemetry confirms the register actually holds this value (or a
        confirmation timeout elapses without it, after retries),
        esy_sunhome_number_changed / esy_sunhome_number_change_timeout fires.
        """
        raw, detail = self._compute_raw(value)

        ok = await self.coordinator.write_register(self._reg.address, raw)
        if not ok:
            raise HomeAssistantError(
                f"Failed to set {self._desc.name} (MQTT not connected?)"
            )

        self._optimistic = value
        self._pending_value = value
        self._pending_raw = raw
        self._pending_detail = detail
        self._retry_count = 0
        self.async_write_ha_state()
        _LOGGER.info(
            "Set %s = %s (register %d = %d)",
            self._desc.name, detail, self._reg.address, raw,
        )
        self._fire_event(
            "esy_sunhome_number_change_requested",
            value=value, attempt=1, max_attempts=NUMBER_MAX_RETRIES + 1,
        )
        self._schedule_confirm_timeout()

    def _is_confirmed(self) -> Optional[float]:
        """Return the telemetry value if it now matches the pending write, else None."""
        if self._pending_value is None:
            return None
        telem = self._telemetry_value()
        if telem is None:
            return None
        tolerance = max(self._desc.step, 1.0) * 1.5
        if abs(telem - self._pending_value) <= tolerance:
            return telem
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Check for confirmation of a pending write on each telemetry update."""
        confirmed_value = self._is_confirmed()
        if confirmed_value is not None:
            _LOGGER.info(
                "%s confirmed via telemetry: %s%s",
                self._desc.name, confirmed_value, self._desc.unit,
            )
            self._fire_event("esy_sunhome_number_changed", value=confirmed_value)
            self._clear_pending()
        self.async_write_ha_state()

    def _schedule_confirm_timeout(self) -> None:
        """Schedule a timeout to retry (or give up) if telemetry never confirms."""
        if self._confirm_timeout_handle:
            self._confirm_timeout_handle.cancel()

        async def _on_timeout():
            if self._pending_value is None:
                return  # Already confirmed

            self._retry_count += 1
            if self._retry_count <= NUMBER_MAX_RETRIES:
                _LOGGER.warning(
                    "%s not confirmed after %ds, retry %d/%d",
                    self._desc.name, NUMBER_CONFIRM_TIMEOUT,
                    self._retry_count + 1, NUMBER_MAX_RETRIES + 1,
                )
                self._fire_event(
                    "esy_sunhome_number_change_retry",
                    value=self._pending_value,
                    attempt=self._retry_count + 1,
                    max_attempts=NUMBER_MAX_RETRIES + 1,
                )
                ok = await self.coordinator.write_register(
                    self._reg.address, self._pending_raw
                )
                if ok:
                    self._schedule_confirm_timeout()
                    return
                # Publish itself failed (e.g. MQTT disconnected) — fall through
                # to give up rather than rescheduling against a dead link.

            _LOGGER.error(
                "%s failed to confirm after %d attempts (%ds total) — giving up",
                self._desc.name, NUMBER_MAX_RETRIES + 1,
                (NUMBER_MAX_RETRIES + 1) * NUMBER_CONFIRM_TIMEOUT,
            )
            self._fire_event(
                "esy_sunhome_number_change_timeout",
                value=self._pending_value,
                total_attempts=self._retry_count + 1,
            )
            # Drop the optimistic guess — show whatever telemetry actually
            # reports rather than continuing to claim an unconfirmed value.
            self._optimistic = None
            self._clear_pending()
            self.async_write_ha_state()

        self._confirm_timeout_handle = self.hass.loop.call_later(
            NUMBER_CONFIRM_TIMEOUT,
            lambda: asyncio.create_task(_on_timeout()),
        )

    def _clear_pending(self) -> None:
        """Clear pending-confirmation state."""
        self._pending_value = None
        self._pending_raw = None
        self._pending_detail = None
        self._retry_count = 0
        if self._confirm_timeout_handle:
            self._confirm_timeout_handle.cancel()
            self._confirm_timeout_handle = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending confirmation timer when the entity is removed."""
        if self._confirm_timeout_handle:
            self._confirm_timeout_handle.cancel()
            self._confirm_timeout_handle = None

    @property
    def extra_state_attributes(self) -> dict:
        """Expose pending-confirmation state for visibility/debugging."""
        return {
            "pending_value": self._pending_value,
            "retry_count": self._retry_count if self._pending_value is not None else 0,
        }


class ESYSunhomeSOCNumber(EsySunhomeEntity, NumberEntity):
    """Number entity for a BEM SOC cutoff value (server-side schedule)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: "ESYSunhomeCoordinator",
        translation_key: str,
        name: str,
        field: str,
        icon: str,
    ) -> None:
        self._attr_translation_key = translation_key
        self._attr_name = name
        self._field = field
        self._attr_icon = icon
        super().__init__(coordinator)

    @property
    def native_value(self) -> float | None:
        schedule = self.coordinator.schedule_data
        if schedule is None:
            return None
        val = schedule.get(self._field)
        if val is None:
            return None
        return float(val)

    async def async_set_native_value(self, value: float) -> None:
        """Set the SOC cutoff via the schedule API."""
        coordinator = self.coordinator
        # Fetch the latest schedule so we send back the full payload
        schedule = await coordinator.api.get_schedule()
        schedule[self._field] = int(value)
        await coordinator.api.save_schedule(schedule)
        coordinator.schedule_data = schedule
        self.async_write_ha_state()
