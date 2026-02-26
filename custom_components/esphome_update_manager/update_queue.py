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

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.status: str = STATUS_QUEUED
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None


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
            }
            for r in self._queue
        ]

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._queue:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def start(self, entity_ids: list[str], stop_addon_slug: str | None = None) -> None:
        if self._running:
            raise RuntimeError("Update queue is already running")

        self._queue = [DeviceUpdateResult(eid) for eid in entity_ids]
        self._running = True
        self._cancelled = False
        self._current_index = 0
        self._stop_addon_slug = stop_addon_slug
        self._addon_was_running = False
        self._task = self.hass.async_create_task(self._run())

    def cancel(self) -> None:
        self._cancelled = True
        if self._task and not self._task.done():
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
        try:
            # Stop add-on if requested
            await self._stop_addon()

            for i, item in enumerate(self._queue):
                self._current_index = i

                if self._cancelled:
                    item.status = STATUS_CANCELLED
                    continue

                await self._update_single(item)

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
            for item in self._queue:
                if item.status == STATUS_QUEUED:
                    item.status = STATUS_CANCELLED
                elif item.status == STATUS_RUNNING:
                    item.status = STATUS_CANCELLED
                    item.error = "Cancelled by user"
                    item.finished_at = datetime.now()
        finally:
            # Always restart add-on if it was stopped
            try:
                await self._restart_addon()
            except Exception as err:
                _LOGGER.error("Failed to restart add-on: %s", err)

            self._running = False
            self.hass.bus.async_fire(
                "esphome_update_manager_finished",
                {"results": self.results, "summary": self.summary},
            )

    async def _wait_for_cancel(self) -> None:
        while not self._cancelled:
            await asyncio.sleep(1)

    def _is_entity_available(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return state.state not in ("unavailable", "unknown")

    async def _update_single(self, item: DeviceUpdateResult) -> None:
        item.status = STATUS_RUNNING
        item.started_at = datetime.now()

        try:
            if not self._is_entity_available(item.entity_id):
                item.status = STATUS_SKIPPED
                item.error = "Device unavailable — skipped"
                item.finished_at = datetime.now()
                return

            try:
                await self.hass.services.async_call(
                    UPDATE_DOMAIN,
                    "install",
                    {"entity_id": item.entity_id},
                    blocking=True,
                )
            except Exception as install_err:
                # Compile error, OTA error, etc.
                error_msg = str(install_err)
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
                item.status = STATUS_SUCCESS
            else:
                # Wait for OTA + reboot to finish
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
        """Wait until update completes.

        Returns (success, error_reason).
        Tracks consecutive unavailable time — if device stays unavailable
        for too long, it's considered lost (not just rebooting).
        """
        end_time = asyncio.get_event_loop().time() + timeout
        unavailable_since: float | None = None
        saw_in_progress = False

        await asyncio.sleep(5)

        while asyncio.get_event_loop().time() < end_time:
            if self._cancelled:
                return False, "Cancelled by user"

            state = self.hass.states.get(entity_id)

            # Entity completely gone
            if state is None:
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    return False, "Device disappeared and did not come back"
                await asyncio.sleep(10)
                continue

            # Entity unavailable
            if state.state == "unavailable":
                if unavailable_since is None:
                    unavailable_since = asyncio.get_event_loop().time()
                elif (asyncio.get_event_loop().time() - unavailable_since) > MAX_UNAVAILABLE_DURATION:
                    if saw_in_progress:
                        return False, "Device went offline during update and did not recover"
                    else:
                        return False, "Device became unavailable"
                await asyncio.sleep(10)
                continue

            # Entity is available again — reset unavailable timer
            unavailable_since = None

            in_progress = state.attributes.get(ATTR_IN_PROGRESS, False)

            if in_progress:
                saw_in_progress = True

            if not in_progress and state.state == "off":
                # No update available anymore = success
                return True, None

            if not in_progress and state.state == "on":
                # Update still available = didn't install
                if saw_in_progress:
                    # Was in progress but now shows update still available
                    # Could be a false read, wait a bit
                    await asyncio.sleep(10)
                    continue
                await asyncio.sleep(10)
                continue

            await asyncio.sleep(5)

        # Overall timeout
        if saw_in_progress:
            return False, "Update timed out — device may still be updating"
        return False, "Update timed out — no progress detected"
