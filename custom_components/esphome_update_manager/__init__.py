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

from .const import (
    DOMAIN,
    DEFAULT_MAX_LOG_BACKUPS,
    CONF_DASHBOARD_URL,
    CONF_DASHBOARD_MODE,
    CONF_DASHBOARD_USERNAME,
    CONF_DASHBOARD_PASSWORD,
    DASHBOARD_MODE_LOCAL,
    DASHBOARD_MODE_EXTERNAL,
)
from .dashboard import ExternalDashboardCoordinator
from .update_queue import UpdateQueue

_LOGGER = logging.getLogger(__name__)

BUILDER_ENTITY_ID = "update.esphome_device_builder_update"
VSCODE_ADDON_SLUG = "a0d7b954_vscode"
STORAGE_KEY = f"{DOMAIN}.settings"
STORAGE_VERSION = 1
LOG_FILENAME = "update_log.txt"
LOG_BACKUP_DIR = "log-backups"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _get_log_path(hass: HomeAssistant) -> Path:
    """Get the path to the update log file."""
    return Path(hass.config.path("www")) / "esphome-update-manager" / LOG_FILENAME


def _get_log_backup_dir(hass: HomeAssistant) -> Path:
    """Get the path to the log backup directory."""
    return Path(hass.config.path("www")) / "esphome-update-manager" / LOG_BACKUP_DIR


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    queue = UpdateQueue(hass)
    hass.data[DOMAIN]["queue"] = queue

    # Setup dashboard coordinator based on mode
    dashboard_mode = entry.data.get(CONF_DASHBOARD_MODE, DASHBOARD_MODE_LOCAL)
    dashboard_url = entry.data.get(CONF_DASHBOARD_URL)
    dashboard_username = entry.data.get(CONF_DASHBOARD_USERNAME)
    dashboard_password = entry.data.get(CONF_DASHBOARD_PASSWORD)

    if dashboard_mode == DASHBOARD_MODE_EXTERNAL and dashboard_url:
        # External dashboard - create our own coordinator
        external_coordinator = ExternalDashboardCoordinator(
            hass, 
            dashboard_url,
            username=dashboard_username,
            password=dashboard_password,
        )
        hass.data[DOMAIN]["external_dashboard"] = external_coordinator
        hass.data[DOMAIN]["dashboard_mode"] = DASHBOARD_MODE_EXTERNAL

        # Connect in background - don't block HA startup
        async def _connect_external_dashboard():
            try:
                # Use short timeout for initial check
                if await external_coordinator.async_check_connection(timeout=3):
                    await external_coordinator.async_config_entry_first_refresh()
                    _LOGGER.info(
                        "Connected to external ESPHome dashboard at %s (version %s)",
                        dashboard_url,
                        external_coordinator.esphome_version,
                    )
                else:
                    _LOGGER.warning(
                        "External ESPHome dashboard at %s is not reachable. "
                        "Will retry periodically.",
                        dashboard_url,
                    )
            except Exception as err:
                _LOGGER.warning(
                    "Failed to connect to external ESPHome dashboard at %s: %s. "
                    "Will retry periodically.",
                    dashboard_url,
                    err,
                )

        hass.async_create_task(_connect_external_dashboard())
        # Keep coordinator polling by adding a listener
        external_coordinator.async_add_listener(lambda: None)
    else:
        hass.data[DOMAIN]["external_dashboard"] = None
        hass.data[DOMAIN]["dashboard_mode"] = DASHBOARD_MODE_LOCAL
        _LOGGER.info("Using local ESPHome dashboard (Supervisor add-on)")

    # Load stored settings
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_settings = await store.async_load()
    if stored_settings is None:
        stored_settings = {"auto_update_enabled": False}
    
    hass.data[DOMAIN]["store"] = store
    hass.data[DOMAIN]["settings"] = stored_settings
    hass.data[DOMAIN]["unsubscribe_listeners"] = []
    hass.data[DOMAIN]["failed_devices"] = stored_settings.get("failed_devices", {})

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
    websocket_api.async_register_command(hass, ws_list_log_backups)
    websocket_api.async_register_command(hass, ws_get_log_backup)
    websocket_api.async_register_command(hass, ws_subscribe_dashboard_status)

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
        
        # Track failed devices for auto-update cooldown
        failed_devices = hass.data[DOMAIN].get("failed_devices", {})
        
        for r in results:
            entity_id = r.get("entity_id")
            status = r.get("status")
            
            if status == "failed" and entity_id:
                failed_devices[entity_id] = True
                _LOGGER.debug("Added %s to failed devices - requires manual update", entity_id)
            elif status == "success" and entity_id in failed_devices:
                failed_devices.pop(entity_id, None)
                _LOGGER.debug("Removed %s from failed devices - manual update succeeded", entity_id)
        
        hass.data[DOMAIN]["failed_devices"] = failed_devices
        
        # Save failed devices to storage
        settings = hass.data[DOMAIN].get("settings", {})
        settings["failed_devices"] = failed_devices
        store: Store = hass.data[DOMAIN]["store"]
        await store.async_save(settings)
        
        # Only process if at least one update was attempted (not just queued/cancelled)
        attempted_statuses = {"success", "failed", "skipped"}
        has_attempted = any(r.get("status") in attempted_statuses for r in results)
        
        if not has_attempted:
            return
        
        await _write_update_log(hass, results)
        await _backup_log(hass)
        
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
    
    manifest_path = Path(__file__).parent / "manifest.json"
    
    def _write_log():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            version = manifest.get("version", "unknown")
        except Exception:
            version = "unknown"
        
        lines = []
        lines.append("=" * 60)
        lines.append(f"ESPHome Update Manager v{version} - Update Log")
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
            from_version = r.get("from_version")
            to_version = r.get("to_version")
            
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
            if from_version and to_version:
                lines.append(f"   Version: {from_version} → {to_version}")
            elif from_version:
                lines.append(f"   Version: {from_version}")
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


