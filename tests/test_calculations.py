"""Unit tests for EcoComfort Sync calculation engine.

These tests validate the maths directly without requiring a live HA instance.
A lightweight mock of HomeAssistant is used where the object is required but
no HA methods are actually called.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ecocomfort_sync.const import (
    HVAC_ACTION_HEATING,
    HVAC_ACTION_IDLE,
    SHORT_CYCLE_THRESHOLD,
)
from custom_components.ecocomfort_sync.data_manager import (
    EcoComfortDataManager,
    HtcSample,
    TRVTracker,
)
from tests.conftest import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(**overrides) -> EcoComfortDataManager:
    """Return a data manager instance with a mock hass (no listeners started)."""
    hass = MagicMock()
    hass.states.async_all.return_value = []
    hass.states.get.return_value = None
    config = {**DEFAULT_CONFIG, **overrides}
    return EcoComfortDataManager(hass, "test_entry_id", config)


def _ts(offset_seconds: float = 0.0) -> datetime:
    """Return a UTC datetime a given number of seconds before 'now'."""
    import homeassistant.util.dt as dt_util
    with patch("homeassistant.util.dt.utcnow") as _:
        pass  # just ensure import succeeds
    base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    return base - timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# Weighted internal temperature
# ---------------------------------------------------------------------------

class TestWeightedInternalTemp:
    def test_single_sensor(self):
        mgr = _make_manager()
        mgr._room_temps = {"sensor.room_a": 20.5}
        assert mgr._calc_weighted_internal_temp() == pytest.approx(20.5)

    def test_multiple_sensors_average(self):
        mgr = _make_manager()
        mgr._room_temps = {"sensor.a": 20.0, "sensor.b": 22.0, "sensor.c": 21.0}
        assert mgr._calc_weighted_internal_temp() == pytest.approx(21.0)

    def test_all_none_returns_none(self):
        mgr = _make_manager()
        mgr._room_temps = {"sensor.a": None, "sensor.b": None}
        assert mgr._calc_weighted_internal_temp() is None

    def test_partial_none_ignores_missing(self):
        mgr = _make_manager()
        mgr._room_temps = {"sensor.a": 20.0, "sensor.b": None}
        assert mgr._calc_weighted_internal_temp() == pytest.approx(20.0)

    def test_empty_returns_none(self):
        mgr = _make_manager()
        assert mgr._calc_weighted_internal_temp() is None


# ---------------------------------------------------------------------------
# HTC calculation
# ---------------------------------------------------------------------------

class TestHTCCalculation:
    def _sample(
        self,
        gas_kwh_delta: float,
        t_internal: float,
        t_external: float,
        wind_speed: float = 0.0,
        period_hours: float = 0.5,
        pump_kwh: float = 0.0,
        offset_seconds: float = 0.0,
    ) -> HtcSample:
        return HtcSample(
            timestamp=_ts(offset_seconds),
            gas_kwh_delta=gas_kwh_delta,
            pump_kwh=pump_kwh,
            t_internal=t_internal,
            t_external=t_external,
            wind_speed=wind_speed,
            period_hours=period_hours,
        )

    def test_basic_htc_no_wind(self):
        """HTC = (gas * eff) / (ΔT * hours) * 1000, no wind."""
        mgr = _make_manager(boiler_efficiency=1.0, wind_factor=0.0)
        # 1 kWh / (10 °C × 1 h) × 1000 = 100 W/°C
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, t_internal=20.0, t_external=10.0,
                         period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc == pytest.approx(100.0, rel=1e-3)

    def test_htc_with_wind_adjustment(self):
        mgr = _make_manager(boiler_efficiency=1.0, wind_factor=0.1)
        # HTC_base = 100 W/°C, wind_speed = 5 → factor = 1 + 0.1*5 = 1.5
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, t_internal=20.0, t_external=10.0,
                         wind_speed=5.0, period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc == pytest.approx(150.0, rel=1e-3)

    def test_htc_with_pump_kwh(self):
        mgr = _make_manager(boiler_efficiency=1.0, wind_factor=0.0)
        # (1.0 gas + 0.1 pump) / (10 * 1) * 1000 = 110 W/°C
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, pump_kwh=0.1, t_internal=20.0,
                         t_external=10.0, period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc == pytest.approx(110.0, rel=1e-3)

    def test_htc_zero_delta_t_returns_none(self):
        mgr = _make_manager()
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, t_internal=15.0, t_external=15.0,
                         period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc is None

    def test_htc_negative_delta_t_returns_none(self):
        mgr = _make_manager()
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, t_internal=10.0, t_external=20.0,
                         period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc is None

    def test_htc_empty_buffer_returns_none(self):
        mgr = _make_manager()
        mgr._htc_buffer = deque()
        mgr._recalculate_htc()
        assert mgr.htc is None

    def test_htc_rolling_multi_sample(self):
        """Two samples should be aggregated correctly."""
        mgr = _make_manager(boiler_efficiency=1.0, wind_factor=0.0)
        # 2 × (0.5 kWh / (10°C × 0.5h)) = 100 W/°C each → 100 W/°C overall
        s = self._sample
        mgr._htc_buffer = deque([
            s(gas_kwh_delta=0.5, t_internal=20.0, t_external=10.0, period_hours=0.5,
              offset_seconds=3600),
            s(gas_kwh_delta=0.5, t_internal=20.0, t_external=10.0, period_hours=0.5),
        ])
        mgr._recalculate_htc()
        assert mgr.htc == pytest.approx(100.0, rel=1e-3)

    def test_htc_boiler_efficiency_scaling(self):
        mgr = _make_manager(boiler_efficiency=0.9, wind_factor=0.0)
        # useful = 1.0 * 0.9 = 0.9 kWh; HTC = 0.9/(10*1)*1000 = 90 W/°C
        mgr._htc_buffer = deque([
            self._sample(gas_kwh_delta=1.0, t_internal=20.0, t_external=10.0,
                         period_hours=1.0)
        ])
        mgr._recalculate_htc()
        assert mgr.htc == pytest.approx(90.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Proportional room energy split
# ---------------------------------------------------------------------------

class TestRoomEnergySplit:
    def test_equal_split_two_rooms(self):
        mgr = _make_manager()
        trv_a = "climate.tado_a"
        trv_b = "climate.tado_b"
        mgr.trv_entities = [trv_a, trv_b]
        mgr._trv = {
            trv_a: TRVTracker(heating_minutes_since_gas=30.0),
            trv_b: TRVTracker(heating_minutes_since_gas=30.0),
        }
        mgr._split_room_energy(2.0)
        assert mgr._trv[trv_a].energy_kwh_today == pytest.approx(1.0)
        assert mgr._trv[trv_b].energy_kwh_today == pytest.approx(1.0)

    def test_proportional_split_3_1(self):
        mgr = _make_manager()
        trv_a, trv_b = "climate.tado_a", "climate.tado_b"
        mgr._trv = {
            trv_a: TRVTracker(heating_minutes_since_gas=30.0),
            trv_b: TRVTracker(heating_minutes_since_gas=10.0),
        }
        mgr._split_room_energy(4.0)
        assert mgr._trv[trv_a].energy_kwh_today == pytest.approx(3.0)
        assert mgr._trv[trv_b].energy_kwh_today == pytest.approx(1.0)

    def test_no_heating_zero_no_allocation(self):
        """If no room was heating, energy is not allocated to any room."""
        mgr = _make_manager()
        trv_a, trv_b = "climate.tado_a", "climate.tado_b"
        mgr._trv = {
            trv_a: TRVTracker(heating_minutes_since_gas=0.0),
            trv_b: TRVTracker(heating_minutes_since_gas=0.0),
        }
        mgr._split_room_energy(2.0)
        assert mgr._trv[trv_a].energy_kwh_today == pytest.approx(0.0)
        assert mgr._trv[trv_b].energy_kwh_today == pytest.approx(0.0)
    def test_flush_in_progress_heating_before_split(self):
        """Heating that's currently active (heating_since set) should be flushed."""
        from datetime import timedelta
        base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
        trv_a, trv_b = "climate.tado_a", "climate.tado_b"
        mgr = _make_manager()
        # trv_a has been heating for 30 minutes (heating_since set)
        tracker_a = TRVTracker(heating_minutes_since_gas=0.0)
        tracker_a.heating_since = base - timedelta(minutes=30)
        # trv_b has 30 pre-counted minutes but is not mid-run
        tracker_b = TRVTracker(heating_minutes_since_gas=30.0)
        mgr._trv = {trv_a: tracker_a, trv_b: tracker_b}
        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            mgr._split_room_energy(2.0)
        # Both rooms contributed 30 min → 50/50 split
        assert mgr._trv[trv_a].energy_kwh_today == pytest.approx(1.0)
        assert mgr._trv[trv_b].energy_kwh_today == pytest.approx(1.0)
        # heating_since reset to 'now' (still heating), not None
        assert mgr._trv[trv_a].heating_since == base

    def test_remove_listener_prevents_further_callbacks(self):
        """Callbacks should stop firing after remove_listener is called."""
        mgr = _make_manager()
        calls: list[int] = []

        def cb1() -> None:
            calls.append(1)

        def cb2() -> None:
            calls.append(2)

        mgr.add_listener("pump", cb1)
        mgr._notify("pump")
        assert len(calls) == 1

        # Remove cb1 — it should no longer fire
        mgr.remove_listener("pump", cb1)
        mgr._notify("pump")
        assert len(calls) == 1

        # add+remove cb2 — should also not fire
        mgr.add_listener("pump", cb2)
        mgr.remove_listener("pump", cb2)
        mgr._notify("pump")
        assert len(calls) == 1

    def test_pump_no_double_counting(self):
        """_calc_current_pump_kwh_today must not double-count the running epoch."""
        from datetime import timedelta
        base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        mgr.pump_wattage = 100.0  # 0.1 kW → 1 h = 0.1 kWh
        # Simulate 1 800 s already accumulated + 1 800 s running epoch
        mgr._pump_on_seconds_today = 1800.0
        mgr._pump_since = base - timedelta(seconds=1800)
        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            kwh = mgr._calc_current_pump_kwh_today()
        # 3600 s × (100 W / 1000) / 3600 = 0.1 kWh
        assert kwh == pytest.approx(0.1, rel=1e-5)
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr._trv = {trv: TRVTracker(heating_minutes_since_gas=20.0)}
        mgr._split_room_energy(1.0)
        assert mgr._trv[trv].heating_minutes_since_gas == 0.0

    def test_cumulative_energy_accumulates(self):
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr._trv = {trv: TRVTracker(heating_minutes_since_gas=30.0)}
        mgr._split_room_energy(1.0)
        mgr._trv[trv].heating_minutes_since_gas = 30.0
        mgr._split_room_energy(1.0)
        assert mgr._trv[trv].energy_kwh_today == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Intra-day HDD
