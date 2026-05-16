"""ESPHome Update Manager integration."""
from __future__ import annotations

import os
import shutil
import logging
import json
import re
import asyncio
from typing import Any
from pathlib import Path
from datetime import datetime, timedelta

import voluptuous as vol

from packaging.version import parse as parse_version
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components import websocket_api
from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant, callback, Event, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
from .dashboard import ExternalDashboardCoordinator, LocalDashboardCoordinator
from .update_queue import UpdateQueue

_LOGGER = logging.getLogger(__name__)

BUILDER_ENTITY_ID = "update.esphome_device_builder_update"
VSCODE_ADDON_SLUG = "a0d7b954_vscode"
STORAGE_KEY = f"{DOMAIN}.settings"
STORAGE_VERSION = 1
LOG_FILENAME = "update_log.txt"
LOG_BACKUP_DIR = "log-backups"

# Directory name under <config>/ for logs and user-visible data
DATA_DIR_NAME = "esphome_update_manager"

# URL path for serving frontend files (JS, logo, lit library)
# Served directly from custom_components/esphome_update_manager/www/
FRONTEND_URL_PREFIX = "/esphome_update_manager_static"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _get_data_dir(hass: HomeAssistant) -> Path:
    """Get the path to the integration data directory (logs, backups)."""
    return Path(hass.config.path(DATA_DIR_NAME))


def _get_log_path(hass: HomeAssistant) -> Path:
    """Get the path to the update log file."""
    return _get_data_dir(hass) / LOG_FILENAME


def _get_log_backup_dir(hass: HomeAssistant) -> Path:
    """Get the path to the log backup directory."""
    return _get_data_dir(hass) / LOG_BACKUP_DIR


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

    # Always try local dashboard
    local_coordinator = LocalDashboardCoordinator(hass)
    if await local_coordinator.async_setup():
        hass.data[DOMAIN]["local_dashboard"] = local_coordinator
        _LOGGER.info("Local ESPHome dashboard API available")
    else:
        hass.data[DOMAIN]["local_dashboard"] = None
        _LOGGER.debug("Local ESPHome dashboard API not available")

    hass.data[DOMAIN]["local_yaml_cache"] = {}
    hass.data[DOMAIN]["project_version_cache"] = {}

    # Setup external dashboard if configured
    if dashboard_url:
        external_coordinator = ExternalDashboardCoordinator(
            hass, 
            dashboard_url,
            username=dashboard_username,
            password=dashboard_password,
        )
        hass.data[DOMAIN]["external_dashboard"] = external_coordinator

        # Connect in background - don't block HA startup
        async def _connect_external_dashboard():
            try:
                if await external_coordinator.async_check_connection(timeout=3):
                    await external_coordinator.async_refresh()
                    if external_coordinator.last_update_success:
                        _LOGGER.info(
                            "Connected to external ESPHome dashboard at %s (version %s)",
                            dashboard_url,
                            external_coordinator.esphome_version,
                        )
                    else:
                        _LOGGER.warning(
                            "External ESPHome dashboard at %s reachable but initial "
                            "data refresh failed: %s. Will retry periodically.",
                            dashboard_url,
                            external_coordinator.last_exception,
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
        external_coordinator.async_add_listener(lambda: None)
    else:
        hass.data[DOMAIN]["external_dashboard"] = None

    # Load stored settings
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_settings = await store.async_load()
    if stored_settings is None:
        stored_settings = {"auto_update_enabled": False}
    
    hass.data[DOMAIN]["store"] = store
    hass.data[DOMAIN]["settings"] = stored_settings
    hass.data[DOMAIN]["unsubscribe_listeners"] = []
    hass.data[DOMAIN]["failed_devices"] = stored_settings.get("failed_devices", {})
    hass.data[DOMAIN]["excluded_devices"] = stored_settings.get("excluded_devices", {})

    # Cleanup remembered external devices that no longer exist in HA
    async def _cleanup_remembered_devices(_hass: HomeAssistant) -> None:
        dev_reg = dr.async_get(hass)
        esphome_device_ids = _get_esphome_device_ids(hass)
        
        current_names: set[str] = set()
        for device in dev_reg.devices.values():
            if device.id in esphome_device_ids:
                if device.name:
                    current_names.add(_normalize_device_name(device.name))
                if device.name_by_user:
                    current_names.add(_normalize_device_name(device.name_by_user))
        
        remembered: dict = stored_settings.get("external_devices", {})
        to_remove = [n for n in remembered if n not in current_names]
        if to_remove:
            for n in to_remove:
                del remembered[n]
            stored_settings["external_devices"] = remembered
            hass.data[DOMAIN]["settings"] = stored_settings
            await store.async_save(stored_settings)
            _LOGGER.debug("Cleaned up %d stale remembered external devices", len(to_remove))
    
    async_at_started(hass, _cleanup_remembered_devices)

    @callback
    def _handle_device_registry_updated(event: Event) -> None:
        """Handle device registry updates - cleanup remembered external devices."""
        action = event.data.get("action")
        if action != "remove":
            return

        # Force refresh external coordinator so cache is up to date
        external_coordinator = hass.data[DOMAIN].get("external_dashboard")
        if external_coordinator:
            hass.async_create_task(external_coordinator.async_request_refresh())
            _LOGGER.debug("Forced external coordinator refresh after device removal")
            
        device_id = event.data.get("device_id")
        if not device_id:
            return
        
        settings = hass.data[DOMAIN].get("settings", {})
        remembered: dict = settings.get("external_devices", {})
        if not remembered:
            return

        # Check if any remembered device no longer exists in HA
        dev_reg = dr.async_get(hass)
        esphome_device_ids = _get_esphome_device_ids(hass)
        
        current_names: set[str] = set()
        for device in dev_reg.devices.values():
            if device.id in esphome_device_ids:
                if device.name:
                    current_names.add(_normalize_device_name(device.name))
                if device.name_by_user:
                    current_names.add(_normalize_device_name(device.name_by_user))
        
        to_remove = [n for n in remembered if n not in current_names]
        if to_remove:
            for n in to_remove:
                del remembered[n]
            settings["external_devices"] = remembered
            hass.data[DOMAIN]["settings"] = settings
            store: Store = hass.data[DOMAIN].get("store")
            if store:
                hass.async_create_task(store.async_save(settings))
            _LOGGER.debug("Removed %d stale remembered external devices after device removal", len(to_remove))

        # Cleanup pending_force_installs that no longer exist in HA
        pending_fi: dict = settings.get("pending_force_installs", {})
        if pending_fi:
            to_remove_fi = [
                did for did in pending_fi
                if dev_reg.async_get(did) is None
            ]
            if to_remove_fi:
                for did in to_remove_fi:
                    del pending_fi[did]
                settings["pending_force_installs"] = pending_fi
                hass.data[DOMAIN]["settings"] = settings
                store: Store = hass.data[DOMAIN].get("store")
                if store:
                    hass.async_create_task(store.async_save(settings))
                _LOGGER.debug("Removed %d stale pending force installs after device removal", len(to_remove_fi))

        # Cleanup excluded_devices that no longer exist in HA
        excluded_dev: dict = settings.get("excluded_devices", {})
        if excluded_dev:
            to_remove_exc = [
                did for did in excluded_dev
                if dev_reg.async_get(did) is None
            ]
            if to_remove_exc:
                for did in to_remove_exc:
                    del excluded_dev[did]
                settings["excluded_devices"] = excluded_dev
                hass.data[DOMAIN]["settings"] = settings
                hass.data[DOMAIN]["excluded_devices"] = excluded_dev
                store: Store = hass.data[DOMAIN].get("store")
                if store:
                    hass.async_create_task(store.async_save(settings))
                _LOGGER.debug("Removed %d stale excluded devices after device removal", len(to_remove_exc))

        # Cleanup project_version_cache for removed devices
        pv_cache: dict = hass.data[DOMAIN].get("project_version_cache", {})
        if pv_cache:
            to_remove_pv = [
                did for did in pv_cache
                if dev_reg.async_get(did) is None
            ]
            if to_remove_pv:
                for did in to_remove_pv:
                    del pv_cache[did]
                hass.data[DOMAIN]["project_version_cache"] = pv_cache
                _LOGGER.debug("Removed %d stale project version cache entries after device removal", len(to_remove_pv))

    unsub_device_registry = hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED,
        _handle_device_registry_updated,
    )
    hass.data[DOMAIN]["unsub_device_registry"] = unsub_device_registry

    @callback
    def _periodic_device_refresh(_now) -> None:
        """Periodically notify frontend so runtime_data fallback updates are picked up
        and check pending force installs (for devices without status sensor)."""
        hass.bus.async_fire("esphome_update_manager_devices_updated")
        hass.async_create_task(_check_pending_force_installs(hass))

    unsub_periodic = async_track_time_interval(
        hass, _periodic_device_refresh, timedelta(seconds=60)
    )
    hass.data[DOMAIN]["unsub_periodic_refresh"] = unsub_periodic

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
    websocket_api.async_register_command(hass, ws_subscribe_devices_updated)
    websocket_api.async_register_command(hass, ws_add_pending_force_install)
    websocket_api.async_register_command(hass, ws_remove_pending_force_install)
    websocket_api.async_register_command(hass, ws_set_excluded_devices)
    websocket_api.async_register_command(hass, ws_refresh_project_versions)

    # Register static paths to serve frontend files directly from the
    # custom_components folder. This avoids any dependency on <config>/www/.
    frontend_source_dir = Path(__file__).parent / "www"
    brand_source_dir = Path(__file__).parent / "brand"
    await _register_frontend_static_path(hass, frontend_source_dir)
    await _register_static_path(hass, f"{FRONTEND_URL_PREFIX}/brand", brand_source_dir)

    # Migrate logs from old <config>/www/esphome-update-manager/ location
    # to the new <config>/esphome_update_manager/ location.
    await hass.async_add_executor_job(_migrate_logs_from_www, hass)

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
                    "module_url": f"{FRONTEND_URL_PREFIX}/esphome-update-panel.js?v={version}",
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
            device_id = r.get("device_id")
            status = r.get("status")
            is_force_install = r.get("is_force_install", False)

            if not device_id:
                continue

            # Don't track force install failures - user must manually retry
            if is_force_install:
                continue

            if status == "failed":
                failed_devices[device_id] = True
                _LOGGER.debug("Added %s to failed devices - requires manual update", device_id)
            elif status == "success" and device_id in failed_devices:
                failed_devices.pop(device_id, None)
                _LOGGER.debug("Removed %s from failed devices - update succeeded", device_id)

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
    
        # Trigger pending force installs check (a device may have come online during the run)
        hass.async_create_task(_check_pending_force_installs(hass))

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

    # Force install service
    async def _handle_force_install(call: ServiceCall) -> None:
        await async_handle_force_install(hass, call)
    
    hass.services.async_register(
        DOMAIN,
        "force_install",
        _handle_force_install,
        schema=vol.Schema({
            vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
        }),
    )

    # Refresh project versions service (for use in automations)
    async def _handle_refresh_project_versions(call: ServiceCall) -> None:
        local_coordinator = hass.data[DOMAIN].get("local_dashboard")
        if local_coordinator:
            try:
                await local_coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.debug("Local dashboard refresh failed during service call: %s", err)

        external_coordinator = hass.data[DOMAIN].get("external_dashboard")
        if external_coordinator:
            try:
                await external_coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.debug("External dashboard refresh failed during service call: %s", err)

        await _check_all_devices_project_version(hass)
        await _check_pending_force_installs(hass)
        await _check_and_start_auto_update(hass)
        hass.bus.async_fire("esphome_update_manager_devices_updated")
        _LOGGER.info("refresh_project_versions service triggered")

    hass.services.async_register(
        DOMAIN,
        "refresh_project_versions",
        _handle_refresh_project_versions,
        schema=vol.Schema({}),
    )

    # Setup auto-update listener if enabled (after HA is fully started)
    if stored_settings.get("auto_update_enabled", False):
        hass.async_create_task(_delayed_setup_auto_update(hass))

    # Always setup status listener for project version check
    hass.async_create_task(_delayed_setup_status_listener(hass))

    # Register as lovelace resource
    url = f"{FRONTEND_URL_PREFIX}/esphome-update-panel.js?v={version}"
    await _register_lovelace_resource(hass, url)

    return True