async def _backup_log(hass: HomeAssistant) -> None:
    """Create a backup of the current log file."""
    log_path = _get_log_path(hass)
    backup_dir = _get_log_backup_dir(hass)
    
    # Get max backups from config entry options
    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0] if entries else None
    max_backups = int(entry.options.get("max_log_backups", DEFAULT_MAX_LOG_BACKUPS)) if entry else DEFAULT_MAX_LOG_BACKUPS
    
    # If max_backups is 0, don't create backups
    if max_backups == 0:
        return
    
    def _do_backup(max_backups_count):
        if not log_path.exists():
            return
        
        # Create backup directory if needed
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate backup filename with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_filename = f"update_log_{timestamp}.txt"
        backup_path = backup_dir / backup_filename
        
        # Copy current log to backup
        shutil.copy2(log_path, backup_path)
        _LOGGER.info("Log backup created: %s", backup_path)
        
        # Clean up old backups (keep only max_backups_count)
        backups = sorted(backup_dir.glob("update_log_*.txt"), reverse=True)
        for old_backup in backups[max_backups_count + 1:]:
            old_backup.unlink()
            _LOGGER.debug("Removed old log backup: %s", old_backup)
    
    await hass.async_add_executor_job(_do_backup, max_backups)


def _list_log_backups(hass: HomeAssistant) -> list[dict]:
    """List all available log backups (excluding the most recent one)."""
    backup_dir = _get_log_backup_dir(hass)
    
    if not backup_dir.exists():
        return []
    
    backups = []
    backup_files = sorted(backup_dir.glob("update_log_*.txt"), reverse=True)
    
    # Skip the first (most recent) backup - it's identical to the current log
    for backup_file in backup_files[1:]:
        # Extract timestamp from filename: update_log_2026-03-07_14-30-25.txt
        filename = backup_file.name
        match = re.match(r"update_log_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.txt", filename)
        if match:
            date_str = match.group(1)
            time_str = match.group(2).replace("-", ":")
            display_name = f"{date_str} {time_str}"
        else:
            display_name = filename
        
        backups.append({
            "filename": filename,
            "display_name": display_name,
            "path": str(backup_file),
        })
    
    return backups


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
    hass.data[DOMAIN].pop("external_dashboard", None)
    hass.data[DOMAIN].pop("dashboard_mode", None)
    hass.data[DOMAIN].pop("failed_devices", None)
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
    """Setup listener for update entity state changes and device status changes."""
    
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
        
        # Skip if it was already "on" (e.g. deep sleep device waking up)
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

    @callback
    def _handle_status_state_change(event: Event) -> None:
        """Handle state change of device status sensors (for deep sleep devices)."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        
        if new_state is None:
            return
        
        # Only trigger when device comes online (status becomes "on")
        if new_state.state != "on":
            return
        
        # Skip if it was already "on"
        if old_state is not None and old_state.state == "on":
            return
        
        old_state_str = old_state.state if old_state else "None"
        _LOGGER.info(
            "ESPHome device came online: %s (state: %s -> %s), checking for updates in 5 seconds",
            entity_id,
            old_state_str,
            new_state.state
        )
        
        # Delay to allow update entity to refresh
        async def _delayed_auto_update():
            await asyncio.sleep(5)
            await _check_and_start_auto_update(hass)
        
        hass.async_create_task(_delayed_auto_update())

    ent_reg = er.async_get(hass)
    
    # Get all update entity IDs
    all_update_entity_ids = [
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.domain == "update" and entity.disabled_by is None
    ]

    # Get all ESPHome status sensor entity IDs (for deep sleep detection)
    esphome_device_ids = _get_esphome_device_ids(hass)
    all_status_entity_ids = [
        entity.entity_id
        for entity in ent_reg.entities.values()
        if (
            entity.domain == "binary_sensor"
            and entity.platform == "esphome"
            and entity.entity_id.endswith("_status")
            and entity.disabled_by is None
            and entity.device_id in esphome_device_ids
        )
    ]

    if not all_update_entity_ids and not all_status_entity_ids:
        _LOGGER.warning("No update or status entities found for auto-update listener")
        return

    # Subscribe to state changes for update entities
    if all_update_entity_ids:
        unsub_updates = async_track_state_change_event(
            hass,
            all_update_entity_ids,
            _handle_update_state_change,
        )
        hass.data[DOMAIN]["unsubscribe_listeners"].append(unsub_updates)

    # Subscribe to state changes for status sensors (deep sleep devices)
    if all_status_entity_ids:
        unsub_status = async_track_state_change_event(
            hass,
            all_status_entity_ids,
            _handle_status_state_change,
        )
        hass.data[DOMAIN]["unsubscribe_listeners"].append(unsub_status)
    
    # Log which entities we're monitoring
    esphome_update_entities = [eid for eid in all_update_entity_ids if _is_esphome_update_entity(hass, eid)]
    _LOGGER.info(
        "Auto-update listener active. Monitoring %d update entities (%d ESPHome) and %d status sensors",
        len(all_update_entity_ids),
        len(esphome_update_entities),
        len(all_status_entity_ids),
    )
    _LOGGER.debug("Status sensors monitored: %s", all_status_entity_ids)


async def _check_and_start_auto_update(hass: HomeAssistant) -> None:
    """Check for available updates and start them automatically."""
    settings = hass.data[DOMAIN].get("settings", {})
    
    if not settings.get("auto_update_enabled", False):
        return
    
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    
    # Don't start if already running
    if queue.is_running:
        return
    
    # Get failed devices (permanent until manual success)
    failed_devices = hass.data[DOMAIN].get("failed_devices", {})
    
    # Get all devices with available updates
    devices = _get_esphome_update_entities(hass)
    
    updatable = []
    version_info = {}
    
    for d in devices:
        entity_id = d["entity_id"]
        
        # Skip devices that previously failed
        if entity_id in failed_devices:
            _LOGGER.debug("Skipping %s - previously failed, requires manual update", entity_id)
            continue
        
        if (
            entity_id
            and d["update_available"]
            and not d["firmware_disabled"]
            and not d["firmware_unavailable"]
            and not d["enabling"]
            and d["online"] is not False
            and not d["in_progress"]
        ):
            updatable.append(entity_id)
            version_info[entity_id] = {
                "from": d["current_version"],
                "to": d["latest_version"],
            }
    
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
        queue.start(updatable, stop_addon_slug=stop_addon_slug, version_info=version_info)
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
        # Use provided entity IDs, but also get version info
        devices = _get_esphome_update_entities(hass)
        device_map = {d["entity_id"]: d for d in devices}
        
        updatable = list(entity_ids)
        version_info = {}
        for eid in updatable:
            if eid in device_map:
                version_info[eid] = {
                    "from": device_map[eid]["current_version"],
                    "to": device_map[eid]["latest_version"],
                }
    else:
        # Find all devices with available updates
        devices = _get_esphome_update_entities(hass)
        updatable = []
        version_info = {}
        
        for d in devices:
            if (
                d["entity_id"]
                and d["update_available"]
                and not d["firmware_disabled"]
                and not d["firmware_unavailable"]
                and not d["enabling"]
                and d["online"] is not False
                and not d["in_progress"]
            ):
                updatable.append(d["entity_id"])
                version_info[d["entity_id"]] = {
                    "from": d["current_version"],
                    "to": d["latest_version"],
                }
    
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
        queue.start(updatable, stop_addon_slug=stop_addon_slug, version_info=version_info)
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


def _is_esphome_version(version: str | None) -> bool:
    """Check if version string contains an ESPHome version (YYYY.M.x format)."""
    if not version:
        return True
    v = str(version)
    if re.search(r"20\d{2}\.\d+\.\d+", v):
        return True
    return False


def _extract_esphome_version(version: str | None) -> str | None:
    """Extract ESPHome version (YYYY.M.x format) from a version string.
    
    Examples:
        "2026.3.3" -> "2026.3.3"
        "1.0.5 (ESPHome 2026.3.3)" -> "2026.3.3"
        "1.0.5" -> None
    """
    if not version:
        return None
    match = re.search(r"(20\d{2}\.\d+\.\d+)", version)
    if match:
        return match.group(1)
    return None


def _get_local_esphome_builder_version(hass: HomeAssistant) -> str | None:
    """Get the ESPHome version from the local HA add-on."""
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


def _get_external_dashboard_version(hass: HomeAssistant) -> str | None:
    """Get the ESPHome version from the external dashboard."""
    coordinator: ExternalDashboardCoordinator | None = hass.data[DOMAIN].get("external_dashboard")
    if coordinator and coordinator.esphome_version:
        return _parse_version(coordinator.esphome_version)
    return None


def _normalize_device_name(name: str) -> str:
    """Normalize device name for matching (ESPHome style: lowercase, spaces/underscores to dashes)."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _match_device_to_external_dashboard(
    hass: HomeAssistant,
    device_name: str,
) -> dict | None:
    """Match a HA device to an external dashboard device. Returns device info if found."""
    coordinator: ExternalDashboardCoordinator | None = hass.data[DOMAIN].get("external_dashboard")
    if not coordinator or not coordinator.data:
        return None
    
    # Use the coordinator's matching method
    return coordinator.get_device_by_name(device_name)


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


