"""Config flow for ESPHome Update Manager."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode

from .const import DOMAIN, DEFAULT_MAX_LOG_BACKUPS


class ESPHomeUpdateManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="ESPHome Update Manager",
                data={},
            )

        return self.async_show_form(step_id="user")

    async def async_step_onboarding(self, data=None):
        """Handle onboarding (auto-discovery)."""
        return self.async_create_entry(
            title="ESPHome Update Manager",
            data={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ESPHomeUpdateManagerOptionsFlow(config_entry)


class ESPHomeUpdateManagerOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """Handle options flow."""

    async def async_step_init(self, user_input=None):
        """Handle options step."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_max = self.config_entry.options.get(
            "max_log_backups", DEFAULT_MAX_LOG_BACKUPS
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("max_log_backups", default=current_max): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=50,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }),
        )