# ---------------------------------------------------------------------------

class TestIntraDayHDD:
    def test_intra_day_hdd_with_samples(self):
        mgr = _make_manager()
        mgr._daily_outdoor_temp_samples = [5.0, 7.0, 8.0]  # avg=6.67, base=18
        hdd = mgr.get_intra_day_hdd()
        assert hdd == pytest.approx(18.0 - (5.0 + 7.0 + 8.0) / 3.0, rel=1e-4)

    def test_intra_day_hdd_falls_back_to_finalised(self):
        mgr = _make_manager()
        mgr.hdd_today = 5.5
        # No daily samples → fall back
        assert mgr.get_intra_day_hdd() == pytest.approx(5.5)

    def test_intra_day_hdd_none_when_no_data(self):
        mgr = _make_manager()
        assert mgr.get_intra_day_hdd() is None

    def test_intra_day_kwh_per_hdd(self):
        mgr = _make_manager()
        mgr._daily_outdoor_temp_samples = [8.0]  # avg=8, base=18 → HDD=10
        mgr._daily_gas_kwh = 30.0
        kwh_hdd = mgr.get_intra_day_kwh_per_hdd()
        assert kwh_hdd == pytest.approx(3.0)

    def test_intra_day_kwh_per_hdd_zero_hdd(self):
        mgr = _make_manager()
        # Outdoor == t_base → HDD = 0 → guard against division
        mgr._daily_outdoor_temp_samples = [18.0]
        mgr._daily_gas_kwh = 10.0
        assert mgr.get_intra_day_kwh_per_hdd() is None

    def test_intra_day_kwh_per_hdd_no_gas(self):
        mgr = _make_manager()
        mgr._daily_outdoor_temp_samples = [5.0]
        # _daily_gas_kwh defaults to 0
        assert mgr.get_intra_day_kwh_per_hdd() is None