async def _register_lovelace_resource(hass, url):
    try:
        lovelace_data = hass.data.get("lovelace")
        if lovelace_data is None:
            _LOGGER.warning("Could not auto-register lovelace resource. Add manually: %s", url)
            return

        resources = getattr(lovelace_data, "resources", None)
        if resources is None:
            _LOGGER.warning("Could not auto-register lovelace resource (no resources collection). Add manually: %s", url)
            return

        # Find existing resources that belong to us
        existing_ours = []
        already_correct = False
        for item in resources.async_items():
            item_url = item.get("url", "")
            # Match any old or new URL pointing to our panel
            if (
                "esphome-update-manager/esphome-update-panel.js" in item_url  # old /local/ URL
                or "esphome_update_manager_static/esphome-update-panel.js" in item_url  # new URL (any version)
            ):
                if item_url == url:
                    already_correct = True
                else:
                    existing_ours.append(item)

        # Remove stale/outdated resource entries (old URL or old version)
        for item in existing_ours:
            try:
                await resources.async_delete_item(item["id"])
                _LOGGER.info("Removed outdated lovelace resource: %s", item.get("url"))
            except Exception as err:
                _LOGGER.debug("Could not remove stale resource %s: %s", item.get("url"), err)

        if already_correct:
            return

        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.info("Registered lovelace resource: %s", url)
    except Exception as err:
        _LOGGER.warning("Failed to register lovelace resource: %s", err)

async def _delayed_setup_status_listener(hass: HomeAssistant) -> None:
    """Setup status listener always (for project version check)."""
    async def _setup_after_start(_hass: HomeAssistant) -> None:
        await asyncio.sleep(30)
        await _setup_always_on_status_listener(hass)
    
    async_at_started(hass, _setup_after_start)


