"""Sensor platform for EcoComfort Sync.

All sensor entities are read-only views over the shared EcoComfortDataManager.
They register a lightweight callback with the manager so that
async_write_ha_state() is called whenever the underlying data changes.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_BATTERY_DRAIN_THRESHOLD,
    DOMAIN,
    NAME,
    SENSOR_BATTERY_DRAIN_PREFIX,
    SENSOR_HDD,
    SENSOR_HTC,
    SENSOR_KWH_PER_HDD,
    SENSOR_PUMP_ELECTRICITY,
    SENSOR_ROOM_ENERGY_PREFIX,
    SENSOR_ROOM_HEATING_MINUTES_PREFIX,
    SENSOR_ROOM_SHORT_CYCLING_PREFIX,
    SENSOR_WEIGHTED_INTERNAL_TEMP,
    SHORT_CYCLE_THRESHOLD,
)
from .data_manager import EcoComfortDataManager
from .helpers import entity_slug

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all EcoComfort Sync sensors."""
    manager: EcoComfortDataManager = hass.data[DOMAIN][entry.entry_id]

    sensors: list[SensorEntity] = [
        HTCSensor(manager, entry),
        HDDSensor(manager, entry),
        KWhPerHDDSensor(manager, entry),
        PumpElectricitySensor(manager, entry),
        WeightedInternalTempSensor(manager, entry),
    ]

    # Per-TRV sensors
    for trv_entity_id in manager.trv_entities:
        slug = entity_slug(trv_entity_id)
        sensors.extend(
            [
                RoomEnergySensor(manager, entry, trv_entity_id, slug),
                RoomShortCyclingSensor(manager, entry, trv_entity_id, slug),
                RoomHeatingMinutesSensor(manager, entry, trv_entity_id, slug),
            ]
        )

    # Per-battery sensors
    for battery_entity_id in manager.battery_entities:
        slug = entity_slug(battery_entity_id)
        sensors.append(BatteryDrainSensor(manager, entry, battery_entity_id, slug))

    async_add_entities(sensors)


# ---------------------------------------------------------------------------
# Shared device info
# ---------------------------------------------------------------------------


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="EcoComfort",
        model="Heating Efficiency Monitor",
        entry_type=DeviceEntryType.SERVICE,
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class EcoComfortSensorBase(SensorEntity):
    """Base class for all EcoComfort Sync sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        sensor_key: str,
        listener_tag: str,
    ) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_{sensor_key}"
        self._attr_device_info = _device_info(entry)
        self._listener_tag = listener_tag

    async def async_added_to_hass(self) -> None:
        """Register with the manager to receive push updates."""
        self._manager.add_listener(self._listener_tag, self._on_data_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback to prevent memory leaks on reload or removal."""
        self._manager.remove_listener(self._listener_tag, self._on_data_update)

    @callback
    def _on_data_update(self) -> None:
        """Called by the manager when new data is available."""
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Building-level sensors
# ---------------------------------------------------------------------------


class HTCSensor(EcoComfortSensorBase):
    """Building Heat Transfer Coefficient — W/°C."""

    _attr_name = "Building Heat Transfer Coefficient"
    _attr_native_unit_of_measurement = "W/°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, manager: EcoComfortDataManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, SENSOR_HTC, "htc")

    @property
    def native_value(self) -> float | None:
        if self._manager.htc is None:
            return None
        return round(self._manager.htc, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"note": "Rolling 24-hour window; unavailable until first 30-min gas update."}


class HDDSensor(EcoComfortSensorBase):
    """Heating Degree Days — today's value."""

    _attr_name = "Heating Degree Days (Today)"
    _attr_native_unit_of_measurement = "°C·day"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-low"

    def __init__(self, manager: EcoComfortDataManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, SENSOR_HDD, "hdd")

    @property
    def native_value(self) -> float | None:
        val = self._manager.get_intra_day_hdd()
        return round(val, 2) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "t_base": self._manager.t_base,
            "finalised_hdd": (
                round(self._manager.hdd_today, 2)
                if self._manager.hdd_today is not None
                else None
            ),
        }


class KWhPerHDDSensor(EcoComfortSensorBase):
    """Daily gas consumption normalised by Heating Degree Days."""

    _attr_name = "Gas kWh per HDD (Today)"
    _attr_native_unit_of_measurement = "kWh/HDD"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:fire"

    def __init__(self, manager: EcoComfortDataManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, SENSOR_KWH_PER_HDD, "hdd")

    @property
    def native_value(self) -> float | None:
        val = self._manager.get_intra_day_kwh_per_hdd()
        return round(val, 3) if val is not None else None


class PumpElectricitySensor(EcoComfortSensorBase):
    """Estimated boiler pump electricity consumption today."""

    _attr_name = "Boiler Pump Electricity Today"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:pump"

    def __init__(self, manager: EcoComfortDataManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, SENSOR_PUMP_ELECTRICITY, "pump")

    @property
    def native_value(self) -> float:
        return round(self._manager.pump_electricity_kwh, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"pump_wattage_w": self._manager.pump_wattage}