class TestHDD:
    def test_positive_hdd(self):
        """HDD = max(0, T_base - avg_outdoor)"""
        base = 18.0
        avg_outdoor = 5.0
        hdd = max(0.0, base - avg_outdoor)
        assert hdd == pytest.approx(13.0)

    def test_hdd_zero_when_warm(self):
        base = 18.0
        avg_outdoor = 22.0
        hdd = max(0.0, base - avg_outdoor)
        assert hdd == 0.0

    def test_hdd_boundary(self):
        base = 18.0
        avg_outdoor = 18.0
        hdd = max(0.0, base - avg_outdoor)
        assert hdd == 0.0

    def test_kwh_per_hdd_calculation(self):
        daily_gas = 30.0  # kWh
        hdd = 10.0
        kwh_per_hdd = daily_gas / hdd
        assert kwh_per_hdd == pytest.approx(3.0)

    def test_kwh_per_hdd_zero_hdd_guard(self):
        """Should return None / be guarded against ZeroDivisionError."""
        daily_gas = 10.0
        hdd = 0.0
        result = (daily_gas / hdd) if hdd > 0 else None
        assert result is None


# ---------------------------------------------------------------------------
# Short cycling
# ---------------------------------------------------------------------------

class TestShortCycling:
    def _make_tracker_with_events(self, offsets_seconds: list[float]) -> TRVTracker:
        from datetime import timedelta
        base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
        tracker = TRVTracker()
        for offset in offsets_seconds:
            tracker.short_cycle_events.append(base - timedelta(seconds=offset))
        return tracker

    def test_no_events(self):
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr._trv = {trv: TRVTracker()}
        assert mgr.get_short_cycle_count(trv) == 0

    def test_count_within_window(self):
        from datetime import timedelta
        mgr = _make_manager()
        trv = "climate.tado_a"
        base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
        tracker = TRVTracker()
        # 3 events, all within the last 60 min
        for minutes_ago in [5, 15, 45]:
            tracker.short_cycle_events.append(base - timedelta(minutes=minutes_ago))
        mgr._trv = {trv: tracker}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            count = mgr.get_short_cycle_count(trv)
        assert count == 3

    def test_stale_events_pruned(self):
        from datetime import timedelta
        mgr = _make_manager()
        trv = "climate.tado_a"
        base = datetime(2026, 2, 21, 12, 0, 0, tzinfo=timezone.utc)
        tracker = TRVTracker()
        # 2 events older than 60 min, 1 fresh
        tracker.short_cycle_events.append(base - timedelta(minutes=90))
        tracker.short_cycle_events.append(base - timedelta(minutes=75))
        tracker.short_cycle_events.append(base - timedelta(minutes=10))
        mgr._trv = {trv: tracker}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            count = mgr.get_short_cycle_count(trv)
        assert count == 1

    def test_short_cycle_threshold(self):
        assert SHORT_CYCLE_THRESHOLD == 6

    def test_unknown_trv_returns_zero(self):
        mgr = _make_manager()
        assert mgr.get_short_cycle_count("climate.nonexistent") == 0


