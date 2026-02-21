"""Shared pytest fixtures for EcoComfort Sync tests."""
from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests in this package."""
    yield


# ---------------------------------------------------------------------------
# Default config dict used in multiple tests
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "gas_kwh_sensor": "sensor.gas_meter_kwh",
    "external_temp_sensor": "sensor.external_temperature",
    "wind_speed_sensor": "sensor.wind_speed",
    "boiler_efficiency": 0.90,
    "pump_wattage": 45,
    "t_base": 18.0,
    "wind_factor": 0.1,
}