def _get_device_sw_version_raw(
    dev_reg: dr.DeviceRegistry,
    device_id: str | None,
) -> str | None:
    """Get raw sw_version without parsing (for ESPHome version detection)."""
    if not device_id:
        return None
    device = dev_reg.async_get(device_id)
    if device:
        return device.sw_version
    return None


def _find_ha_device_by_name(hass: HomeAssistant, name: str) -> dr.DeviceEntry | None:
    """Find a HA device by name (for external devices that exist in HA but have no update entity)."""
    dev_reg = dr.async_get(hass)
    esphome_device_ids = _get_esphome_device_ids(hass)
    
    normalized_search = _normalize_device_name(name)
    
    for device in dev_reg.devices.values():
        if device.id not in esphome_device_ids:
            continue
        
        device_name = device.name_by_user or device.name
        if device_name and _normalize_device_name(device_name) == normalized_search:
            return device
    
    return None


def _get_esphome_update_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Get all ESPHome update entities with their status."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    
    esphome_device_ids = _get_esphome_device_ids(hass)
    
    # Get versions from both sources
    local_builder_version = _get_local_esphome_builder_version(hass)
    external_builder_version = _get_external_dashboard_version(hass)
    
    # Check if we're using external dashboard
    dashboard_mode = hass.data[DOMAIN].get("dashboard_mode", DASHBOARD_MODE_LOCAL)
    external_coordinator: ExternalDashboardCoordinator | None = hass.data[DOMAIN].get("external_dashboard")
    
    # Check if external dashboard is available
    external_dashboard_available = (
        external_coordinator is not None 
        and external_coordinator.available
    )
    
    # Load remembered external devices from storage
    settings = hass.data[DOMAIN].get("settings", {})
    remembered_external_devices: dict[str, bool] = dict(settings.get("external_devices", {}))
    remembered_external_devices_changed = False
    
    # Get failed devices for marking
    failed_devices = hass.data[DOMAIN].get("failed_devices", {})
    
    devices = []
    esphome_device_ids = _get_esphome_device_ids(hass)
    
    # Track which external devices we've already processed (by normalized name)
    processed_external_devices: set[str] = set()
    
    # Track which device_ids we've processed (for cleanup)
    processed_device_ids: set[str] = set()

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
        registry_version_raw = _get_device_sw_version_raw(dev_reg, device_id)
        if device_id:
            device = dev_reg.async_get(device_id)
            if device:
                name = device.name_by_user or device.name or entity_id
            processed_device_ids.add(device_id)

        online = _is_device_online(hass, ent_reg, device_id)
        state = hass.states.get(entity_id)

        # Check if this device is managed by external dashboard
        external_device_info = None
        is_external_device = False
        is_dashboard_unavailable = False
        builder_version = local_builder_version  # Default to local
        
        normalized_name = _normalize_device_name(name)
        
        if dashboard_mode == DASHBOARD_MODE_EXTERNAL and external_coordinator:
            external_device_info = _match_device_to_external_dashboard(hass, name)
            if external_device_info:
                is_external_device = True
                builder_version = external_builder_version
                # Mark as processed
                processed_external_devices.add(normalized_name)
                # Check if dashboard is available for updates
                if not external_dashboard_available:
                    is_dashboard_unavailable = True
                # Remember this device as external
                if normalized_name not in remembered_external_devices:
                    remembered_external_devices[normalized_name] = True
                    remembered_external_devices_changed = True

        if is_disabled:
            # For offline devices, extract ESPHome version from raw string for comparison
            # "1.0.5 (ESPHome 2026.3.3)" -> use "2026.3.3" for comparison
            installed = _extract_esphome_version(registry_version_raw) or registry_version
            
            # For external devices, use deployed_version from dashboard if available
            if is_external_device and external_device_info:
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)
            
            # Skip non-ESPHome firmware - check raw version (contains full string like "1.0.5 (ESPHome 2026.3.3)")
            if not _is_esphome_version(registry_version_raw):
                continue
            
            update_available = _is_update_available(installed, builder_version)

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": builder_version if update_available else None,
                "update_available": update_available and not is_dashboard_unavailable,
                "in_progress": False,
                "firmware_disabled": True,
                "firmware_unavailable": is_dashboard_unavailable,
                "enabling": False,
                "online": online,
                "skipped": False,
                "is_external": is_external_device,
                "failed": entity_id in failed_devices,
            })

        elif state is None or state.state == "unavailable":
            is_enabling = state is None and online is not False

            # For offline devices, extract ESPHome version from raw string for comparison
            # "1.0.5 (ESPHome 2026.3.3)" -> use "2026.3.3" for comparison
            installed = _extract_esphome_version(registry_version_raw) or registry_version
            
            # For external devices, use deployed_version from dashboard if available
            if is_external_device and external_device_info:
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)
            
            # Skip non-ESPHome firmware - check raw version (contains full string like "1.0.5 (ESPHome 2026.3.3)")
            if not _is_esphome_version(registry_version_raw):
                continue
            
            update_available = _is_update_available(installed, builder_version)

            is_fw_unavailable = state is not None and state.state == "unavailable" and not is_enabling
            
            # For external devices with unavailable state, check if dashboard has info
            if is_external_device and is_fw_unavailable and external_device_info:
                # Device is in external dashboard, so it's not truly unavailable
                is_fw_unavailable = False
            
            # If external dashboard is unavailable, mark firmware as unavailable
            if is_dashboard_unavailable:
                is_fw_unavailable = True

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": builder_version if update_available else None,
                "update_available": update_available and not is_dashboard_unavailable,
                "in_progress": False,
                "firmware_disabled": False,
                "firmware_unavailable": is_fw_unavailable,
                "enabling": False,
                "online": online,
                "skipped": False,
                "is_external": is_external_device,
                "failed": entity_id in failed_devices,
            })

        else:
            state_version = _parse_version(
                state.attributes.get("installed_version")
            )
            installed = state_version or registry_version
            
            # For external devices, prefer deployed_version from dashboard
            if is_external_device and external_device_info:
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)

            # Skip non-ESPHome firmware - check raw version or state attributes
            if not _is_esphome_version(registry_version_raw) and not _is_esphome_version(state.attributes.get("installed_version")):
                continue

            # For external devices, use external builder version as latest
            if is_external_device:
                latest = builder_version
            else:
                state_latest = _parse_version(
                    state.attributes.get("latest_version")
                )
                latest = state_latest or builder_version

            # Calculate update availability based on correct builder version
            actually_newer = _is_update_available(installed, latest)
            
            # For external devices, ignore HA's state (it compares with wrong builder)
            if is_external_device:
                ha_says_update = actually_newer
            else:
                ha_says_update = state.state == "on"
            
            # Check if update was skipped (HA says no update, but newer version exists)
            is_skipped = not ha_says_update and actually_newer and not is_external_device

            devices.append({
                "entity_id": entity_id,
                "name": name,
                "current_version": installed,
                "latest_version": latest if (ha_says_update and actually_newer) or is_skipped else None,
                "update_available": ha_says_update and actually_newer and not is_dashboard_unavailable,
                "in_progress": state.attributes.get("in_progress", False),
                "firmware_disabled": False,
                "firmware_unavailable": is_dashboard_unavailable,
                "enabling": False,
                "online": online,
                "skipped": is_skipped,
                "is_external": is_external_device,
                "failed": entity_id in failed_devices,
            })

    # ── Process ESPHome devices WITHOUT update entity ──────────────────
    # These are devices in ESPHome integration but without firmware entity
    for device in dev_reg.devices.values():
        if device.id not in esphome_device_ids:
            continue
        
        # Skip if already processed via update entity
        if device.id in devices_with_update_entity:
            continue
        
        # Skip sub-devices (they have a via_device_id pointing to parent)
        if device.via_device_id is not None:
            continue

        # Mark as processed
        processed_device_ids.add(device.id)
        
        name = device.name_by_user or device.name or "Unknown device"
        normalized_name = _normalize_device_name(name)
        
        # Skip if already processed (by name)
        if normalized_name in processed_external_devices:
            continue
        
        processed_external_devices.add(normalized_name)
        
        installed = _parse_version(device.sw_version)
        online = _is_device_online(hass, ent_reg, device.id)
        
        # Skip non-ESPHome firmware - check raw sw_version
        if not _is_esphome_version(device.sw_version):
            continue
        
        # Determine if this is an external device and if it can be updated
        is_external_device = False
        builder_version = None
        is_unavailable = True  # Default: unavailable until we find a dashboard
        external_device_info = None
        
        # Check if we can match to external dashboard
        if dashboard_mode == DASHBOARD_MODE_EXTERNAL and external_coordinator:
            external_device_info = _match_device_to_external_dashboard(hass, name)
            if external_device_info:
                is_external_device = True
                builder_version = external_builder_version
                is_unavailable = not external_dashboard_available
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)
                # Remember this device as external
                if normalized_name not in remembered_external_devices:
                    remembered_external_devices[normalized_name] = True
                    remembered_external_devices_changed = True
            elif normalized_name in remembered_external_devices:
                # Remembered as external but dashboard offline or device not found
                is_external_device = True
                builder_version = external_builder_version
                is_unavailable = True
        
        # In LOCAL mode, devices without update entity are always unavailable
        # (they're not in the local ESPHome add-on)
        
        update_available = False
        if builder_version and not is_unavailable:
            update_available = _is_update_available(installed, builder_version)
        
        # Create unique identifier
        if is_external_device:
            device_entity_id = f"external:{normalized_name}"
        else:
            device_entity_id = f"device:{normalized_name}"
        
        devices.append({
            "entity_id": device_entity_id,
            "name": name,
            "current_version": installed,
            "latest_version": builder_version if update_available else None,
            "update_available": update_available,
            "in_progress": False,
            "firmware_disabled": False,
            "firmware_unavailable": is_unavailable,
            "enabling": False,
            "online": online,
            "skipped": False,
            "is_external": is_external_device,
            "failed": device_entity_id in failed_devices,
        })

    # ── Cleanup: Remove remembered devices that no longer exist in HA ──────
    # Get all current ESPHome device names (only from HA, not from external dashboard)
    current_esphome_device_names: set[str] = set()
    for device in dev_reg.devices.values():
        if device.id in esphome_device_ids:
            device_name = device.name_by_user or device.name
            if device_name:
                current_esphome_device_names.add(_normalize_device_name(device_name))
    
    # Remove remembered devices that no longer exist in HA
    remembered_to_remove = [
        name for name in remembered_external_devices
        if name not in current_esphome_device_names
    ]
    for name in remembered_to_remove:
        del remembered_external_devices[name]
        remembered_external_devices_changed = True
        _LOGGER.debug("Removed remembered external device '%s' (no longer exists in HA)", name)

    # Save remembered external devices if changed
    if remembered_external_devices_changed:
        settings["external_devices"] = remembered_external_devices
        hass.data[DOMAIN]["settings"] = settings
        # Schedule async save
        store: Store = hass.data[DOMAIN].get("store")
        if store:
            hass.async_create_task(store.async_save(settings))

    devices.sort(key=lambda d: (d["name"] or "").lower())
    return devices

    # ── Process ESPHome devices WITHOUT update entity ──────────────────
    # These are devices in ESPHome integration but without firmware entity
    for device in dev_reg.devices.values():
        if device.id not in esphome_device_ids:
            continue
        
        # Skip if already processed via update entity
        if device.id in devices_with_update_entity:
            continue
        
        # Mark as processed
        processed_device_ids.add(device.id)
        
        name = device.name_by_user or device.name or "Unknown device"
        normalized_name = _normalize_device_name(name)
        
        # Skip if already processed (by name)
        if normalized_name in processed_external_devices:
            continue
        
        processed_external_devices.add(normalized_name)
        
        installed = _parse_version(device.sw_version)
        online = _is_device_online(hass, ent_reg, device.id)
        
        # Skip non-ESPHome firmware
        if not _is_esphome_version(installed):
            continue
        
        # Determine if this is an external device and if it can be updated
        is_external_device = False
        builder_version = None
        is_unavailable = True  # Default: unavailable until we find a dashboard
        external_device_info = None
        
        # Check if we can match to external dashboard
        if dashboard_mode == DASHBOARD_MODE_EXTERNAL and external_coordinator:
            external_device_info = _match_device_to_external_dashboard(hass, name)
            if external_device_info:
                is_external_device = True
                builder_version = external_builder_version
                is_unavailable = not external_dashboard_available
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)
                # Remember this device as external
                if normalized_name not in remembered_external_devices:
                    remembered_external_devices[normalized_name] = True
                    remembered_external_devices_changed = True
            elif normalized_name in remembered_external_devices:
                # Remembered as external but dashboard offline or device not found
                is_external_device = True
                builder_version = external_builder_version
                is_unavailable = True
        
        # In LOCAL mode, devices without update entity are always unavailable
        # (they're not in the local ESPHome add-on)
        
        update_available = False
        if builder_version and not is_unavailable:
            update_available = _is_update_available(installed, builder_version)
        
        # Create unique identifier
        if is_external_device:
            device_entity_id = f"external:{normalized_name}"
        else:
            device_entity_id = f"device:{normalized_name}"
        
        devices.append({
            "entity_id": device_entity_id,
            "name": name,
            "current_version": installed,
            "latest_version": builder_version if update_available else None,
            "update_available": update_available,
            "in_progress": False,
            "firmware_disabled": False,
            "firmware_unavailable": is_unavailable,
            "enabling": False,
            "online": online,
            "skipped": False,
            "is_external": is_external_device,
            "failed": device_entity_id in failed_devices,
        })

    # ── Cleanup: Remove remembered devices that no longer exist in HA ──────
    # Get all current ESPHome device names (only from HA, not from external dashboard)
    current_esphome_device_names: set[str] = set()
    for device in dev_reg.devices.values():
        if device.id in esphome_device_ids:
            device_name = device.name_by_user or device.name
            if device_name:
                current_esphome_device_names.add(_normalize_device_name(device_name))
    
    # Remove remembered devices that no longer exist in HA
    remembered_to_remove = [
        name for name in remembered_external_devices
        if name not in current_esphome_device_names
    ]
    for name in remembered_to_remove:
        del remembered_external_devices[name]
        remembered_external_devices_changed = True
        _LOGGER.debug("Removed remembered external device '%s' (no longer exists in HA)", name)

    # Save remembered external devices if changed
    if remembered_external_devices_changed:
        settings["external_devices"] = remembered_external_devices
        hass.data[DOMAIN]["settings"] = settings
        # Schedule async save
        store: Store = hass.data[DOMAIN].get("store")
        if store:
            hass.async_create_task(store.async_save(settings))

    devices.sort(key=lambda d: (d["name"] or "").lower())
    return devices