# ---------------------------------------------------------------------------
# Pump electricity
# ---------------------------------------------------------------------------

class TestPumpElectricity:
    def test_pump_kwh_formula(self):
        """pump_kwh = pump_on_seconds / 3600 * pump_wattage / 1000"""
        pump_wattage = 45.0  # W
        pump_on_seconds = 3600.0  # 1 hour
        expected_kwh = (pump_on_seconds / 3600.0) * (pump_wattage / 1000.0)
        assert expected_kwh == pytest.approx(0.045)

    def test_pump_kwh_zero(self):
        assert (0.0 / 3600.0) * (45.0 / 1000.0) == 0.0

    def test_pump_kwh_half_hour(self):
        pump_on_seconds = 1800.0
        pump_wattage = 45.0
        expected = (pump_on_seconds / 3600.0) * (pump_wattage / 1000.0)
        assert expected == pytest.approx(0.0225)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Minute tick — room notifications
# ---------------------------------------------------------------------------

class TestMinuteTick:
    def test_tick_increments_heating_minutes_today(self):
        """Minute tick should add 1.0 to heating_minutes_today for heating TRVs."""
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr._trv = {trv: TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_today == pytest.approx(1.0)

    def test_tick_does_not_change_heating_minutes_since_gas(self):
        """Minute tick must NOT touch heating_minutes_since_gas (event-based only)."""
        mgr = _make_manager()
        trv = "climate.tado_a"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)
        tracker.heating_minutes_since_gas = 5.0
        mgr._trv = {trv: tracker}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_since_gas == pytest.approx(5.0)

    def test_tick_idle_trv_unchanged(self):
        """TRVs in idle should not accumulate any minutes on the tick."""
        mgr = _make_manager()
        trv = "climate.tado_b"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_IDLE)
        tracker.heating_minutes_today = 10.0
        mgr._trv = {trv: tracker}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_today == pytest.approx(10.0)

    def test_tick_notifies_heating_room(self):
        """Minute tick must push a notification for each actively-heating TRV."""
        mgr = _make_manager()
        trv = "climate.tado_kitchen"
        mgr._trv = {trv: TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)}
        calls: list[str] = []
        mgr.add_listener(f"room_{trv}", lambda: calls.append(trv))
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert trv in calls

    def test_tick_does_not_notify_idle_room(self):
        """Minute tick must NOT notify rooms for TRVs that are idle."""
        mgr = _make_manager()
        trv = "climate.tado_lounge"
        mgr._trv = {trv: TRVTracker(last_hvac_action=HVAC_ACTION_IDLE)}
        calls: list[str] = []
        mgr.add_listener(f"room_{trv}", lambda: calls.append(trv))
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert trv not in calls


