"""Queue manager for sequential ESPHome OTA updates."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.components.update import DOMAIN as UPDATE_DOMAIN, ATTR_IN_PROGRESS

from .const import (
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


class DeviceUpdateResult:
    """Result of a single device update."""

    def __init__(self, entity_id: str, from_version: str | None = None, to_version: str | None = None) -> None:
        self.entity_id = entity_id
        self.status: str = STATUS_QUEUED
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None
        self.from_version: str | None = from_version
        self.to_version: str | None = to_version


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

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def results(self) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": r.entity_id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "error": r.error,
                "from_version": r.from_version,
                "to_version": r.to_version,
            }
            for r in self._queue
        ]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._queue:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def start(self, entity_ids: list[str], stop_addon_slug: str | None = None, version_info: dict | None = None) -> None:
        if self._running:
            raise RuntimeError("Update queue is already running")

        if version_info is None:
            version_info = {}

        _LOGGER.debug("Starting update queue with %d entities: %s", len(entity_ids), entity_ids)
        
        self._queue = [
            DeviceUpdateResult(
                eid,
                from_version=version_info.get(eid, {}).get("from"),
                to_version=version_info.get(eid, {}).get("to"),
            )
            for eid in entity_ids
        ]
        self._running = True
        self._cancelled = False
        self._current_index = 0
        self._stop_addon_slug = stop_addon_slug
        self._addon_was_running = False
        self._task = self.hass.async_create_task(self._run())

    def cancel(self) -> None:
        _LOGGER.warning("Cancel requested - setting _cancelled = True (was: %s)", self._cancelled)
        # Log stack trace to see where cancel was called from
        import traceback
        _LOGGER.debug("Cancel call stack:\n%s", "".join(traceback.format_stack()))
        
        self._cancelled = True
        if self._task and not self._task.done():
            _LOGGER.debug("Cancelling asyncio task")
            self._task.cancel()

    def clear(self) -> None:
        """Clear results. Only allowed when not running."""
        if self._running:
            raise RuntimeError("Cannot clear while updates are running")
        self._queue.clear()

    async def _stop_addon(self) -> None:
        """Stop add-on before updates if requested."""
        if not self._stop_addon_slug:
            return

        from . import async_get_addon_info, async_stop_addon

        info = await async_get_addon_info(self.hass, self._stop_addon_slug)
        if info and info.get("state") == "started":
            self._addon_was_running = True
            addon_name = info.get("name", self._stop_addon_slug)
            _LOGGER.info("Stopping add-on %s before updates", addon_name)
            success = await async_stop_addon(self.hass, self._stop_addon_slug)
            if success:
                _LOGGER.info("Add-on %s stopped successfully", addon_name)
                # Give it a moment to free memory
                await asyncio.sleep(5)
            else:
                _LOGGER.warning("Failed to stop add-on %s", addon_name)
                self._addon_was_running = False

    async def _restart_addon(self) -> None:
        """Restart add-on after updates if it was running before."""
        if not self._stop_addon_slug or not self._addon_was_running:
            return

        from . import async_start_addon, async_get_addon_info

        info = await async_get_addon_info(self.hass, self._stop_addon_slug)
        addon_name = info.get("name", self._stop_addon_slug) if info else self._stop_addon_slug

        _LOGGER.info("Restarting add-on %s after updates", addon_name)
        success = await async_start_addon(self.hass, self._stop_addon_slug)
        if success:
            _LOGGER.info("Add-on %s restarted successfully", addon_name)
        else:
            _LOGGER.warning("Failed to restart add-on %s", addon_name)

    async def _run(self) -> None:
        _LOGGER.debug("Update queue _run() started")
        try:
            # Stop add-on if requested
            await self._stop_addon()

            for i, item in enumerate(self._queue):
                self._current_index = i
                _LOGGER.debug("Processing queue item %d/%d: %s (cancelled=%s)", 
                            i + 1, len(self._queue), item.entity_id, self._cancelled)

                if self._cancelled:
                    _LOGGER.info("Marking %s as cancelled (cancelled flag was set before processing)", item.entity_id)
                    item.status = STATUS_CANCELLED
                    continue

                await self._update_single(item)

                _LOGGER.debug("Finished updating %s with status: %s", item.entity_id, item.status)

                self.hass.bus.async_fire(
                    "esphome_update_manager_progress",
                    {"results": self.results, "summary": self.summary},
                )

                if i < len(self._queue) - 1 and not self._cancelled:
                    _LOGGER.debug("Waiting %d seconds before next update (cancelled=%s)", 
                                DEFAULT_DELAY_BETWEEN, self._cancelled)
                    try:
                        await asyncio.wait_for(
                            self._wait_for_cancel(),
                            timeout=DEFAULT_DELAY_BETWEEN,
                        )
                        _LOGGER.debug("Wait interrupted - cancel detected")
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            _LOGGER.warning("Update queue task was cancelled via CancelledError")
            for item in self._queue:
                if item.status == STATUS_QUEUED:
                    _LOGGER.debug("Marking queued item %s as cancelled", item.entity_id)
                    item.status = STATUS_CANCELLED
                elif item.status == STATUS_RUNNING:
                    _LOGGER.debug("Marking running item %s as cancelled", item.entity_id)
                    item.status = STATUS_CANCELLED
                    item.error = "Cancelled by user"
                    item.finished_at = datetime.now()
        finally:
            _LOGGER.debug("Update queue _run() finishing, restarting addon if needed")
            # Always restart add-on if it was stopped
            try:
                await self._restart_addon()
            except Exception as err:
                _LOGGER.error("Failed to restart add-on: %s", err)

            self._running = False
            _LOGGER.info("Update queue finished. Summary: %s", self.summary)
            self.hass.bus.async_fire(
                "esphome_update_manager_finished",
                {"results": self.results, "summary": self.summary},
            )

            # Check for any devices that came online during the batch update
            if not self._cancelled:
                _LOGGER.debug("Checking for devices that came online during batch update")
                # Import here to avoid circular import
                from . import _check_and_start_auto_update
                # Small delay to allow entities to stabilize
                await asyncio.sleep(3)
                await _check_and_start_auto_update(self.hass)

    async def _wait_for_cancel(self) -> None:
        while not self._cancelled:
            await asyncio.sleep(1)

    def _is_entity_available(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state not in ("unavailable", "unknown")

    async def _update_single(self, item: DeviceUpdateResult) -> None:
        _LOGGER.debug("Starting update for %s", item.entity_id)
        item.status = STATUS_RUNNING
        item.started_at = datetime.now()

        try:
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
                _LOGGER.debug("update.install call completed for %s", item.entity_id)
            except Exception as install_err:
                # Compile error, OTA error, etc.
                error_msg = str(install_err)
                _LOGGER.error("Install failed for %s: %s", item.entity_id, error_msg)
                # Clean up the message for display
                if "Error compiling" in error_msg:
                    item.status = STATUS_FAILED
                    item.error = f"Compile failed — {error_msg}"
                else:
                    item.status = STATUS_FAILED
                    item.error = f"Install failed — {error_msg}"
                item.finished_at = datetime.now()
                return

            # If we get here, install call succeeded — verify completion
            state = self.hass.states.get(item.entity_id)
            if state and state.state == "off":
                # Already done (fast update)
                _LOGGER.debug("Device %s already shows state=off, marking success", item.entity_id)
                item.status = STATUS_SUCCESS
            else:
                # Wait for OTA + reboot to finish
                _LOGGER.debug("Waiting for completion of %s (current state: %s)", 
                            item.entity_id, state.state if state else "None")
                success, error_reason = await self._wait_for_completion(item.entity_id)
                if self._cancelled:
                    _LOGGER.info("Device %s marked cancelled after wait_for_completion (cancelled=%s)", 
                                item.entity_id, self._cancelled)
                    item.status = STATUS_CANCELLED
                    item.error = "Cancelled by user"
                elif success:
                    _LOGGER.info("Device %s update completed successfully", item.entity_id)
                    item.status = STATUS_SUCCESS
                else:
                    _LOGGER.warning("Device %s update failed: %s", item.entity_id, error_reason)
                    item.status = STATUS_FAILED
                    item.error = error_reason or "Update failed"

        except asyncio.CancelledError:
            _LOGGER.warning("_update_single for %s caught CancelledError", item.entity_id)
            item.status = STATUS_CANCELLED
            item.error = "Cancelled by user"
            raise
        except Exception as err:
            _LOGGER.error("Failed to update %s: %s", item.entity_id, err)
            item.status = STATUS_FAILED
            item.error = str(err)
        finally:
            item.finished_at = item.finished_at or datetime.now()
            _LOGGER.debug("_update_single finished for %s, status=%s", item.entity_id, item.status)

    async def _wait_for_start(
        self, entity_id: str, timeout: int = INITIAL_PROGRESS_TIMEOUT
    ) -> bool:
        end_time = asyncio.get_event_loop().time() + timeout
        await asyncio.sleep(3)

        while asyncio.get_event_loop().time() < end_time:
            if self._cancelled:
                _LOGGER.debug("_wait_for_start: cancelled flag detected for %s", entity_id)
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
        """Wait until update completes.

        Returns (success, error_reason).
        Tracks consecutive unavailable time — if device stays unavailable
        for too long, it's considered lost (not just rebooting).
        """
        _LOGGER.debug("_wait_for_completion started for %s (timeout=%ds)", entity_id, timeout)
        end_time = asyncio.get_event_loop().time() + timeout
        unavailable_since: float | None = None
        saw_in_progress = False
        loop_count = 0

        await asyncio.sleep(5)

        while asyncio.get_event_loop().time() < end_time:
            loop_count += 1
            
            if self._cancelled:
                _LOGGER.info("_wait_for_completion: cancelled flag detected for %s (loop %d)", 
                            entity_id, loop_count)
                return False, "Cancelled by user"

            state = self.hass.states.get(entity_id)

            # Entity completely gone
            if state is None:
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                    _LOGGER.debug("Entity %s is None, starting unavailable timer", entity_id)
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    _LOGGER.warning("Entity %s disappeared for too long", entity_id)
                    return False, "Device disappeared and did not come back"
                await asyncio.sleep(10)
                continue

            # Entity unavailable
            if state.state == "unavailable":
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                    _LOGGER.debug("Entity %s became unavailable, starting timer", entity_id)
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    if saw_in_progress:
                        _LOGGER.warning("Entity %s went offline during update", entity_id)
                        return False, "Device went offline during update and did not recover"
                    else:
                        _LOGGER.warning("Entity %s became unavailable", entity_id)
                        return False, "Device became unavailable"
                await asyncio.sleep(10)
                continue

            # Entity is available again — reset unavailable timer
            if unavailable_since is not None:
                _LOGGER.debug("Entity %s is available again after %.1fs", 
                            entity_id, asyncio.get_event_loop().time() - unavailable_since)
            unavailable_since = None

            in_progress = state.attributes.get(ATTR_IN_PROGRESS, False)

            if in_progress and not saw_in_progress:
                _LOGGER.debug("Entity %s now shows in_progress=True", entity_id)
                saw_in_progress = True

            if not in_progress and state.state == "off":
                # No update available anymore = success
                _LOGGER.debug("Entity %s state=off, in_progress=False - success!", entity_id)
                return True, None

            if not in_progress and state.state == "on":
                # Update still available = didn't install
                if saw_in_progress:
                    # Was in progress but now shows update still available
                    # Could be a false read, wait a bit
                    _LOGGER.debug("Entity %s was in_progress but now state=on, waiting...", entity_id)
                    await asyncio.sleep(10)
                    continue
                await asyncio.sleep(10)
                continue

            # Log state every ~30 seconds
            if loop_count % 6 == 0:
                _LOGGER.debug("Entity %s still waiting: state=%s, in_progress=%s, saw_in_progress=%s", 
                            entity_id, state.state, in_progress, saw_in_progress)

            await asyncio.sleep(5)

        # Overall timeout
        _LOGGER.warning("Entity %s timed out after %ds (saw_in_progress=%s)", 
                        entity_id, timeout, saw_in_progress)
        if saw_in_progress:
            return False, "Update timed out — device may still be updating"
        return False, "Update timed out — no progress detected"
