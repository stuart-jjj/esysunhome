"""Base entity for ESY Sunhome."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_DEVICE_ID

if TYPE_CHECKING:
    from .coordinator import ESYSunhomeCoordinator


class EsySunhomeEntity(CoordinatorEntity["ESYSunhomeCoordinator"]):
    """Implementation of the base EsySunhome Entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: "ESYSunhomeCoordinator") -> None:
        """Initialize the EsySunhome Entity."""
        super().__init__(coordinator=coordinator)
        self._attr_unique_id = (
            f"{coordinator.api.device_id}_{self._attr_translation_key}"
        )

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.data[CONF_DEVICE_ID])},
            manufacturer="EsySunhome",
            model=_model_label(coordinator),
        )


def _model_label(coordinator: "ESYSunhomeCoordinator") -> str:
    """Build a descriptive model label from detected protocol parameters.

    ESY's API doesn't expose a real model name/number (deviceModel just
    echoes pvPower), so approximate one from the pv_power/phase-count
    values already used to select the correct register map.
    """
    protocol = coordinator.protocol
    if protocol and protocol.pv_power:
        return f"{protocol.pv_power}kW {coordinator.phase_count}-Phase"
    return "HM6"