# ---------------------------------------------------------------------------
# TRV state change — heating_minutes_today NOT written by event handler
# ---------------------------------------------------------------------------

class TestTRVTransitionHeatingMinutesToday:
    def test_idle_to_heating_does_not_increment_today(self):
        """idle→heating transition must not write to heating_minutes_today."""
        from unittest.mock import MagicMock
        base = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        trv = "climate.tado_a"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_IDLE)
        tracker.heating_minutes_today = 0.0
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.attributes = {"hvac_action": HVAC_ACTION_HEATING}
        event = MagicMock()
        event.data = {"entity_id": trv, "new_state": state}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            mgr._handle_trv_state_change(event)

        assert tracker.heating_minutes_today == pytest.approx(0.0)

    def test_heating_to_idle_does_not_write_today(self):
        """heating→idle transition must only update heating_minutes_since_gas."""
        from datetime import timedelta
        from unittest.mock import MagicMock
        base = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        trv = "climate.tado_a"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)
        tracker.heating_since = base - timedelta(minutes=20)
        tracker.heating_minutes_since_gas = 0.0
        tracker.heating_minutes_today = 0.0  # tick hasn't run yet
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.attributes = {"hvac_action": HVAC_ACTION_IDLE}
        event = MagicMock()
        event.data = {"entity_id": trv, "new_state": state}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ):
            mgr._handle_trv_state_change(event)

        # 20 minutes captured into allocation counter
        assert tracker.heating_minutes_since_gas == pytest.approx(20.0)
        # display counter untouched by event handler — still 0
        assert tracker.heating_minutes_today == pytest.approx(0.0)
        assert tracker.heating_since is None


