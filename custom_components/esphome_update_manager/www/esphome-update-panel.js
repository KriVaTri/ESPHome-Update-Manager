import {
  LitElement,
  html,
  css,
} from "./lit/lit-element.js";

const ENABLING_TIMEOUT_MS = 90000;
const UPDATING_TIMEOUT_MS = 1200000;

class ESPHomeUpdatePanel extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
      narrow: { type: Boolean },
      devices: { type: Array },
      selected: { type: Object },
      results: { type: Array },
      running: { type: Boolean },
      _pendingEnables: { type: Object },
      _updatingIds: { type: Object },
      _localResults: { type: Array },
      _addonInfo: { type: Object },
      _stopAddonDuringUpdate: { type: Boolean },
      _autoUpdateEnabled: { type: Boolean },
      _showLogPopup: { type: Boolean },
      _logContent: { type: String },
      _logTitle: { type: String },
      _cancelling: { type: Boolean },
      _tooltipName: { type: String },
      _tooltipX: { type: Number },
      _tooltipY: { type: Number },
      _showMenu: { type: Boolean },
      _logBackups: { type: Array },
      _isMixedSetup: { type: Boolean },
      _dashboardAvailable: { type: Boolean },
      _phase: { type: String },
      _addonName: { type: String },
    };
  }

  constructor() {
    super();
    this.devices = [];
    this.narrow = false;
    this.selected = new Set();
    this.results = [];
    this.running = false;
    this._pendingEnables = new Map();
    this._updatingIds = new Map();
    this._localResults = [];
    this._expiredEnables = new Set();
    this._enablingPollTimer = null;
    this._pollInterval = null;
    this._refreshDebounce = null;
    this._prevHassStates = null;
    this._addonInfo = null;
    this._addonPollTimer = null;
    this._backgroundCheckTimer = null;
    this._stopAddonDuringUpdate = true;
    this._autoUpdateEnabled = false;
    this._showLogPopup = false;
    this._logContent = null;
    this._logTitle = "Update Log";
    this._cancelling = false;
    this._tooltipName = null;
    this._tooltipX = 0;
    this._tooltipY = 0;
    this._showMenu = false;
    this._logBackups = [];
    this._isMixedSetup = false;
    this._dashboardAvailable = null;
    this._dashboardSubscription = null;
    this._loadPendingFromStorage();
    this._phase = "idle";
    this._addonName = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadDevices();
    this._loadAddonInfo();
    this._loadAutoUpdateSettings();
    this._addonPollTimer = setInterval(() => this._loadAddonInfo(), 30000);
    this._pollStatus().then(() => {
      if (this.running) {
        this._restoreUpdatingState();
        this._startStatusPolling();
      }
    });
    
    // Hide tooltip on scroll
    this._scrollHandler = () => {
      if (this._tooltipName) {
        this._tooltipName = null;
        this.requestUpdate();
      }
    };
    window.addEventListener('scroll', this._scrollHandler, true);
    
    // Close menu when clicking outside
    this._documentClickHandler = (e) => {
      if (this._showMenu) {
        const menu = this.shadowRoot?.querySelector('.header-menu');
        const menuBtn = this.shadowRoot?.querySelector('.menu-btn');
        if (menu && !menu.contains(e.composedPath()[0]) && 
            menuBtn && !menuBtn.contains(e.composedPath()[0])) {
          this._showMenu = false;
          this.requestUpdate();
        }
      }
    };
    document.addEventListener('click', this._documentClickHandler);
    
    // Start polling immediately to catch backend-initiated updates
    this._startBackgroundStatusCheck();
    
    // Subscribe to dashboard status changes
    this._subscribeToDashboardStatus();
    
    // Check URL for show_log parameter
    this._checkUrlForLogParam();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stopEnablingPoll();
    for (const [, info] of this._pendingEnables) {
      if (info.timeoutId) clearTimeout(info.timeoutId);
    }
    this._pendingEnables.clear();
    for (const [, info] of this._updatingIds) {
      if (info.timeoutId) clearTimeout(info.timeoutId);
    }
    this._updatingIds.clear();
    if (this._pollInterval) {
      clearInterval(this._pollInterval);
      this._pollInterval = null;
    }
    if (this._refreshDebounce) {
      clearTimeout(this._refreshDebounce);
      this._refreshDebounce = null;
    }
    if (this._addonPollTimer) {
      clearInterval(this._addonPollTimer);
      this._addonPollTimer = null;
    }
    if (this._backgroundCheckTimer) {
      clearInterval(this._backgroundCheckTimer);
      this._backgroundCheckTimer = null;
    }
    if (this._scrollHandler) {
      window.removeEventListener('scroll', this._scrollHandler, true);
    }
    if (this._documentClickHandler) {
      document.removeEventListener('click', this._documentClickHandler);
    }
    if (this._dashboardSubscription) {
      this._dashboardSubscription();
      this._dashboardSubscription = null;
    }
  }

  async _subscribeToDashboardStatus() {
    try {
      this._dashboardSubscription = await this.hass.connection.subscribeMessage(
        (event) => {
          console.log("Dashboard status event:", event);
          const wasAvailable = this._dashboardAvailable;
          this._dashboardAvailable = event.available;
          
          // If availability changed, reload devices
          if (wasAvailable !== null && wasAvailable !== event.available) {
            console.log("Dashboard availability changed:", wasAvailable, "->", event.available);
            this._loadDevices();
          }
        },
        { type: "esphome_update_manager/subscribe_dashboard_status" }
      );
    } catch (e) {
      console.error("Failed to subscribe to dashboard status:", e);
    }
  }

  _checkUrlForLogParam() {
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get("show_log") === "1") {
      this._openLogPopup();
      // Clean up URL
      const newUrl = window.location.pathname;
      window.history.replaceState({}, "", newUrl);
    }
  }

  updated(changedProps) {
    if (!changedProps.has("hass") || !this.hass) return;
    const prev = this._prevHassStates;
    const curr = this.hass.states;
    if (prev && curr !== prev) {
      if (this._hasRelevantChange(prev, curr)) {
        this._scheduleRefresh();
      }
    }
    this._prevHassStates = curr;
  }

  _hasRelevantChange(prev, curr) {
    for (const d of this.devices) {
      if (d.entity_id) {
        if (prev[d.entity_id] !== curr[d.entity_id]) return true;
      }
    }
    for (const key in curr) {
      if (
        key.startsWith("binary_sensor.") &&
        key.endsWith("_status") &&
        prev[key] !== curr[key]
      ) {
        return true;
      }
    }
    return false;
  }

  _scheduleRefresh() {
    if (this._refreshDebounce) clearTimeout(this._refreshDebounce);
    this._refreshDebounce = setTimeout(() => {
      this._refreshDebounce = null;
      this._loadDevices();
      this._loadAddonInfo();
    }, 2000);
  }

  // ── Sidebar Toggle ──────────────────────────────────────────────

  _toggleSidebar() {
    this.dispatchEvent(
      new CustomEvent("hass-toggle-menu", {
        bubbles: true,
        composed: true,
      })
    );
  }

  // ── Menu ────────────────────────────────────────────────────────

  async _toggleMenu() {
    if (!this._showMenu) {
      // Load backups when opening menu
      await this._loadLogBackups();
    }
    this._showMenu = !this._showMenu;
    this.requestUpdate();
  }

  async _loadLogBackups() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/list_log_backups" });
      this._logBackups = res.backups || [];
    } catch (e) {
      console.error("Failed to load log backups", e);
      this._logBackups = [];
    }
  }

  // ── Log Popup ───────────────────────────────────────────────────

  async _openLogPopup() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/get_update_log" });
      if (res.exists) {
        this._logContent = res.content;
        this._logTitle = "📄 Latest Update Log";
        this._showLogPopup = true;
      } else {
        this._logContent = "No update log available yet.";
        this._logTitle = "📄 Update Log";
        this._showLogPopup = true;
      }
    } catch (e) {
      console.error("Failed to load update log", e);
      this._logContent = "Failed to load update log.";
      this._logTitle = "📄 Update Log";
      this._showLogPopup = true;
    }
    this._showMenu = false;
  }

  async _openBackupLog(filename, displayName) {
    try {
      const res = await this.hass.callWS({ 
        type: "esphome_update_manager/get_log_backup",
        filename: filename,
      });
      this._logContent = res.content;
      this._logTitle = `📋 Log: ${displayName}`;
      this._showLogPopup = true;
    } catch (e) {
      console.error("Failed to load backup log", e);
      this._logContent = "Failed to load backup log.";
      this._logTitle = "📋 Backup Log";
      this._showLogPopup = true;
    }
    this._showMenu = false;
  }

  _closeLogPopup() {
    this._showLogPopup = false;
    this._logContent = null;
  }

  // ── Results ─────────────────────────────────────────────────────

  get _allResults() {
    return [...this.results, ...this._localResults];
  }

  _addLocalResult(entityId, status, error) {
    const device = this.devices.find((d) => d.entity_id === entityId);
    const name = device?.name || entityId;
    this._localResults = [
      ...this._localResults,
      {
        entity_id: name,
        status,
        error,
        started_at: null,
        finished_at: new Date().toISOString(),
      },
    ];
    this.requestUpdate();
  }

  // ── Pending Storage ─────────────────────────────────────────────

  _loadPendingFromStorage() {
      try {
        // Load pending enables
        const stored = localStorage.getItem('esphome_pending_enables');
        if (stored) {
          const data = JSON.parse(stored);
          const now = Date.now();
          
          for (const [entityId, info] of Object.entries(data)) {
            const elapsed = now - info.startedAt;
            const remaining = ENABLING_TIMEOUT_MS - elapsed;
            
            if (remaining > 0) {
              // Still within timeout, restore it
              const timeoutId = setTimeout(() => this._expireEnabling(entityId), remaining);
              this._pendingEnables.set(entityId, { startedAt: info.startedAt, timeoutId });
            } else {
              // Timeout already expired, mark as expired
              this._expiredEnables.add(entityId);
            }
          }
        }
        
        // Load expired enables
        const storedExpired = localStorage.getItem('esphome_expired_enables');
        if (storedExpired) {
          const expiredList = JSON.parse(storedExpired);
          expiredList.forEach(entityId => this._expiredEnables.add(entityId));
        }
      } catch (e) {
        console.error("Failed to load pending enables from storage", e);
      }
  }

  _savePendingToStorage() {
      try {
        // Save pending enables
        const data = {};
        for (const [entityId, info] of this._pendingEnables) {
          data[entityId] = { startedAt: info.startedAt };
        }
        localStorage.setItem('esphome_pending_enables', JSON.stringify(data));
        
        // Save expired enables
        localStorage.setItem('esphome_expired_enables', JSON.stringify([...this._expiredEnables]));
      } catch (e) {
        console.error("Failed to save pending enables to storage", e);
      }
  }

  // ── Data ────────────────────────────────────────────────────────

  _restoreUpdatingState() {
    if (!this.results || this.results.length === 0) return;
    this._updatingIds = new Map(this._updatingIds);
    for (const r of this.results) {
      if (r.status === "running" || r.status === "queued") {
        if (!this._updatingIds.has(r.entity_id)) {
          if (r.status === "running") {
            const timeoutId = setTimeout(() => {
              this._expireUpdating(r.entity_id);
            }, UPDATING_TIMEOUT_MS);
            this._updatingIds.set(r.entity_id, { startedAt: Date.now(), timeoutId, isRunning: true });
          } else {
            this._updatingIds.set(r.entity_id, { startedAt: null, timeoutId: null, isRunning: false });
          }
        }
      }
    }
    this.requestUpdate();
  }

  _isEnablingPending(entityId) {
    return this._pendingEnables.has(entityId);
  }

  _expireEnabling(entityId) {
      const info = this._pendingEnables.get(entityId);
      if (info?.timeoutId) clearTimeout(info.timeoutId);
      this._pendingEnables.delete(entityId);
      this._pendingEnables = new Map(this._pendingEnables);
      
      // Remember this entity's enable has expired
      this._expiredEnables.add(entityId);
      
      this._savePendingToStorage();
      this._loadDevices();
      this.requestUpdate();
  }

  _isUpdatingPending(entityId) {
    return this._updatingIds.has(entityId);
  }