async def _setup_always_on_status_listener(hass: HomeAssistant) -> None:
    """Setup status listener that is always active, independent of auto-update."""
    ent_reg = er.async_get(hass)
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

    if not all_status_entity_ids:
        _LOGGER.debug("No status entities found for always-on status listener")
        return

    @callback
    def _handle_status_state_change(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return
        if new_state.state != "on":
            return
        if old_state is not None and old_state.state == "on":
            return

        async def _delayed_check():
            await asyncio.sleep(5)
            await _check_project_version_update(hass, entity_id)
            await _check_pending_force_installs(hass)

        hass.async_create_task(_delayed_check())

    async def _check_all_online_devices() -> None:
        """Check all devices (online + offline) for project version updates.

        Offline devices only update the cache (so the frontend can show the
        pending bump) — they are not queued for auto force install.
        """
        _LOGGER.debug("Checking all devices for project version updates")
        for entity_id in all_status_entity_ids:
            hass.async_create_task(_check_project_version_update(hass, entity_id))

    @callback
    def _handle_dashboard_available(event: Event) -> None:
        """Handle external dashboard coming online."""
        if not event.data.get("available"):
            return
        _LOGGER.debug("External dashboard came online - checking all online devices for project version updates")
        hass.async_create_task(_check_all_online_devices())
        hass.async_create_task(_check_pending_force_installs(hass))

    unsub = async_track_state_change_event(
        hass,
        all_status_entity_ids,
        _handle_status_state_change,
    )
    hass.data[DOMAIN]["unsub_always_on_status"] = unsub

    # Subscribe to external dashboard availability changes
    unsub_dashboard = hass.bus.async_listen(
        "esphome_update_manager_dashboard_changed",
        _handle_dashboard_available,
    )
    hass.data[DOMAIN]["unsub_always_on_dashboard"] = unsub_dashboard

    # Initial check: HA just restarted, check all currently online devices
    await _check_all_online_devices()

    _LOGGER.debug(
        "Always-on status listener active for %d status sensors",
        len(all_status_entity_ids),
    )


async def _delayed_setup_auto_update(hass: HomeAssistant) -> None:
    """Setup auto-update listener after Home Assistant is fully started."""
    
    async def _setup_after_start(_hass: HomeAssistant) -> None:
        # Additional delay to ensure all entities are fully loaded
        await asyncio.sleep(30)
        _LOGGER.debug("Setting up auto-update listener after Home Assistant started")
        await _setup_auto_update_listener(hass)
        # Check for any pending updates
        await _check_and_start_auto_update(hass)
    
    async_at_started(hass, _setup_after_start)


async def _register_static_path(hass: HomeAssistant, url_path: str, source_dir: Path) -> None:
    """Register a static path for serving files from a directory."""
    if not source_dir.exists():
        _LOGGER.debug("Static path source directory does not exist, skipping: %s", source_dir)
        return

    try:
        # Modern API (HA 2024.7+)
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths([
            StaticPathConfig(url_path, str(source_dir), cache_headers=False)
        ])
        _LOGGER.debug("Registered static path %s -> %s", url_path, source_dir)
        return
    except ImportError:
        pass
    except Exception as err:
        _LOGGER.debug("async_register_static_paths failed for %s, falling back: %s", url_path, err)

    # Fallback for older HA versions
    try:
        hass.http.register_static_path(url_path, str(source_dir), cache_headers=False)
        _LOGGER.debug("Registered static path (legacy) %s -> %s", url_path, source_dir)
    except Exception as err:
        _LOGGER.error("Failed to register static path %s: %s", url_path, err)


async def _register_frontend_static_path(hass: HomeAssistant, source_dir: Path) -> None:
    """Register the main frontend static path."""
    await _register_static_path(hass, FRONTEND_URL_PREFIX, source_dir)


def _migrate_logs_from_www(hass: HomeAssistant) -> None:
    """Migrate logs and backups from old <config>/www/esphome-update-manager/
    to the new <config>/esphome_update_manager/ location.

    Also cleans up old frontend files (js, logo, lit) that were previously
    copied to www/ but are no longer used.
    """
    old_dir = Path(hass.config.path("www")) / "esphome-update-manager"
    new_dir = _get_data_dir(hass)

    if not old_dir.exists():
        return

    try:
        new_dir.mkdir(parents=True, exist_ok=True)

        # Migrate main log file
        old_log = old_dir / LOG_FILENAME
        new_log = new_dir / LOG_FILENAME
        if old_log.exists() and not new_log.exists():
            shutil.move(str(old_log), str(new_log))
            _LOGGER.info("Migrated update log to %s", new_log)

        # Migrate backup directory
        old_backups = old_dir / LOG_BACKUP_DIR
        new_backups = new_dir / LOG_BACKUP_DIR
        if old_backups.exists() and not new_backups.exists():
            shutil.move(str(old_backups), str(new_backups))
            _LOGGER.info("Migrated log backups to %s", new_backups)

        # Clean up stale frontend files we used to copy there
        for stale in ("esphome-update-panel.js", "logo.png"):
            f = old_dir / stale
            if f.exists():
                try:
                    f.unlink()
                except Exception as err:
                    _LOGGER.debug("Could not remove stale file %s: %s", f, err)

        lit_dir = old_dir / "lit"
        if lit_dir.exists():
            try:
                shutil.rmtree(lit_dir)
            except Exception as err:
                _LOGGER.debug("Could not remove stale lit dir: %s", err)

        # Remove old directory if now empty
        try:
            if old_dir.exists() and not any(old_dir.iterdir()):
                old_dir.rmdir()
                _LOGGER.info("Removed empty legacy directory %s", old_dir)
        except Exception as err:
            _LOGGER.debug("Could not remove old dir %s: %s", old_dir, err)
    except Exception as err:
        _LOGGER.warning("Log migration from www/ failed: %s", err)


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
            entity_id = r.get("name") or r.get("entity_id", "Unknown")
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
            if r.get("is_force_install"):
                lines.append(f"   Type: Force Install (compile + OTA)")
            else:
                lines.append(f"   Type: Firmware Update (OTA)")
            if status == "success" and from_version and to_version:
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
    unsub_always_on = hass.data[DOMAIN].get("unsub_always_on_status")
    if unsub_always_on:
        unsub_always_on()
    hass.data[DOMAIN].pop("unsub_always_on_status", None)
    unsub_always_on_dashboard = hass.data[DOMAIN].get("unsub_always_on_dashboard")
    if unsub_always_on_dashboard:
        unsub_always_on_dashboard()
    hass.data[DOMAIN].pop("unsub_always_on_dashboard", None)
    unsub_device_registry = hass.data[DOMAIN].get("unsub_device_registry")
    if unsub_device_registry:
        unsub_device_registry()
    hass.data[DOMAIN].pop("unsub_device_registry", None)
    unsub_periodic = hass.data[DOMAIN].get("unsub_periodic_refresh")
    if unsub_periodic:
        unsub_periodic()
    hass.data[DOMAIN].pop("unsub_periodic_refresh", None)

    # Remove services
    hass.services.async_remove(DOMAIN, "start_updates")
    hass.services.async_remove(DOMAIN, "force_install")
    hass.services.async_remove(DOMAIN, "refresh_project_versions")
    
    hass.data[DOMAIN].pop("queue", None)
    hass.data[DOMAIN].pop("store", None)
    hass.data[DOMAIN].pop("settings", None)
    hass.data[DOMAIN].pop("unsubscribe_listeners", None)
    hass.data[DOMAIN].pop("unsub_finished", None)
    hass.data[DOMAIN].pop("external_dashboard", None)
    hass.data[DOMAIN].pop("local_dashboard", None)
    hass.data[DOMAIN].pop("failed_devices", None)
    hass.data[DOMAIN].pop("excluded_devices", None)
    hass.data[DOMAIN].pop("pending_project_installs", None)
    hass.data[DOMAIN].pop("pending_project_install_timer", None)
    hass.data[DOMAIN].pop("pending_force_install_timer", None)
    hass.data[DOMAIN].pop("pending_force_install_retry", None)
    hass.data[DOMAIN].pop("project_version_cache", None)
    hass.data[DOMAIN].pop("project_version_check_running", None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of the config entry."""
    _LOGGER.info("Removing ESPHome Update Manager - cleaning up files")

    # Remove new data directory (logs + backups)
    data_dir = _get_data_dir(hass)
    if await hass.async_add_executor_job(data_dir.exists):
        await hass.async_add_executor_job(shutil.rmtree, data_dir)
        _LOGGER.info("Removed data directory: %s", data_dir)

    # Also clean up any legacy files still present in <config>/www/
    legacy_dir = Path(hass.config.path("www")) / "esphome-update-manager"
    if await hass.async_add_executor_job(legacy_dir.exists):
        await hass.async_add_executor_job(shutil.rmtree, legacy_dir)
        _LOGGER.info("Removed legacy www directory: %s", legacy_dir)

    # Remove storage file
    storage_path = Path(hass.config.path(".storage")) / STORAGE_KEY
    if await hass.async_add_executor_job(storage_path.exists):
        await hass.async_add_executor_job(storage_path.unlink)
        _LOGGER.info("Removed storage file: %s", storage_path)


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
        _LOGGER.debug(
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
        _LOGGER.debug(
            "ESPHome device came online: %s (state: %s -> %s), checking for updates in 5 seconds",
            entity_id,
            old_state_str,
            new_state.state,
        )

        async def _delayed_check():
            await asyncio.sleep(5)
            # First check project version update
            await _check_project_version_update(hass, entity_id)
            # Then check normal auto-update
            await _check_and_start_auto_update(hass)

        hass.async_create_task(_delayed_check())

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
    _LOGGER.debug(
        "Auto-update listener active. Monitoring %d update entities (%d ESPHome) and %d status sensors",
        len(all_update_entity_ids),
        len(esphome_update_entities),
        len(all_status_entity_ids),
    )
    _LOGGER.debug("Status sensors monitored: %s", all_status_entity_ids)


async def _check_project_version_update(hass: HomeAssistant, entity_id: str) -> None:
    """Check if a device needs a project version update.

    Cache update always runs (so frontend can show pending bumps).
    Auto force install only runs if settings allow it.
    """

    # Find device via entity_id (status sensor → device)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    entity = ent_reg.async_get(entity_id)
    if not entity or not entity.device_id:
        return

    device = dev_reg.async_get(entity.device_id)
    if not device:
        return

    # Skip sub-devices
    if device.via_device_id is not None:
        return

    original_name = device.name
    if not original_name:
        return

    display_name = device.name_by_user or original_name

    # NOTE: excluded check moved below — we still want to cache the bump
    # so the frontend can show it. Excluded only prevents the auto force install.

    # Skip if device was recently successfully updated — also clear stale cache
    queue = hass.data[DOMAIN].get("queue")
    if queue:
        for result in queue.results:
            if result.get("device_id") == entity.device_id and result.get("status") == "success":
                _LOGGER.debug(
                    "%s: Recently successfully updated, skipping project version check",
                    display_name
                )
                cache = hass.data[DOMAIN].get("project_version_cache", {})
                if cache.pop(entity.device_id, None) is not None:
                    hass.bus.async_fire("esphome_update_manager_devices_updated")
                return

    raw_version = device.sw_version
    if not raw_version:
        return

    match = re.match(r'^([\d.]+)\s*\(ESPHome', str(raw_version))
    if not match:
        _LOGGER.debug("%s: No project version in sw_version (%s), skipping project version check", display_name, raw_version)
        return

    device_project_version = match.group(1)

    local_coordinator = hass.data[DOMAIN].get("local_dashboard")
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")

    coordinator = None
    configuration = None

    if external_coordinator and external_coordinator.available:
        mac_suffix = _get_mac_suffix(device)
        external_device = _match_device_to_external_dashboard(
            hass, device.name_by_user or original_name, original_name=original_name, mac_suffix=mac_suffix
        )
        if external_device:
            coordinator = external_coordinator
            configuration = external_device.get("configuration", original_name)

    if not coordinator and local_coordinator and local_coordinator.available:
        coordinator = local_coordinator
        mac_suffix = _get_mac_suffix(device)
        configuration = _get_local_config_filename(original_name, mac_suffix)

    if not coordinator:
        _LOGGER.debug("%s: No dashboard available for project version check", display_name)
        return

    if not configuration.endswith(".yaml"):
        configuration = f"{configuration}.yaml"

    try:
        config = await coordinator.async_get_config(configuration)
    except Exception as err:
        _LOGGER.debug("%s: Failed to get config for project version check: %s", display_name, err)
        return

    if not config:
        return

    project = config.get("esphome", {}).get("project", {})
    yaml_version = project.get("version")

    if not yaml_version:
        _LOGGER.debug("%s: No project version in yaml, skipping", display_name)
        return

    try:
        yaml_v = parse_version(yaml_version)
        device_v = parse_version(device_project_version)
    except Exception as err:
        _LOGGER.warning("%s: Failed to parse project versions (yaml=%s, device=%s): %s", display_name, yaml_version, device_project_version, err)
        return

    # Update cache (used by frontend to show pending project bumps)
    cache = hass.data[DOMAIN].setdefault("project_version_cache", {})
    if yaml_v > device_v:
        prev = cache.get(entity.device_id)
        cache[entity.device_id] = yaml_version
        if prev != yaml_version:
            hass.bus.async_fire("esphome_update_manager_devices_updated")
    else:
        # No bump (or downgrade) — clear any stale cache entry
        if cache.pop(entity.device_id, None) is not None:
            hass.bus.async_fire("esphome_update_manager_devices_updated")
        _LOGGER.debug("%s: Project version up to date (yaml=%s, device=%s)", display_name, yaml_version, device_project_version)
        return

    # Cache is updated. Now decide whether we should auto force install.
    settings = hass.data[DOMAIN].get("settings", {})
    if not settings.get("auto_update_enabled", False):
        _LOGGER.debug(
            "%s: project bump cached but auto-update disabled, not queuing", display_name,
        )
        return
    if settings.get("auto_update_scope", "firmware") == "firmware":
        _LOGGER.debug(
            "%s: project bump cached but scope=firmware, not queuing", display_name,
        )
        return

    # Excluded? Cache has been updated above so frontend will still show the bump.
    excluded_devices = hass.data[DOMAIN].get("excluded_devices", {})
    if entity.device_id in excluded_devices:
        _LOGGER.debug(
            "%s: device is excluded, project bump cached for frontend but not queued",
            display_name,
        )
        return

    # Only queue auto force install when device is actually online — offline
    # devices will be picked up by the online-flow when they come back.
    state = hass.states.get(entity_id)
    if not (state and state.state == "on"):
        _LOGGER.debug(
            "%s: project bump cached but device offline, not queuing",
            display_name,
        )
        return

    _LOGGER.info(
        "%s: Project version update needed: %s → %s, queuing force install",
        display_name, device_project_version, yaml_version,
    )

    pending = hass.data[DOMAIN].setdefault("pending_project_installs", {})
    pending[entity.device_id] = {
        "entity_id": f"force:{_normalize_device_name(original_name)}",
        "device_id": entity.device_id,
        "name": display_name,
        "from_version": raw_version,
        "to_version": _build_to_version(raw_version, None, yaml_version),
    }

    if hass.data[DOMAIN].get("pending_project_install_timer"):
        return

    async def _flush_pending():
        await asyncio.sleep(15)
        hass.data[DOMAIN].pop("pending_project_install_timer", None)
        settings = hass.data[DOMAIN].get("settings", {})
        if (
            not settings.get("auto_update_enabled", False)
            or settings.get("auto_update_scope", "firmware") == "firmware"
        ):
            hass.data[DOMAIN].pop("pending_project_installs", None)
            return
        await _flush_pending_project_installs(hass)

    hass.data[DOMAIN]["pending_project_install_timer"] = True
    hass.async_create_task(_flush_pending())


async def _check_all_devices_project_version(hass: HomeAssistant) -> None:
    """Trigger a project version check on all ESPHome devices (online + offline).

    For offline devices, only the cache is updated (so the frontend can show
    the pending bump). Auto force install is only queued for online devices.

    Dedup guard: if a check is already running, a second concurrent call is
    skipped to avoid duplicate yaml fetches when the user rapidly toggles
    settings or clicks refresh.
    """
    if hass.data[DOMAIN].get("project_version_check_running"):
        _LOGGER.debug("Project version check already in progress, skipping duplicate")
        return

    hass.data[DOMAIN]["project_version_check_running"] = True
    try:
        ent_reg = er.async_get(hass)
        esphome_device_ids = _get_esphome_device_ids(hass)

        status_entity_ids = [
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

        await asyncio.gather(
            *[_check_project_version_update(hass, eid) for eid in status_entity_ids],
            return_exceptions=True,
        )
    finally:
        hass.data[DOMAIN].pop("project_version_check_running", None)


async def _flush_pending_project_installs(hass: HomeAssistant) -> None:
    """Flush pending project version installs."""
    pending = hass.data[DOMAIN].pop("pending_project_installs", {})
    if not pending:
        return

    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    if queue.is_running:
        hass.data[DOMAIN]["pending_project_installs"] = pending
        async def _retry():
            await asyncio.sleep(60)
            await _flush_pending_project_installs(hass)
        hass.async_create_task(_retry())
        return

    # Re-validate: drop entries whose project bump is no longer pending
    # (e.g. a firmware update meanwhile recompiled the yaml and applied it).
    pv_cache = hass.data[DOMAIN].get("project_version_cache", {})
    dev_reg = dr.async_get(hass)
    stale_ids = []
    for device_id in list(pending.keys()):
        device = dev_reg.async_get(device_id)
        if not device:
            stale_ids.append(device_id)
            continue
        # If cache no longer has this device, the bump was already applied
        if device_id not in pv_cache:
            stale_ids.append(device_id)
            continue
        # Double-check vs current sw_version
        raw = device.sw_version
        if raw:
            match = re.match(r'^([\d.]+)\s*\(ESPHome', str(raw))
            if match:
                try:
                    if parse_version(match.group(1)) >= parse_version(pv_cache[device_id]):
                        stale_ids.append(device_id)
                        continue
                except Exception:
                    pass

    for sid in stale_ids:
        pending.pop(sid, None)
        _LOGGER.debug(
            "Dropped stale pending project install for %s (bump already applied)",
            sid,
        )

    if not pending:
        return

    force_install_devices = list(pending.values())
    _LOGGER.info(
        "Starting force install for %d device(s) with project version update: %s",
        len(force_install_devices),
        [d["name"] for d in force_install_devices],
    )
    stop_addon_slug = None
    settings = hass.data[DOMAIN].get("settings", {})
    if settings.get("stop_addon_during_update", True):
        addon_info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
        if addon_info and addon_info.get("state") == "started":
            stop_addon_slug = VSCODE_ADDON_SLUG
    try:
        queue.start(
            entity_ids=[d["entity_id"] for d in force_install_devices],
            force_install_devices=force_install_devices,
            stop_addon_slug=stop_addon_slug,
        )
    except RuntimeError as err:
        _LOGGER.warning("Failed to start force install for project version: %s", err)
        

async def _build_version_info_for_devices(hass: HomeAssistant, devices: list[dict]) -> tuple[list[str], dict]:
    updatable = []
    version_info = {}

    for d in devices:
        entity_id = d["entity_id"]
        from_ver = d.get("sw_version_raw") or d["current_version"]
        new_project_version = None
        if from_ver and re.match(r'^([\d.]+)\s*\(ESPHome', str(from_ver)):
            new_project_version = await _get_yaml_project_version(hass, d)

        # Extract pure ESPHome version from latest_version (which may be
        # formatted as "2026.4.5 (2.29)" after pv_cache enrichment)
        latest_esphome = _extract_esphome_version(d["latest_version"]) or d["latest_version"]

        updatable.append(entity_id)
        version_info[entity_id] = {
            "from": _strip_sw_version_timestamp(from_ver),
            "to": _build_to_version(from_ver, latest_esphome, new_project_version),
            "name": d["name"],
            "device_id": d["device_id"],
        }

    return updatable, version_info

async def _check_and_start_auto_update(hass: HomeAssistant) -> None:
    """Check for available updates and start them automatically."""
    settings = hass.data[DOMAIN].get("settings", {})

    if not settings.get("auto_update_enabled", False):
        return

    # Respect scope: skip firmware updates in project-only mode
    scope = settings.get("auto_update_scope", "firmware")
    if scope == "project":
        return

    queue: UpdateQueue = hass.data[DOMAIN]["queue"]

    # Don't start if already running
    if queue.is_running:
        return

    # Get failed and excluded devices (permanent until manual success)
    failed_devices = hass.data[DOMAIN].get("failed_devices", {})
    excluded_devices = hass.data[DOMAIN].get("excluded_devices", {})

    # Get all devices with available updates
    devices = _get_esphome_update_entities(hass)

    updatable_devices = []
    for d in devices:
        entity_id = d["entity_id"]

        # Skip devices that previously failed
        device_id = d.get("device_id")
        if device_id and device_id in failed_devices:
            continue
        if device_id and device_id in excluded_devices:
            continue

        # Project-only bumps must always go via the project flow (force install),
        # never via the firmware-OTA flow — regardless of scope.
        if d.get("project_bump_only"):
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
            updatable_devices.append(d)

    if not updatable_devices:
        return

    updatable, version_info = await _build_version_info_for_devices(hass, updatable_devices)

    _LOGGER.debug("Starting auto-update for %d devices: %s", len(updatable), updatable)

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
    _LOGGER.debug("Auto-update listener removed")


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
    devices = _get_esphome_update_entities(hass)

    if entity_ids:
        device_map = {d["entity_id"]: d for d in devices}
        updatable_devices = [device_map[eid] for eid in entity_ids if eid in device_map]
    else:
        updatable_devices = [
            d for d in devices
            if (
                d["entity_id"]
                and d["update_available"]
                and not d["firmware_disabled"]
                and not d["firmware_unavailable"]
                and not d["enabling"]
                and d["online"] is not False
                and not d["in_progress"]
            )
        ]

    if not updatable_devices:
        _LOGGER.info("No devices available for update")
        return

    updatable, version_info = await _build_version_info_for_devices(hass, updatable_devices)

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

async def async_handle_force_install(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the force_install service call.

    Online devices are force-installed immediately.
    Offline devices are added to the pending_force_installs list and will
    be force-installed automatically once they come online.
    """
    device_ids = call.data.get("device_id")
    if not device_ids:
        raise HomeAssistantError("No device_id provided")

    if isinstance(device_ids, str):
        device_ids = [device_ids]

    device_ids = list(dict.fromkeys(device_ids))

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    online_devices: list[dict] = []
    offline_device_ids: list[str] = []

    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if not device:
            continue
        if device.via_device_id is not None:
            continue

        name = device.name_by_user or device.name or device_id
        raw = device.sw_version

        # Split into online vs offline
        if _is_device_online(hass, ent_reg, device_id) is True:
            online_devices.append({
                "entity_id": f"force:{_normalize_device_name(name)}",
                "device_id": device_id,
                "name": name,
                "from_version": raw,
                "to_version": raw,
            })
        else:
            offline_device_ids.append(device_id)

    if not online_devices and not offline_device_ids:
        raise HomeAssistantError("No valid devices found")

    settings = hass.data[DOMAIN].get("settings", {})
    store: Store = hass.data[DOMAIN].get("store")

    # Save offline devices to pending_force_installs
    if offline_device_ids:
        pending: dict = settings.get("pending_force_installs", {})
        for did in offline_device_ids:
            device = dev_reg.async_get(did)
            if not device:
                continue
            pending[did] = {
                "name": device.name_by_user or device.name or did,
                "queued_at": datetime.now().isoformat(),
            }
        settings["pending_force_installs"] = pending
        hass.data[DOMAIN]["settings"] = settings
        if store:
            await store.async_save(settings)
        hass.bus.async_fire("esphome_update_manager_devices_updated")
        _LOGGER.info(
            "Added %d offline device(s) to pending force install list",
            len(offline_device_ids),
        )
        # In case any are actually already online, trigger a check
        hass.async_create_task(_check_pending_force_installs(hass))

    # If there are no online devices, we are done
    if not online_devices:
        return

    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    if queue.is_running:
        # Online devices can't run now → fall back to pending so they retry later
        pending: dict = settings.get("pending_force_installs", {})
        for d in online_devices:
            pending[d["device_id"]] = {
                "name": d["name"],
                "queued_at": datetime.now().isoformat(),
            }
        settings["pending_force_installs"] = pending
        hass.data[DOMAIN]["settings"] = settings
        if store:
            await store.async_save(settings)
        hass.bus.async_fire("esphome_update_manager_devices_updated")
        raise HomeAssistantError(
            "Update queue is already running. "
            f"{len(online_devices)} device(s) added to pending force install list."
        )

    stop_addon_slug = None
    if settings.get("stop_addon_during_update", True):
        addon_info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
        if addon_info and addon_info.get("state") == "started":
            stop_addon_slug = VSCODE_ADDON_SLUG

    try:
        queue.start(
            entity_ids=[d["entity_id"] for d in online_devices],
            force_install_devices=online_devices,
            stop_addon_slug=stop_addon_slug,
        )
        _LOGGER.info(
            "Started force install for %d online device(s): %s",
            len(online_devices),
            [d["name"] for d in online_devices],
        )
    except RuntimeError as err:
        raise HomeAssistantError(str(err)) from err

# ── Supervisor / Add-on helpers ────────────────────────────────────

async def _supervisor_api(hass: HomeAssistant, method: str, path: str) -> dict | None:
    """Call the Supervisor API."""

    # Skip silently on non-Supervisor installs (Container / Core)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None

    try:
        session = async_get_clientsession(hass)
        url = f"http://supervisor{path}"
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
    """Extract ESPHome version from a version string (including suffix)."""
    if not version_string:
        return None
    
    version_string = version_string.strip()
    
    # Check for project version format: "X.XX (ESPHome YYYY.MM.N)"
    if "(ESPHome" in version_string:
        match = re.search(r"\(ESPHome\s+(20\d{2}\.\d+\.\d+(?:-dev|b\d+|rc\d+|a\d+)?)", version_string)
        if match:
            return match.group(1)
    
    # Standard ESPHome version: 20XX.X.X with optional suffix
    match = re.match(r"(20\d{2}\.\d+\.\d+(?:-dev|b\d+|rc\d+|a\d+)?)", version_string)
    if match:
        return match.group(1)
    
    return None


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    # Strip pre-release suffix for comparison (e.g. "2026.5.0-dev" -> "2026.5.0")
    base = re.match(r"(\d+(?:\.\d+)*)", str(version))
    if not base:
        return None
    try:
        return tuple(int(x) for x in base.group(1).split("."))
    except (ValueError, AttributeError):
        return None


def _is_update_available(installed: str | None, latest: str | None) -> bool:
    inst = _version_tuple(installed)
    lat = _version_tuple(latest)
    if inst is None or lat is None:
        return False
    return lat > inst


def _is_esphome_version(version: str | None) -> bool:
    """Check if version string contains or is an ESPHome version."""
    if not version:
        return True
    v = str(version)
    # YYYY.M.x format (4-digit year)
    if re.match(r"^20\d{2}\.\d+\.\d+", v):
        return True
    # Project version format: "1.0.2 (ESPHome 2026.3.3)"
    if "ESPHome" in v:
        return True
    return False


def _extract_esphome_version(version: str | None) -> str | None:
    """Extract ESPHome version (YYYY.M.x format with optional suffix) from a version string.
    
    Examples:
        "2026.3.3"                          -> "2026.3.3"
        "2026.5.0-dev"                      -> "2026.5.0-dev"
        "2026.5.0b2"                        -> "2026.5.0b2"
        "1.0.5 (ESPHome 2026.3.3)"          -> "2026.3.3"
        "1.0.5"                             -> None
    """
    if not version:
        return None
    match = re.search(r"(20\d{2}\.\d+\.\d+(?:-dev|b\d+|rc\d+|a\d+)?)", version)
    if match:
        return match.group(1)
    return None


def _format_display_version(sw_version_raw: str | None, esphome_version: str | None) -> str | None:
    """Format version for the frontend panel.

    If sw_version_raw has the project format 'X.Y.Z (ESPHome YYYY.M.P)',
    return 'YYYY.M.P(X.Y.Z)'. Otherwise just the ESPHome version.
    """
    if not esphome_version:
        return esphome_version
    if not sw_version_raw:
        return esphome_version
    match = re.match(r'^([\d.]+)\s*\(ESPHome', str(sw_version_raw))
    if match:
        return f"{esphome_version} ({match.group(1)})"
    return esphome_version


def _strip_sw_version_timestamp(version: str | None) -> str | None:
    """Strip compile timestamp from sw_version, keep ESPHome project format intact.

    - "2026.4.4 (2026-05-05 11:46:10 +0200)" → "2026.4.4"
    - "2.28 (ESPHome 2025.12.0)"             → "2.28 (ESPHome 2025.12.0)"
    - "2026.4.5"                             → "2026.4.5"
    """
    if not version:
        return version
    v = str(version)
    # Keep the project format
    if "ESPHome" in v:
        return v
    # Strip "(YYYY-MM-DD HH:MM:SS ±ZZZZ)" timestamp
    return re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[^)]*\)\s*$", "", v).strip()


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
    """Normalize device name for matching (ESPHome style: lowercase, only a-z, 0-9, - allowed)."""
    normalized = name.lower()
    # Replace spaces and underscores with dashes
    normalized = normalized.replace(" ", "-").replace("_", "-")
    # Remove characters not allowed in ESPHome names
    normalized = re.sub(r'[^a-z0-9-]', '', normalized)
    # Reduce multiple dashes to single dash
    normalized = re.sub(r'-+', '-', normalized)
    # Remove leading/trailing dashes
    normalized = normalized.strip('-')
    return normalized