# ── WebSocket Commands ────────────────────────────────────────────

@websocket_api.websocket_command({"type": "esphome_update_manager/devices"})
@callback
def ws_get_devices(hass, connection, msg):
    devices = _get_esphome_update_entities(hass)
    
    # Check if mixed setup (both local and external devices)
    has_local = any(not d.get("is_external", False) for d in devices)
    has_external = any(d.get("is_external", False) for d in devices)
    is_mixed_setup = has_local and has_external
    
    connection.send_result(msg["id"], {
        "devices": devices,
        "is_mixed_setup": is_mixed_setup,
    })


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/start",
        "entity_ids": vol.All(vol.Coerce(list), [str]),
        vol.Optional("stop_addon_slug"): vol.Any(str, None),
        vol.Optional("version_info"): vol.Any(dict, None),
    }
)
@callback
def ws_start_updates(hass, connection, msg):
    _LOGGER.debug("WebSocket: start updates requested for %s", msg["entity_ids"])
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    stop_addon_slug = msg.get("stop_addon_slug")
    version_info = msg.get("version_info", {})
    try:
        queue.start(msg["entity_ids"], stop_addon_slug=stop_addon_slug, version_info=version_info)
        connection.send_result(msg["id"], {"started": True})
    except RuntimeError as err:
        _LOGGER.warning("WebSocket: start updates failed - %s", err)
        connection.send_error(msg["id"], "already_running", str(err))


