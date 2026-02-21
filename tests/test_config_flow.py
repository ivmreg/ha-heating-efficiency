"""Integration tests for EcoComfort Sync config flow."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ecocomfort_sync.const import DOMAIN
from tests.conftest import DEFAULT_CONFIG


def _mock_state(entity_id: str, state: str = "1.0", attributes: dict | None = None):
    """Return a minimal mock state object."""
    s = MagicMock()
    s.entity_id = entity_id
    s.state = state
    s.attributes = attributes or {}
    return s


def _hass_with_states(states: list) -> MagicMock:
    """Return a mock hass whose state machine contains *states*."""
    hass = MagicMock()
    state_map = {s.entity_id: s for s in states}
    hass.states.get.side_effect = state_map.get
    return hass


# ---------------------------------------------------------------------------
# _validate_sensors helper
# ---------------------------------------------------------------------------

class TestValidateSensors:
    """Test the config-flow sensor validation helper."""

    def test_valid_sensors_no_errors(self):
        from custom_components.ecocomfort_sync.config_flow import _validate_sensors
        states_machine = MagicMock()
        good_state = MagicMock()
        good_state.state = "1.0"
        states_machine.get.return_value = good_state

        errors = _validate_sensors(states_machine, DEFAULT_CONFIG)
        assert errors == {}

    def test_missing_entity_flagged(self):
        from custom_components.ecocomfort_sync.config_flow import _validate_sensors
        states_machine = MagicMock()
        states_machine.get.return_value = None  # entity not found

        errors = _validate_sensors(states_machine, DEFAULT_CONFIG)
        assert "gas_kwh_sensor" in errors
        assert errors["gas_kwh_sensor"] == "entity_not_found"

    def test_unavailable_entity_flagged(self):
        from custom_components.ecocomfort_sync.config_flow import _validate_sensors
        unavail = MagicMock()
        unavail.state = "unavailable"
        states_machine = MagicMock()
        states_machine.get.return_value = unavail

        errors = _validate_sensors(states_machine, DEFAULT_CONFIG)
        assert errors.get("gas_kwh_sensor") == "entity_unavailable"

    def test_unknown_state_flagged(self):
        from custom_components.ecocomfort_sync.config_flow import _validate_sensors
        unknown = MagicMock()
        unknown.state = "unknown"
        states_machine = MagicMock()
        states_machine.get.return_value = unknown

        errors = _validate_sensors(states_machine, DEFAULT_CONFIG)
        assert errors.get("gas_kwh_sensor") == "entity_unavailable"


# ---------------------------------------------------------------------------
# Config flow steps (require hass fixture from pytest-homeassistant)
# ---------------------------------------------------------------------------

def _register_config_entities(hass) -> None:
    """Register all sensor entities from DEFAULT_CONFIG as valid states."""
    for key in ("gas_kwh_sensor", "external_temp_sensor", "wind_speed_sensor"):
        hass.states.async_set(DEFAULT_CONFIG[key], "1.0")


@pytest.mark.asyncio
async def test_config_flow_user_step_success(hass):
    """Happy-path: valid sensors → entry is created."""
    _register_config_entities(hass)

    with patch(
        "custom_components.ecocomfort_sync.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=DEFAULT_CONFIG
        )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "EcoComfort Sync"
    assert result2["data"]["gas_kwh_sensor"] == DEFAULT_CONFIG["gas_kwh_sensor"]


@pytest.mark.asyncio
async def test_config_flow_user_step_entity_not_found(hass):
    """Unknown entity (not in state machine) → form is re-shown with an error."""
    # Do NOT register any states so entities are "not found"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=DEFAULT_CONFIG
    )

    assert result2["type"] == FlowResultType.FORM
    assert "gas_kwh_sensor" in result2.get("errors", {})


@pytest.mark.asyncio
async def test_config_flow_aborts_if_already_configured(hass):
    """Second setup attempt is aborted."""
    _register_config_entities(hass)

    with patch(
        "custom_components.ecocomfort_sync.async_setup_entry",
        return_value=True,
    ):
        # First setup
        r1 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        r1 = await hass.config_entries.flow.async_configure(
            r1["flow_id"], user_input=DEFAULT_CONFIG
        )
        assert r1["type"] == FlowResultType.CREATE_ENTRY

        # Second attempt — init shows the form again
        r2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert r2["type"] == FlowResultType.FORM

        # Submitting the form should abort because the unique_id is taken
        r2_submitted = await hass.config_entries.flow.async_configure(
            r2["flow_id"], user_input=DEFAULT_CONFIG
        )
    assert r2_submitted["type"] == FlowResultType.ABORT
    assert r2_submitted["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_options_flow_updates_values(hass):
    """Options flow should update boiler_efficiency."""
    _register_config_entities(hass)

    with patch(
        "custom_components.ecocomfort_sync.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=DEFAULT_CONFIG
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]

    # Open options flow
    with patch(
        "custom_components.ecocomfort_sync.async_setup_entry",
        return_value=True,
    ):
        opt_result = await hass.config_entries.options.async_init(entry.entry_id)
        new_options = {**DEFAULT_CONFIG, "boiler_efficiency": 0.95}
        opt_result2 = await hass.config_entries.options.async_configure(
            opt_result["flow_id"], user_input=new_options
        )

    assert opt_result2["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options.get("boiler_efficiency") == pytest.approx(0.95)
