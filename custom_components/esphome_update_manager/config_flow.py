"""Config flow for ESPHome Update Manager."""
import asyncio
import voluptuous as vol
import aiohttp
import logging

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    DOMAIN,
    DEFAULT_MAX_LOG_BACKUPS,
    CONF_DASHBOARD_URL,
    CONF_DASHBOARD_MODE,
    DASHBOARD_MODE_LOCAL,
    DASHBOARD_MODE_EXTERNAL,
)

_LOGGER = logging.getLogger(__name__)

async def _validate_dashboard_url(hass, url: str) -> tuple[bool, str | None]:
    """Validate that the dashboard URL is reachable and returns valid data.
    
    Returns: (valid, error_key)
    """
    if not url:
        return True, None
    
    # Ensure URL doesn't end with slash
    url = url.rstrip("/")
    
    try:
        session = async_get_clientsession(hass)
        async with session.get(f"{url}/devices", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "configured" in data:
                    return True, None
                return False, "invalid_response"
            elif resp.status == 401:
                return False, "auth_required"
            elif resp.status == 404:
                return False, "not_found"
            else:
                return False, "http_error"
    except aiohttp.ClientConnectorError:
        return False, "connection_failed"
    except aiohttp.ClientError:
        return False, "connection_error"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception:
        return False, "unknown_error"


class ESPHomeUpdateManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step - simple URL input."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors = {}
        display_url = ""

        if user_input is not None:
            raw_url = user_input.get(CONF_DASHBOARD_URL, "").strip()
            
            # Treat empty, "http://" or "https://" as "no URL"
            if raw_url in ("", "http://", "https://"):
                url = None
            else:
                url = raw_url
            
            # Validate if URL is provided
            if url:
                valid, error_msg = await _validate_dashboard_url(self.hass, url)
                if not valid:
                    errors["base"] = error_msg
            
            if not errors:
                # Determine mode based on URL presence
                mode = DASHBOARD_MODE_EXTERNAL if url else DASHBOARD_MODE_LOCAL
                title = "ESPHome Update Manager"
                
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_DASHBOARD_MODE: mode,
                        CONF_DASHBOARD_URL: url.rstrip("/") if url else None,
                    },
                    options={
                        "max_log_backups": user_input.get("max_log_backups", DEFAULT_MAX_LOG_BACKUPS),
                    },
                )
            else:
                # On error, keep the entered value (or reset to http:// if empty)
                display_url = raw_url if raw_url not in ("", "http://", "https://") else "http://"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_DASHBOARD_URL,
                    description={"suggested_value": display_url}
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional("max_log_backups", default=DEFAULT_MAX_LOG_BACKUPS): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=50,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }),
            errors=errors,
            description_placeholders={
                "example_url": "http://192.168.1.100:6052",
            },
        )

    async def async_step_onboarding(self, data=None):
        """Handle onboarding (auto-discovery)."""
        return self.async_create_entry(
            title="ESPHome Update Manager",
            data={
                CONF_DASHBOARD_MODE: DASHBOARD_MODE_LOCAL,
                CONF_DASHBOARD_URL: None,
            },
            options={
                "max_log_backups": DEFAULT_MAX_LOG_BACKUPS,
            },
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
        errors = {}

        # Current values
        current_url = self.config_entry.data.get(CONF_DASHBOARD_URL) or ""
        current_max = self.config_entry.options.get("max_log_backups", DEFAULT_MAX_LOG_BACKUPS)
        
        # Default to http:// if no URL configured
        display_url = current_url if current_url else "http://"

        if user_input is not None:
            raw_url = user_input.get(CONF_DASHBOARD_URL, "").strip()
            
            # Treat empty, "http://" or "https://" as "no URL" (cleared)
            if raw_url in ("", "http://", "https://"):
                url = None
            else:
                url = raw_url
            
            # Validate if URL is provided
            if url:
                valid, error_msg = await _validate_dashboard_url(self.hass, url)
                if not valid:
                    errors["base"] = error_msg
            
            if not errors:
                # Determine mode based on URL presence
                mode = DASHBOARD_MODE_EXTERNAL if url else DASHBOARD_MODE_LOCAL
                
                # Check if dashboard config changed
                old_url = self.config_entry.data.get(CONF_DASHBOARD_URL)
                dashboard_changed = (url != old_url)
                
                # Update config entry data AND title
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title="ESPHome Update Manager",
                    data={
                        CONF_DASHBOARD_MODE: mode,
                        CONF_DASHBOARD_URL: url.rstrip("/") if url else None,
                    },
                )
                
                # Schedule reload if dashboard config changed
                if dashboard_changed:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self.config_entry.entry_id)
                    )
                
                # Save options (max_log_backups)
                return self.async_create_entry(
                    title="",
                    data={
                        "max_log_backups": user_input.get("max_log_backups", DEFAULT_MAX_LOG_BACKUPS),
                    },
                )
            else:
                # On error, keep the entered value (or reset to http:// if empty)
                display_url = raw_url if raw_url not in ("", "http://", "https://") else "http://"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_DASHBOARD_URL, default=display_url): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional("max_log_backups", default=current_max): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=50,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }),
            errors=errors,
            description_placeholders={
                "example_url": "http://192.168.1.100:6052",
            },
        )