_expireUpdating(entityId) {
    const info = this._updatingIds.get(entityId);
    if (info?.timeoutId) clearTimeout(info.timeoutId);
    this._updatingIds.delete(entityId);
    this._updatingIds = new Map(this._updatingIds);
    this._addLocalResult(entityId, "failed", "Update timed out — device may be unresponsive");
    this._loadDevices();
}

  _clearAllUpdatingTimers() {
    for (const [, info] of this._updatingIds) {
      if (info.timeoutId) clearTimeout(info.timeoutId);
    }
    this._updatingIds.clear();
    this._updatingIds = new Map(this._updatingIds);
  }

  _mergedDevices() {
    const toRemove = [];
    
    const result = this.devices.map((d) => {
      const isPending = this._isEnablingPending(d.entity_id);
      const hasExpired = this._expiredEnables.has(d.entity_id);
      
      if (isPending) {
        // Check if enabling completed successfully (enabled AND available)
        if (!d.firmware_disabled && !d.enabling && !d.firmware_unavailable) {
          // Success! Mark for removal after mapping
          toRemove.push(d.entity_id);
          return d;
        }
        
        // Still waiting - show as enabling
        return { ...d, firmware_disabled: false, enabling: true };
      }
      
      // If enable expired, override backend's enabling flag
      if (hasExpired && d.enabling) {
        return { ...d, enabling: false, firmware_unavailable: true };
      }
      
      // Trust the backend's enabling flag
      return d;
    });
    
    // Clean up completed enables after mapping
    if (toRemove.length > 0) {
      toRemove.forEach(entityId => {
        const info = this._pendingEnables.get(entityId);
        if (info?.timeoutId) clearTimeout(info.timeoutId);
        this._pendingEnables.delete(entityId);
        this._expiredEnables.delete(entityId);
      });
      this._pendingEnables = new Map(this._pendingEnables);
      this._savePendingToStorage();
    }
    
    return result;
  }

  async _loadDevices() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/devices" });
      const newDevices = res.devices || [];
      this._isMixedSetup = res.is_mixed_setup || false;
      
      // Force new array reference to trigger LitElement update
      this.devices = [...newDevices];
      
      const merged = this._mergedDevices();
      const hasEnabling = merged.some((d) => d.enabling) || this._pendingEnables.size > 0;
      if (hasEnabling && !this._enablingPollTimer) this._startEnablingPoll();
      else if (!hasEnabling && this._enablingPollTimer) this._stopEnablingPoll();
      
      // Force re-render
      this.requestUpdate();
    } catch (e) {
      console.error("Failed to load devices", e);
    }
  }

  async _loadAddonInfo() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/addon_info" });
      this._addonInfo = res;
    } catch (e) {
      this._addonInfo = null;
    }
  }

  async _loadAutoUpdateSettings() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/get_auto_update_settings" });
      this._autoUpdateEnabled = res.auto_update_enabled || false;
      this._stopAddonDuringUpdate = res.stop_addon_during_update !== false;
      this.requestUpdate();
    } catch (e) {
      console.error("Failed to load auto-update settings", e);
    }
  }

  async _saveAutoUpdateSettings() {
    try {
      await this.hass.callWS({
        type: "esphome_update_manager/set_auto_update_settings",
        auto_update_enabled: this._autoUpdateEnabled,
        stop_addon_during_update: this._stopAddonDuringUpdate,
      });
      
      // If auto-update was just enabled, poll status after a short delay
      // to catch any immediate updates (success or failure)
      if (this._autoUpdateEnabled) {
        setTimeout(async () => {
          await this._pollStatus();
          if (this.running) {
            this._restoreUpdatingState();
            this._startStatusPolling();
          }
          await this._loadDevices();
          this.requestUpdate();
        }, 2000);
      }
    } catch (e) {
      console.error("Failed to save auto-update settings", e);
    }
  }

  _startEnablingPoll() {
    this._enablingPollTimer = setInterval(() => this._loadDevices(), 5000);
  }

  _stopEnablingPoll() {
    if (this._enablingPollTimer) {
      clearInterval(this._enablingPollTimer);
      this._enablingPollTimer = null;
    }
  }

  _startBackgroundStatusCheck() {
    // Check every 5 seconds if an update was started from backend
    if (this._backgroundCheckTimer) return;
    
    this._backgroundCheckTimer = setInterval(async () => {
      // Check URL for show_log parameter (in case notification was clicked)
      this._checkUrlForLogParam();
      
      try {
        const res = await this.hass.callWS({ type: "esphome_update_manager/status" });
        
        if (res.running && !this.running) {
          // Backend started an update, sync our state
          this.running = true;
          this.results = res.results || [];
          this._restoreUpdatingState();
          this._startStatusPolling();
          this._loadDevices();
          this.requestUpdate();
        } else if (!res.running && this.running) {
          // Backend finished but we still think it's running
          this.running = false;
          this.results = res.results || [];
          this._clearAllUpdatingTimers();
          this._cancelling = false;
          this.selected.clear();
          await this._loadDevices();
          await this._loadAddonInfo();
          this.requestUpdate();
        }
      } catch (e) {
        // Ignore errors
      }
    }, 5000);
  }

  async _pollStatus() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/status" });
      
      this.running = res.running;
      this.results = res.results || [];
      this._phase = res.phase || "idle";
      this._addonName = res.addon_name || null;

      if (this._updatingIds.size > 0) {
        const activeIds = new Set(
          this.results
            .filter((r) => r.status === "running" || r.status === "queued")
            .map((r) => r.entity_id)
        );
        
        for (const [entityId, info] of this._updatingIds) {
          if (!activeIds.has(entityId)) {
            if (info.timeoutId) clearTimeout(info.timeoutId);
            this._updatingIds.delete(entityId);
          } else {
            const result = this.results.find((r) => r.entity_id === entityId);
            
            if (result?.status === "running" && !info.isRunning) {
              if (info.timeoutId) clearTimeout(info.timeoutId);
              const timeoutId = setTimeout(() => {
                this._expireUpdating(entityId);
              }, UPDATING_TIMEOUT_MS);
              this._updatingIds.set(entityId, { startedAt: Date.now(), timeoutId, isRunning: true });
            }
          }
        }
        this._updatingIds = new Map(this._updatingIds);
      }

      if (!this.running && this._pollInterval) {
        clearInterval(this._pollInterval);
        this._pollInterval = null;
        this._clearAllUpdatingTimers();
        this._cancelling = false;
        this._phase = "idle";
        this._addonName = null;
        this.selected.clear();
        await this._loadDevices();
        await this._loadAddonInfo();
        this.requestUpdate();
      }
    } catch (e) {
      console.error("_pollStatus error:", e);
    }
}

  // ── Actions ─────────────────────────────────────────────────────

  _toggleSelect(entityId) {
    if (this.selected.has(entityId)) this.selected.delete(entityId);
    else this.selected.add(entityId);
    this.requestUpdate();
  }

  _selectAll() {
    const merged = this._mergedDevices();
    const selectable = merged.filter((d) => this._canSelect(d));
    if (selectable.length > 0 && this.selected.size === selectable.length) {
      this.selected.clear();
    } else {
      this.selected.clear();
      selectable.forEach((d) => this.selected.add(d.entity_id));
    }
    this.requestUpdate();
  }

  async _enableEntity(entityId) {
      // Clear expired state if user tries again
      this._expiredEnables.delete(entityId);
      this._savePendingToStorage();  // Save the cleared expired state
      
      const timeoutId = setTimeout(() => this._expireEnabling(entityId), ENABLING_TIMEOUT_MS);
      this._pendingEnables = new Map(this._pendingEnables);
      this._pendingEnables.set(entityId, { startedAt: Date.now(), timeoutId });
      this._savePendingToStorage();
      this.requestUpdate();
      if (!this._enablingPollTimer) this._startEnablingPoll();

      try {
        await this.hass.callWS({ type: "esphome_update_manager/enable_entity", entity_id: entityId });
      } catch (e) {
        const info = this._pendingEnables.get(entityId);
        if (info?.timeoutId) clearTimeout(info.timeoutId);
        this._pendingEnables.delete(entityId);
        this._pendingEnables = new Map(this._pendingEnables);
        this._savePendingToStorage();
        this._addLocalResult(entityId, "failed", "Enable failed: " + e.message);
        this.requestUpdate();
      }
  }

  _getStopAddonSlug() {
    if (this._stopAddonDuringUpdate && this._addonInfo?.installed && this._addonInfo?.running) {
      return "a0d7b954_vscode";
    }
    return null;
  }

  async _updateSingle(entityId) {
    try {
      const device = this.devices.find(d => d.entity_id === entityId);
      const versionInfo = {};
      if (device) {
        versionInfo[entityId] = {
          from: device.current_version || "unknown",
          to: device.latest_version || "unknown",
        };
      }

      const timeoutId = setTimeout(() => this._expireUpdating(entityId), UPDATING_TIMEOUT_MS);
      this._updatingIds = new Map(this._updatingIds);
      this._updatingIds.set(entityId, { startedAt: Date.now(), timeoutId });
      this.requestUpdate();

      await this.hass.callWS({
        type: "esphome_update_manager/start",
        entity_ids: [entityId],
        stop_addon_slug: this._getStopAddonSlug(),
        version_info: versionInfo,
      });
      this.running = true;
      this._startStatusPolling();
    } catch (e) {
      console.error("[ESPHome Update Manager] _updateSingle error:", e);
      const info = this._updatingIds.get(entityId);
      if (info?.timeoutId) clearTimeout(info.timeoutId);
      this._updatingIds.delete(entityId);
      this._updatingIds = new Map(this._updatingIds);
      this._addLocalResult(entityId, "failed", "Update failed: " + String(e?.message || e));
      this.requestUpdate();
    }
  }

  async _startBatchUpdate() {
    if (this.selected.size === 0) return;
    const ids = [...this.selected];

    try {
      const versionInfo = {};
      ids.forEach(id => {
        const device = this.devices.find(d => d.entity_id === id);
        if (device) {
          versionInfo[id] = {
            from: device.current_version || "unknown",
            to: device.latest_version || "unknown",
          };
        }
      });

      this._updatingIds = new Map(this._updatingIds);
      ids.forEach((id) => {
        this._updatingIds.set(id, { startedAt: null, timeoutId: null, isRunning: false });
      });
      this.requestUpdate();

      await this.hass.callWS({
        type: "esphome_update_manager/start",
        entity_ids: ids,
        stop_addon_slug: this._getStopAddonSlug(),
        version_info: versionInfo,
      });
      this.running = true;
      this._startStatusPolling();
    } catch (e) {
      console.error("[ESPHome Update Manager] _startBatchUpdate error:", e);
      ids.forEach((id) => {
        this._updatingIds.delete(id);
        this._addLocalResult(id, "failed", "Batch update failed: " + String(e?.message || e));
      });
      this._updatingIds = new Map(this._updatingIds);
      this.requestUpdate();
    }
  }

  async _cancelUpdates() {
    this._cancelling = true;
    this.requestUpdate();
    try {
      await this.hass.callWS({ type: "esphome_update_manager/cancel" });
    } catch (e) {
      console.error("Cancel failed:", e);
    }
    // _cancelling will be reset when running becomes false
  }

  async _clearResults() {
    try {
      await this.hass.callWS({ type: "esphome_update_manager/clear_results" });
      this.results = [];
      this._localResults = [];
      this.requestUpdate();
    } catch (e) {
      alert("Failed to clear results: " + e.message);
    }
  }

  _startStatusPolling() {
    if (this._pollInterval) return;
    this._pollInterval = setInterval(async () => {
      await this._pollStatus();
      if (!this.running) {
        clearInterval(this._pollInterval);
        this._pollInterval = null;
        this._clearAllUpdatingTimers();
        this._cancelling = false;
        this.selected.clear();
        await this._loadDevices();
        await this._loadAddonInfo();
        this.requestUpdate();
      }
    }, 3000);
  }

  // ── Rendering helpers ───────────────────────────────────────────

  _statusIcon(status) {
    const icons = {
      queued: "⏳", running: "🔄", success: "✅",
      failed: "❌", cancelled: "⛔", skipped: "⏭️",
    };
    return icons[status] || "❓";
  }

  _onlineIcon(online) {
    if (online === true) return "🟢";
    if (online === false) return "🔴";
    return "🟡";
  }

  _getStatusText() {
    if (this._cancelling) {
      return "Cancelling…";
    }
    
    switch (this._phase) {
      case "stopping_addon":
        return `Stopping ${this._addonName || "add-on"}…`;
      case "starting_addon":
        return `Starting ${this._addonName || "add-on"}…`;
      case "updating":
        return "Updating…";
      default:
        return "Updating…";
    }
  }

  _getAddonStatusDisplay() {
    // During update phases, show the transitional states
    if (this.running && this._stopAddonDuringUpdate) {
      if (this._phase === "stopping_addon") {
        return { text: "● Stopping", cls: "addon-stopping" };
      }
      if (this._phase === "starting_addon") {
        return { text: "● Starting", cls: "addon-starting" };
      }
      // During updating phase, addon is stopped (if it was running)
      if (this._phase === "updating" && this._addonName) {
        return { text: "● Stopped", cls: "addon-stopped" };
      }
    }
    
    // Default: show actual running state
    if (this._addonInfo?.running) {
      return { text: "● Running", cls: "addon-running" };
    }
    return { text: "● Stopped", cls: "addon-stopped" };
  }

  _getDeviceButton(d) {
    const isUpdating = this._isUpdatingPending(d.entity_id) || d.in_progress;
    if (d.online === false) {
      return { label: "Offline", cls: "btn-offline", disabled: true, action: null, spinner: false };
    }
    if (d.enabling) {
      return { label: "Enabling…", cls: "btn-enabling", disabled: true, action: null, spinner: true };
    }
    if (d.firmware_disabled) {
      return { label: "Enable", cls: "btn-enable", disabled: false, action: "enable", spinner: false };
    }
    if (d.firmware_unavailable) {
      return { label: "Unavailable", cls: "btn-unavailable", disabled: true, action: null, spinner: false };
    }
    if (isUpdating) {
      return { label: "Updating…", cls: "btn-updating", disabled: true, action: null, spinner: true };
    }
    if (d.update_available) {
      return { label: "Update", cls: "btn-update", disabled: false, action: "update", spinner: false };
    }
    if (d.skipped) {
      return { label: "Skipped", cls: "btn-skipped", disabled: true, action: null, spinner: false };
    }
    return { label: "Up to date", cls: "btn-uptodate", disabled: true, action: null, spinner: false };
  }

  _handleButtonClick(d) {
    const btn = this._getDeviceButton(d);
    if (btn.action === "enable") {
      this._enableEntity(d.entity_id).catch(e => {
        console.error("[ESPHome Update Manager] Enable error:", e);
        this._addLocalResult(d.entity_id, "failed", "Enable failed to start: " + String(e?.message || e));
      });
    } else if (btn.action === "update") {
      this._updateSingle(d.entity_id).catch(e => {
        console.error("[ESPHome Update Manager] Update error:", e);
        this._addLocalResult(d.entity_id, "failed", "Update failed to start: " + String(e?.message || e));
      });
    }
  }

  _canSelect(d) {
    return (
      d.update_available &&
      !d.firmware_disabled &&
      !d.firmware_unavailable &&
      !d.enabling &&
      d.online !== false &&
      !this._isUpdatingPending(d.entity_id) &&
      !d.in_progress &&
      d.entity_id
    );
  }

  _showNameTooltip(e, name) {
    const el = e.target;
    if (el.scrollWidth > el.clientWidth) {
      this._tooltipName = name;
      this._tooltipX = e.clientX;
      this._tooltipY = e.clientY;
      this.requestUpdate();
    }
  }

  // ── Styles ──────────────────────────────────────────────────────

  static get styles() {
    return css`
      :host {
        display: block;
        padding: 0;
        font-family: var(--paper-font-body1_-_font-family, "Roboto", sans-serif);
      }
      /* App toolbar (HA style) */
      .app-toolbar {
        display: flex;
        align-items: center;
        height: 56px;
        padding: 0 16px;
        background: var(--app-header-background-color, var(--primary-color, #03a9f4));
        color: var(--app-header-text-color, #fff);
        font-size: 20px;
        font-weight: 400;
      }
      .app-toolbar .title {
        margin-left: 16px;
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .sidebar-toggle {
        background: none;
        border: none;
        cursor: pointer;
        padding: 8px;
        margin: 0;
        color: inherit;
        display: flex;
        align-items: center;
        justify-content: center;
        border-radius: 50%;
      }
      .sidebar-toggle:hover {
        background: rgba(255, 255, 255, 0.1);
      }
      .sidebar-toggle svg {
        width: 24px;
        height: 24px;
        fill: currentColor;
      }
      h1 { 
        margin: 0 0 16px; 
        padding: 8px 16px;
        background: var(--secondary-background-color, #e0e0e0);
        display: flex;
        align-items: center;
      }
      .header-spacer {
        flex: 1;
      }
      .content {
        padding: 0 16px 16px;
      }

      /* Header menu */
      .header-menu-container {
        position: relative;
      }
      .menu-btn {
        background: none;
        border: none;
        font-size: 1em;
        cursor: pointer;
        width: 32px;
        height: 32px;
        padding: 20px;
        border-radius: 50%;
        color: var(--primary-text-color);
        opacity: 0.6;
        display: flex;
        align-items: center;
        justify-content: center;
        line-height: 1;
      }
      .menu-btn:hover {
        background: rgba(0, 0, 0, 0.2);
      }
      .header-menu {
        position: absolute;
        top: 100%;
        right: 0;
        background: var(--card-background-color, #fff);
        border-radius: 0;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        min-width: 200px;
        z-index: 100;
      }
      .menu-item {
        display: block;
        width: 100%;
        padding: 12px 16px;
        border: none;
        background: none;
        text-align: left;
        cursor: pointer;
        font-size: 0.5em;
        color: var(--primary-text-color);
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 0;
      }
      .menu-item:last-child {
        border-bottom: none;
      }
      .menu-item:hover {
        background: var(--secondary-background-color, #f5f5f5);
      }
      .menu-item.current-log {
        font-size: 0.6em;
      }
      .menu-section-title {
        padding: 8px 16px 4px;
        font-size: 0.5em;
        text-transform: uppercase;
        color: var(--secondary-text-color, #666);
        letter-spacing: 0.5px;
      }
      .menu-divider {
        height: 1px;
        background: var(--divider-color, #e0e0e0);
        margin: 4px 0;
      }
      .no-backups {
        padding: 12px 16px;
        color: var(--secondary-text-color, #666);
        font-style: italic;
        font-size: 0.5em;
      }
      .name.failed {
        color: #f44336;
      }
      .toolbar {
        display: flex; align-items: center; gap: 8px;
        margin: 16px 0; padding: 8px 12px;
        background: #ccc; border-radius: 8px;
      }
      .toolbar-info { flex: 1; color: #555; font-size: 0.9em; }

      .device-list { margin: 0; }
      .device-row {
        display: flex; align-items: center; gap: 12px;
        padding: 10px 20px; border-bottom: 1px solid #555;
        background: rgba(128, 128, 128, 0.1);
      }

      /* Header row */
      .device-list-header {
        border-bottom: 1.5px solid var(--secondary-text-color, #888);
        font-size: 1em;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .device-list-header .header-label { font-weight: 700; color: var(--primary-text-color); }
      .btn-placeholder {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 110px;
        padding: 6px 16px;
      }

      .online-status { flex: 0 0 20px; text-align: center; font-size: 0.75em; }
      .version { color: #666; font-size: 0.85em; white-space: nowrap; }
      .version .arrow { color: #4caf50; font-weight: bold; }

      .checkbox-col { flex: 0 0 24px; display: flex; align-items: center; justify-content: center; }
      .checkbox-col input { margin: 0; }
      .checkbox-col input:disabled { opacity: 0; }

      button {
        padding: 6px 16px; border: none; border-radius: 16px;
        cursor: pointer; font-size: 0.85em; font-weight: 500;
        white-space: nowrap;
        display: inline-flex; align-items: center; gap: 6px;
        min-height: 32px;
        box-sizing: border-box;
      }
      button:disabled { cursor: default; }

      .btn-uptodate { background: #4caf50; color: white; opacity: 0.8; }
      .btn-enable { background: #ff9800; color: white; }
      .btn-enable:hover:not(:disabled) { background: #f57c00; }
      .btn-enabling { background: #ff9800; color: white; opacity: 0.9; }
      .btn-update { background: #2196f3; color: white; }
      .btn-update:hover:not(:disabled) { background: #1976d2; }
      .btn-updating { background: #2196f3; color: white; opacity: 0.9; }
      .btn-unavailable { background: #58a9eb; color: white; opacity: 0.8; }
      .btn-offline { background: #666; color: white; opacity: 0.8; }
      .btn-skipped { background: #9c27b0; color: white; opacity: 0.8; }

      .btn-select-all { background: #666; color: white; }
      .btn-select-all:hover { background: #555; }
      .btn-batch-update { background: #2196f3; color: white; }
      .btn-batch-update:hover:not(:disabled) { background: #1976d2; }
      
      .btn-cancel { background: #f44336; color: white; }
      .btn-cancel:hover:not(:disabled) { background: #c62828; }
      .btn-cancel:disabled { opacity: 0.7; cursor: not-allowed; }

      .spinner {
        display: inline-block;
        width: 12px; height: 12px;
        border: 2px solid rgba(255,255,255,0.3);
        border-top-color: white;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }
      @keyframes spin { to { transform: rotate(360deg); } }

      /* Addon option */
      .addon-option {
        display: flex; align-items: center; gap: 8px;
        margin: 8px 0; padding: 8px 12px;
        background: #2a2a2a; border-radius: 8px;
        font-size: 0.9em; color: #ccc;
      }
      .addon-option input[type="checkbox"] { margin: 0; }
      .addon-option .addon-name { color: #ff9800; font-weight: 500; }
      .addon-option .addon-status { margin-left: auto; font-size: 0.85em; }
      .addon-running { color: #4caf50; margin-right: 2px; }
      .addon-stopped { color: #f44336; margin-right: 2px; }
      .addon-stopping { color: #ff9800; margin-right: 2px; }
      .addon-starting { color: #ff9800; margin-right: 2px; }

      .results { margin-top: 24px; }
      .results-header {
        display: flex; align-items: center; gap: 12px;
      }
      .results-header h3 { margin: 0; flex: 1; }
      .btn-clear {
        background: none; color: #f44336; border: 1px solid #888;
        border-radius: 16px; padding: 4px 12px; font-size: 0.8em;
      }
      .btn-clear:hover { background: #ccc; color: #333; }
      .btn-log {
        background: #666; color: white;
        border-radius: 16px; padding: 4px 12px; font-size: 0.8em;
      }
      .btn-log:hover { background: #555; }
      .result-row {
        display: flex; align-items: center; gap: 8px;
        padding: 4px 0;
      }
      .summary { color: #666; font-size: 0.9em; margin: 8px 0; }

      /* Log Popup */
      .log-overlay {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.7);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      }
      .log-popup {
        background: var(--card-background-color, #1c1c1c);
        border-radius: 12px;
        width: 90%;
        max-width: 700px;
        max-height: 80vh;
        display: flex;
        flex-direction: column;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
      }
      .log-popup-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px 20px;
        border-bottom: 1px solid #444;
      }
      .log-popup-header h2 {
        margin: 0;
        font-size: 1.2em;
      }
      .log-popup-close {
        background: none;
        border: none;
        color: #999;
        font-size: 1.5em;
        cursor: pointer;
        padding: 0;
        line-height: 1;
      }
      .log-popup-close:hover {
        color: #f44336;
      }
      .log-popup-content {
        flex: 1;
        overflow: auto;
        padding: 16px 20px;
      }
      .log-popup-content pre {
        margin: 0;
        white-space: pre-wrap;
        word-wrap: break-word;
        font-family: monospace;
        font-size: 0.85em;
        line-height: 1.5;
        color: var(--primary-text-color, #111);
      }

      /* Name tooltip */
      .name-tooltip {
        position: fixed;
        background: #333;
        color: white;
        padding: 8px 12px;
        border-radius: 8px;
        font-size: 0.9em;
        z-index: 1000;
        max-width: 80vw;
        word-wrap: break-word;
        box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        transform: translate(-50%, -100%) translateY(-10px);
        pointer-events: none;
      }

      .name {
        flex: 1;
        font-weight: 500;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
      }

      @media (max-width: 600px) and (pointer: coarse) {
        .content {
          padding-left: 0px;
          padding-right: 0px;
        }
        .device-row {
          padding-left: 10px;
          padding-right: 10px;
        }
        .toolbar {
          margin-left: 0;
        }
        .result-row {
          padding-left: 10px;
          padding-right: 10px;
        }
        .name {
          cursor: pointer;
        }
      }
    `;
  }

  // ── Main render ─────────────────────────────────────────────────

  render() {
    const merged = this._mergedDevices();
    const allResults = this._allResults;
    const selectableCount = merged.filter((d) => this._canSelect(d)).length;
    const onlineCount = merged.filter((d) => d.online === true).length;
    const offlineCount = merged.filter((d) => d.online === false).length;
    const unknownCount = merged.filter((d) => d.online === null).length;

    const showAddonOption = this._addonInfo?.installed;

    return html`
      ${this._tooltipName ? html`
        <div class="name-tooltip" style="top: ${this._tooltipY}px; left: ${this._tooltipX}px">
          ${this._tooltipName}
        </div>
      ` : ""}

      ${this._showLogPopup ? this._renderLogPopup() : ""}

      ${this.narrow ? html`
        <div class="app-toolbar">
          <button class="sidebar-toggle" @click=${this._toggleSidebar} title="Open sidebar">
            <svg viewBox="0 0 24 24">
              <path d="M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z" />
            </svg>
          </button>
          <span class="title">ESPHome Update Manager</span>
        </div>
      ` : ""}
      
      <h1>
        <img src="/local/esphome-update-manager/logo.png"
            style="height: 40px; vertical-align: middle; margin-right: 12px;">
        ESPHome Update Manager
        ${this._version ? html`<span class="version-badge">v${this._version}</span>` : ""}
        <span class="header-spacer"></span>
        <div class="header-menu-container">
          <button class="menu-btn" @click=${this._toggleMenu} title="View logs">⋮</button>
          ${this._showMenu ? this._renderMenu() : ""}
        </div>
      </h1>
      <div class="content">
        <div class="summary">
          ${merged.length} devices
          — ${onlineCount} online, ${offlineCount} offline${unknownCount > 0 ? html`, ${unknownCount} unknown` : ""}
        </div>

        ${showAddonOption ? html`
          <div class="addon-option">
            <input type="checkbox"
              .checked=${this._stopAddonDuringUpdate}
              ?disabled=${this.running}
              @change=${(e) => {
                this._stopAddonDuringUpdate = e.target.checked;
                this._saveAutoUpdateSettings();
              }} />
            <span>Stop <span class="addon-name">${this._addonInfo.name}</span> during updates</span>
            <span class="addon-status ${this._getAddonStatusDisplay().cls}">${this._getAddonStatusDisplay().text}</span>
          </div>
        ` : ""}

        <div class="addon-option">
          <input type="checkbox"
            .checked=${this._autoUpdateEnabled}
            @change=${(e) => {
              this._autoUpdateEnabled = e.target.checked;
              this._saveAutoUpdateSettings();
            }} />
          <span>Automatically start updates when available</span>
          ${this._autoUpdateEnabled
            ? html`<span class="addon-status addon-running">● Enabled</span>`
            : html`<span class="addon-status addon-stopped">● Disabled</span>`
          }
        </div>

        ${selectableCount > 0 || this.running ? html`
          <div class="toolbar">
            ${this.running ? html`
              <button class="btn-cancel" 
                ?disabled=${this._cancelling}
                @click=${() => this._cancelUpdates().catch(e => {
                  console.error("[ESPHome Update Manager] Cancel error:", e);
                  this._addLocalResult("Cancel", "failed", "Cancel failed to start: " + String(e?.message || e));
                })}>
                ${this._cancelling ? html`<span class="spinner"></span>` : ""}
                ${this._cancelling ? "Cancelling…" : "⏹ Cancel"}
              </button>
              <span class="toolbar-info">${this._getStatusText()}</span>
            ` : html`
              <button class="btn-select-all" @click=${this._selectAll}>
                ${this.selected.size === selectableCount ? "Deselect all" : "Select all"}
              </button>
              <button class="btn-batch-update"
                ?disabled=${this.selected.size === 0}
                @click=${() => this._startBatchUpdate().catch(e => {
                  console.error("[ESPHome Update Manager] Batch update error:", e);
                  this._addLocalResult("Batch update", "failed", "Batch update failed to start: " + String(e?.message || e));
                })}>
                ▶ Update selected (${this.selected.size})
              </button>
              <span class="toolbar-info">${selectableCount} device${selectableCount !== 1 ? "s" : ""} can be updated</span>
            `}
          </div>
        ` : ""}

        <div class="device-row device-list-header">
          <span class="checkbox-col"></span>
          <span class="online-status"></span>
          <span class="name header-label">DEVICES</span>
          <span class="version"></span>
          <span class="header-label btn-placeholder">FIRMWARE</span>
        </div>

        <div class="device-list">
          ${merged.map((d) => this._renderDevice(d))}
        </div>

        ${allResults.length > 0 ? this._renderResults(allResults) : ""}
      </div>
    `;
  }

  _renderMenu() {
    return html`
      <div class="header-menu">
        <button class="menu-item current-log" @click=${this._openLogPopup}>
          📄 Latest Log
        </button>
        ${this._logBackups.length > 0 ? html`
          <div class="menu-divider"></div>
          <div class="menu-section-title">Previous Logs</div>
          ${this._logBackups.map(backup => html`
            <button class="menu-item" 
              @click=${() => this._openBackupLog(backup.filename, backup.display_name)}>
              📋 ${backup.display_name}
            </button>
          `)}
        ` : html`
          <div class="menu-divider"></div>
          <div class="no-backups">No previous logs available</div>
        `}
      </div>
    `;
  }

  _renderDevice(d) {
    const btn = this._getDeviceButton(d);
    const canSelect = this._canSelect(d);
    const isOffline = d.online === false;
    
    const displayName = (this._isMixedSetup && d.is_external) 
      ? `${d.name} (ext)` 
      : d.name;

    return html`
      <div class="device-row ${isOffline ? "offline" : ""}">
        <span class="checkbox-col">
          ${canSelect ? html`
            <input type="checkbox"
              .checked=${this.selected.has(d.entity_id)}
              @change=${() => this._toggleSelect(d.entity_id)} />
          ` : html`
            <input type="checkbox" disabled .checked=${false} />
          `}
        </span>
        <span class="online-status">${this._onlineIcon(d.online)}</span>
        <span class="name ${d.failed ? 'failed' : ''}" 
          @click=${(e) => this._showNameTooltip(e, d.name)}
          title="${d.name}">
          ${displayName}
        </span>
        <span class="version">
          ${d.current_version || "?"}${(d.update_available || d.skipped) && d.latest_version
            ? html` <span class="arrow">→</span> ${d.latest_version}`
            : ""}
        </span>
        <button class="${btn.cls}"
          ?disabled=${btn.disabled}
          @click=${() => this._handleButtonClick(d)}>
          ${btn.spinner ? html`<span class="spinner"></span>` : ""}
          ${btn.label}
        </button>
      </div>
    `;
  }

  _renderResults(allResults) {
    return html`
      <div class="results">
        <div class="results-header">
          <h3>Results</h3>
          ${!this.running ? html`
            <button class="btn-log" @click=${this._openLogPopup}>📄 View Log</button>
            <button class="btn-clear" @click=${this._clearResults}>✕ Clear</button>
          ` : ""}
        </div>
        ${allResults.map((r) => html`
          <div class="result-row">
            <span>${this._statusIcon(r.status)}</span>
            <span class="name">${r.entity_id}</span>
            <span>${r.status}</span>
            ${r.error ? html`<span style="color:red; font-size:0.85em">— ${r.error}</span>` : ""}
          </div>
        `)}
      </div>
    `;
  }

  _renderLogPopup() {
    return html`
      <div class="log-overlay" @click=${(e) => {
        if (e.target.classList.contains("log-overlay")) this._closeLogPopup();
      }}>
        <div class="log-popup">
          <div class="log-popup-header">
            <h2>${this._logTitle}</h2>
            <button class="log-popup-close" @click=${this._closeLogPopup}>✕</button>
          </div>
          <div class="log-popup-content">
            <pre>${this._logContent || "Loading..."}</pre>
          </div>
        </div>
      </div>
    `;
  }
}

