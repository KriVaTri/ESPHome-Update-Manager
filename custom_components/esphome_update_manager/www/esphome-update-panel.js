import {
  LitElement,
  html,
  css,
} from "https://unpkg.com/lit-element@2.4.0/lit-element.js?module";

const ENABLING_TIMEOUT_MS = 90000;
const UPDATING_TIMEOUT_MS = 600000;

class ESPHomeUpdatePanel extends LitElement {
  static get properties() {
    return {
      hass: { type: Object },
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
      _cancelling: { type: Boolean },
    };
  }

  constructor() {
    super();
    this.devices = [];
    this.selected = new Set();
    this.results = [];
    this.running = false;
    this._pendingEnables = new Map();
    this._updatingIds = new Map();
    this._localResults = [];
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
    this._cancelling = false;
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
    
    // Start polling immediately to catch backend-initiated updates
    this._startBackgroundStatusCheck();
    
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

  // ── Log Popup ───────────────────────────────────────────────────

  async _openLogPopup() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/get_update_log" });
      if (res.exists) {
        this._logContent = res.content;
        this._showLogPopup = true;
      } else {
        this._logContent = "No update log available yet.";
        this._showLogPopup = true;
      }
    } catch (e) {
      console.error("Failed to load update log", e);
      this._logContent = "Failed to load update log.";
      this._showLogPopup = true;
    }
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

  // ── Data ────────────────────────────────────────────────────────

