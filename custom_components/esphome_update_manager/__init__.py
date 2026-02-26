"""ESPHome Update Manager integration."""
from __future__ import annotations

import shutil
import logging
import json
import re
from typing import Any
from pathlib import Path

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .update_queue import UpdateQueue

_LOGGER = logging.getLogger(__name__)

BUILDER_ENTITY_ID = "update.esphome_device_builder_update"
VSCODE_ADDON_SLUG = "a0d7b954_vscode"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    queue = UpdateQueue(hass)
    hass.data[DOMAIN]["queue"] = queue

    websocket_api.async_register_command(hass, ws_get_devices)
    websocket_api.async_register_command(hass, ws_start_updates)
    websocket_api.async_register_command(hass, ws_cancel_updates)
    websocket_api.async_register_command(hass, ws_get_status)
    websocket_api.async_register_command(hass, ws_enable_entity)
    websocket_api.async_register_command(hass, ws_clear_results)
    websocket_api.async_register_command(hass, ws_get_addon_info)

    # Copy frontend files to www
    source = Path(__file__).parent / "www" / "esphome-update-panel.js"
    dest_dir = Path(hass.config.path("www")) / "esphome-update-manager"
    dest = dest_dir / "esphome-update-panel.js"

    await hass.async_add_executor_job(_copy_frontend, source, dest_dir, dest)

    # Lees versie uit manifest.json
    manifest_path = Path(__file__).parent / "manifest.json"
    manifest = await hass.async_add_executor_job(_read_manifest, manifest_path)
    version = manifest.get("version", "0.0.0")

    # Only register panel if not already registered
    if "esphome-update-manager" not in hass.data.get("frontend_panels", {}):
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="ESPHome Updates",
            sidebar_icon="mdi:cellphone-arrow-down",
            frontend_url_path="esphome-update-manager",
            config={
                "_panel_custom": {
                    "name": "esphome-update-panel",
                    "module_url": f"/local/esphome-update-manager/esphome-update-panel.js?v={version}",
                }
            },
        )

    return True