def _get_mac_suffix(device: dr.DeviceEntry) -> str | None:
    for conn_type, conn_val in device.connections:
        if conn_type == dr.CONNECTION_NETWORK_MAC:
            mac_clean = conn_val.replace(":", "").replace("-", "").lower()
            if len(mac_clean) == 12:
                return mac_clean[-6:]
    return None


def _build_to_version(sw_version_raw: str | None, new_esphome_version: str | None, new_project_version: str | None = None) -> str | None:
    """Build expected to_version string based on current sw_version format."""
    if not sw_version_raw:
        return new_esphome_version
    
    # Check if sw_version has project version format: "1.0.1 (ESPHome 2025.12.0)"
    match = re.match(r'^([\d.]+)\s*\(ESPHome\s+([\d.]+)\)', str(sw_version_raw))
    if match:
        project_v = new_project_version or match.group(1)
        esphome_v = new_esphome_version or match.group(2)
        return f"{project_v} (ESPHome {esphome_v})"
    
    # No project version → just return new ESPHome version
    return new_esphome_version or sw_version_raw


def _get_local_config_filename(name: str, mac_suffix: str | None = None) -> str:
    normalized = _normalize_device_name(name)
    if mac_suffix and normalized.endswith(f"-{mac_suffix}"):
        result = f"{normalized[:-7]}.yaml"
        return result
    return f"{normalized}.yaml"