  _restoreUpdatingState() {
    if (!this.results || this.results.length === 0) return;
    this._updatingIds = new Map(this._updatingIds);
    for (const r of this.results) {
      if (r.status === "running" || r.status === "queued") {
        if (!this._updatingIds.has(r.entity_id)) {
          const timeoutId = setTimeout(() => {
            this._expireUpdating(r.entity_id);
          }, UPDATING_TIMEOUT_MS);
          this._updatingIds.set(r.entity_id, { startedAt: Date.now(), timeoutId });
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
    this._addLocalResult(entityId, "failed", "Enable timed out — device may be unavailable");
    this._loadDevices();
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
    this._cancelUpdates();
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
    return this.devices.map((d) => {
      const isPending = this._isEnablingPending(d.entity_id);
      if (isPending && !d.firmware_disabled && !d.enabling) {
        const info = this._pendingEnables.get(d.entity_id);
        if (info?.timeoutId) clearTimeout(info.timeoutId);
        this._pendingEnables.delete(d.entity_id);
        this._pendingEnables = new Map(this._pendingEnables);
        return d;
      }
      if (isPending && d.firmware_disabled) {
        return { ...d, firmware_disabled: false, enabling: true };
      }
      return d;
    });
  }

  async _loadDevices() {
    try {
      const res = await this.hass.callWS({ type: "esphome_update_manager/devices" });
      this.devices = res.devices || [];
      const merged = this._mergedDevices();
      const hasEnabling = merged.some((d) => d.enabling) || this._pendingEnables.size > 0;
      if (hasEnabling && !this._enablingPollTimer) this._startEnablingPoll();
      else if (!hasEnabling && this._enablingPollTimer) this._stopEnablingPoll();
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
          }
        }
        this._updatingIds = new Map(this._updatingIds);
      }
    } catch (e) {
      // Not yet available
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
    const timeoutId = setTimeout(() => this._expireEnabling(entityId), ENABLING_TIMEOUT_MS);
    this._pendingEnables = new Map(this._pendingEnables);
    this._pendingEnables.set(entityId, { startedAt: Date.now(), timeoutId });
    this.requestUpdate();
    if (!this._enablingPollTimer) this._startEnablingPoll();

    try {
      await this.hass.callWS({ type: "esphome_update_manager/enable_entity", entity_id: entityId });
    } catch (e) {
      const info = this._pendingEnables.get(entityId);
      if (info?.timeoutId) clearTimeout(info.timeoutId);
      this._pendingEnables.delete(entityId);
      this._pendingEnables = new Map(this._pendingEnables);
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
    const timeoutId = setTimeout(() => this._expireUpdating(entityId), UPDATING_TIMEOUT_MS);
    this._updatingIds = new Map(this._updatingIds);
    this._updatingIds.set(entityId, { startedAt: Date.now(), timeoutId });
    this.requestUpdate();

    try {
      await this.hass.callWS({
        type: "esphome_update_manager/start",
        entity_ids: [entityId],
        stop_addon_slug: this._getStopAddonSlug(),
      });
      this.running = true;
      this._startStatusPolling();
    } catch (e) {
      const info = this._updatingIds.get(entityId);
      if (info?.timeoutId) clearTimeout(info.timeoutId);
      this._updatingIds.delete(entityId);
      this._updatingIds = new Map(this._updatingIds);
      this._addLocalResult(entityId, "failed", "Update failed to start: " + e.message);
      this.requestUpdate();
    }
  }

  async _startBatchUpdate() {
    if (this.selected.size === 0) return;
    const ids = [...this.selected];
    this._updatingIds = new Map(this._updatingIds);
    ids.forEach((id) => {
      const timeoutId = setTimeout(() => this._expireUpdating(id), UPDATING_TIMEOUT_MS);
      this._updatingIds.set(id, { startedAt: Date.now(), timeoutId });
    });
    this.requestUpdate();

    try {
      await this.hass.callWS({
        type: "esphome_update_manager/start",
        entity_ids: ids,
        stop_addon_slug: this._getStopAddonSlug(),
      });
      this.running = true;
      this._startStatusPolling();
    } catch (e) {
      ids.forEach((id) => {
        const info = this._updatingIds.get(id);
        if (info?.timeoutId) clearTimeout(info.timeoutId);
        this._updatingIds.delete(id);
        this._addLocalResult(id, "failed", "Batch update failed to start: " + e.message);
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
    return { label: "Up to date", cls: "btn-uptodate", disabled: true, action: null, spinner: false };
  }

  _handleButtonClick(d) {
    const btn = this._getDeviceButton(d);
    if (btn.action === "enable") this._enableEntity(d.entity_id);
    else if (btn.action === "update") this._updateSingle(d.entity_id);
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

  // ── Styles ──────────────────────────────────────────────────────

  static get styles() {
    return css`
      :host {
        display: block;
        padding: 0;
        font-family: var(--paper-font-body1_-_font-family, "Roboto", sans-serif);
      }
      h1 { 
        margin: 0 0 16px; 
        padding: 8px 16px;
        background: var(--secondary-background-color, #e0e0e0);
        display: flex;
        align-items: center;
      }
      .content {
        padding: 0 16px 16px;
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
      .name { flex: 1; font-weight: 500; }
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
      .btn-unavailable { background: #90caf9; color: white; opacity: 0.7; }
      .btn-offline { background: #666; color: white; opacity: 0.8; }

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
      .addon-running { color: #4caf50; margin-right: 10px; }
      .addon-stopped { color: #f44336; margin-right: 10px; }

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
      ${this._showLogPopup ? this._renderLogPopup() : ""}
      
      <h1>
        <img src="/local/esphome-update-manager/logo.png"
            style="height: 40px; vertical-align: middle; margin-right: 12px;">
        ESPHome Update Manager
        ${this._version ? html`<span class="version-badge">v${this._version}</span>` : ""}
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
              @change=${(e) => {
                this._stopAddonDuringUpdate = e.target.checked;
                this._saveAutoUpdateSettings();
              }} />
            <span>Stop <span class="addon-name">${this._addonInfo.name}</span> during updates to free memory</span>
            ${this._addonInfo.running
              ? html`<span class="addon-status addon-running">● Running</span>`
              : html`<span class="addon-status addon-stopped">● Stopped</span>`
            }
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
                @click=${this._cancelUpdates}>
                ${this._cancelling ? html`<span class="spinner"></span>` : ""}
                ${this._cancelling ? "Cancelling…" : "⏹ Cancel"}
              </button>
              <span class="toolbar-info">${this._cancelling ? "Cancelling…" : "Updating…"}</span>
            ` : html`
              <button class="btn-select-all" @click=${this._selectAll}>
                ${this.selected.size === selectableCount ? "Deselect all" : "Select all"}
              </button>
              <button class="btn-batch-update"
                ?disabled=${this.selected.size === 0}
                @click=${this._startBatchUpdate}>
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

  _renderDevice(d) {
    const btn = this._getDeviceButton(d);
    const canSelect = this._canSelect(d);
    const isOffline = d.online === false;

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
        <span class="name">${d.name}</span>
        <span class="version">
          ${d.current_version || "?"}${d.update_available && d.latest_version
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
            <h2>📄 Update Log</h2>
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