# ---------------------------------------------------------------------------
# Gas counter reset warning
# ---------------------------------------------------------------------------

class TestGasCounterReset:
    def test_counter_reset_logs_warning(self):
        """A drop in the gas counter should emit a warning and produce 0 delta."""
        from unittest.mock import MagicMock
        base = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        mgr._last_gas_kwh = 500.0
        mgr._last_gas_ts = base - timedelta(seconds=1800)

        new_state = MagicMock()
        new_state.state = "10.0"  # counter reset to 10 from 500
        event = MagicMock()
        event.data = {"new_state": new_state}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager._is_valid_state",
            return_value=True,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager._LOGGER"
        ) as mock_logger:
            mgr._handle_gas_state_change(event)
            mock_logger.warning.assert_called_once()
            warning_msg = mock_logger.warning.call_args[0][0]
            assert "reset" in warning_msg.lower() or "counter" in warning_msg.lower()

        # delta_kwh=0 → _daily_gas_kwh unchanged (still 0 from fresh manager)
        assert mgr._daily_gas_kwh == pytest.approx(0.0)

    def test_normal_increment_no_warning(self):
        """A normal positive delta must not log any warning."""
        from unittest.mock import MagicMock
        base = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        mgr._last_gas_kwh = 100.0
        mgr._last_gas_ts = base - timedelta(seconds=1800)

        new_state = MagicMock()
        new_state.state = "102.5"
        event = MagicMock()
        event.data = {"new_state": new_state}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager._is_valid_state",
            return_value=True,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager._LOGGER"
        ) as mock_logger:
            mgr._handle_gas_state_change(event)
            mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Outdoor temp samples on every external change (not only gas events)
# ---------------------------------------------------------------------------

class TestOutdoorTempSampling:
    def test_sample_collected_on_each_external_temp_change(self):
        """Every valid external temp update should be stored in the daily samples."""
        from unittest.mock import MagicMock
        mgr = _make_manager()

        def _make_event(value: str) -> MagicMock:
            state = MagicMock()
            state.state = value
            event = MagicMock()
            event.data = {"new_state": state}
            return event

        with patch(
            "custom_components.ecocomfort_sync.data_manager._is_valid_state",
            return_value=True,
        ):
            mgr._handle_external_temp_change(_make_event("5.0"))
            mgr._handle_external_temp_change(_make_event("6.0"))
            mgr._handle_external_temp_change(_make_event("7.0"))

        assert list(mgr._daily_outdoor_temp_samples) == pytest.approx([5.0, 6.0, 7.0])

    def test_intra_day_hdd_uses_external_temp_samples(self):
        """Intra-day HDD reflects all outdoor samples, not just gas-event ones."""
        mgr = _make_manager()
        # Three readings at 8°C → avg=8, t_base=18 → HDD=10
        mgr._daily_outdoor_temp_samples.extend([8.0, 8.0, 8.0])
        assert mgr.get_intra_day_hdd() == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Midnight resets and notifies room sensors
# ---------------------------------------------------------------------------