def _match_device_to_external_dashboard(
    hass: HomeAssistant,
    device_name: str,
    entity_id: str | None = None,
    original_name: str | None = None,
    mac_suffix: str | None = None,
) -> dict | None:
    """Match a HA device to an external dashboard device."""
    coordinator = hass.data[DOMAIN].get("external_dashboard")
    if not coordinator or not coordinator.data:
        return None
    
    # First try matching by device name
    result = coordinator.get_device_by_name(device_name)
    if result:
        return result
    
    # Try original ESPHome name (before user renamed it)
    if original_name:
        result = coordinator.get_device_by_name(original_name)
        if result:
            return result
    
    # Try stripping MAC suffix - only if we can verify it's the actual MAC suffix
    if mac_suffix:
        normalized = _normalize_device_name(device_name)
        if normalized.endswith(f"-{mac_suffix}"):
            base_name = normalized[:-7]
            result = coordinator.get_device_by_name(base_name)
            if result:
                return result
        
        if original_name:
            normalized_original = _normalize_device_name(original_name)
            if normalized_original.endswith(f"-{mac_suffix}"):
                result = coordinator.get_device_by_name(normalized_original[:-7])
                if result:
                    return result

    # If no match and we have entity_id, try extracting from entity
    if entity_id:
        name_from_entity = (
            entity_id
            .replace("update.", "")
            .replace("_firmware_update", "")
            .replace("_update", "")
        )
        result = coordinator.get_device_by_name(name_from_entity)
        if result:
            return result
    
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
    """Determine online status with fallback chain.

    Priority:
    1. ESPHome 'status' binary_sensor (user-configured, fastest, most reliable)
    2. Fallback: ESPHome integration runtime_data.available (native API connection)
    3. None (unknown)
    """
    if not device_id:
        return None

    # 1. Status sensor (primary — always correct when present and available)
    status_entity_id = _find_status_entity(hass, ent_reg, device_id)
    if status_entity_id:
        state = hass.states.get(status_entity_id)
        if state is not None and state.state not in ("unavailable", "unknown"):
            return state.state == "on"
        # status sensor exists but is unavailable/unknown → fall through to fallback

    # 2. Fallback: ESPHome runtime_data.available (works for both local and external devices,
    #    as long as the device is part of the ESPHome integration in HA)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if device is None:
        return None

    try:
        for entry in hass.config_entries.async_entries("esphome"):
            if entry.entry_id in device.config_entries:
                runtime_data = getattr(entry, "runtime_data", None)
                if runtime_data is not None:
                    available = getattr(runtime_data, "available", None)
                    if available is not None:
                        return bool(available)
                break
    except Exception as err:
        _LOGGER.debug("runtime_data lookup failed for device %s: %s", device_id, err)

    return None


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


