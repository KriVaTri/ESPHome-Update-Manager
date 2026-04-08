"""Dashboard coordinator for ESPHome Update Manager."""
from __future__ import annotations

import re
import asyncio
import base64
import logging
import json
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(minutes=1)


class ExternalDashboardCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to interact with an external ESPHome dashboard."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Initialize the dashboard coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="ESPHome External Dashboard",
            update_interval=REFRESH_INTERVAL,
            always_update=False,
        )
        self.url = url.rstrip("/")
        self._session = async_get_clientsession(hass)
        self._username = username
        self._password = password
        self._auth = aiohttp.BasicAuth(username, password) if username and password else None
        self._esphome_version: str | None = None
        self._available = False
        self._previous_available: bool | None = None

    @property
    def esphome_version(self) -> str | None:
        """Return the ESPHome version of the external dashboard."""
        return self._esphome_version

    @property
    def available(self) -> bool:
        """Return True if the dashboard is available."""
        return self._available

    @property
    def has_authentication(self) -> bool:
        """Return True if authentication is configured."""
        return self._auth is not None

    def _get_ws_headers(self) -> dict[str, str]:
        """Get headers for WebSocket connection including authentication."""
        headers = {}
        if self._username and self._password:
            credentials = base64.b64encode(
                f"{self._username}:{self._password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credentials}"
        return headers

    def _fire_availability_changed(self) -> None:
        """Fire event when dashboard availability changes."""
        if self._previous_available is not None and self._previous_available != self._available:
            _LOGGER.info(
                "External dashboard availability changed: %s -> %s",
                self._previous_available,
                self._available,
            )
            self.hass.bus.async_fire(
                "esphome_update_manager_dashboard_changed",
                {"available": self._available, "url": self.url},
            )
            
            # If dashboard came online, trigger auto-update check
            if self._available:
                self.hass.async_create_task(self._trigger_auto_update_check())
        
        self._previous_available = self._available

    async def _trigger_auto_update_check(self) -> None:
        """Trigger auto-update check when dashboard comes online."""
        from .const import DOMAIN
        
        # Wait a moment for devices to be refreshed
        await asyncio.sleep(3)
        
        # Import here to avoid circular import
        from . import _check_and_start_auto_update
        
        _LOGGER.info("Dashboard came online, checking for auto-updates")
        await _check_and_start_auto_update(self.hass)

    async def async_check_connection(self, timeout: int = 10) -> bool:
        """Check if the dashboard is reachable. Does not raise exceptions."""
        try:
            async with self._session.get(
                f"{self.url}/version",
                timeout=aiohttp.ClientTimeout(total=timeout),
                auth=self._auth,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._esphome_version = data.get("version")
                    self._available = True
                    self._fire_availability_changed()
                    return True
                elif resp.status == 401:
                    _LOGGER.error(
                        "Authentication failed for dashboard at %s. Check username and password.",
                        self.url
                    )
                    self._available = False
                    self._fire_availability_changed()
                    return False
                else:
                    _LOGGER.debug("Dashboard returned status %s", resp.status)
                    self._available = False
                    self._fire_availability_changed()
                    return False
        except aiohttp.ClientConnectorError as err:
            _LOGGER.debug("Dashboard connection failed: %s", err)
            self._available = False
            self._fire_availability_changed()
            return False
        except Exception as err:
            _LOGGER.debug("Dashboard not reachable: %s", err)
            self._available = False
            self._fire_availability_changed()
            return False

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device data from external dashboard."""
        # Use shorter timeout for connection check during refresh
        if not await self.async_check_connection(timeout=5):
            _LOGGER.warning("External ESPHome dashboard at %s is not reachable", self.url)
            # Return existing data instead of empty dict to preserve state
            return self.data or {}

        try:
            async with self._session.get(
                f"{self.url}/devices",
                timeout=aiohttp.ClientTimeout(total=30),
                auth=self._auth,
            ) as resp:
                if resp.status == 401:
                    raise UpdateFailed("Authentication failed - check username and password")
                if resp.status != 200:
                    raise UpdateFailed(f"Dashboard returned status {resp.status}")
                data = await resp.json()

                configured = data.get("configured", [])
                devices = {}
                for dev in configured:
                    name = dev.get("name", "")
                    if name:
                        devices[name] = dev
                        friendly = dev.get("friendly_name")
                        if friendly and friendly != name:
                            devices[friendly] = dev
                
                _LOGGER.debug(
                    "Fetched %d devices from external dashboard (version %s)",
                    len(configured),
                    self._esphome_version,
                )
                return devices

        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Failed to fetch devices: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Failed to fetch devices: {err}") from err

    async def async_compile(self, configuration: str) -> bool:
        """Compile a device configuration via WebSocket."""
        if not self._available:
            _LOGGER.error("Cannot compile: dashboard not available")
            return False

        return await self._run_websocket_command("compile", configuration)

    async def async_upload(self, configuration: str, port: str = "OTA") -> bool:
        """Upload firmware to a device via WebSocket."""
        if not self._available:
            _LOGGER.error("Cannot upload: dashboard not available")
            return False

        return await self._run_websocket_command("upload", configuration, port=port)

    async def _run_websocket_command(
        self, 
        command: str, 
        configuration: str, 
        port: str = "OTA",
        timeout: int = 300,
    ) -> bool:
        """Run a compile/upload command via WebSocket.
        
        The ESPHome dashboard uses WebSocket for these operations.
        Messages need a "type" field to specify the action.
        """
        # Convert http(s) to ws(s)
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/{command}"
        
        _LOGGER.info("Starting %s for %s via WebSocket: %s", command, configuration, ws_url)
        
        # Get authentication headers for WebSocket
        headers = self._get_ws_headers()
        
        try:
            async with self._session.ws_connect(
                ws_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=headers,
            ) as ws:
                # Send the command with the required "type" field
                if command == "compile":
                    await ws.send_json({
                        "type": "spawn",
                        "configuration": configuration,
                    })
                elif command == "upload":
                    await ws.send_json({
                        "type": "spawn",
                        "configuration": configuration,
                        "port": port,
                    })
                else:
                    await ws.send_json({
                        "type": "spawn",
                        "configuration": configuration,
                    })
                
                _LOGGER.debug("Sent %s command for %s", command, configuration)
                
                # Read responses until we get a result
                success = False
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        line = msg.data.strip()
                        
                        # Try to parse as JSON first
                        try:
                            data = json.loads(line)
                            event_type = data.get("event")
                            
                            if event_type == "line":
                                # Log output line
                                log_line = data.get("data", "")
                                if any(x in log_line.lower() for x in ["error", "failed", "success", "done", "compiling", "uploading", "connecting", "ota"]):
                                    _LOGGER.info("[%s] %s", command, log_line)
                                else:
                                    _LOGGER.debug("[%s] %s", command, log_line)
                            
                            elif event_type == "exit":
                                # Process finished
                                exit_code = data.get("code", -1)
                                success = exit_code == 0
                                _LOGGER.info("%s completed with exit code %s (success=%s)", command, exit_code, success)
                                break
                            
                            else:
                                _LOGGER.debug("[%s] Event: %s", command, data)
                        
                        except json.JSONDecodeError:
                            # Plain text line
                            if any(x in line.lower() for x in ["error", "failed", "success", "done"]):
                                _LOGGER.info("[%s] %s", command, line)
                            else:
                                _LOGGER.debug("[%s] %s", command, line)
                    
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        _LOGGER.error("WebSocket error during %s: %s", command, ws.exception())
                        return False
                    
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.debug("WebSocket closed for %s", command)
                        break
                
                return success
        
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                _LOGGER.error(
                    "WebSocket authentication failed for %s. Check username and password.",
                    command
                )
            else:
                _LOGGER.error("WebSocket handshake failed for %s: %s", command, err)
            return False
        except asyncio.TimeoutError:
            _LOGGER.error("%s timed out after %d seconds", command, timeout)
            return False
        except aiohttp.ClientError as err:
            _LOGGER.error("WebSocket connection failed for %s: %s", command, err)
            return False
        except Exception as err:
            _LOGGER.error("Unexpected error during %s: %s", command, err)
            return False

    async def async_get_device_online_status(self) -> dict[str, bool]:
        """Get online status of all devices via /ping endpoint."""
        if not self._available:
            return {}

        try:
            async with self._session.get(
                f"{self.url}/ping",
                timeout=aiohttp.ClientTimeout(total=10),
                auth=self._auth,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    _LOGGER.warning("Authentication failed when getting device status")
                    return {}
                return {}
        except Exception as err:
            _LOGGER.debug("Failed to get ping status: %s", err)
            return {}

    def get_device_by_name(self, name: str) -> dict[str, Any] | None:
        """Get device info by name (case-insensitive matching)."""
        if not self.data:
            return None

        if name in self.data:
            return self.data[name]

        name_lower = name.lower()
        for key, device in self.data.items():
            if key.lower() == name_lower:
                return device

        def normalize(s: str) -> str:
            """Normalize device name for matching (ESPHome style: lowercase, only a-z, 0-9, - allowed)."""
            normalized = s.lower()
            # Replace spaces and underscores with dashes
            normalized = normalized.replace(" ", "-").replace("_", "-")
            # Remove characters not allowed in ESPHome names
            normalized = re.sub(r'[^a-z0-9-]', '', normalized)
            # Reduce multiple dashes to single dash
            normalized = re.sub(r'-+', '-', normalized)
            # Remove leading/trailing dashes
            normalized = normalized.strip('-')
            return normalized

        name_normalized = normalize(name)
        for key, device in self.data.items():
            if normalize(key) == name_normalized:
                return device

        return None

    @property
    def supports_update(self) -> bool:
        """Check if dashboard supports updates."""
        return self._available and self.last_update_success

    @property
    def current_version(self) -> str | None:
        """Get the ESPHome version from the dashboard itself."""
        return self._esphome_version

    def update_credentials(self, username: str | None, password: str | None) -> None:
        """Update the authentication credentials.
        
        This can be used to update credentials without recreating the coordinator.
        """
        self._username = username
        self._password = password
        self._auth = aiohttp.BasicAuth(username, password) if username and password else None
        _LOGGER.info(
            "Dashboard credentials updated (auth %s)",
            "enabled" if self._auth else "disabled"
        )
