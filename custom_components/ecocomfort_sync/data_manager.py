"""Core data manager for EcoComfort Sync.

Owns all rolling-window buffers, state tracking for TRVs, and number
-crunching.  Sensor entities register here and are notified via lightweight
callbacks whenever their underlying data changes.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_GAS_KWH_SENSOR,
    CONF_PUMP_WATTAGE,
    CONF_T_BASE,
    CONF_WIND_FACTOR,
    CONF_WIND_SPEED_SENSOR,
    DEFAULT_BATTERY_DRAIN_THRESHOLD,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_PUMP_WATTAGE,
    DEFAULT_T_BASE,
    DEFAULT_WIND_FACTOR,
    HVAC_ACTION_HEATING,
    HVAC_ACTION_IDLE,
    SHORT_CYCLE_THRESHOLD,
    WINDOW_1H,
    WINDOW_24H,
)
from .helpers import (
    discover_battery_sensors,
    discover_room_temp_sensors,
    discover_trv_entities,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------


@dataclass
class HtcSample:
    """One 30-min data point used in the rolling 24-h HTC calculation."""

    timestamp: datetime
    gas_kwh_delta: float
    pump_kwh: float
    t_internal: float
    t_external: float
    wind_speed: float
    period_hours: float


@dataclass
class TRVTracker:
    """Per-TRV mutable state."""

    last_hvac_action: str | None = None
    # Minutes accumulator since the last Hildebrand gas update (reset after split)
    heating_minutes_since_gas: float = 0.0
    # Daily total heating minutes (reset at midnight)
    heating_minutes_today: float = 0.0
    # Timestamps of idle→heating transitions within WINDOW_1H
    short_cycle_events: deque = field(default_factory=deque)
    # When the current heating run started (for fractional-minute tracking)
    heating_since: datetime | None = None
    # Cumulative room energy today (kWh)
    energy_kwh_today: float = 0.0


@dataclass
class BatteryTracker:
    """Per-battery mutable state."""

    # Up to 7 (date, level) snapshots
    daily_snapshots: deque = field(default_factory=lambda: deque(maxlen=7))
    # Drain rate in %/day; None until at least 2 snapshots exist
    drain_rate_per_day: float | None = None


# ---------------------------------------------------------------------------
# Main manager class
# ---------------------------------------------------------------------------


class EcoComfortDataManager:
    """Holds all real-time state and calculated metrics for EcoComfort Sync."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id

        # Config values
        self.gas_kwh_sensor: str = config[CONF_GAS_KWH_SENSOR]
        self.external_temp_sensor: str = config[CONF_EXTERNAL_TEMP_SENSOR]
        self.wind_speed_sensor: str = config[CONF_WIND_SPEED_SENSOR]
        self.boiler_efficiency: float = float(
            config.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY)
        )
        self.pump_wattage: float = float(
            config.get(CONF_PUMP_WATTAGE, DEFAULT_PUMP_WATTAGE)
        )
        self.t_base: float = float(config.get(CONF_T_BASE, DEFAULT_T_BASE))
        self.wind_factor: float = float(
            config.get(CONF_WIND_FACTOR, DEFAULT_WIND_FACTOR)
        )
        # Battery drain rate threshold above which "premature drain" is flagged (%/day)
        self.battery_drain_threshold: float = float(
            config.get("battery_drain_threshold", DEFAULT_BATTERY_DRAIN_THRESHOLD)
        )

        # Discovered entity lists (populated in async_discover_entities)
        self.trv_entities: list[str] = []
        self.room_temp_entities: list[str] = []
        self.battery_entities: list[str] = []

        # Per-TRV tracking
        self._trv: dict[str, TRVTracker] = {}

        # Per-battery tracking
        self._battery: dict[str, BatteryTracker] = {}

        # Rolling 24-h HTC sample buffer
        self._htc_buffer: deque[HtcSample] = deque()

        # Latest room temperature readings: entity_id → °C
        self._room_temps: dict[str, float | None] = {}

        # Latest external temp / wind speed
        self._latest_external_temp: float | None = None
        self._latest_wind_speed: float | None = None

        # Gas sensor: last cumulative kWh value and its timestamp
        self._last_gas_kwh: float | None = None
        self._last_gas_ts: datetime | None = None

        # Pump electricity tracking
        # _pump_on_seconds_today: total pump-on seconds accumulated since midnight
        #   via event-based tracking (TRV state transitions).
        # _pump_period_seconds: seconds accumulated since the last gas event, used
        #   to compute the per-period pump kWh input to the HTC sample.
        # _pump_since: timestamp when any-TRV-heating epoch began (or None).
        self._pump_on_seconds_today: float = 0.0
        self._pump_period_seconds: float = 0.0
        self._pump_since: datetime | None = None

        # Daily accumulators (reset at midnight)
        self._daily_gas_kwh: float = 0.0
        self._daily_outdoor_temp_samples: list[float] = []

        # Calculated / published values (None = not yet available)
        self.htc: float | None = None
        self.hdd_today: float | None = None
        self.kwh_per_hdd_today: float | None = None
        self.pump_electricity_kwh: float = 0.0
        self.weighted_internal_temp: float | None = None

        # Listener registry: tag → list of HA state-write callables
        self._listeners: dict[str, list[Callable]] = {}

        # HA unsubscribe handles
        self._unsubs: list[Callable] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_discover_entities(self) -> None:
        """Discover and initialise all trackable entities."""
        self.trv_entities = discover_trv_entities(self._hass)
        self.room_temp_entities = discover_room_temp_sensors(self._hass)
        self.battery_entities = discover_battery_sensors(self._hass)

        for entity_id in self.trv_entities:
            self._trv[entity_id] = TRVTracker()
        for entity_id in self.battery_entities:
            self._battery[entity_id] = BatteryTracker()
        for entity_id in self.room_temp_entities:
            self._room_temps[entity_id] = None

        # Seed latest values from current state
        self._seed_initial_values()

    async def async_start(self) -> None:
        """Register all HA event listeners and periodic timers."""
        # Gas sensor — triggers HTC + room energy split
        if self.gas_kwh_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    [self.gas_kwh_sensor],
                    self._handle_gas_state_change,
                )
            )

        # External temp sensor
        if self.external_temp_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    [self.external_temp_sensor],
                    self._handle_external_temp_change,
                )
            )

        # Wind speed sensor
        if self.wind_speed_sensor:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    [self.wind_speed_sensor],
                    self._handle_wind_speed_change,
                )
            )

        # TRV climate entities
        if self.trv_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    self.trv_entities,
                    self._handle_trv_state_change,
                )
            )

        # Room temperature sensors
        if self.room_temp_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    self.room_temp_entities,
                    self._handle_room_temp_change,
                )
            )

        # Battery sensors
        if self.battery_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass,
                    self.battery_entities,
                    self._handle_battery_change,
                )
            )

        # 1-minute tick for pump electricity & heating-minutes counters
        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._handle_minute_tick,
                timedelta(minutes=1),
            )
        )

        # Midnight reset for daily accumulators and HDD finalisation.
        # async_track_time_change fires at *local* midnight so the daily
        # boundary aligns with the user's clock (e.g. BST in summer UK).
        self._unsubs.append(
            async_track_time_change(
                self._hass,
                self._handle_midnight,
                hour=0,
                minute=0,
                second=0,
            )
        )

    async def async_stop(self) -> None:
        """Cancel all HA subscriptions."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def add_listener(self, tag: str, callback_fn: Callable) -> None:
        """Register a callback to be called when data tagged *tag* changes."""
        self._listeners.setdefault(tag, []).append(callback_fn)

    def remove_listener(self, tag: str, callback_fn: Callable) -> None:
        """Unregister a previously added callback (e.g. on entity removal)."""
        listeners = self._listeners.get(tag)
        if listeners:
            try:
                listeners.remove(callback_fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @callback
    def _handle_gas_state_change(self, event: Any) -> None:
        new_state: State | None = event.data.get("new_state")
        if not _is_valid_state(new_state):
            return

        try:
            new_kwh = float(new_state.state)
        except (ValueError, TypeError):
            return

        now = dt_util.utcnow()

        if self._last_gas_kwh is not None and self._last_gas_ts is not None:
            if new_kwh < self._last_gas_kwh:
                _LOGGER.warning(
                    "EcoComfort Sync: gas counter appears to have reset "
                    "(old=%.3f kWh, new=%.3f kWh). "
                    "Energy consumed since last reading will be unaccounted.",
                    self._last_gas_kwh,
                    new_kwh,
                )
            delta_kwh = max(0.0, new_kwh - self._last_gas_kwh)
            period_hours = (now - self._last_gas_ts).total_seconds() / 3600.0

            if delta_kwh > 0 and period_hours > 0:
                self._daily_gas_kwh += delta_kwh

                # Snapshot pump kWh for *this specific period* (not cumulative)
                # and reset the period counter so future periods start clean.
                pump_kwh_period = self._flush_pump_period(now)

                # Snapshot inputs for HTC sample
                t_internal = self._calc_weighted_internal_temp()
                t_external = self._latest_external_temp
                wind_speed = self._latest_wind_speed or 0.0

                if t_internal is not None and t_external is not None:
                    delta_t = t_internal - t_external
                    if delta_t > 0:
                        sample = HtcSample(
                            timestamp=now,
                            gas_kwh_delta=delta_kwh,
                            pump_kwh=pump_kwh_period,
                            t_internal=t_internal,
                            t_external=t_external,
                            wind_speed=wind_speed,
                            period_hours=period_hours,
                        )
                        self._htc_buffer.append(sample)
                        self._prune_24h_buffer()
                        self._recalculate_htc()

                # Proportional room energy split
                self._split_room_energy(delta_kwh)

                # Update HDD sensors with latest daily gas total
                self._notify("hdd")

        self._last_gas_kwh = new_kwh
        self._last_gas_ts = now

    @callback
    def _handle_external_temp_change(self, event: Any) -> None:
        new_state: State | None = event.data.get("new_state")
        if not _is_valid_state(new_state):
            return
        try:
            temp = float(new_state.state)
            self._latest_external_temp = temp
            # Accumulate every outdoor reading for intra-day HDD, not only
            # readings that coincide with a gas event.
            self._daily_outdoor_temp_samples.append(temp)
        except (ValueError, TypeError):
            self._latest_external_temp = None
        self._notify("hdd")

    @callback
    def _handle_wind_speed_change(self, event: Any) -> None:
        new_state: State | None = event.data.get("new_state")
        if not _is_valid_state(new_state):
            return
        try:
            self._latest_wind_speed = float(new_state.state)
        except (ValueError, TypeError):
            self._latest_wind_speed = None

    @callback
    def _handle_trv_state_change(self, event: Any) -> None:
        entity_id: str = event.data["entity_id"]
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return

        new_action = new_state.attributes.get("hvac_action")
        tracker = self._trv.get(entity_id)
        if tracker is None:
            tracker = TRVTracker()
            self._trv[entity_id] = tracker

        now = dt_util.utcnow()
        prev_action = tracker.last_hvac_action

        # Short-cycle detection: idle → heating transition
        if prev_action == HVAC_ACTION_IDLE and new_action == HVAC_ACTION_HEATING:
            tracker.short_cycle_events.append(now)

        # Prune stale short-cycle events (older than 1 h)
        cutoff = now - timedelta(seconds=WINDOW_1H)
        while (
            tracker.short_cycle_events
            and tracker.short_cycle_events[0] < cutoff
        ):
            tracker.short_cycle_events.popleft()

        # Track heating start for fine-grained allocation counter.
        # NOTE: heating_minutes_today (display) is written ONLY by the
        # minute tick to avoid double-counting with this event path.
        if new_action == HVAC_ACTION_HEATING and prev_action != HVAC_ACTION_HEATING:
            tracker.heating_since = now
        elif new_action != HVAC_ACTION_HEATING and tracker.heating_since is not None:
            # Accumulate fractional minutes into the allocation counter only
            elapsed_minutes = (
                (now - tracker.heating_since).total_seconds() / 60.0
            )
            tracker.heating_minutes_since_gas += elapsed_minutes
            tracker.heating_since = None

        tracker.last_hvac_action = new_action

        # Update pump continuity
        self._update_pump_continuity(now)

        # Notify per-room sensors
        self._notify(f"room_{entity_id}")

    @callback
    def _handle_room_temp_change(self, event: Any) -> None:
        entity_id: str = event.data["entity_id"]
        new_state: State | None = event.data.get("new_state")
        if _is_valid_state(new_state):
            try:
                self._room_temps[entity_id] = float(new_state.state)
            except (ValueError, TypeError):
                self._room_temps[entity_id] = None
        else:
            self._room_temps[entity_id] = None

        new_internal = self._calc_weighted_internal_temp()
        self.weighted_internal_temp = new_internal
        self._notify("internal_temp")

    @callback
    def _handle_battery_change(self, event: Any) -> None:
        entity_id: str = event.data["entity_id"]
        new_state: State | None = event.data.get("new_state")
        if not _is_valid_state(new_state):
            return
        try:
            float(new_state.state)  # validate parseable
        except ValueError:
            pass
        # Battery drain is computed at midnight; live state changes just
        # ensure we have the latest value when midnight arrives.

    @callback
    def _handle_minute_tick(self, _now: datetime) -> None:
        """Called every minute to update heating-minute display counters and pump kWh.

        Only ``heating_minutes_today`` (display) is incremented here.
        ``heating_minutes_since_gas`` is managed exclusively by the
        event-based handlers (``_handle_trv_state_change`` on transition,
        ``_split_room_energy`` flush on gas event) to avoid double-counting.

        The HTC buffer is also pruned here so stale samples are evicted even
        when gas stops reporting (e.g. summer, sensor offline).
        """
        # Increment display counter for TRVs currently heating and push updates
        for entity_id, tracker in self._trv.items():
            if tracker.last_hvac_action == HVAC_ACTION_HEATING:
                tracker.heating_minutes_today += 1.0
                self._notify(f"room_{entity_id}")

        # Refresh pump kWh display from event-based tracker (no double-counting)
        self.pump_electricity_kwh = self._calc_current_pump_kwh_today()
        self._notify("pump")

        # Prune stale HTC samples so the value goes None when heating stops
        before = len(self._htc_buffer)
        self._prune_24h_buffer()
        if len(self._htc_buffer) != before:
            self._recalculate_htc()
            self._notify("htc")

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        """Midnight: finalise daily HDD + kWh/HDD, snapshot batteries, reset."""
        midnight_now = dt_util.utcnow()

        # Finalise HDD
        if self._daily_outdoor_temp_samples:
            avg_outdoor = sum(self._daily_outdoor_temp_samples) / len(
                self._daily_outdoor_temp_samples
            )
            self.hdd_today = max(0.0, self.t_base - avg_outdoor)
        else:
            self.hdd_today = None

        if self.hdd_today and self.hdd_today > 0:
            self.kwh_per_hdd_today = self._daily_gas_kwh / self.hdd_today
        else:
            self.kwh_per_hdd_today = None

        self._notify("hdd")

        # Snapshot battery levels
        today = dt_util.now().date()
        for entity_id, bat_tracker in self._battery.items():
            state = self._hass.states.get(entity_id)
            if state is None or not _is_valid_state(state):
                continue
            try:
                level = float(state.state)
            except (ValueError, TypeError):
                continue
            bat_tracker.daily_snapshots.append((today, level))
            # Compute drain rate from snapshots.
            # Only *drain* days count (battery-replacement / recharge days
            # where the level increased are excluded from the average so they
            # don't suppress the reported drain rate).
            snaps = list(bat_tracker.daily_snapshots)
            if len(snaps) >= 2:
                drain_days = [
                    snaps[i][1] - snaps[i + 1][1]
                    for i in range(len(snaps) - 1)
                    if snaps[i][1] > snaps[i + 1][1]  # only genuine drain days
                ]
                bat_tracker.drain_rate_per_day = (
                    sum(drain_days) / len(drain_days) if drain_days else 0.0
                )
            self._notify(f"battery_{entity_id}")

        # Reset daily accumulators
        self._daily_gas_kwh = 0.0
        self._daily_outdoor_temp_samples.clear()
        # Handle any pump epoch straddling midnight.  Compute how many seconds
        # of the active epoch belong to the *new* day (i.e. from midnight
        # onwards) and carry those forward.  This must be calculated *before*
        # zeroing _pump_on_seconds_today.
        pump_carry_seconds = 0.0
        if self._pump_since is not None:
            # Seconds before midnight were yesterday's; discard them.
            # Reset epoch start to midnight so new-day accumulates cleanly.
            pump_carry_seconds = 0.0  # nothing has elapsed yet in new day
            self._pump_since = midnight_now
        self._pump_on_seconds_today = pump_carry_seconds
        self._pump_period_seconds = 0.0
        for entity_id, tracker in self._trv.items():
            tracker.heating_minutes_today = 0.0
            tracker.energy_kwh_today = 0.0
            self._notify(f"room_{entity_id}")

        # Reset pump display counter and notify so the sensor shows 0 immediately
        self.pump_electricity_kwh = 0.0
        self._notify("pump")

    # ------------------------------------------------------------------
    # Calculations
    # ------------------------------------------------------------------

    def _recalculate_htc(self) -> None:
        samples = list(self._htc_buffer)
        if not samples:
            self.htc = None
            self._notify("htc")
            return

        total_hours = sum(s.period_hours for s in samples)
        if total_hours <= 0:
            self.htc = None
            self._notify("htc")
            return

        total_useful_energy_kwh = sum(
            s.gas_kwh_delta * self.boiler_efficiency + s.pump_kwh for s in samples
        )
        # Time-weighted average ΔT
        weighted_delta_t = (
            sum(s.period_hours * (s.t_internal - s.t_external) for s in samples)
            / total_hours
        )

        if weighted_delta_t <= 0:
            self.htc = None
            self._notify("htc")
            return

        # HTC in W/°C  (kWh / (°C·h) * 1000 = W/°C)
        htc_base = total_useful_energy_kwh / (weighted_delta_t * total_hours) * 1000.0

        # Wind adjustment
        avg_wind = (
            sum(s.wind_speed * s.period_hours for s in samples) / total_hours
        )
        self.htc = htc_base * (1.0 + self.wind_factor * avg_wind)
        self._notify("htc")

    def _split_room_energy(self, delta_kwh: float) -> None:
        """Proportionally allocate gas delta to rooms by heating-minute share.

        Any TRVs that are mid-heating-run when this is called have their
        in-progress minutes flushed into the period counter first so the
        split is accurate.  The heating epoch restarts from *now* so future
        minutes are only counted once.

        If *no* TRV recorded any heating this period the gas delta is not
        attributed to any room (this can happen at start-up or during
        periods of standby heat-loss).
        """
        now = dt_util.utcnow()

        # Flush any in-progress heating runs into the allocation counter.
        # heating_minutes_today is deliberately NOT updated here; the minute
        # tick is its sole writer.
        for tracker in self._trv.values():
            if tracker.heating_since is not None:
                elapsed_minutes = (
                    (now - tracker.heating_since).total_seconds() / 60.0
                )
                tracker.heating_minutes_since_gas += elapsed_minutes
                tracker.heating_since = now  # epoch continues; restart from now

        total_mins = sum(t.heating_minutes_since_gas for t in self._trv.values())

        if total_mins == 0:
            _LOGGER.debug(
                "EcoComfort Sync: gas increment of %.4f kWh occurred but no TRV was "
                "heating during this period — energy not allocated to any room.",
                delta_kwh,
            )
            for tracker in self._trv.values():
                tracker.heating_minutes_since_gas = 0.0
            return

        for entity_id, tracker in self._trv.items():
            share = tracker.heating_minutes_since_gas / total_mins
            tracker.energy_kwh_today += delta_kwh * share
            tracker.heating_minutes_since_gas = 0.0
            self._notify(f"room_{entity_id}")

    def _calc_weighted_internal_temp(self) -> float | None:
        """Return a simple average of all known room temperatures."""
        valid = [v for v in self._room_temps.values() if v is not None]
        if not valid:
            return None
        return sum(valid) / len(valid)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prune_24h_buffer(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(seconds=WINDOW_24H)
        while self._htc_buffer and self._htc_buffer[0].timestamp < cutoff:
            self._htc_buffer.popleft()

    def _any_trv_heating(self) -> bool:
        return any(
            t.last_hvac_action == HVAC_ACTION_HEATING for t in self._trv.values()
        )

    def _update_pump_continuity(self, now: datetime) -> None:
        """Accumulate pump-on seconds (event-based) when TRVs start/stop heating.

        This is the *only* place pump seconds are accumulated — the minute
        tick only reads the result, ensuring no double-counting.
        """
        if self._any_trv_heating():
            if self._pump_since is None:
                self._pump_since = now
        else:
            if self._pump_since is not None:
                elapsed = (now - self._pump_since).total_seconds()
                self._pump_on_seconds_today += elapsed
                self._pump_period_seconds += elapsed
                self._pump_since = None

    def _calc_current_pump_kwh_today(self) -> float:
        """Compute today's pump kWh including any currently active heating epoch."""
        seconds = self._pump_on_seconds_today
        if self._pump_since is not None:
            seconds += (dt_util.utcnow() - self._pump_since).total_seconds()
        return seconds / 3600.0 * self.pump_wattage / 1000.0

    def _flush_pump_period(self, now: datetime) -> float:
        """Return period pump kWh since last gas event and reset the period counter.

        If a heating epoch is currently active, its elapsed seconds up to *now*
        are captured for the period and the epoch is restarted from *now* so
        future accumulation belongs to the next period.
        """
        period_seconds = self._pump_period_seconds
        if self._pump_since is not None:
            elapsed = (now - self._pump_since).total_seconds()
            period_seconds += elapsed
            self._pump_on_seconds_today += elapsed
            self._pump_since = now  # epoch restarts for the next gas period
        self._pump_period_seconds = 0.0
        return period_seconds / 3600.0 * self.pump_wattage / 1000.0

    def get_intra_day_hdd(self) -> float | None:
        """Return a running HDD estimate for today using outdoor temp samples so far.

        Falls back to the last finalised daily value if no intra-day samples
        exist (e.g. early morning before the first gas event).
        """
        if self._daily_outdoor_temp_samples:
            avg = sum(self._daily_outdoor_temp_samples) / len(
                self._daily_outdoor_temp_samples
            )
            return max(0.0, self.t_base - avg)
        return self.hdd_today  # may be None

    def get_intra_day_kwh_per_hdd(self) -> float | None:
        """Return a running kWh/HDD estimate for today.

        Returns None if HDD is zero (too warm to heat) or unavailable.
        """
        hdd = self.get_intra_day_hdd()
        if hdd is None or hdd <= 0 or self._daily_gas_kwh <= 0:
            return None
        return self._daily_gas_kwh / hdd

    def _seed_initial_values(self) -> None:
        """Populate latest values from the current HA state machine."""
        for entity_id in self.room_temp_entities:
            state = self._hass.states.get(entity_id)
            if _is_valid_state(state):
                try:
                    self._room_temps[entity_id] = float(state.state)
                except (ValueError, TypeError):
                    pass

        state = self._hass.states.get(self.external_temp_sensor)
        if _is_valid_state(state):
            try:
                self._latest_external_temp = float(state.state)
            except (ValueError, TypeError):
                pass

        state = self._hass.states.get(self.wind_speed_sensor)
        if _is_valid_state(state):
            try:
                self._latest_wind_speed = float(state.state)
            except (ValueError, TypeError):
                pass

        state = self._hass.states.get(self.gas_kwh_sensor)
        if _is_valid_state(state):
            try:
                self._last_gas_kwh = float(state.state)
                self._last_gas_ts = dt_util.utcnow()
            except (ValueError, TypeError):
                pass

        now = dt_util.utcnow()
        for entity_id, tracker in self._trv.items():
            state = self._hass.states.get(entity_id)
            if state is not None:
                action = state.attributes.get("hvac_action")
                tracker.last_hvac_action = action
                # If the TRV is already heating at startup, record the epoch
                # start so the first _split_room_energy flush is accurate.
                if action == HVAC_ACTION_HEATING:
                    tracker.heating_since = now

        # Seed pump epoch for any TRVs already heating at startup
        self._update_pump_continuity(now)

        self.weighted_internal_temp = self._calc_weighted_internal_temp()

    def _notify(self, tag: str) -> None:
        """Fire all registered callbacks for *tag*."""
        for cb in self._listeners.get(tag, []):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("EcoComfort Sync: error in listener for tag %s", tag)

    # ------------------------------------------------------------------
    # Read-only accessors used by sensor entities
    # ------------------------------------------------------------------

    def get_short_cycle_count(self, trv_entity_id: str) -> int:
        tracker = self._trv.get(trv_entity_id)
        if tracker is None:
            return 0
        now = dt_util.utcnow()
        cutoff = now - timedelta(seconds=WINDOW_1H)
        while (
            tracker.short_cycle_events
            and tracker.short_cycle_events[0] < cutoff
        ):
            tracker.short_cycle_events.popleft()
        return len(tracker.short_cycle_events)

    def get_room_energy_kwh(self, trv_entity_id: str) -> float | None:
        tracker = self._trv.get(trv_entity_id)
        return tracker.energy_kwh_today if tracker else None

    def get_heating_minutes_today(self, trv_entity_id: str) -> float | None:
        tracker = self._trv.get(trv_entity_id)
        return tracker.heating_minutes_today if tracker else None

    def get_battery_drain_rate(self, battery_entity_id: str) -> float | None:
        tracker = self._battery.get(battery_entity_id)
        if tracker is None:
            return None
        return tracker.drain_rate_per_day


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _is_valid_state(state: State | None) -> bool:
    return (
        state is not None
        and state.state is not None
        and state.state not in ("unknown", "unavailable", "")
    )