def _is_esphome_firmware_entity(
    hass: HomeAssistant,
    entity: er.RegistryEntry,
) -> bool:
    """Check if this update entity is for ESPHome firmware (not a custom update component)."""
    # Real ESPHome firmware entity always has original_device_class "firmware"
    # Custom update components have original_device_class None
    return entity.original_device_class == "firmware"


async def _refresh_local_yaml_cache(
    hass: HomeAssistant,
    checks: list[tuple[str, str]],
) -> None:
    """Background refresh of local yaml cache, then notify panel."""
    local_coordinator = hass.data[DOMAIN].get("local_dashboard")
    if not local_coordinator or not local_coordinator.available:
        return

    cache = hass.data[DOMAIN].setdefault("local_yaml_cache", {})
    changed = False

    for device_id, config_filename in checks:
        try:
            config = await local_coordinator.async_get_config(config_filename)
            cache[config_filename] = config is not None and bool(config)
            changed = True
        except Exception:
            cache[config_filename] = False

    if changed:
        hass.bus.async_fire("esphome_update_manager_devices_updated")


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
    
    # Load remembered external devices and pending force installs from storage
    settings = hass.data[DOMAIN].get("settings", {})
    remembered_external_devices: dict[str, bool] = dict(settings.get("external_devices", {}))
    remembered_external_devices_changed = False
    pending_force_installs: dict = settings.get("pending_force_installs", {})
    excluded_devices: dict = settings.get("excluded_devices", {})
    
    # Get failed devices for marking
    failed_devices = hass.data[DOMAIN].get("failed_devices", {})
    failed_devices_changed = False
    
    devices = []
    esphome_device_ids = _get_esphome_device_ids(hass)
    
    # Track which external devices we've already processed (by normalized name)
    processed_external_devices: set[str] = set()
    
    # Track which device_ids we've processed (for cleanup)
    processed_device_ids: set[str] = set()

    esphome_update_entities: list[er.RegistryEntry] = []
    devices_with_update_entity: set[str] = set()

    pending_yaml_checks: list[tuple[str, str]] = []

    for entity in ent_reg.entities.values():
        if (
            entity.domain == "update"
            and entity.device_id in esphome_device_ids
        ):
            esphome_update_entities.append(entity)
            if entity.device_id and entity.original_device_class == "firmware":
                devices_with_update_entity.add(entity.device_id)

    for entity in esphome_update_entities:
        if not _is_esphome_firmware_entity(hass, entity):
            _LOGGER.debug("Skipping non-ESPHome firmware entity: %s", entity.entity_id)
            continue
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
        
        if external_coordinator:
            original_name = None
            mac_suffix = None
            if device_id:
                dev = dev_reg.async_get(device_id)
                if dev:
                    original_name = dev.name
                    mac_suffix = _get_mac_suffix(dev)
            
            external_device_info = _match_device_to_external_dashboard(
                hass, name, entity_id, original_name=original_name, mac_suffix=mac_suffix
            )
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

        # Fallback: remembered as external even without coordinator
        if not is_external_device:
            if normalized_name in remembered_external_devices:
                if not external_coordinator or not external_dashboard_available:
                    _LOGGER.debug("Device %s: marked external via remembered_external_devices", name)
                    is_external_device = True
                    builder_version = None
                    is_dashboard_unavailable = True
                else:
                    _LOGGER.debug("Device %s: removing from remembered_external_devices (coordinator available, no match)", name)
                    del remembered_external_devices[normalized_name]
                    remembered_external_devices_changed = True
            else:
                dev = dev_reg.async_get(device_id) if device_id else None
                if dev and dev.name and _normalize_device_name(dev.name) in remembered_external_devices:
                    if not external_coordinator or not external_dashboard_available:
                        is_external_device = True
                        builder_version = None
                        is_dashboard_unavailable = True
                    else:
                        _LOGGER.debug("Device %s: removing original name from remembered_external_devices (coordinator available, no match)", name)
                        del remembered_external_devices[_normalize_device_name(dev.name)]
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
                "device_id": device_id,
                "name": name,
                "current_version": _format_display_version(registry_version_raw, installed),
                "latest_version": _format_display_version(registry_version_raw, builder_version) if update_available else None,
                "update_available": update_available and not is_dashboard_unavailable,
                "in_progress": False,
                "firmware_disabled": True,
                "firmware_unavailable": is_dashboard_unavailable,
                "enabling": False,
                "online": online,
                "skipped": False,
                "is_external": is_external_device,
                "failed": device_id is not None and device_id in failed_devices,
                "sw_version_raw": registry_version_raw,
                "pending_force_install": device_id is not None and device_id in pending_force_installs,
                "excluded": device_id is not None and device_id in excluded_devices,
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
                "device_id": device_id,
                "name": name,
                "current_version": _format_display_version(registry_version_raw, installed),
                "latest_version": _format_display_version(registry_version_raw, builder_version) if update_available else None,
                "update_available": update_available and not is_dashboard_unavailable,
                "in_progress": False,
                "firmware_disabled": False,
                "firmware_unavailable": is_fw_unavailable,
                "enabling": False,
                "online": online,
                "skipped": False,
                "is_external": is_external_device,
                "failed": device_id is not None and device_id in failed_devices,
                "sw_version_raw": registry_version_raw,
                "pending_force_install": device_id is not None and device_id in pending_force_installs,
                "excluded": device_id is not None and device_id in excluded_devices,
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
                "device_id": device_id,
                "name": name,
                "current_version": _format_display_version(registry_version_raw, installed),
                "latest_version": (
                    _format_display_version(registry_version_raw, latest)
                    if (ha_says_update and actually_newer) or is_skipped
                    else None
                ),
                "update_available": ha_says_update and actually_newer and not is_dashboard_unavailable,
                "in_progress": state.attributes.get("in_progress", False),
                "firmware_disabled": False,
                "firmware_unavailable": is_dashboard_unavailable,
                "enabling": False,
                "online": online,
                "skipped": is_skipped,
                "is_external": is_external_device,
                "failed": device_id is not None and device_id in failed_devices,
                "sw_version_raw": registry_version_raw,
                "pending_force_install": device_id is not None and device_id in pending_force_installs,
                "excluded": device_id is not None and device_id in excluded_devices,
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
        original_name = device.name
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
        if external_coordinator:
            mac_suffix = _get_mac_suffix(device)
            external_device_info = _match_device_to_external_dashboard(
                hass, name, None, original_name=original_name, mac_suffix=mac_suffix
            )
            
            if external_device_info:
                is_external_device = True
                builder_version = external_builder_version
                is_unavailable = not external_dashboard_available
                deployed = external_device_info.get("deployed_version")
                if deployed:
                    installed = _parse_version(deployed)
                # Remember this device as external - use original name if available
                remember_name = _normalize_device_name(original_name) if original_name else normalized_name
                if remember_name not in remembered_external_devices:
                    remembered_external_devices[remember_name] = True
                    remembered_external_devices_changed = True

        # Local dashboard yaml check (for suffix devices without update entity)
        if not is_external_device and is_unavailable:
            local_coordinator = hass.data[DOMAIN].get("local_dashboard")
            if local_coordinator and local_coordinator.available:
                mac_suffix = _get_mac_suffix(device)
                config_filename = _get_local_config_filename(original_name, mac_suffix)
                local_yaml_cache = hass.data[DOMAIN].get("local_yaml_cache", {})
                if config_filename in local_yaml_cache:
                    if local_yaml_cache[config_filename]:
                        builder_version = local_builder_version
                        is_unavailable = False
                else:
                    # Cache miss - schedule background check
                    pending_yaml_checks.append((device.id, config_filename))

        # Fallback: remembered as external even without coordinator
        if not is_external_device:
            if normalized_name in remembered_external_devices:
                if not external_coordinator or not external_dashboard_available:
                    is_external_device = True
                    builder_version = None
                    is_unavailable = True
                else:
                    _LOGGER.debug("Device %s: removing from remembered_external_devices (coordinator available, no match)", name)
                    del remembered_external_devices[normalized_name]
                    remembered_external_devices_changed = True
            else:
                orig_normalized = _normalize_device_name(original_name) if original_name else None
                if orig_normalized and orig_normalized in remembered_external_devices:
                    if not external_coordinator or not external_dashboard_available:
                        is_external_device = True
                        builder_version = None
                        is_unavailable = True
                    else:
                        _LOGGER.debug("Device %s: removing original name from remembered_external_devices (coordinator available, no match)", name)
                        del remembered_external_devices[orig_normalized]
                        remembered_external_devices_changed = True
        
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
            "device_id": device.id,
            "name": name,
            "current_version": _format_display_version(device.sw_version, installed),
            "latest_version": _format_display_version(device.sw_version, builder_version) if update_available else None,
            "update_available": update_available,
            "in_progress": False,
            "firmware_disabled": False,
            "firmware_unavailable": is_unavailable,
            "enabling": False,
            "online": online,
            "skipped": False,
            "is_external": is_external_device,
            "failed": device.id in failed_devices,
            "sw_version_raw": device.sw_version,
            "pending_force_install": device.id in pending_force_installs,
            "excluded": device.id in excluded_devices,
        })

    # ── Cleanup: Remove remembered devices that no longer exist in HA ──────
    # Get all current ESPHome device names (only from HA, not from external dashboard)
    current_esphome_device_names: set[str] = set()
    for device in dev_reg.devices.values():
        if device.id in esphome_device_ids:
            # Add current name (user-defined or original)
            device_name = device.name_by_user or device.name
            if device_name:
                current_esphome_device_names.add(_normalize_device_name(device_name))
            # Also add original name to prevent cleanup of renamed devices
            if device.name:
                current_esphome_device_names.add(_normalize_device_name(device.name))
    
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

    # ── Cleanup: Remove devices from failed list if they are now up-to-date ──────
    for d in devices:
        device_id = d.get("device_id")
        if device_id and device_id in failed_devices:
            if not d.get("update_available") and not d.get("skipped"):
                del failed_devices[device_id]
                failed_devices_changed = True
                _LOGGER.debug("Removed %s from failed devices - now up-to-date", device_id)

    # Save failed devices if changed
    if failed_devices_changed:
        hass.data[DOMAIN]["failed_devices"] = failed_devices
        settings["failed_devices"] = failed_devices
        hass.data[DOMAIN]["settings"] = settings
        store: Store = hass.data[DOMAIN].get("store")
        if store:
            hass.async_create_task(store.async_save(settings))

    # Apply project version cache: if integration has seen a yaml project bump
    # that hasn't been installed yet, surface it in the frontend.
    pv_cache = hass.data[DOMAIN].get("project_version_cache", {})
    if pv_cache:
        stale_ids = []
        for d in devices:
            did = d.get("device_id")
            if not did or did not in pv_cache:
                continue
            sw_raw = d.get("sw_version_raw")
            if not sw_raw:
                continue
            yaml_v = pv_cache[did]
            # Self-validate: is the device still actually behind?
            match = re.match(r'^([\d.]+)\s*\(ESPHome', str(sw_raw))
            if match:
                device_pv = match.group(1)
                try:
                    if parse_version(device_pv) >= parse_version(yaml_v):
                        stale_ids.append(did)
                        continue
                except Exception:
                    pass

            # Prefer the ESPHome version from an already-detected firmware update.
            # Falls back to the device's current ESPHome version if there is no
            # pending firmware update.
            existing_latest = d.get("latest_version")
            esphome_v = (
                _extract_esphome_version(existing_latest)
                if existing_latest else None
            ) or _extract_esphome_version(sw_raw)
            if not esphome_v:
                continue

            had_firmware_update = d.get("update_available", False)
            d["latest_version"] = f"{esphome_v} ({yaml_v})"
            d["project_bump_pending"] = True
            d["project_bump_only"] = not had_firmware_update
            # If only the project version was pending (no firmware update),
            # surface it as updatable so the frontend shows an action button.
            if not had_firmware_update:
                d["update_available"] = True
        if stale_ids:
            for sid in stale_ids:
                pv_cache.pop(sid, None)
            hass.bus.async_fire("esphome_update_manager_devices_updated")

    devices.sort(key=lambda d: (d["name"] or "").lower())
    # Trigger background yaml cache refresh for cache misses
    if pending_yaml_checks:
        hass.async_create_task(_refresh_local_yaml_cache(hass, pending_yaml_checks))
    return devices