class TestMidnightRoomNotify:
    def test_midnight_notifies_all_rooms(self):
        """After midnight reset, every registered room tag must fire."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        trv_a, trv_b = "climate.tado_a", "climate.tado_b"
        mgr._trv = {
            trv_a: TRVTracker(heating_minutes_today=45.0, energy_kwh_today=1.5),
            trv_b: TRVTracker(heating_minutes_today=20.0, energy_kwh_today=0.5),
        }

        notified: list[str] = []
        mgr.add_listener(f"room_{trv_a}", lambda: notified.append(trv_a))
        mgr.add_listener(f"room_{trv_b}", lambda: notified.append(trv_b))

        # Patch battery/states helpers to avoid AttributeErrors
        mgr._battery = {}
        mgr._hass.states.get.return_value = None

        base = datetime(2026, 2, 22, 0, 0, tzinfo=timezone.utc)
        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.now",
            return_value=base,
        ):
            mgr._handle_midnight(base)

        # Both rooms were notified
        assert trv_a in notified
        assert trv_b in notified
        # Accumulators were actually reset
        assert mgr._trv[trv_a].heating_minutes_today == pytest.approx(0.0)
        assert mgr._trv[trv_a].energy_kwh_today == pytest.approx(0.0)

    def test_midnight_hdd_notified(self):
        """Midnight handler must fire the hdd tag."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        mgr._trv = {}
        mgr._battery = {}
        mgr._hass.states.get.return_value = None
        mgr._daily_outdoor_temp_samples.extend([5.0, 6.0])

        hdd_calls: list[int] = []
        mgr.add_listener("hdd", lambda: hdd_calls.append(1))

        base = datetime(2026, 2, 22, 0, 0, tzinfo=timezone.utc)
        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.now",
            return_value=base,
        ):
            mgr._handle_midnight(base)

        assert len(hdd_calls) == 1


# ---------------------------------------------------------------------------
# Gas event notifies HDD listeners
# ---------------------------------------------------------------------------

class TestGasEventNotifiesHDD:
    def test_gas_event_fires_hdd_notify(self):
        """After a positive gas delta, hdd listeners must be notified immediately."""
        from unittest.mock import MagicMock
        base = datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc)
        mgr = _make_manager()
        mgr._last_gas_kwh = 100.0
        mgr._last_gas_ts = base - timedelta(seconds=1800)
        mgr._latest_external_temp = 5.0
        mgr._room_temps = {"sensor.room_a": 20.0}

        hdd_calls: list[int] = []
        mgr.add_listener("hdd", lambda: hdd_calls.append(1))

        new_state = MagicMock()
        new_state.state = "101.5"
        event = MagicMock()
        event.data = {"new_state": new_state}

        with patch(
            "custom_components.ecocomfort_sync.data_manager.dt_util.utcnow",
            return_value=base,
        ), patch(
            "custom_components.ecocomfort_sync.data_manager._is_valid_state",
            return_value=True,
        ):
            mgr._handle_gas_state_change(event)

        assert len(hdd_calls) == 1


# ---------------------------------------------------------------------------
# Startup seeding — heating_since set for already-heating TRVs
# ---------------------------------------------------------------------------

class TestSeedInitialValues:
    def test_heating_trv_gets_heating_since(self):
        """TRVs in heating state at startup should have heating_since populated."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr.trv_entities = [trv]
        tracker = TRVTracker()
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.state = "heat"
        state.attributes = {"hvac_action": HVAC_ACTION_HEATING}
        mgr._hass.states.get.side_effect = lambda eid: state if eid == trv else None

        # Patch other sensors as None
        mgr.room_temp_entities = []
        mgr._seed_initial_values()

        assert tracker.heating_since is not None
        assert tracker.last_hvac_action == HVAC_ACTION_HEATING

    def test_idle_trv_has_no_heating_since(self):
        """TRVs in idle at startup must not have heating_since set."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        trv = "climate.tado_b"
        mgr.trv_entities = [trv]
        tracker = TRVTracker()
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.state = "heat"
        state.attributes = {"hvac_action": HVAC_ACTION_IDLE}
        mgr._hass.states.get.side_effect = lambda eid: state if eid == trv else None

        mgr.room_temp_entities = []
        mgr._seed_initial_values()

        assert tracker.heating_since is None

    def test_heating_trv_seeds_pump_since(self):
        """If a TRV is heating at startup, _pump_since should be set."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr.trv_entities = [trv]
        tracker = TRVTracker()
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.state = "heat"
        state.attributes = {"hvac_action": HVAC_ACTION_HEATING}
        mgr._hass.states.get.side_effect = lambda eid: state if eid == trv else None

        mgr.room_temp_entities = []
        mgr._seed_initial_values()

        # Pump epoch should be active since at least one TRV is heating
        assert mgr._pump_since is not None

    def test_no_heating_trv_no_pump_since(self):
        """If no TRV is heating at startup, _pump_since must remain None."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr.trv_entities = [trv]
        tracker = TRVTracker()
        mgr._trv = {trv: tracker}

        state = MagicMock()
        state.state = "idle"
        state.attributes = {"hvac_action": HVAC_ACTION_IDLE}
        mgr._hass.states.get.side_effect = lambda eid: state if eid == trv else None

        mgr.room_temp_entities = []
        mgr._seed_initial_values()

        assert mgr._pump_since is None


