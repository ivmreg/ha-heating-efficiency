# Project: EcoComfort Sync (Home Assistant Custom Component)

## 1. Project Overview
You are an expert Python developer and Home Assistant (HA) integration specialist. Your task is to build a HACS-compatible custom integration called `ecocomfort_sync`. 
This integration calculates advanced building physics, heating efficiency, and hardware maintenance metrics by combining data from Tado smart radiator valves (TRVs), external room sensors (Qingping), Hildebrand MQTT gas consumption, and outdoor weather data.

## 2. Core Features & Mathematical Models
The integration must implement the following physical calculations as Home Assistant sensors.

### A. Total Building Heat Transfer Coefficient (HTC)
Calculates how much energy (in Watts) is required to maintain a 1°C difference between inside and outside, adjusted for wind infiltration.
* Formula: `HTC_base = ((E_gas_kwh * boiler_efficiency) + E_elec_kwh) / ((T_internal - T_external) * hours)`
* Wind Adjustment: `HTC_adjusted = HTC_base * (1 + (wind_factor * wind_speed))`
* Variables:
  * `E_gas_kwh`: Sourced directly from the user's Hildebrand MQTT gas kWh sensor.
  * `T_external`: Sourced from a local external temperature sensor.
  * `wind_speed`: Sourced from the Met Office weather integration.
  * `T_internal`: A weighted average calculated from the Qingping/CO2 temperature sensors.
* Logic: Use a rolling 24-hour window to smooth out boiler cycling and environmental delays.

### B. Proportional Room Energy Allocation
Estimates gas usage per room based on TRV demand time, as Tado does not expose valve position percentage.
* Logic: Track the `hvac_action` attribute (specifically `heating` vs `idle`) for each `climate.tado_*` entity. 
* Formula: `Room_Energy_kWh = Total_Gas_kWh_Increment * (Room_Heating_Minutes / Sum_Of_All_Rooms_Heating_Minutes)`
* Update Frequency: The Hildebrand gas sensor updates half-hourly. Listen for state changes on the Hildebrand kWh sensor. When it increments, calculate the proportional split for the last 30 minutes, update the individual room energy sensors, and reset the heating minute counters.

### C. Heating Degree Days (HDD)
Calculates the daily heating requirement to normalize energy efficiency.
* Formula: `HDD = max(0, T_base - T_outdoor_average_24h)`
* Logic: `T_base` should be a configurable threshold (default 18.0). Create a daily sensor that divides daily gas kWh by daily HDD to output `kwh_per_hdd`.

### D. Hardware Health & Maintenance
* Short Cycling: Count how many times a TRV changes its `hvac_action` from `idle` to `heating` within a 60-minute rolling window. Create a sensor that flags `True` or displays the count if > 6.
* Pump Electricity (`E_elec_kwh`): Track the total continuous time *any* TRV is in the `heating` state. Multiply this time by a fixed configurable `pump_wattage` (default 45W) to estimate the boiler pump's kWh usage.
* Battery Drain Velocity: Calculate the daily percentage drop for all `sensor.*_battery` entities to warn of premature drain.

## 3. Entity Ecosystem (User's Environment)
The integration should map these via `config_flow`:
* TRVs: `climate.tado_smart_radiator_thermostat_*` (Must track the `hvac_action` attribute).
* True Room Temperatures: `sensor.qp_sensor_*` and `sensor.co2_meter*`.
* Weather: Met Office Integration (for wind speed) + standalone external temp sensor.
* Energy: Hildebrand MQTT Gas Sensor (provides raw kWh cumulative).

## 4. Home Assistant Development Guidelines
* Async First: Use HA's `asyncio` event loop. Never use blocking I/O operations in the main loop.
* State Tracking: Use `async_track_state_change_