def _copy_frontend(source: Path, dest_dir: Path, dest: Path) -> None:
    """Copy frontend panel file to www directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _read_manifest(manifest_path: Path) -> dict:
    """Read manifest.json file."""
    with open(manifest_path) as f:
        return json.load(f)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN].pop("queue", None)
    return True


# ── Supervisor / Add-on helpers ────────────────────────────────────

async def _supervisor_api(hass: HomeAssistant, method: str, path: str) -> dict | None:
    """Call the Supervisor API."""
    import os
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    try:
        session = async_get_clientsession(hass)
        url = f"http://supervisor{path}"
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"}

        if method == "GET":
            resp = await session.get(url, headers=headers)
        elif method == "POST":
            resp = await session.post(url, headers=headers)
        else:
            return None

        if resp.status == 200:
            return await resp.json()
        else:
            _LOGGER.warning("Supervisor API %s %s returned %s", method, path, resp.status)
            return None
    except Exception as err:
        _LOGGER.error("Supervisor API call failed: %s", err)
        return None


async def async_get_addon_info(hass: HomeAssistant, slug: str) -> dict | None:
    """Get add-on info from Supervisor."""
    result = await _supervisor_api(hass, "GET", f"/addons/{slug}/info")
    if result and result.get("result") == "ok":
        return result.get("data", {})
    return None


async def async_stop_addon(hass: HomeAssistant, slug: str) -> bool:
    """Stop an add-on."""
    result = await _supervisor_api(hass, "POST", f"/addons/{slug}/stop")
    return result is not None and result.get("result") == "ok"


async def async_start_addon(hass: HomeAssistant, slug: str) -> bool:
    """Start an add-on."""
    result = await _supervisor_api(hass, "POST", f"/addons/{slug}/start")
    return result is not None and result.get("result") == "ok"


# ── Version / device helpers ──────────────────────────────────────

def _parse_version(version_string: str | None) -> str | None:
    if not version_string:
        return None
    match = re.match(r"(\d+\.\d+\.\d+)", version_string.strip())
    if match:
        return match.group(1)
    return version_string.strip()


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    try:
        return tuple(int(x) for x in version.split("."))
    except (ValueError, AttributeError):
        return None


def _is_update_available(installed: str | None, latest: str | None) -> bool:
    inst = _version_tuple(installed)
    lat = _version_tuple(latest)
    if inst is None or lat is None:
        return False
    return lat > inst


def _get_esphome_builder_version(hass: HomeAssistant) -> str | None:
    state = hass.states.get(BUILDER_ENTITY_ID)
    if state:
        installed = state.attributes.get("installed_version")
        if installed:
            return _parse_version(installed)

    ent_reg = er.async_get(hass)
    for entity in ent_reg.entities.values():
        if (
            entity.domain == "update"
            and entity.platform == "esphome"
            and entity.disabled_by is None
        ):
            st = hass.states.get(entity.entity_id)
            if st:
                latest = st.attributes.get("latest_version")
                if latest:
                    return _parse_version(latest)

    return None


def _find_status_entity(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    device_id: str,
) -> str | None:
    for entity in ent_reg.entities.values():
        if (
            entity.device_id == device_id
            and entity.domain == "binary_sensor"
            and entity.platform == "esphome"
            and entity.entity_id.endswith("_status")
            and entity.disabled_by is None
        ):
            return entity.entity_id
    return None


def _is_device_online(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    device_id: str | None,
) -> bool | None:
    if not device_id:
        return None
    status_entity_id = _find_status_entity(hass, ent_reg, device_id)
    if not status_entity_id:
        return None
    state = hass.states.get(status_entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    return state.state == "on"


def _get_device_sw_version(
    dev_reg: dr.DeviceRegistry,
    device_id: str | None,
) -> str | None:
    if not device_id:
        return None
    device = dev_reg.async_get(device_id)
    if device:
        return _parse_version(device.sw_version)
    return None


def _get_esphome_update_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    builder_version = _get_esphome_builder_version(hass)
    devices = []

    esphome_update_entities: list[er.RegistryEntry] = []
    devices_with_update_entity: set[str] = set()

    for entity in ent_reg.entities.values():
        if (
            entity.domain == "update"
            and entity.platform == "esphome"
        ):
            esphome_update_entities.append(entity)
            if entity.device_id:
                devices_with_update_entity.add(entity.device_id)

    for entity in esphome_update_entities:
        entity_id = entity.entity_id
        device_id = entity.device_id
        is_disabled = entity.disabled_by is not None

        name = entity_id
        registry_version = _get_device_sw_version(dev_reg, device_id)
        if device_id:
            device = dev_reg.async_get(device_id)
            if device:
                name = device.name_by_user or device.name or entity_id

        online = _is_device_online(hass, ent_reg, device_id)
        state = hass.states.get(entity_id)

        if is_disabled:
            installed = registry_version
            update_available = _is_update_available(installed, builder_version)

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": builder_version if update_available else None,
                "update_available": update_available,
                "in_progress": False,
                "firmware_disabled": True,
                "firmware_unavailable": False,
                "enabling": False,
                "online": online,
            })

        elif state is None or state.state == "unavailable":
            is_enabling = state is None and online is not False

            installed = registry_version
            update_available = _is_update_available(installed, builder_version)

            is_fw_unavailable = state is not None and state.state == "unavailable" and not is_enabling

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": builder_version if update_available else None,
                "update_available": update_available,
                "in_progress": False,
                "firmware_disabled": False,
                "firmware_unavailable": is_fw_unavailable,
                "enabling": is_enabling,
                "online": online,
            })

        else:
            state_version = _parse_version(
                state.attributes.get("installed_version")
            )
            installed = state_version or registry_version

            state_latest = _parse_version(
                state.attributes.get("latest_version")
            )
            latest = state_latest or builder_version

            ha_says_update = state.state == "on"
            actually_newer = _is_update_available(installed, latest)

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": latest if (ha_says_update and actually_newer) else None,
                "update_available": ha_says_update and actually_newer,
                "in_progress": state.attributes.get("in_progress", False),
                "firmware_disabled": False,
                "firmware_unavailable": False,
                "enabling": False,
                "online": online,
            })

    esphome_config_entry_ids: set[str] = set()
    for entry in hass.config_entries.async_entries("esphome"):
        esphome_config_entry_ids.add(entry.entry_id)

    for device in dev_reg.devices.values():
        if not any(
            ceid in esphome_config_entry_ids
            for ceid in device.config_entries
        ):
            continue

        if device.id in devices_with_update_entity:
            continue

        installed = _parse_version(device.sw_version)
        update_available = _is_update_available(installed, builder_version)

        online = _is_device_online(hass, ent_reg, device.id)
        name = device.name_by_user or device.name or "Unknown device"

        devices.append({
            "entity_id": None,
            "name": name,
            "current_version": installed,
            "latest_version": builder_version if update_available else None,
            "update_available": update_available,
            "in_progress": False,
            "firmware_disabled": True,
            "firmware_unavailable": False,
            "enabling": False,
            "online": online,
        })

    devices.sort(key=lambda d: (d["name"] or "").lower())
    return devices


# ── WebSocket Commands ──────────────��──────────────────────────────

@websocket_api.websocket_command({"type": "esphome_update_manager/devices"})
@callback
def ws_get_devices(hass, connection, msg):
    devices = _get_esphome_update_entities(hass)
    connection.send_result(msg["id"], {"devices": devices})


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/start",
        "entity_ids": vol.All(vol.Coerce(list), [str]),
        vol.Optional("stop_addon_slug"): vol.Any(str, None),
    }
)
@callback
def ws_start_updates(hass, connection, msg):
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    stop_addon_slug = msg.get("stop_addon_slug")
    try:
        queue.start(msg["entity_ids"], stop_addon_slug=stop_addon_slug)
        connection.send_result(msg["id"], {"started": True})
    except RuntimeError as err:
        connection.send_error(msg["id"], "already_running", str(err))


@websocket_api.websocket_command({"type": "esphome_update_manager/cancel"})
@callback
def ws_cancel_updates(hass, connection, msg):
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    queue.cancel()
    connection.send_result(msg["id"], {"cancelled": True})


@websocket_api.websocket_command({"type": "esphome_update_manager/status"})
@callback
def ws_get_status(hass, connection, msg):
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    connection.send_result(
        msg["id"],
        {
            "running": queue.is_running,
            "results": queue.results,
            "summary": queue.summary,
        },
    )


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/enable_entity",
        "entity_id": str,
    }
)
@callback
def ws_enable_entity(hass, connection, msg):
    registry = er.async_get(hass)
    try:
        registry.async_update_entity(
            msg["entity_id"],
            disabled_by=None,
        )
        connection.send_result(msg["id"], {"enabled": True})
    except Exception as err:
        connection.send_error(msg["id"], "enable_failed", str(err))


@websocket_api.websocket_command({"type": "esphome_update_manager/clear_results"})
@callback
def ws_clear_results(hass, connection, msg):
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    try:
        queue.clear()
        connection.send_result(msg["id"], {"cleared": True})
    except RuntimeError as err:
        connection.send_error(msg["id"], "clear_failed", str(err))


@websocket_api.websocket_command({"type": "esphome_update_manager/addon_info"})
@websocket_api.async_response
async def ws_get_addon_info(hass, connection, msg):
    """Get VS Code Server add-on status."""
    info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
    if info is None:
        connection.send_result(msg["id"], {
            "installed": False,
            "running": False,
            "name": None,
        })
    else:
        connection.send_result(msg["id"], {
            "installed": True,
            "running": info.get("state") == "started",
            "name": info.get("name", "VS Code Server"),
        })