# ---------------------------------------------------------------------------
# _is_valid_state — None state.state guard
# ---------------------------------------------------------------------------

class TestIsValidState:
    def test_none_state_returns_false(self):
        from custom_components.ecocomfort_sync.data_manager import _is_valid_state
        from unittest.mock import MagicMock
        state = MagicMock()
        state.state = None
        assert _is_valid_state(state) is False

    def test_unavailable_returns_false(self):
        from custom_components.ecocomfort_sync.data_manager import _is_valid_state
        from unittest.mock import MagicMock
        state = MagicMock()
        state.state = "unavailable"
        assert _is_valid_state(state) is False

    def test_valid_numeric_string(self):
        from custom_components.ecocomfort_sync.data_manager import _is_valid_state
        from unittest.mock import MagicMock
        state = MagicMock()
        state.state = "21.5"
        assert _is_valid_state(state) is True


# ---------------------------------------------------------------------------
# External temp notifies HDD listeners
# ---------------------------------------------------------------------------

class TestExternalTempNotifiesHDD:
    def test_external_temp_change_triggers_hdd_notify(self):
        """Changing outdoor temp should notify 'hdd' listeners for intra-day HDD."""
        from unittest.mock import MagicMock
        mgr = _make_manager()
        calls: list[str] = []
        mgr.add_listener("hdd", lambda: calls.append("hdd"))

        new_state = MagicMock()
        new_state.state = "8.0"
        event = MagicMock()
        event.data = {"new_state": new_state}

        # Patch _is_valid_state to return True for this state
        with patch(
            "custom_components.ecocomfort_sync.data_manager._is_valid_state",
            return_value=True,
        ):
            mgr._handle_external_temp_change(event)

        assert "hdd" in calls

    def test_tick_increments_heating_minutes_today(self):
        """Minute tick should add 1.0 to heating_minutes_today for heating TRVs."""
        mgr = _make_manager()
        trv = "climate.tado_a"
        mgr._trv = {trv: TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_today == pytest.approx(1.0)

    def test_tick_does_not_change_heating_minutes_since_gas(self):
        """Minute tick must NOT touch heating_minutes_since_gas (event-based only)."""
        mgr = _make_manager()
        trv = "climate.tado_a"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_HEATING)
        tracker.heating_minutes_since_gas = 5.0
        mgr._trv = {trv: tracker}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_since_gas == pytest.approx(5.0)

    def test_tick_idle_trv_unchanged(self):
        """TRVs in idle should not accumulate any minutes on the tick."""
        mgr = _make_manager()
        trv = "climate.tado_b"
        tracker = TRVTracker(last_hvac_action=HVAC_ACTION_IDLE)
        tracker.heating_minutes_today = 10.0
        mgr._trv = {trv: tracker}
        mgr._handle_minute_tick(datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc))
        assert mgr._trv[trv].heating_minutes_today == pytest.approx(10.0)


