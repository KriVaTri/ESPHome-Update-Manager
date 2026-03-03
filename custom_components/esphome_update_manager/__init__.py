"""ESPHome Update Manager integration."""
from __future__ import annotations

import shutil
import logging
import json
import re
import asyncio
from typing import Any
from pathlib import Path
from datetime import datetime

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant, callback, Event, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN
from .update_queue import UpdateQueue

_LOGGER = logging.getLogger(__name__)

BUILDER_ENTITY_ID = "update.esphome_device_builder_update"
VSCODE_ADDON_SLUG = "a0d7b954_vscode"
STORAGE_KEY = f"{DOMAIN}.settings"
STORAGE_VERSION = 1
LOG_FILENAME = "update_log.txt"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _get_log_path(hass: HomeAssistant) -> Path:
    """Get the path to the update log file."""
    return Path(hass.config.path("www")) / "esphome-update-manager" / LOG_FILENAME


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    queue = UpdateQueue(hass)
    hass.data[DOMAIN]["queue"] = queue

    # Load stored settings
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_settings = await store.async_load()
    if stored_settings is None:
        stored_settings = {"auto_update_enabled": False}
    
    hass.data[DOMAIN]["store"] = store
    hass.data[DOMAIN]["settings"] = stored_settings
    hass.data[DOMAIN]["unsubscribe_listeners"] = []

    websocket_api.async_register_command(hass, ws_get_devices)
    websocket_api.async_register_command(hass, ws_start_updates)
    websocket_api.async_register_command(hass, ws_cancel_updates)
    websocket_api.async_register_command(hass, ws_get_status)
    websocket_api.async_register_command(hass, ws_enable_entity)
    websocket_api.async_register_command(hass, ws_clear_results)
    websocket_api.async_register_command(hass, ws_get_addon_info)
    websocket_api.async_register_command(hass, ws_get_auto_update_settings)
    websocket_api.async_register_command(hass, ws_set_auto_update_settings)
    websocket_api.async_register_command(hass, ws_get_update_log)

    # Copy frontend files to www
    source = Path(__file__).parent / "www" / "esphome-update-panel.js"
    dest_dir = Path(hass.config.path("www")) / "esphome-update-manager"
    dest = dest_dir / "esphome-update-panel.js"

    await hass.async_add_executor_job(_copy_frontend, source, dest_dir, dest)

    # Read version from manifest.json
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

    # Listen for update finished events
    async def _handle_update_finished(event: Event) -> None:
        """Handle update finished event - write log and send notification if needed."""
        results = event.data.get("results", [])
        summary = event.data.get("summary", {})
        
        # Only process if there are actual results
        if not results:
            return
        
        # Only process if at least one update was attempted (not just queued/cancelled)
        attempted_statuses = {"success", "failed", "skipped"}
        has_attempted = any(r.get("status") in attempted_statuses for r in results)
        
        if not has_attempted:
            return
        
        await _write_update_log(hass, results)
        
        # Check for failures
        failed_count = summary.get("failed", 0)
        if failed_count > 0:
            await _send_failure_notification(hass, results, failed_count)
    
    unsub_finished = hass.bus.async_listen("esphome_update_manager_finished", _handle_update_finished)
    hass.data[DOMAIN]["unsub_finished"] = unsub_finished

    # Register services
    async def _handle_start_updates(call: ServiceCall) -> None:
        await async_handle_start_updates(hass, call)
    
    hass.services.async_register(
        DOMAIN,
        "start_updates",
        _handle_start_updates,
        schema=vol.Schema({
            vol.Optional("entity_ids"): vol.All(cv.ensure_list, [cv.entity_id]),
            vol.Optional("stop_addon"): cv.boolean,
        }),
    )

    # Setup auto-update listener if enabled (after HA is fully started)
    if stored_settings.get("auto_update_enabled", False):
        hass.async_create_task(_delayed_setup_auto_update(hass))

    return True


async def _delayed_setup_auto_update(hass: HomeAssistant) -> None:
    """Setup auto-update listener after Home Assistant is fully started."""
    
    async def _setup_after_start(_hass: HomeAssistant) -> None:
        # Additional delay to ensure all entities are fully loaded
        await asyncio.sleep(30)
        _LOGGER.info("Setting up auto-update listener after Home Assistant started")
        await _setup_auto_update_listener(hass)
        # Check for any pending updates
        await _check_and_start_auto_update(hass)
    
    async_at_started(hass, _setup_after_start)


