# EcoComfort Sync

A [HACS](https://hacs.xyz/) custom integration for Home Assistant that derives building physics metrics and per-room heating efficiency data from your existing smart-home devices.

It consumes readings from Tado TRVs, room temperature sensors (e.g. Qingping CO₂), a gas/electricity energy meter (e.g. Hildebrand MQTT), an outdoor temperature sensor and a wind-speed source (e.g. Met Office), and produces a suite of calculated sensors that tell you how well your building and boiler are performing — updated in real time, no cloud dependency, fully local.

---

## Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Sensors reference](#sensors-reference)
- [How it works](#how-it-works)
- [Entity discovery](#entity-discovery)
- [Automation examples](#automation-examples)
- [Known limitations](#known-limitations)
- [Development](#development)

---

## Prerequisites

| Requirement | Minimum version |
|---|---|
| Home Assistant | 2024.1.0 |
| HACS | 2.x |

The following integrations (or equivalent entity providers) must already be set up and producing entities before you configure EcoComfort Sync:

| Role | Example integration | Required entity |
|---|---|---|
| Gas / electricity energy meter | Hildebrand MQTT / any energy integration | Sensor with `device_class: energy` (kWh) |
| Outdoor temperature | Met Office, OpenWeatherMap, any weather integration | Sensor with `device_class: temperature` |
| Wind speed | Met Office / any weather integration | Sensor with `device_class: wind_speed` |
| Smart TRVs | [Tado](https://www.home-assistant.io/integrations/tado/) | `climate.tado_smart_radiator_thermostat_*` entities |
| Room temperature sensors | Qingping CO₂ / any temperature sensor | Sensors with `device_class: temperature` |
| Battery sensors (optional) | Any device battery sensor | `sensor.*_battery` entities |

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → three-dot menu → **Custom repositories**.
2. Add `https://github.com/ivmreg/ha-heating-efficiency` with category **Integration**.
3. Search for **EcoComfort Sync** and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/ecocomfort_sync/` directory into your HA config directory:
   ```
   <config>/custom_components/ecocomfort_sync/
   ```
2. Restart Home Assistant.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **EcoComfort Sync**.

Only one instance of the integration can be configured at a time.

### Required fields

| Field | Description | Example entity |
|---|---|---|
| **Gas kWh Sensor** | Cumulative energy consumption sensor (`device_class: energy`). This is the primary fuel input for HTC and efficiency calculations. | `sensor.gas_consumed_cumulative` |
| **External Temperature Sensor** | Outdoor temperature (`device_class: temperature`). Used for HDD and HTC delta-T. | `sensor.met_office_temperature` |
| **Wind Speed Sensor** | Outdoor wind speed (`device_class: wind_speed`). Used to apply a fabric-infiltration correction to HTC. | `sensor.met_office_wind_speed` |

### Optional fields

| Field | Default | Range | Description |
|---|---|---|---|
| **Boiler Efficiency** | `0.90` | 0.10 – 1.10 | Fraction of gas energy delivered as heat. Use seasonal efficiency from your boiler datasheet (e.g. 0.88 for an older condensing boiler). |
| **Pump Wattage** | `45 W` | 1 – 500 W | Nameplate wattage of the central-heating circulation pump. |
| **Base Temperature (T_base)** | `18 °C` | 5 – 30 °C | Threshold below which Heating Degree Days are calculated. |
| **Wind Factor** | `0.10` | 0.00 – 1.00 | Sensitivity of the HTC wind-speed correction. Increase for leakier buildings. |

Settings can be changed post-setup via **Configure** on the integration card.

---

## Sensors reference

### Building-level sensors (5)

| Entity | Unit | Description |
|---|---|---|
| `sensor.ecocomfort_sync_building_heat_transfer_coefficient` | W/°C | Rolling 24-hour Heat Transfer Coefficient (HTC). Measures how quickly the building loses heat per unit of temperature difference between inside and outside. Lower is better-insulated. Unavailable until the first gas reading is received. |
| `sensor.ecocomfort_sync_heating_degree_days` | °C·day | Heating Degree Days accumulated since local midnight, updated live from continuous outdoor temperature readings. |
| `sensor.ecocomfort_sync_gas_kwh_per_hdd` | kWh/HDD | Gas consumed today divided by today's HDD — a normalised efficiency metric (lower = more efficient). |
| `sensor.ecocomfort_sync_boiler_pump_electricity_today` | kWh | Electricity consumed by the circulation pump today, derived from pump wattage × running time. Resets at local midnight. |
| `sensor.ecocomfort_sync_weighted_internal_temperature` | °C | Simple average of all discovered room temperature sensors. |

### Per-TRV sensors (3 per discovered TRV)

One set is created for every `climate.tado_smart_radiator_thermostat_*` entity found at startup.

| Entity pattern | Unit | Description |
|---|---|---|
| `sensor.ecocomfort_sync_{room}_energy_today` | kWh | Gas energy attributed to this room today, split proportionally by heating minutes. Resets at local midnight. |
| `sensor.ecocomfort_sync_{room}_short_cycling_1h` | cycles | Number of heat-on → heat-off cycles in the last 60 minutes. Attributes include `is_short_cycling` (bool), `cycle_count`, and `threshold`. |
| `sensor.ecocomfort_sync_{room}_heating_minutes_today` | min | Minutes the TRV has been calling for heat today. Resets at local midnight. |

### Per-battery sensors (1 per discovered battery sensor)

One sensor is created for every `sensor.*_battery` entity found at startup.

| Entity pattern | Unit | Description |
|---|---|---|
| `sensor.ecocomfort_sync_{device}_battery_drain_rate` | %/day | Average daily battery drain rate. Attributes include `is_premature_drain` (bool), `drain_threshold_pct_per_day` (default 5 %/day), and `source_entity`. Recharge/replacement days are excluded from the average. |

---

## How it works

### Heat Transfer Coefficient (HTC)

$$\text{HTC}_{base} = \frac{E_{gas} \times \eta_{boiler} + E_{elec}}{(T_{internal} - T_{external}) \times \Delta t}$$

where $E_{gas}$ is gas consumed (kWh), $\eta_{boiler}$ is boiler efficiency, $E_{elec}$ is pump electricity (kWh), $T_{internal}$ is the weighted mean room temperature, $T_{external}$ is outdoor temperature, and $\Delta t$ is the period in hours.

A wind-speed correction is then applied:

$$\text{HTC}_{adjusted} = \text{HTC}_{base} \times (1 + k_{wind} \times v_{wind})$$

HTC samples are accumulated in a rolling 24-hour buffer. The reported value is the mean across all samples in that window.

### Per-room energy split

When gas is consumed, the kWh is divided among all rooms whose TRV was calling for heat during that period, in proportion to each room's heating minutes. Rooms not calling for heat receive no allocation.

### Heating Degree Days (HDD)

$$\text{HDD} = \max(0,\; T_{base} - \bar{T}_{outdoor,day})$$

Computed from the simple daily average of all outdoor temperature readings received since local midnight, and reset to zero at the next midnight.

### Short cycling detection

A TRV is considered to be short cycling if it has completed ≥ 6 heat-on/heat-off cycles within any rolling 60-minute window.

### Battery drain rate

The drain rate (% per day) is computed from daily snapshot differences. Days where the battery level increases (charging or replacement) are excluded so that rechargeable or swapped batteries don't deflate the reported drain. A drain rate exceeding 5 %/day is flagged as `is_premature_drain`.

---

## Entity discovery

EcoComfort Sync auto-discovers the following entities at startup. No manual entity mapping is needed.

| Role | Discovery pattern |
|---|---|
| Smart TRVs | Entity IDs matching `^climate\.tado_smart_radiator_thermostat` |
| Room temperature sensors | Entity IDs matching `sensor.qp_sensor_*` or `sensor.co2_meter*` **and** `device_class == "temperature"` |
| Battery sensors | Entity IDs matching `sensor.*_battery`, or any sensor with `device_class == "battery"` |

Discovery runs once at integration startup. If you add new TRVs or sensors, restart Home Assistant (or reload the integration) to pick them up.

---

## Automation examples

### Alert on short cycling

```yaml
alias: "Alert: TRV short cycling"
trigger:
  - platform: state
    entity_id: sensor.ecocomfort_sync_bedroom_short_cycling_1h
condition:
  - condition: template
    value_template: "{{ state_attr('sensor.ecocomfort_sync_bedroom_short_cycling_1h', 'is_short_cycling') }}"
action:
  - service: notify.mobile_app
    data:
      message: >
        Bedroom TRV is short cycling
        ({{ states('sensor.ecocomfort_sync_bedroom_short_cycling_1h') }} cycles in the last hour).
```

### Alert on premature battery drain

```yaml
alias: "Alert: Premature battery drain"
trigger:
  - platform: template
    value_template: >
      {{ states.sensor
         | selectattr('object_id', 'match', 'ecocomfort_sync_.*_battery_drain_rate')
         | selectattr('attributes.is_premature_drain', 'eq', true)
         | list | count > 0 }}
action:
  - service: notify.mobile_app
    data:
      message: "One or more devices have unusually high battery drain rates."
```

### Daily efficiency report

```yaml
alias: "Daily heating efficiency report"
trigger:
  - platform: time
    at: "21:00:00"
action:
  - service: notify.mobile_app
    data:
      message: >
        Today's heating: {{ states('sensor.ecocomfort_sync_gas_kwh_per_hdd') }} kWh/HDD,
        HTC {{ states('sensor.ecocomfort_sync_building_heat_transfer_coefficient') }} W/°C,
        pump {{ states('sensor.ecocomfort_sync_boiler_pump_electricity_today') }} kWh.
```

---

## Known limitations

- **No persistence across restarts.** Daily accumulators (heating minutes, room energy, pump electricity, HDD) reset to zero whenever Home Assistant is restarted mid-day. This is a design trade-off to avoid stale data; long-term trend tracking should use the HA statistics engine (e.g. via `sensor` history or an energy dashboard).
- **Options flow resets daily state.** Saving changes via **Configure** triggers a full integration reload, which also resets in-memory daily state.
- **TRV discovery is Tado-specific.** Only `climate.tado_smart_radiator_thermostat_*` entities are matched. If you use a different TRV brand, you can adjust the pattern in `helpers.py` (`discover_trv_entities`).
- **Single instance only.** The integration can be configured once per HA instance.

---

## Development

```bash
git clone https://github.com/ivmreg/ha-heating-efficiency
cd ha-heating-efficiency
python -m venv .venv
source .venv/bin/activate
pip install -r requirements_dev.txt
pytest tests/ -v
```

The test suite uses `pytest-homeassistant-custom-component` and runs fully offline; no running HA instance is required.

---

## License

[MIT](LICENSE)