if (!customElements.get("esphome-update-panel")) {
  customElements.define("esphome-update-panel", ESPHomeUpdatePanel);
}

// ── Console version log ─────────────────────────────────────────────────

(function() {
  // Extract version from script URL (?v=x.x.x)
  const scripts = document.querySelectorAll('script[src*="esphome-update-panel"]');
  let version = "unknown";
  
  for (const script of scripts) {
    const match = script.src.match(/[?&]v=([^&]+)/);
    if (match) {
      version = match[1];
      break;
    }
  }
  
  // Also check module imports
  if (version === "unknown") {
    const currentScript = document.currentScript;
    if (currentScript?.src) {
      const match = currentScript.src.match(/[?&]v=([^&]+)/);
      if (match) version = match[1];
    }
  }
  
  console.info(
    `%c  ESPHOME-UPDATE-MANAGER  %c  v${version}  `,
    "color: #fff; background: #039be5; font-weight: bold; padding: 2px 0;",
    "color: #039be5; background: #fff; font-weight: bold; padding: 2px 0;"
  );
})();

// ── Auto reload ─────────────────────────────────────────────────

(function() {
  let lastActiveTime = Date.now();
  const INACTIVE_THRESHOLD = 300000; // 5 minutes

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      lastActiveTime = Date.now();
    }

    if (document.visibilityState === 'visible') {
      const inactiveTime = Date.now() - lastActiveTime;
      const isOnPanel = window.location.pathname.includes('esphome-update-manager');
      if (inactiveTime > INACTIVE_THRESHOLD) {
        location.reload();
      }
    }
  });
})();
