"""EcoComfort Sync – Home Assistant custom integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .data_manager import EcoComfortDataManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EcoComfort Sync from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Merge static data with any options overrides
    config = {**entry.data, **entry.options}

    # Create and initialise the shared data manager
    manager = EcoComfortDataManager(hass, entry.entry_id, config)
    await manager.async_discover_entities()
    await manager.async_start()

    hass.data[DOMAIN][entry.entry_id] = manager

    # Reload the config entry automatically when options are updated
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        manager: EcoComfortDataManager | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if manager is not None:
            await manager.async_stop()
    return unload_ok
