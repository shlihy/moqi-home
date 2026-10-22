"""Install once; select devices with HA's native entity labels."""
from homeassistant import config_entries


class MoqiConfigFlow(config_entries.ConfigFlow, domain='moqi'):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id('moqi_household_gateway')
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title='Moqi',data={})
        return self.async_show_form(step_id='user')