async def _check_pending_force_installs(hass: HomeAssistant) -> None:
    """Check pending force installs and trigger them when ready (debounced 15s)."""
    settings = hass.data[DOMAIN].get("settings", {})
    pending: dict = settings.get("pending_force_installs", {})
    if not pending:
        return

    queue: UpdateQueue | None = hass.data[DOMAIN].get("queue")
    if queue is None:
        return

    if queue.is_running:
        if hass.data[DOMAIN].get("pending_force_install_retry"):
            return
        hass.data[DOMAIN]["pending_force_install_retry"] = True

        async def _retry():
            await asyncio.sleep(60)
            hass.data[DOMAIN].pop("pending_force_install_retry", None)
            await _check_pending_force_installs(hass)

        hass.async_create_task(_retry())
        return

    # Are any devices ready?
    if not _has_ready_pending_devices(hass, pending):
        return

    # Debounce 15s so multiple devices coming online get batched together
    if hass.data[DOMAIN].get("pending_force_install_timer"):
        return
    hass.data[DOMAIN]["pending_force_install_timer"] = True

    async def _flush():
        await asyncio.sleep(15)
        hass.data[DOMAIN].pop("pending_force_install_timer", None)
        await _execute_pending_force_installs(hass)

    hass.async_create_task(_flush())