@websocket_api.websocket_command({"type": "esphome_update_manager/cancel"})
@callback
def ws_cancel_updates(hass, connection, msg):
    _LOGGER.debug("WebSocket: cancel updates requested")
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
            "phase": queue.phase,
            "addon_name": queue.addon_name,
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
    entity_id = msg["entity_id"]
    
    # Check if entity exists in registry
    entity = registry.async_get(entity_id)
    if entity is None:
        connection.send_error(
            msg["id"], 
            "entity_not_found", 
            "Entity not found in registry. Try restarting Home Assistant."
        )
        return
    
    # Check if entity has a config entry
    if entity.config_entry_id is None:
        connection.send_error(
            msg["id"], 
            "no_config_entry", 
            "Entity has no config entry. Try restarting Home Assistant."
        )
        return
    
    try:
        registry.async_update_entity(
            entity_id,
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


@websocket_api.websocket_command({"type": "esphome_update_manager/list_log_backups"})
@websocket_api.async_response
async def ws_list_log_backups(hass, connection, msg):
    """List all available log backups."""
    backups = await hass.async_add_executor_job(_list_log_backups, hass)
    connection.send_result(msg["id"], {"backups": backups})


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/get_log_backup",
        "filename": str,
    }
)
@websocket_api.async_response
async def ws_get_log_backup(hass, connection, msg):
    """Get the content of a specific log backup."""
    backup_dir = _get_log_backup_dir(hass)
    filename = msg["filename"]
    
    # Security: ensure filename doesn't contain path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        connection.send_error(msg["id"], "invalid_filename", "Invalid filename")
        return
    
    backup_path = backup_dir / filename
    
    def _read_backup():
        if backup_path.exists():
            with open(backup_path, "r", encoding="utf-8") as f:
                return f.read()
        return None
    
    content = await hass.async_add_executor_job(_read_backup)
    
    if content is None:
        connection.send_error(msg["id"], "not_found", "Backup file not found")
        return
    
    connection.send_result(msg["id"], {
        "filename": filename,
        "content": content,
    })

@websocket_api.websocket_command({"type": "esphome_update_manager/subscribe_dashboard_status"})
@websocket_api.async_response
async def ws_subscribe_dashboard_status(hass, connection, msg):
    """Subscribe to dashboard availability changes."""
    
    @callback
    def handle_dashboard_changed(event):
        """Handle dashboard availability change."""
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {
                    "available": event.data.get("available"),
                    "url": event.data.get("url"),
                },
            )
        )
    
    # Subscribe to the event
    unsub = hass.bus.async_listen("esphome_update_manager_dashboard_changed", handle_dashboard_changed)
    
    # Store unsubscribe function for cleanup
    connection.subscriptions[msg["id"]] = unsub
    
    # Send initial status
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")
    connection.send_result(msg["id"])
    
    # Send current status immediately
    if external_coordinator:
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {
                    "available": external_coordinator.available,
                    "url": external_coordinator.url if hasattr(external_coordinator, 'url') else None,
                },
            )
        )