def _copy_frontend(source: Path, dest_dir: Path, dest: Path) -> None:
    """Copy frontend panel files to www directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    
    logo_source = source.parent.parent / "brand" / "logo.png"
    if logo_source.exists():
        shutil.copy2(logo_source, dest_dir / "logo.png")


def _read_manifest(manifest_path: Path) -> dict:
    """Read manifest.json file."""
    with open(manifest_path) as f:
        return json.load(f)


async def _write_update_log(hass: HomeAssistant, results: list[dict]) -> None:
    """Write update results to log file."""
    log_path = _get_log_path(hass)
    
    def _write_log():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        lines = []
        lines.append("=" * 60)
        lines.append("ESPHome Update Manager - Update Log")
        lines.append(f"Timestamp: {timestamp}")
        lines.append("=" * 60)
        lines.append("")
        
        # Summary
        success_count = sum(1 for r in results if r.get("status") == "success")
        failed_count = sum(1 for r in results if r.get("status") == "failed")
        skipped_count = sum(1 for r in results if r.get("status") == "skipped")
        cancelled_count = sum(1 for r in results if r.get("status") == "cancelled")
        
        lines.append(f"Summary: {len(results)} device(s) processed")
        lines.append(f"  ✅ Success:   {success_count}")
        lines.append(f"  ❌ Failed:    {failed_count}")
        lines.append(f"  ⏭️  Skipped:   {skipped_count}")
        lines.append(f"  ⛔ Cancelled: {cancelled_count}")
        lines.append("")
        lines.append("-" * 60)
        lines.append("Details:")
        lines.append("-" * 60)
        lines.append("")
        
        # Details per device
        for r in results:
            entity_id = r.get("entity_id", "Unknown")
            status = r.get("status", "unknown")
            error = r.get("error")
            started_at = r.get("started_at", "")
            finished_at = r.get("finished_at", "")
            
            status_icon = {
                "success": "✅",
                "failed": "❌",
                "skipped": "⏭️",
                "cancelled": "⛔",
                "running": "🔄",
                "queued": "⏳",
            }.get(status, "❓")
            
            lines.append(f"{status_icon} {entity_id}")
            lines.append(f"   Status: {status}")
            if started_at:
                lines.append(f"   Started: {started_at}")
            if finished_at:
                lines.append(f"   Finished: {finished_at}")
            if error:
                lines.append(f"   Error: {error}")
            lines.append("")
        
        lines.append("=" * 60)
        lines.append("End of log")
        lines.append("=" * 60)
        
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    
    await hass.async_add_executor_job(_write_log)
    _LOGGER.info("Update log written to %s", log_path)


async def _send_failure_notification(hass: HomeAssistant, results: list[dict], failed_count: int) -> None:
    """Send a persistent notification when updates fail."""
    message = (
        f"Update for {failed_count} ESPHome device(s) has failed.\n\n"
        f"[View update log](/esphome-update-manager?show_log=1)"
    )
    
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": "ESPHome Update Failed",
            "message": message,
            "notification_id": "esphome_update_manager_failure",
        },
    )
    _LOGGER.warning("Sent failure notification for %d device(s)", failed_count)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Unsubscribe from all listeners
    for unsub in hass.data[DOMAIN].get("unsubscribe_listeners", []):
        unsub()
    
    # Unsubscribe from finished event
    unsub_finished = hass.data[DOMAIN].get("unsub_finished")
    if unsub_finished:
        unsub_finished()
    
    # Remove services
    hass.services.async_remove(DOMAIN, "start_updates")
    
    hass.data[DOMAIN].pop("queue", None)
    hass.data[DOMAIN].pop("store", None)
    hass.data[DOMAIN].pop("settings", None)
    hass.data[DOMAIN].pop("unsubscribe_listeners", None)
    hass.data[DOMAIN].pop("unsub_finished", None)
    return True


# ── Auto-update logic ──────────────────────────────────────────────

def _get_esphome_device_ids(hass: HomeAssistant) -> set[str]:
    """Get all device IDs that belong to ESPHome."""
    dev_reg = dr.async_get(hass)
    
    # Get all ESPHome config entry IDs
    esphome_config_entry_ids: set[str] = set()
    for entry in hass.config_entries.async_entries("esphome"):
        esphome_config_entry_ids.add(entry.entry_id)
    
    # Find all devices that belong to ESPHome
    esphome_device_ids: set[str] = set()
    for device in dev_reg.devices.values():
        if any(ceid in esphome_config_entry_ids for ceid in device.config_entries):
            esphome_device_ids.add(device.id)
    
    return esphome_device_ids


def _is_esphome_update_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """Check if an entity_id is an ESPHome device update entity."""
    if not entity_id.startswith("update."):
        return False
    
    # Exclude the builder itself
    if entity_id == BUILDER_ENTITY_ID:
        return False
    
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get(entity_id)
    
    if entity is None:
        return False
    
    if entity.device_id is None:
        return False
    
    esphome_device_ids = _get_esphome_device_ids(hass)
    return entity.device_id in esphome_device_ids


async def _setup_auto_update_listener(hass: HomeAssistant) -> None:
    """Setup listener for ALL update entity state changes, filter for ESPHome in callback."""
    
    # Remove existing listeners first
    for unsub in hass.data[DOMAIN].get("unsubscribe_listeners", []):
        unsub()
    hass.data[DOMAIN]["unsubscribe_listeners"] = []

    @callback
    def _handle_update_state_change(event: Event) -> None:
        """Handle state change of update entities."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        
        if new_state is None:
            return
        
        # Only trigger when state becomes "on" (update available)
        if new_state.state != "on":
            return
        
        # Skip if it was already "on"
        if old_state is not None and old_state.state == "on":
            return
        
        # Check if this is an ESPHome device update entity
        if not _is_esphome_update_entity(hass, entity_id):
            return
        
        old_state_str = old_state.state if old_state else "None"
        _LOGGER.info(
            "ESPHome update available: %s (state: %s -> %s), triggering auto-update in 5 seconds",
            entity_id,
            old_state_str,
            new_state.state
        )
        
        # Delay to allow related entities (like _status sensor) to update
        async def _delayed_auto_update():
            await asyncio.sleep(5)
            await _check_and_start_auto_update(hass)
        
        hass.async_create_task(_delayed_auto_update())

    # Get all current update entity IDs
    ent_reg = er.async_get(hass)
    all_update_entity_ids = [
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.domain == "update" and entity.disabled_by is None
    ]

    if not all_update_entity_ids:
        _LOGGER.warning("No update entities found for auto-update listener")
        return

    # Subscribe to state changes for ALL update entities
    unsub = async_track_state_change_event(
        hass,
        all_update_entity_ids,
        _handle_update_state_change,
    )
    hass.data[DOMAIN]["unsubscribe_listeners"].append(unsub)
    
    # Log which ESPHome entities we're actually monitoring
    esphome_entities = [eid for eid in all_update_entity_ids if _is_esphome_update_entity(hass, eid)]
    _LOGGER.info(
        "Auto-update listener active. Monitoring %d update entities, %d are ESPHome devices: %s",
        len(all_update_entity_ids),
        len(esphome_entities),
        esphome_entities
    )