class WeightedInternalTempSensor(EcoComfortSensorBase):
    """Weighted average of all room temperature sensors."""

    _attr_name = "Weighted Internal Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-thermometer-outline"

    def __init__(self, manager: EcoComfortDataManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, SENSOR_WEIGHTED_INTERNAL_TEMP, "internal_temp")

    @property
    def native_value(self) -> float | None:
        if self._manager.weighted_internal_temp is None:
            return None
        return round(self._manager.weighted_internal_temp, 2)


# ---------------------------------------------------------------------------
# Per-room sensors (dynamic — one set per Tado TRV)
# ---------------------------------------------------------------------------


class _RoomSensorBase(EcoComfortSensorBase):
    """Base for per-room sensors with a shared room-name attribute."""

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        trv_entity_id: str,
        room_slug: str,
        sensor_key: str,
    ) -> None:
        super().__init__(
            manager,
            entry,
            f"{sensor_key}_{room_slug}",
            f"room_{trv_entity_id}",
        )
        self._trv_entity_id = trv_entity_id
        self._room_slug = room_slug

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"trv_entity_id": self._trv_entity_id}


class RoomEnergySensor(_RoomSensorBase):
    """Estimated gas energy consumed by this room today (kWh)."""

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        trv_entity_id: str,
        room_slug: str,
    ) -> None:
        super().__init__(
            manager, entry, trv_entity_id, room_slug, SENSOR_ROOM_ENERGY_PREFIX
        )
        friendly_room = room_slug.replace("_", " ").title()
        self._attr_name = f"{friendly_room} Energy Today"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:radiator"

    @property
    def native_value(self) -> float | None:
        val = self._manager.get_room_energy_kwh(self._trv_entity_id)
        return round(val, 4) if val is not None else None


class RoomShortCyclingSensor(_RoomSensorBase):
    """Number of short-cycling events for this TRV in the last 60 minutes."""

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        trv_entity_id: str,
        room_slug: str,
    ) -> None:
        super().__init__(
            manager, entry, trv_entity_id, room_slug, SENSOR_ROOM_SHORT_CYCLING_PREFIX
        )
        friendly_room = room_slug.replace("_", " ").title()
        self._attr_name = f"{friendly_room} Short Cycling (1 h)"
        self._attr_native_unit_of_measurement = "cycles"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self) -> int:
        """Number of short-cycling events in the last 60 minutes."""
        return self._manager.get_short_cycle_count(self._trv_entity_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        count = self._manager.get_short_cycle_count(self._trv_entity_id)
        return {
            **super().extra_state_attributes,
            "is_short_cycling": count > SHORT_CYCLE_THRESHOLD,
            "cycle_count": count,
            "threshold": SHORT_CYCLE_THRESHOLD,
        }


class RoomHeatingMinutesSensor(_RoomSensorBase):
    """Total minutes this TRV has been heating today."""

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        trv_entity_id: str,
        room_slug: str,
    ) -> None:
        super().__init__(
            manager, entry, trv_entity_id, room_slug, SENSOR_ROOM_HEATING_MINUTES_PREFIX
        )
        friendly_room = room_slug.replace("_", " ").title()
        self._attr_name = f"{friendly_room} Heating Minutes Today"
        self._attr_native_unit_of_measurement = "min"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:timer-outline"

    @property
    def native_value(self) -> float | None:
        val = self._manager.get_heating_minutes_today(self._trv_entity_id)
        return round(val, 1) if val is not None else None


# ---------------------------------------------------------------------------
# Per-battery sensors (dynamic)
# ---------------------------------------------------------------------------


class BatteryDrainSensor(EcoComfortSensorBase):
    """Average daily battery drain rate for a device (%/day)."""

    def __init__(
        self,
        manager: EcoComfortDataManager,
        entry: ConfigEntry,
        battery_entity_id: str,
        device_slug: str,
    ) -> None:
        super().__init__(
            manager,
            entry,
            f"{SENSOR_BATTERY_DRAIN_PREFIX}_{device_slug}",
            f"battery_{battery_entity_id}",
        )
        self._battery_entity_id = battery_entity_id
        friendly_device = device_slug.replace("_battery", "").replace("_", " ").title()
        self._attr_name = f"{friendly_device} Battery Drain Rate"
        self._attr_native_unit_of_measurement = "%/day"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:battery-alert"

    @property
    def native_value(self) -> float | None:
        rate = self._manager.get_battery_drain_rate(self._battery_entity_id)
        return round(rate, 2) if rate is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rate = self._manager.get_battery_drain_rate(self._battery_entity_id)
        threshold = self._manager.battery_drain_threshold
        return {
            "source_entity": self._battery_entity_id,
            "is_premature_drain": (
                rate is not None and rate > threshold
            ),
            "drain_threshold_pct_per_day": threshold,
        }
