"""Queue manager for sequential ESPHome OTA updates."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN, ATTR_IN_PROGRESS

from .const import (
    DOMAIN,
    DEFAULT_UPDATE_TIMEOUT,
    DEFAULT_DELAY_BETWEEN,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_CANCELLED,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait for initial progress after triggering install
INITIAL_PROGRESS_TIMEOUT = 60  # 1 minute

# How long entity can stay unavailable during update before we give up
# Normal OTA reboot takes ~30s, give generous margin
MAX_UNAVAILABLE_DURATION = 120  # 2 minutes

# Queue phases
PHASE_IDLE = "idle"
PHASE_STOPPING_ADDON = "stopping_addon"
PHASE_UPDATING = "updating"
PHASE_STARTING_ADDON = "starting_addon"


class DeviceUpdateResult:
    def __init__(
        self,
        entity_id: str,
        from_version: str | None = None,
        to_version: str | None = None,
        is_force_install: bool = False,
        device_id: str | None = None,
        name: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.name = name
        self.status: str = STATUS_QUEUED
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None
        self.from_version: str | None = from_version
        self.to_version: str | None = to_version
        self.is_force_install: bool = is_force_install
        self.device_id: str | None = device_id


class UpdateQueue:
    """Manages sequential OTA updates for ESPHome devices."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._queue: list[DeviceUpdateResult] = []
        self._running = False
        self._cancelled = False
        self._current_index = 0
        self._task: asyncio.Task | None = None
        self._stop_addon_slug: str | None = None
        self._addon_was_running = False
        self._phase: str = PHASE_IDLE
        self._addon_name: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def addon_name(self) -> str | None:
        return self._addon_name

    @property
    def results(self) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": r.entity_id,
                "name": r.name if hasattr(r, 'name') and r.name else r.entity_id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "error": r.error,
                "from_version": r.from_version,
                "to_version": r.to_version,
                "is_force_install": r.is_force_install,
                "device_id": r.device_id,
            }
            for r in self._queue
        ]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._queue:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def start(
        self,
        entity_ids: list[str],
        stop_addon_slug: str | None = None,
        version_info: dict | None = None,
        force_install_devices: list[dict] | None = None,
    ) -> None:
        """Start the update queue.
        
        Args:
            entity_ids: List of entity_ids for normal updates, OR device names for force install.
            stop_addon_slug: Optional add-on to stop during updates.
            version_info: Optional version info per entity_id.
            force_install_devices: If set, list of dicts with device info for force install.
                Each dict: {"entity_id": str, "device_id": str, "name": str}
        """
        if self._running:
            raise RuntimeError("Update queue is already running")

        if version_info is None:
            version_info = {}

        if force_install_devices is not None:
            # Force install mode - build queue from device list
            _LOGGER.debug(
                "Starting force install queue with %d devices: %s",
                len(force_install_devices),
                [d["name"] for d in force_install_devices],
            )
            self._queue = [
                DeviceUpdateResult(
                    entity_id=d["entity_id"],
                    is_force_install=True,
                    device_id=d.get("device_id"),
                    name=d.get("name"),
                    from_version=d.get("from_version"),
                    to_version=d.get("to_version"),
                )
                for d in force_install_devices
            ]
        else:
            _LOGGER.debug(
                "Starting update queue with %d entities: %s", len(entity_ids), entity_ids
            )
            self._queue = [
                DeviceUpdateResult(
                    eid,
                    from_version=version_info.get(eid, {}).get("from"),
                    to_version=version_info.get(eid, {}).get("to"),
                    is_force_install=False,
                    name=version_info.get(eid, {}).get("name"),
                    device_id=version_info.get(eid, {}).get("device_id"),
                )
                for eid in entity_ids
            ]

        self._running = True
        self._cancelled = False
        self._current_index = 0
        self._stop_addon_slug = stop_addon_slug
        self._addon_was_running = False
        self._phase = PHASE_IDLE
        self._addon_name = None
        self._task = self.hass.async_create_task(self._run())

    def cancel(self) -> None:
        _LOGGER.debug("Cancel requested - setting _cancelled = True (was: %s)", self._cancelled)
        self._cancelled = True
        if self._task and not self._task.done():
            _LOGGER.debug("Cancelling asyncio task")
            self._task.cancel()

    def clear(self) -> None:
        """Clear results. Only allowed when not running."""
        if self._running:
            raise RuntimeError("Cannot clear while updates are running")
        self._queue.clear()

    def _fire_phase_changed(self) -> None:
        self.hass.bus.async_fire(
            "esphome_update_manager_phase_changed",
            {"phase": self._phase, "addon_name": self._addon_name},
        )

    async def _stop_addon(self) -> None:
        if not self._stop_addon_slug:
            return
        from . import async_get_addon_info, async_stop_addon
        info = await async_get_addon_info(self.hass, self._stop_addon_slug)
        if info and info.get("state") == "started":
            self._addon_was_running = True
            self._addon_name = info.get("name", self._stop_addon_slug)
            self._phase = PHASE_STOPPING_ADDON
            self._fire_phase_changed()
            _LOGGER.info("Stopping add-on %s before updates", self._addon_name)
            success = await async_stop_addon(self.hass, self._stop_addon_slug)
            if success:
                _LOGGER.info("Add-on %s stopped successfully", self._addon_name)
                await asyncio.sleep(5)
            else:
                _LOGGER.warning("Failed to stop add-on %s", self._addon_name)
                self._addon_was_running = False

    async def _restart_addon(self) -> None:
        if not self._stop_addon_slug or not self._addon_was_running:
            return
        from . import async_start_addon, async_get_addon_info
        info = await async_get_addon_info(self.hass, self._stop_addon_slug)
        self._addon_name = info.get("name", self._stop_addon_slug) if info else self._stop_addon_slug
        self._phase = PHASE_STARTING_ADDON
        self._fire_phase_changed()
        _LOGGER.info("Restarting add-on %s after updates", self._addon_name)
        success = await async_start_addon(self.hass, self._stop_addon_slug)
        if success:
            for _ in range(30):
                await asyncio.sleep(2)
                info = await async_get_addon_info(self.hass, self._stop_addon_slug)
                if info and info.get("state") == "started":
                    _LOGGER.info("Add-on %s is now running", self._addon_name)
                    break
            else:
                _LOGGER.warning("Add-on %s did not start within timeout", self._addon_name)
        else:
            _LOGGER.warning("Failed to restart add-on %s", self._addon_name)

    async def _run(self) -> None:
        _LOGGER.debug("Update queue _run() started")
        try:
            await self._stop_addon()
            self._phase = PHASE_UPDATING
            self._fire_phase_changed()

            for i, item in enumerate(self._queue):
                self._current_index = i
                _LOGGER.debug(
                    "Processing queue item %d/%d: %s (cancelled=%s, force=%s)",
                    i + 1, len(self._queue), item.entity_id, self._cancelled, item.is_force_install,
                )

                if self._cancelled:
                    _LOGGER.info("Marking %s as cancelled", item.entity_id)
                    item.status = STATUS_CANCELLED
                    continue

                await self._update_single(item)

                _LOGGER.debug(
                    "Finished %s with status: %s", item.entity_id, item.status
                )

                self.hass.bus.async_fire(
                    "esphome_update_manager_progress",
                    {"results": self.results, "summary": self.summary},
                )

                if i < len(self._queue) - 1 and not self._cancelled:
                    try:
                        await asyncio.wait_for(
                            self._wait_for_cancel(),
                            timeout=DEFAULT_DELAY_BETWEEN,
                        )
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            _LOGGER.debug("Update queue task was cancelled via CancelledError")
            for item in self._queue:
                if item.status == STATUS_QUEUED:
                    item.status = STATUS_CANCELLED
                elif item.status == STATUS_RUNNING:
                    item.status = STATUS_CANCELLED
                    item.error = "Cancelled by user"
                    item.finished_at = datetime.now()
        finally:
            try:
                await self._restart_addon()
            except Exception as err:
                _LOGGER.error("Failed to restart add-on: %s", err)

            self._phase = PHASE_IDLE
            self._fire_phase_changed()
            self._running = False
            _LOGGER.info("Update queue finished. Summary: %s", self.summary)
            self.hass.bus.async_fire(
                "esphome_update_manager_finished",
                {"results": self.results, "summary": self.summary},
            )

            if not self._cancelled:
                from . import _check_and_start_auto_update
                await asyncio.sleep(3)
                await _check_and_start_auto_update(self.hass)

    async def _wait_for_cancel(self) -> None:
        while not self._cancelled:
            await asyncio.sleep(1)

    def _is_external_device(self, entity_id: str) -> bool:
        return entity_id.startswith("external:")

    def _get_external_device_name(self, entity_id: str) -> str:
        return entity_id.replace("external:", "", 1)

    def _is_entity_available(self, entity_id: str) -> bool:
        if self._is_external_device(entity_id):
            return True
        if entity_id.startswith("device:"):
            # No HA update entity - check via status sensor
            from homeassistant.helpers import entity_registry as er
            from homeassistant.helpers import device_registry as dr
            from . import _get_esphome_device_ids, _find_status_entity
            ent_reg = er.async_get(self.hass)
            dev_reg = dr.async_get(self.hass)
            esphome_device_ids = _get_esphome_device_ids(self.hass)
            normalized = entity_id.replace("device:", "", 1)
            for device in dev_reg.devices.values():
                if device.id not in esphome_device_ids:
                    continue
                from . import _normalize_device_name
                name = device.name_by_user or device.name
                if name and _normalize_device_name(name) == normalized:
                    status_entity = _find_status_entity(self.hass, ent_reg, device.id)
                    if status_entity:
                        state = self.hass.states.get(status_entity)
                        return state is not None and state.state == "on"
                    return True  # No status sensor → assume available
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state not in ("unavailable", "unknown")

    async def _update_single(self, item: DeviceUpdateResult) -> None:
        _LOGGER.debug("Starting update for %s (force=%s)", item.entity_id, item.is_force_install)
        item.status = STATUS_RUNNING
        item.started_at = datetime.now()

        try:
            if item.is_force_install:
                await self._force_install_device(item)
                return

            if self._is_external_device(item.entity_id):
                await self._update_external_device(item)
                return

            if item.entity_id.startswith("device:"):
                await self._force_install_device(item)
                return

            if not self._is_entity_available(item.entity_id):
                _LOGGER.info("Device %s unavailable - skipping", item.entity_id)
                item.status = STATUS_SKIPPED
                item.error = "Device unavailable — skipped"
                item.finished_at = datetime.now()
                return

            _LOGGER.debug("Calling update.install for %s", item.entity_id)
            try:
                await self.hass.services.async_call(
                    UPDATE_DOMAIN,
                    "install",
                    {"entity_id": item.entity_id},
                    blocking=True,
                )
            except Exception as install_err:
                error_msg = str(install_err)
                _LOGGER.error("Install failed for %s: %s", item.entity_id, error_msg)
                item.status = STATUS_FAILED
                item.error = (
                    f"Compile failed — {error_msg}"
                    if "Error compiling" in error_msg
                    else f"Install failed — {error_msg}"
                )
                item.finished_at = datetime.now()
                return

            state = self.hass.states.get(item.entity_id)
            if state and state.state == "off":
                item.status = STATUS_SUCCESS
            else:
                success, error_reason = await self._wait_for_completion(item.entity_id)
                if self._cancelled:
                    item.status = STATUS_CANCELLED
                    item.error = "Cancelled by user"
                elif success:
                    item.status = STATUS_SUCCESS
                else:
                    item.status = STATUS_FAILED
                    item.error = error_reason or "Update failed"

        except asyncio.CancelledError:
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            raise
        except Exception as err:
            _LOGGER.error("Failed to update %s: %s", item.entity_id, err)
            item.status = STATUS_FAILED
            item.error = str(err)
        finally:
            item.finished_at = item.finished_at or datetime.now()
            if item.status in (STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED):
                if not item.is_force_install:
                    item.to_version = item.from_version

    async def _force_install_device(self, item: DeviceUpdateResult) -> None:
        """Force install a device via ESPHome dashboard (compile + OTA upload)."""
        from homeassistant.helpers import device_registry as dr
        from . import _normalize_device_name

        local_coordinator = self.hass.data[DOMAIN].get("local_dashboard")
        external_coordinator = self.hass.data[DOMAIN].get("external_dashboard")

        if not (local_coordinator and local_coordinator.available) and \
           not (external_coordinator and external_coordinator.available):
            item.status = STATUS_FAILED
            item.error = "No dashboard available"
            item.finished_at = datetime.now()
            return

        # Resolve device name from device_id
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(item.device_id) if item.device_id else None

        if not device:
            item.status = STATUS_FAILED
            item.error = "Device not found"
            item.finished_at = datetime.now()
            return

        original_name = device.name
        user_name = device.name_by_user
        display_name = user_name or original_name or item.device_id

        if not original_name:
            item.status = STATUS_FAILED
            item.error = "No device name"
            item.finished_at = datetime.now()
            return

        # Determine coordinator and configuration
        coordinator = None
        configuration = None

        if external_coordinator and external_coordinator.available:
            from . import _match_device_to_external_dashboard, _get_mac_suffix
            mac_suffix = _get_mac_suffix(device)
            external_device = _match_device_to_external_dashboard(
                self.hass,
                user_name or original_name,
                original_name=original_name,
                mac_suffix=mac_suffix,
            )
            if external_device:
                coordinator = external_coordinator
                configuration = external_device.get("configuration", original_name)
                _LOGGER.debug("%s: Found in external dashboard, config=%s", display_name, configuration)

        if not coordinator and local_coordinator and local_coordinator.available:
            coordinator = local_coordinator
            from . import _get_mac_suffix, _get_local_config_filename
            mac_suffix = _get_mac_suffix(device)
            configuration = _get_local_config_filename(original_name, mac_suffix)
            _LOGGER.debug("%s: Using local dashboard, config=%s", display_name, configuration)

        if not coordinator:
            item.status = STATUS_FAILED
            item.error = "No dashboard available"
            item.finished_at = datetime.now()
            return

        if not configuration.endswith(".yaml"):
            configuration = f"{configuration}.yaml"

        from . import _build_to_version, _get_local_esphome_builder_version, _get_external_dashboard_version
        if coordinator == external_coordinator:
            builder_version = _get_external_dashboard_version(self.hass)
            if not builder_version:
                builder_version = _get_local_esphome_builder_version(self.hass)
        else:
            builder_version = _get_local_esphome_builder_version(self.hass)
            if not builder_version:
                builder_version = _get_external_dashboard_version(self.hass)

        if not builder_version:
            _LOGGER.warning(
                "Could not determine builder ESPHome version for force install of %s — "
                "to_version will be left empty",
                display_name,
            )

        yaml_project_version = None
        try:
            config = await coordinator.async_get_config(configuration)
            if config:
                yaml_project_version = config.get("esphome", {}).get("project", {}).get("version")
        except Exception:
            pass

        item.to_version = _build_to_version(item.from_version, builder_version, yaml_project_version)

        _LOGGER.info(
            "Force installing %s (config: %s) via %s dashboard",
            display_name,
            configuration,
            "external" if coordinator == external_coordinator else "local",
        )

        # Step 1: Compile
        if self._cancelled:
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            item.finished_at = datetime.now()
            return

        compile_success = await coordinator.async_compile(configuration)
        if not compile_success:
            item.status = STATUS_FAILED
            item.error = "Compile failed — check ESPHome dashboard for details"
            item.finished_at = datetime.now()
            return

        # Step 2: Upload via OTA
        if self._cancelled:
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            item.finished_at = datetime.now()
            return

        upload_success = await coordinator.async_upload(configuration, "OTA")
        if not upload_success:
            item.status = STATUS_FAILED
            item.error = "OTA upload failed — check ESPHome dashboard for details"
            item.finished_at = datetime.now()
            return

        item.status = STATUS_SUCCESS
        item.finished_at = datetime.now()
        _LOGGER.info("Force install completed for %s", display_name)

    async def _update_external_device(self, item: DeviceUpdateResult) -> None:
        """Update a device via the external ESPHome dashboard."""
        from .dashboard import ExternalDashboardCoordinator
        from . import _normalize_device_name

        coordinator: ExternalDashboardCoordinator | None = self.hass.data[DOMAIN].get("external_dashboard")

        if not coordinator:
            item.status = STATUS_FAILED
            item.error = "External dashboard not configured"
            item.finished_at = datetime.now()
            return

        if not coordinator.available:
            item.status = STATUS_FAILED
            item.error = "External dashboard not reachable"
            item.finished_at = datetime.now()
            return

        ha_device_name = self._get_external_device_name(item.entity_id)
        configuration = None

        if item.device_id:
            from homeassistant.helpers import device_registry as dr
            from . import _match_device_to_external_dashboard, _get_mac_suffix
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(item.device_id)
            if device:
                mac_suffix = _get_mac_suffix(device)
                external_device = _match_device_to_external_dashboard(
                    self.hass,
                    device.name_by_user or device.name,
                    original_name=device.name,
                    mac_suffix=mac_suffix,
                )
                if external_device:
                    configuration = external_device.get("configuration")
                    if configuration and not configuration.endswith(".yaml"):
                        configuration = f"{configuration}.yaml"

        if not configuration:
            item.status = STATUS_FAILED
            item.error = f"Could not find ESPHome configuration for {ha_device_name}"
            item.finished_at = datetime.now()
            return

        _LOGGER.info("Updating external device via dashboard: %s", configuration)

        compile_success = await coordinator.async_compile(configuration)
        if not compile_success:
            item.status = STATUS_FAILED
            item.error = "Compile failed on external dashboard"
            item.finished_at = datetime.now()
            return

        if self._cancelled:
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            item.finished_at = datetime.now()
            return

        upload_success = await coordinator.async_upload(configuration, "OTA")
        if not upload_success:
            item.status = STATUS_FAILED
            item.error = "OTA upload failed on external dashboard"
            item.finished_at = datetime.now()
            return

        if self._cancelled:
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            item.finished_at = datetime.now()
            return

        await asyncio.sleep(10)
        try:
            await coordinator.async_request_refresh()
        except Exception:
            pass

        item.status = STATUS_SUCCESS
        item.finished_at = datetime.now()
        _LOGGER.info("External device %s updated successfully", configuration)

    async def _wait_for_start(
        self, entity_id: str, timeout: int = INITIAL_PROGRESS_TIMEOUT
    ) -> bool:
        end_time = asyncio.get_event_loop().time() + timeout
        await asyncio.sleep(3)
        while asyncio.get_event_loop().time() < end_time:
            if self._cancelled:
                return False
            state = self.hass.states.get(entity_id)
            if state is None or state.state == "unavailable":
                return False
            in_progress = state.attributes.get(ATTR_IN_PROGRESS, False)
            if in_progress:
                return True
            if state.state == "off":
                return True
            await asyncio.sleep(3)
        return False

    async def _wait_for_completion(
        self, entity_id: str, timeout: int = DEFAULT_UPDATE_TIMEOUT
    ) -> tuple[bool, str | None]:
        _LOGGER.debug("_wait_for_completion started for %s (timeout=%ds)", entity_id, timeout)
        end_time = asyncio.get_event_loop().time() + timeout
        unavailable_since: float | None = None
        saw_in_progress = False
        loop_count = 0

        await asyncio.sleep(5)

        while asyncio.get_event_loop().time() < end_time:
            loop_count += 1
            if self._cancelled:
                return False, "Cancelled by user"

            state = self.hass.states.get(entity_id)

            if state is None:
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    return False, "Device disappeared and did not come back"
                await asyncio.sleep(10)
                continue

            if state.state == "unavailable":
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    return (
                        False,
                        "Device went offline during update and did not recover"
                        if saw_in_progress
                        else "Device became unavailable",
                    )
                await asyncio.sleep(10)
                continue

            if unavailable_since is not None:
                unavailable_since = None

            in_progress = state.attributes.get(ATTR_IN_PROGRESS, False)
            if in_progress and not saw_in_progress:
                saw_in_progress = True

            if not in_progress and state.state == "off":
                return True, None

            if not in_progress and state.state == "on":
                if saw_in_progress:
                    await asyncio.sleep(10)
                    continue
                await asyncio.sleep(10)
                continue

            if loop_count % 6 == 0:
                _LOGGER.debug(
                    "Entity %s still waiting: state=%s, in_progress=%s",
                    entity_id, state.state, in_progress,
                )

            await asyncio.sleep(5)

        if saw_in_progress:
            return False, "Update timed out — device may still be updating"
        return False, "Update timed out — no progress detected"
