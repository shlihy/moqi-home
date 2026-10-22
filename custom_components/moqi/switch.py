"""HA-native execution switch, independent of model or habit configuration."""
from homeassistant.components.switch import SwitchEntity

from . import DOMAIN


async def async_setup_entry(hass,entry,async_add_entities):
    async_add_entities([ExecutionSwitch(hass.data[DOMAIN])])


class ExecutionSwitch(SwitchEntity):
    _attr_name = 'Moqi automatic execution'
    _attr_unique_id = 'moqi_execution_enabled'
    _attr_icon = 'mdi:home-automation'
    _attr_should_poll = False

    def __init__(self,gateway):
        self.gateway = gateway

    @property
    def is_on(self):
        return self.gateway.data['enabled']

    async def async_turn_on(self,**kwargs):
        await self.gateway.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self,**kwargs):
        await self.gateway.set_enabled(False)
        self.async_write_ha_state()
