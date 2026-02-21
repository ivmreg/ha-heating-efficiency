"""Entity discovery helpers for EcoComfort Sync."""
from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Match patterns for entity discovery
# Targets specifically Tado Smart Radiator Thermostat TRV entities
_TADO_CLIMATE_PATTERN = re.compile(r"^climate\.tado_smart_radiator_thermostat", re.IGNORECASE)
_QINGPING_SENSOR_PATTERN = re.compile(r"^sensor\.qp_sensor", re.IGNORECASE)
_CO2_METER_PATTERN = re.compile(r"^sensor\.co2_meter", re.IGNORECASE)


def discover_trv_entities(hass: HomeAssistant) -> list[str]:
    """Return all Tado TRV climate entity IDs found in the state machine."""
    results = [
        state.entity_id
        for state in hass.states.async_all("climate")
        if _TADO_CLIMATE_PATTERN.match(state.entity_id)
    ]
    if not results:
        _LOGGER.warning(
            "EcoComfort Sync: no Tado TRV climate entities discovered "
            "(expected entities matching climate.tado*). "
            "Per-room sensors will not be created."
        )
    else:
        _LOGGER.debug("EcoComfort Sync: discovered TRV entities: %s", results)
    return results


def discover_room_temp_sensors(hass: HomeAssistant) -> list[str]:
    """Return Qingping and CO2 meter sensor entity IDs that report temperature.

    Only entities with device_class 'temperature' are included to avoid
    accidentally incorporating CO2, humidity, or PM2.5 readings from
    multi-sensor Qingping devices into the internal temperature average.
    """
    results = [
        state.entity_id
        for state in hass.states.async_all("sensor")
        if (
            _QINGPING_SENSOR_PATTERN.match(state.entity_id)
            or _CO2_METER_PATTERN.match(state.entity_id)
        )
        and state.attributes.get("device_class") == "temperature"
    ]
    if not results:
        _LOGGER.warning(
            "EcoComfort Sync: no room temperature sensors discovered "
            "(expected sensor.qp_sensor_* or sensor.co2_meter*). "
            "Weighted internal temperature will be unavailable."
        )
    else:
        _LOGGER.debug("EcoComfort Sync: discovered room temp sensors: %s", results)
    return results


def entity_slug(entity_id: str) -> str:
    """Convert an entity_id to a safe slug suitable for use in a unique_id."""
    # Strip domain prefix and replace dots/spaces with underscores
    name = entity_id.split(".", 1)[-1]
    return re.sub(r"[^a-z0-9_]", "_", name.lower())
