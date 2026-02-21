"""Constants for the EcoComfort Sync integration."""

DOMAIN = "ecocomfort_sync"
NAME = "EcoComfort Sync"

# Config entry keys
CONF_GAS_KWH_SENSOR = "gas_kwh_sensor"
CONF_EXTERNAL_TEMP_SENSOR = "external_temp_sensor"
CONF_WIND_SPEED_SENSOR = "wind_speed_sensor"
CONF_BOILER_EFFICIENCY = "boiler_efficiency"
CONF_PUMP_WATTAGE = "pump_wattage"
CONF_T_BASE = "t_base"
CONF_WIND_FACTOR = "wind_factor"

# Defaults
DEFAULT_BOILER_EFFICIENCY = 0.90
DEFAULT_PUMP_WATTAGE = 45
DEFAULT_T_BASE = 18.0
DEFAULT_WIND_FACTOR = 0.1
# Battery drain rate above which a "premature drain" warning is issued (%/day)
DEFAULT_BATTERY_DRAIN_THRESHOLD = 5.0

# Sensor keys — building-level (unique per entry)
SENSOR_HTC = "building_htc"
SENSOR_HDD = "heating_degree_days"
SENSOR_KWH_PER_HDD = "kwh_per_hdd"
SENSOR_PUMP_ELECTRICITY = "pump_electricity"
SENSOR_WEIGHTED_INTERNAL_TEMP = "weighted_internal_temp"

# Sensor key prefixes — per-room (one instance per discovered TRV)
SENSOR_ROOM_ENERGY_PREFIX = "room_energy_kwh"
SENSOR_ROOM_SHORT_CYCLING_PREFIX = "room_short_cycling"
SENSOR_ROOM_HEATING_MINUTES_PREFIX = "room_heating_minutes_today"

# Sensor key prefix — per-battery device
SENSOR_BATTERY_DRAIN_PREFIX = "battery_drain_rate"

# Rolling window durations (seconds)
WINDOW_24H = 24 * 3600
WINDOW_30MIN = 30 * 60
WINDOW_1H = 3600

# Short-cycling threshold: flag if a TRV switches idle→heating more than this
# many times within WINDOW_1H
SHORT_CYCLE_THRESHOLD = 6

# TRV hvac_action states
HVAC_ACTION_HEATING = "heating"
HVAC_ACTION_IDLE = "idle"