async def _check_and_start_auto_update(hass: HomeAssistant) -> None:
    """Check for available updates and start them automatically."""
    settings = hass.data[DOMAIN].get("settings", {})
    
    if not settings.get("auto_update_enabled", False):
        return
    
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    
    # Don't start if already running
    if queue.is_running:
        return
    
    # Get all devices with available updates
    devices = _get_esphome_update_entities(hass)
    
    updatable = [
        d["entity_id"]
        for d in devices
        if d["entity_id"]
        and d["update_available"]
        and not d["firmware_disabled"]
        and not d["firmware_unavailable"]
        and not d["enabling"]
        and d["online"] is not False
        and not d["in_progress"]
    ]
    
    if not updatable:
        return
    
    _LOGGER.info("Starting auto-update for %d devices: %s", len(updatable), updatable)
    
    # Check if VS Code Server should be stopped
    stop_addon_slug = None
    if settings.get("stop_addon_during_update", True):
        addon_info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
        if addon_info and addon_info.get("state") == "started":
            stop_addon_slug = VSCODE_ADDON_SLUG
    
    # Double-check before starting (race condition protection)
    if queue.is_running:
        return
    
    try:
        queue.start(updatable, stop_addon_slug=stop_addon_slug)
    except RuntimeError:
        # Queue was started by another task, ignore silently
        pass


async def _remove_auto_update_listener(hass: HomeAssistant) -> None:
    """Remove the auto-update listener."""
    for unsub in hass.data[DOMAIN].get("unsubscribe_listeners", []):
        unsub()
    hass.data[DOMAIN]["unsubscribe_listeners"] = []
    _LOGGER.info("Auto-update listener removed")