def _has_ready_pending_devices(hass: HomeAssistant, pending: dict) -> bool:
    """Return True if at least one pending device is currently ready (online + dashboard ok)."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")
    external_available = external_coordinator is not None and external_coordinator.available

    for device_id in list(pending.keys()):
        device = dev_reg.async_get(device_id)
        if not device:
            continue
        if _is_device_online(hass, ent_reg, device_id) is not True:
            continue
        # External devices need both HA and dashboard online
        if external_coordinator:
            mac_suffix = _get_mac_suffix(device)
            ext_dev = _match_device_to_external_dashboard(
                hass,
                device.name_by_user or device.name or "",
                original_name=device.name,
                mac_suffix=mac_suffix,
            )
            if ext_dev and not external_available:
                continue
        return True
    return False


async def _execute_pending_force_installs(hass: HomeAssistant) -> None:
    """Execute pending force installs for devices that are ready."""
    settings = hass.data[DOMAIN].get("settings", {})
    pending: dict = settings.get("pending_force_installs", {})
    if not pending:
        return

    queue: UpdateQueue = hass.data[DOMAIN].get("queue")
    if queue is None:
        return

    if queue.is_running:
        async def _retry():
            await asyncio.sleep(60)
            await _check_pending_force_installs(hass)
        hass.async_create_task(_retry())
        return

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")
    external_available = external_coordinator is not None and external_coordinator.available

    ready_devices = []
    processed_ids = []
    pending_changed = False

    for device_id in list(pending.keys()):
        device = dev_reg.async_get(device_id)
        if not device:
            del pending[device_id]
            pending_changed = True
            continue

        if _is_device_online(hass, ent_reg, device_id) is not True:
            continue

        if external_coordinator:
            mac_suffix = _get_mac_suffix(device)
            ext_dev = _match_device_to_external_dashboard(
                hass,
                device.name_by_user or device.name or "",
                original_name=device.name,
                mac_suffix=mac_suffix,
            )
            if ext_dev and not external_available:
                continue

        name = device.name_by_user or device.name or device_id
        ready_devices.append({
            "entity_id": f"force:{_normalize_device_name(device.name or name)}",
            "device_id": device_id,
            "name": name,
            "from_version": device.sw_version,
            "to_version": device.sw_version,
        })
        processed_ids.append(device_id)

    if not ready_devices:
        if pending_changed:
            settings["pending_force_installs"] = pending
            hass.data[DOMAIN]["settings"] = settings
            store: Store = hass.data[DOMAIN].get("store")
            if store:
                hass.async_create_task(store.async_save(settings))
        return

    # Remove ready devices from pending list
    for did in processed_ids:
        pending.pop(did, None)
    settings["pending_force_installs"] = pending
    hass.data[DOMAIN]["settings"] = settings
    store: Store = hass.data[DOMAIN].get("store")
    if store:
        await store.async_save(settings)

    hass.bus.async_fire("esphome_update_manager_devices_updated")

    stop_addon_slug = None
    if settings.get("stop_addon_during_update", True):
        addon_info = await async_get_addon_info(hass, VSCODE_ADDON_SLUG)
        if addon_info and addon_info.get("state") == "started":
            stop_addon_slug = VSCODE_ADDON_SLUG

    _LOGGER.info(
        "Starting pending force install for %d device(s): %s",
        len(ready_devices),
        [d["name"] for d in ready_devices],
    )

    try:
        queue.start(
            entity_ids=[d["entity_id"] for d in ready_devices],
            force_install_devices=ready_devices,
            stop_addon_slug=stop_addon_slug,
        )
    except RuntimeError as err:
        _LOGGER.warning("Failed to start pending force install, restoring to pending: %s", err)
        for d in ready_devices:
            pending[d["device_id"]] = {
                "name": d["name"],
                "queued_at": datetime.now().isoformat(),
            }
        settings["pending_force_installs"] = pending
        hass.data[DOMAIN]["settings"] = settings
        if store:
            await store.async_save(settings)
        hass.bus.async_fire("esphome_update_manager_devices_updated")

# ── WebSocket Commands ────────────────────────────────────────────

@websocket_api.websocket_command({"type": "esphome_update_manager/devices"})
@callback
def ws_get_devices(hass, connection, msg):
    devices = _get_esphome_update_entities(hass)
    
    has_local = any(not d.get("is_external", False) for d in devices)
    has_external = any(d.get("is_external", False) for d in devices)
    is_mixed_setup = has_local and has_external
    
    connection.send_result(msg["id"], {
        "devices": devices,
        "is_mixed_setup": is_mixed_setup,
    })


async def _get_yaml_project_version(hass: HomeAssistant, device: dict) -> str | None:
    """Get project version from yaml config."""
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get(device.get("device_id")) if device.get("device_id") else None
    if not device_entry:
        return None

    original_name = device_entry.name
    local_coordinator = hass.data[DOMAIN].get("local_dashboard")
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")

    coordinator = None
    configuration = None

    if external_coordinator and external_coordinator.available:
        mac_suffix = _get_mac_suffix(device_entry)
        ext_dev = _match_device_to_external_dashboard(
            hass, device_entry.name_by_user or original_name, original_name=original_name, mac_suffix=mac_suffix
        )
        if ext_dev:
            coordinator = external_coordinator
            configuration = ext_dev.get("configuration", original_name)

    if not coordinator and local_coordinator and local_coordinator.available:
        coordinator = local_coordinator
        mac_suffix = _get_mac_suffix(device_entry)
        configuration = _get_local_config_filename(original_name, mac_suffix)

    if not coordinator:
        return None

    if not configuration.endswith(".yaml"):
        configuration = f"{configuration}.yaml"

    try:
        config = await coordinator.async_get_config(configuration)
        if config:
            return config.get("esphome", {}).get("project", {}).get("version")
    except Exception:
        return None


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/start",
        "entity_ids": vol.All(vol.Coerce(list), [str]),
        vol.Optional("stop_addon_slug"): vol.Any(str, None),
        vol.Optional("version_info"): vol.Any(dict, None),
        vol.Optional("force_install_devices"): vol.Any(list, None),
    }
)
@websocket_api.async_response
async def ws_start_updates(hass, connection, msg):
    queue: UpdateQueue = hass.data[DOMAIN]["queue"]
    stop_addon_slug = msg.get("stop_addon_slug")
    version_info = msg.get("version_info", {})
    force_install_devices = msg.get("force_install_devices")

    if version_info and not force_install_devices:
        devices = _get_esphome_update_entities(hass)
        device_map = {d["entity_id"]: d for d in devices}

        for eid, info in version_info.items():
            if eid in device_map:
                d = device_map[eid]
                sw_raw = d.get("sw_version_raw")
                latest = d.get("latest_version")
                info["from"] = sw_raw or info.get("from")

                new_project_version = None
                if sw_raw and re.match(r'^([\d.]+)\s*\(ESPHome', str(sw_raw)):
                    new_project_version = await _get_yaml_project_version(hass, d)

                info["to"] = _build_to_version(sw_raw, latest, new_project_version)

    try:
        queue.start(
            msg["entity_ids"],
            stop_addon_slug=stop_addon_slug,
            version_info=version_info,
            force_install_devices=force_install_devices,
        )
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
        "auto_update_scope": settings.get("auto_update_scope", "firmware"),
    })


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/set_auto_update_settings",
        vol.Optional("auto_update_enabled"): bool,
        vol.Optional("stop_addon_during_update"): bool,
        vol.Optional("auto_update_scope"): vol.In(["firmware", "project", "both"]),
    }
)
@websocket_api.async_response
async def ws_set_auto_update_settings(hass, connection, msg):
    """Set auto-update settings."""
    settings = hass.data[DOMAIN].get("settings", {})
    store: Store = hass.data[DOMAIN]["store"]

    # Track changes
    old_auto_update = settings.get("auto_update_enabled", False)
    old_scope = settings.get("auto_update_scope", "firmware")
    auto_update_changed = False
    scope_changed = False

    if "auto_update_enabled" in msg:
        if msg["auto_update_enabled"] != old_auto_update:
            auto_update_changed = True
        settings["auto_update_enabled"] = msg["auto_update_enabled"]
    if "stop_addon_during_update" in msg:
        settings["stop_addon_during_update"] = msg["stop_addon_during_update"]
    if "auto_update_scope" in msg:
        if msg["auto_update_scope"] != old_scope:
            scope_changed = True
        settings["auto_update_scope"] = msg["auto_update_scope"]

    hass.data[DOMAIN]["settings"] = settings
    await store.async_save(settings)

    # Setup/teardown listener when auto_update_enabled flipped
    if auto_update_changed:
        if settings.get("auto_update_enabled", False):
            await _setup_auto_update_listener(hass)
        else:
            await _remove_auto_update_listener(hass)

    # Project version cache should always reflect reality, regardless of settings.
    # Re-check on any settings change so cache is fresh.
    if auto_update_changed or scope_changed:
        await _check_all_devices_project_version(hass)

        # Firmware auto-update kick when applicable
        if settings.get("auto_update_enabled", False):
            new_scope = settings.get("auto_update_scope", "firmware")
            if new_scope in ("firmware", "both"):
                await _check_and_start_auto_update(hass)

    connection.send_result(msg["id"], {"saved": True})


@websocket_api.websocket_command({"type": "esphome_update_manager/refresh_project_versions"})
@websocket_api.async_response
async def ws_refresh_project_versions(hass, connection, msg):
    """Manual refresh: dashboards + project versions + pending + auto-update kick."""
    # Force local dashboard refresh (picks up new yaml files / version)
    local_coordinator = hass.data[DOMAIN].get("local_dashboard")
    if local_coordinator:
        try:
            await local_coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.debug("Local dashboard refresh failed during manual refresh: %s", err)

    # Force external dashboard refresh (deployed versions, device list, etc.)
    external_coordinator = hass.data[DOMAIN].get("external_dashboard")
    if external_coordinator:
        try:
            await external_coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.debug("External dashboard refresh failed during manual refresh: %s", err)

    # Re-check project versions for all devices (fills cache)
    await _check_all_devices_project_version(hass)

    # Re-evaluate any pending force installs
    await _check_pending_force_installs(hass)

    # Kick auto-update for newly-detected firmware updates (skipped internally
    # if auto-update is disabled or scope is project-only)
    await _check_and_start_auto_update(hass)

    # Notify frontend so device list re-renders
    hass.bus.async_fire("esphome_update_manager_devices_updated")

    connection.send_result(msg["id"], {"triggered": True})


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

@websocket_api.websocket_command({"type": "esphome_update_manager/subscribe_devices_updated"})
@websocket_api.async_response
async def ws_subscribe_devices_updated(hass, connection, msg):
    @callback
    def handle_devices_updated(event):
        connection.send_message(
            websocket_api.event_message(msg["id"], {"updated": True})
        )
    
    unsub = hass.bus.async_listen("esphome_update_manager_devices_updated", handle_devices_updated)
    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])

@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/add_pending_force_install",
        "device_ids": vol.All(vol.Coerce(list), [str]),
    }
)
@websocket_api.async_response
async def ws_add_pending_force_install(hass, connection, msg):
    """Add devices to the pending force install list."""
    settings = hass.data[DOMAIN].get("settings", {})
    pending: dict = settings.get("pending_force_installs", {})
    dev_reg = dr.async_get(hass)

    added = 0
    for device_id in msg["device_ids"]:
        device = dev_reg.async_get(device_id)
        if not device:
            continue
        pending[device_id] = {
            "name": device.name_by_user or device.name or device_id,
            "queued_at": datetime.now().isoformat(),
        }
        added += 1

    settings["pending_force_installs"] = pending
    hass.data[DOMAIN]["settings"] = settings
    store: Store = hass.data[DOMAIN].get("store")
    if store:
        await store.async_save(settings)

    hass.bus.async_fire("esphome_update_manager_devices_updated")
    # Some may already be online → check immediately
    hass.async_create_task(_check_pending_force_installs(hass))

    connection.send_result(msg["id"], {"added": added})


@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/remove_pending_force_install",
        "device_id": str,
    }
)
@websocket_api.async_response
async def ws_remove_pending_force_install(hass, connection, msg):
    """Remove a device from the pending force install list."""
    settings = hass.data[DOMAIN].get("settings", {})
    pending: dict = settings.get("pending_force_installs", {})

    removed = pending.pop(msg["device_id"], None) is not None

    if removed:
        settings["pending_force_installs"] = pending
        hass.data[DOMAIN]["settings"] = settings
        store: Store = hass.data[DOMAIN].get("store")
        if store:
            await store.async_save(settings)
        hass.bus.async_fire("esphome_update_manager_devices_updated")

    connection.send_result(msg["id"], {"removed": removed})

@websocket_api.websocket_command(
    {
        "type": "esphome_update_manager/set_excluded_devices",
        "device_ids": vol.All(vol.Coerce(list), [str]),
    }
)
@websocket_api.async_response
async def ws_set_excluded_devices(hass, connection, msg):
    """Replace the entire excluded devices list."""
    settings = hass.data[DOMAIN].get("settings", {})
    dev_reg = dr.async_get(hass)

    new_excluded: dict = {}
    for device_id in msg["device_ids"]:
        device = dev_reg.async_get(device_id)
        if not device:
            continue
        new_excluded[device_id] = {
            "name": device.name_by_user or device.name or device_id,
            "excluded_at": datetime.now().isoformat(),
        }

    settings["excluded_devices"] = new_excluded
    hass.data[DOMAIN]["settings"] = settings
    hass.data[DOMAIN]["excluded_devices"] = new_excluded

    store: Store = hass.data[DOMAIN].get("store")
    if store:
        await store.async_save(settings)

    # If auto-update is enabled, trigger a check (devices may have been un-excluded)
    if settings.get("auto_update_enabled", False):
        scope = settings.get("auto_update_scope", "firmware")
        if scope in ("firmware", "both"):
            hass.async_create_task(_check_and_start_auto_update(hass))
        if scope in ("project", "both"):
            hass.async_create_task(_check_all_devices_project_version(hass))
        
    hass.bus.async_fire("esphome_update_manager_devices_updated")
    _LOGGER.info("Excluded devices list updated: %d device(s)", len(new_excluded))
    connection.send_result(msg["id"], {"count": len(new_excluded)})

# EOF
