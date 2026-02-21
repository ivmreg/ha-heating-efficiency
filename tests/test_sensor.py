"""Tests for EcoComfort Sync sensor entities."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ecocomfort_sync.const import (
    DOMAIN,
    SHORT_CYCLE_THRESHOLD,
)
from custom_components.ecocomfort_sync.data_manager import (
    EcoComfortDataManager,
    TRVTracker,
)
from custom_components.ecocomfort_sync.helpers import entity_slug
from custom_components.ecocomfort_sync.sensor import (
    BatteryDrainSensor,
    HDDSensor,
    HTCSensor,
    KWhPerHDDSensor,
    PumpElectricitySensor,
    RoomEnergySensor,
    RoomHeatingMinutesSensor,
    RoomShortCyclingSensor,
    WeightedInternalTempSensor,
)
from tests.conftest import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = DEFAULT_CONFIG
    entry.options = {}
    return entry


def _make_manager() -> EcoComfortDataManager:
    hass = MagicMock()
    hass.states.async_all.return_value = []
    hass.states.get.return_value = None
    return EcoComfortDataManager(hass, "test_entry", DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# entity_slug helper
# ---------------------------------------------------------------------------


class TestEntitySlug:
    def test_strips_domain(self):
        assert entity_slug("climate.tado_kitchen") == "tado_kitchen"

    def test_replaces_special_chars(self):
        assert entity_slug("sensor.qp_sensor-bedroom") == "qp_sensor_bedroom"

    def test_lowercases(self):
        assert entity_slug("sensor.CO2_Meter_Office") == "co2_meter_office"

    def test_no_domain(self):
        assert entity_slug("bare_entity") == "bare_entity"


# ---------------------------------------------------------------------------
# Building-level sensor values
# ---------------------------------------------------------------------------


class TestBuildingLevelSensors:
    def test_htc_sensor_none_when_no_data(self):
        mgr = _make_manager()
        sensor = HTCSensor(mgr, _make_entry())
        assert sensor.native_value is None

    def test_htc_sensor_rounding(self):
        mgr = _make_manager()
        mgr.htc = 123.456789
        sensor = HTCSensor(mgr, _make_entry())
        assert sensor.native_value == pytest.approx(123.46)

    def test_hdd_sensor_none_when_no_data(self):
        mgr = _make_manager()
        sensor = HDDSensor(mgr, _make_entry())
        assert sensor.native_value is None

    def test_hdd_sensor_value(self):
        mgr = _make_manager()
        mgr.hdd_today = 12.5
        sensor = HDDSensor(mgr, _make_entry())
        assert sensor.native_value == pytest.approx(12.5)

    def test_hdd_sensor_t_base_in_attributes(self):
        mgr = _make_manager()
        sensor = HDDSensor(mgr, _make_entry())
        assert sensor.extra_state_attributes["t_base"] == pytest.approx(18.0)

    def test_kwh_per_hdd_none_when_no_data(self):
        mgr = _make_manager()
        sensor = KWhPerHDDSensor(mgr, _make_entry())
        assert sensor.native_value is None

    def test_kwh_per_hdd_value(self):
        """Intra-day value is computed from _daily_gas_kwh and outdoor samples."""
        mgr = _make_manager()
        # avg outdoor = 8°C, T_base = 18 → HDD = 10
        mgr._daily_outdoor_temp_samples.append(8.0)
        mgr._daily_gas_kwh = 30.0  # 30 kWh / 10 HDD = 3.000
        sensor = KWhPerHDDSensor(mgr, _make_entry())
        assert sensor.native_value == pytest.approx(3.0)

    def test_pump_sensor_zero_initially(self):
        mgr = _make_manager()
        sensor = PumpElectricitySensor(mgr, _make_entry())
        assert sensor.native_value == pytest.approx(0.0)

    def test_pump_sensor_value(self):
        mgr = _make_manager()
        mgr.pump_electricity_kwh = 0.045
        sensor = PumpElectricitySensor(mgr, _make_entry())
        assert sensor.native_value == pytest.approx(0.045)

    def test_internal_temp_none_when_no_data(self):
        mgr = _make_manager()
        sensor = WeightedInternalTempSensor(mgr, _make_entry())
        assert sensor.native_value is None

    def test_internal_temp_value(self):
        mgr = _make_manager()
        mgr.weighted_internal_temp = 20.555
        sensor = WeightedInternalTempSensor(mgr, _make_entry())
        # Python uses banker's rounding: round(20.555, 2) → 20.55
        assert sensor.native_value == pytest.approx(20.55)


# ---------------------------------------------------------------------------
# Per-room sensor values
# ---------------------------------------------------------------------------


ROOM_TRV = "climate.tado_kitchen"
ROOM_SLUG = entity_slug(ROOM_TRV)


class TestRoomSensors:
    def test_room_energy_sensor_initial(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker()}
        sensor = RoomEnergySensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        assert sensor.native_value == pytest.approx(0.0)

    def test_room_energy_accumultes(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker(energy_kwh_today=1.5)}
        sensor = RoomEnergySensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        assert sensor.native_value == pytest.approx(1.5)

    def test_room_short_cycling_zero(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker()}
        sensor = RoomShortCyclingSensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        assert sensor.native_value == 0

    def test_room_short_cycling_not_flagged_below_threshold(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker()}
        sensor = RoomShortCyclingSensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        with patch.object(mgr, "get_short_cycle_count", return_value=3):
            assert sensor.native_value == 3
            attrs = sensor.extra_state_attributes
        assert attrs["is_short_cycling"] is False
        assert attrs["cycle_count"] == 3

    def test_room_short_cycling_flagged_above_threshold(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker()}
        sensor = RoomShortCyclingSensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        with patch.object(mgr, "get_short_cycle_count", return_value=SHORT_CYCLE_THRESHOLD + 1):
            assert sensor.native_value == SHORT_CYCLE_THRESHOLD + 1
            attrs = sensor.extra_state_attributes
        assert attrs["is_short_cycling"] is True

    def test_room_heating_minutes_initial(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker()}
        sensor = RoomHeatingMinutesSensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        assert sensor.native_value == pytest.approx(0.0)

    def test_room_heating_minutes_accumulated(self):
        mgr = _make_manager()
        mgr._trv = {ROOM_TRV: TRVTracker(heating_minutes_today=87.3)}
        sensor = RoomHeatingMinutesSensor(mgr, _make_entry(), ROOM_TRV, ROOM_SLUG)
        assert sensor.native_value == pytest.approx(87.3)


# ---------------------------------------------------------------------------
# Battery drain sensor
# ---------------------------------------------------------------------------

BAT_ENTITY = "sensor.tado_battery"
BAT_SLUG = entity_slug(BAT_ENTITY)


class TestBatterySensor:
    def test_battery_drain_none_initially(self):
        mgr = _make_manager()
        from custom_components.ecocomfort_sync.data_manager import BatteryTracker
        mgr._battery = {BAT_ENTITY: BatteryTracker()}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        assert sensor.native_value is None

    def test_battery_drain_value(self):
        mgr = _make_manager()
        from custom_components.ecocomfort_sync.data_manager import BatteryTracker
        tracker = BatteryTracker()
        tracker.drain_rate_per_day = 2.456
        mgr._battery = {BAT_ENTITY: tracker}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        assert sensor.native_value == pytest.approx(2.46)

    def test_battery_source_entity_in_attributes(self):
        mgr = _make_manager()
        mgr._battery = {}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        assert sensor.extra_state_attributes["source_entity"] == BAT_ENTITY

    def test_battery_is_premature_drain_false(self):
        mgr = _make_manager()
        from custom_components.ecocomfort_sync.data_manager import BatteryTracker
        tracker = BatteryTracker()
        tracker.drain_rate_per_day = 2.0  # below default threshold of 5.0
        mgr._battery = {BAT_ENTITY: tracker}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        assert sensor.extra_state_attributes["is_premature_drain"] is False

    def test_battery_is_premature_drain_true(self):
        mgr = _make_manager()
        from custom_components.ecocomfort_sync.data_manager import BatteryTracker
        tracker = BatteryTracker()
        tracker.drain_rate_per_day = 8.0  # above default threshold of 5.0
        mgr._battery = {BAT_ENTITY: tracker}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        assert sensor.extra_state_attributes["is_premature_drain"] is True

    def test_battery_drain_threshold_in_attributes(self):
        mgr = _make_manager()
        mgr._battery = {}
        sensor = BatteryDrainSensor(mgr, _make_entry(), BAT_ENTITY, BAT_SLUG)
        attrs = sensor.extra_state_attributes
        assert "drain_threshold_pct_per_day" in attrs


# ---------------------------------------------------------------------------
# Sensor unique ID composition
# ---------------------------------------------------------------------------


class TestUniqueIds:
    def test_htc_unique_id(self):
        mgr = _make_manager()
        s = HTCSensor(mgr, _make_entry("my_entry"))
        assert s.unique_id == "my_entry_building_htc"

    def test_room_energy_unique_id(self):
        mgr = _make_manager()
        s = RoomEnergySensor(mgr, _make_entry("eid"), ROOM_TRV, ROOM_SLUG)
        assert s.unique_id == f"eid_room_energy_kwh_{ROOM_SLUG}"

    def test_battery_drain_unique_id(self):
        mgr = _make_manager()
        s = BatteryDrainSensor(mgr, _make_entry("eid"), BAT_ENTITY, BAT_SLUG)
        assert s.unique_id == f"eid_battery_drain_rate_{BAT_SLUG}"


# ---------------------------------------------------------------------------
# Sensor platform setup (requires hass fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_setup_creates_entities(hass):
    """async_setup_entry should add building-level sensors."""
    mgr = EcoComfortDataManager(hass, "entry_id", DEFAULT_CONFIG)
    # Pre-set discovered entities directly — no discovery needed
    mgr.trv_entities = []
    mgr.room_temp_entities = []
    mgr.battery_entities = []

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_id"] = mgr

    entry = MagicMock()
    entry.entry_id = "entry_id"
    entry.data = DEFAULT_CONFIG
    entry.options = {}

    added_entities: list = []

    def _add(entities, **kwargs):
        added_entities.extend(entities)

    from custom_components.ecocomfort_sync import sensor as sensor_mod

    await sensor_mod.async_setup_entry(hass, entry, _add)

    entity_names = [e._attr_name for e in added_entities]
    assert any("Heat Transfer Coefficient" in n for n in entity_names)
    assert any("Heating Degree Days" in n for n in entity_names)
    assert any("Pump Electricity" in n for n in entity_names)


@pytest.mark.asyncio
async def test_sensor_setup_creates_per_room_entities(hass):
    """When TRV entities are present, per-room sensors should be created."""
    mgr = EcoComfortDataManager(hass, "entry_id", DEFAULT_CONFIG)
    mgr.trv_entities = ["climate.tado_bedroom", "climate.tado_lounge"]
    mgr.room_temp_entities = []
    mgr.battery_entities = []

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_id"] = mgr

    entry = MagicMock()
    entry.entry_id = "entry_id"
    entry.data = DEFAULT_CONFIG
    entry.options = {}

    added_entities: list = []

    def _add(entities, **kwargs):
        added_entities.extend(entities)

    from custom_components.ecocomfort_sync import sensor as sensor_mod

    await sensor_mod.async_setup_entry(hass, entry, _add)

    # 5 building-level + 3 × 2 rooms = 11 total
    assert len(added_entities) == 11
