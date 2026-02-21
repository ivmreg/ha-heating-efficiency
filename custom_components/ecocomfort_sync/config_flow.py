"""Config flow for EcoComfort Sync."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BOILER_EFFICIENCY,
    CONF_EXTERNAL_TEMP_SENSOR,
    CONF_GAS_KWH_SENSOR,
    CONF_PUMP_WATTAGE,
    CONF_T_BASE,
    CONF_WIND_FACTOR,
    CONF_WIND_SPEED_SENSOR,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_PUMP_WATTAGE,
    DEFAULT_T_BASE,
    DEFAULT_WIND_FACTOR,
    DOMAIN,
    NAME,
)


def _build_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the configuration schema, optionally pre-filling defaults."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_GAS_KWH_SENSOR,
                default=d.get(CONF_GAS_KWH_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="energy",
                )
            ),
            vol.Required(
                CONF_EXTERNAL_TEMP_SENSOR,
                default=d.get(CONF_EXTERNAL_TEMP_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="temperature",
                )
            ),
            vol.Required(
                CONF_WIND_SPEED_SENSOR,
                default=d.get(CONF_WIND_SPEED_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class="wind_speed",
                )
            ),
            vol.Optional(
                CONF_BOILER_EFFICIENCY,
                default=d.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=1.0,
                    step=0.01,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_PUMP_WATTAGE,
                default=d.get(CONF_PUMP_WATTAGE, DEFAULT_PUMP_WATTAGE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=500,
                    step=1,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_T_BASE,
                default=d.get(CONF_T_BASE, DEFAULT_T_BASE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5.0,
                    max=30.0,
                    step=0.5,
                    unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_WIND_FACTOR,
                default=d.get(CONF_WIND_FACTOR, DEFAULT_WIND_FACTOR),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=1.0,
                    step=0.01,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


class EcoComfortSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup config flow for EcoComfort Sync."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the user-initiated setup step."""
        if user_input is not None:
            # Prevent duplicate entries
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(title=NAME, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EcoComfortSyncOptionsFlow:
        """Return an options flow handler so settings can be changed after setup."""
        return EcoComfortSyncOptionsFlow(config_entry)


class EcoComfortSyncOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to update integration options after initial setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle options update."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Pre-fill with current values (options override data for updated fields)
        current = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current),
        )