# ── Service handlers ───────────────────────────────────────────────

async def async_handle_start_updates(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the start_updates service call."""
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    settings = hass.data[DOMAIN].get("settings", {})
    
    # Don't start if already running
    if queue.is_running:
        _LOGGER.warning("Update queue already running, ignoring service call")
        return
    
    # Get entity_ids from service call or find all updatable devices
    entity_ids = call.data.get("entity_ids")
    
    if entity_ids:
        # Use provided entity IDs
        updatable = list(entity_ids)
    else:
        # Find all devices with available updates
        devices = _get_esphome_update_entities(hass)
        updatable = [
            d["entity_id"]
            for d in devices
            if d["entity_id"]
            and d["update_available"]
            and not d["firmware_disabled"]
            and not d["firmware_unavailable"]
            and not d["enabling"]
            and d["online"] is not False
            and not d["in_progress"]
        ]
    
    if not updatable:
        _LOGGER.info("No devices available for update")
        return
    
    _LOGGER.info("Starting updates via service for %d devices: %s", len(updatable), updatable)
    
    # Check if VS Code Server should be stopped
    stop_addon = call.data.get("stop_addon")
    if stop_addon is None:
        stop_addon = settings.get("stop_addon_during_update", True)
    
    stop_addon_slug = None
    if stop_addon:
        addon_info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
        if addon_info and addon_info.get("state") == "started":
            stop_addon_slug = VSCODE_ADDON_SLUG
    
    try:
        queue.start(updatable, stop_addon_slug=stop_addon_slug)
    except RuntimeError as err:
        _LOGGER.warning("Failed to start updates via service: %s", err)


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

    esphome_device_ids = _get_esphome_device_ids(hass)

    esphome_update_entities: list[er.RegistryEntry] = []
    devices_with_update_entity: set[str] = set()

    for entity in ent_reg.entities.values():
        if (
            entity.domain == "update"
            and entity.device_id in esphome_device_ids
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

    for device in dev_reg.devices.values():
        if device.id not in esphome_device_ids:
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


# ── WebSocket Commands ─────────────────────────────────────────────

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


@websocket_api.websocket_command({"type": "esphome_update_manager/get_auto_update_settings"})
@callback
def ws_get_auto_update_settings(hass, connection, msg):
    """Get auto-update settings."""
    settings = hass.data[DOMAIN].get("settings", {})
    connection.send_result(msg["id"], {
        "auto_update_enabled": settings.get("auto_update_enabled", False),
        "stop_addon_during_update": settings.get("stop_addon_during_update", True),
    })


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/set_auto_update_settings",
        vol.Optional("auto_update_enabled"): bool,
        vol.Optional("stop_addon_during_update"): bool,
    }
)
@websocket_api.async_response
async def ws_set_auto_update_settings(hass, connection, msg):
    """Set auto-update settings."""
    settings = hass.data[DOMAIN].get("settings", {})
    store: Store = hass.data[DOMAIN]["store"]
    
    # Track if auto_update_enabled changed
    auto_update_changed = False
    old_auto_update = settings.get("auto_update_enabled", False)
    
    # Update settings
    if "auto_update_enabled" in msg:
        if msg["auto_update_enabled"] != old_auto_update:
            auto_update_changed = True
        settings["auto_update_enabled"] = msg["auto_update_enabled"]
    if "stop_addon_during_update" in msg:
        settings["stop_addon_during_update"] = msg["stop_addon_during_update"]
    
    hass.data[DOMAIN]["settings"] = settings
    
    # Save to storage
    await store.async_save(settings)
    
    # Only setup/remove listener and check for updates if auto_update_enabled changed
    if auto_update_changed:
        if settings.get("auto_update_enabled", False):
            await _setup_auto_update_listener(hass)
            # Also check immediately for any pending updates
            await _check_and_start_auto_update(hass)
        else:
            await _remove_auto_update_listener(hass)
    
    connection.send_result(msg["id"], {"saved": True})


@websocket_api.websocket_command({"type": "esphome_update_manager/get_update_log"})
@websocket_api.async_response
async def ws_get_update_log(hass, connection, msg):
    """Get the update log content."""
    log_path = _get_log_path(hass)
    
    def _read_log():
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                return f.read()
        return None
    
    content = await hass.async_add_executor_job(_read_log)
    connection.send_result(msg["id"], {
        "exists": content is not None,
        "content": content,
        "url": f"/local/esphome-update-manager/{LOG_FILENAME}",
    })
