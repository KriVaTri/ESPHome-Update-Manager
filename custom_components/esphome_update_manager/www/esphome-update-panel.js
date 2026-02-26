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
    const stored = localStorage.getItem("esphome_update_manager_stop_addon");
    this._stopAddonDuringUpdate = stored !== null ? stored === "true" : true;
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadDevices();
    this._loadAddonInfo();
    this._addonPollTimer = setInterval(() => this._loadAddonInfo(), 30000);
    this._pollStatus().then(() => {
      if (this.running) {
        this._restoreUpdatingState();
        this._startStatusPolling();
      }
    });
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

  _startEnablingPoll() {
    this._enablingPollTimer = setInterval(() => this._loadDevices(), 5000);
  }

  _stopEnablingPoll() {
    if (this._enablingPollTimer) {
      clearInterval(this._enablingPollTimer);
      this._enablingPollTimer = null;
    }
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
    try { await this.hass.callWS({ type: "esphome_update_manager/cancel" }); }
    catch (e) { console.error("Cancel failed:", e); }
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
        padding: 16px;
        font-family: var(--paper-font-body1_-_font-family, "Roboto", sans-serif);
      }
      h1 { margin: 0 0 16px; }

      .toolbar {
        display: flex; align-items: center; gap: 8px;
        margin: 16px 0; padding: 8px 12px;
        background: #ccc; border-radius: 8px;
      }
      .toolbar-info { flex: 1; color: #555; font-size: 0.9em; }

      .device-list { margin: 0; }
      .device-row {
        display: flex; align-items: center; gap: 12px;
        padding: 10px 12px; border-bottom: 1px solid #555;
      }

      /* Header row */
      .device-list-header {
        border-bottom: 1.5px solid #eee;
        font-size: 1em;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .device-list-header:hover { background: transparent !important; }
      .device-list-header .header-label { font-weight: 700; color: #ffffff; }
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
      .btn-offline { background: #666; color: white; opacity: 0.8;}

      .btn-select-all { background: #666; color: white; }
      .btn-batch-update { background: #2196f3; color: white; }
      .btn-batch-update:hover:not(:disabled) { background: #1976d2; }
      .btn-cancel { background: #f44336; color: white; }

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
      .addon-running { color: #4caf50; }
      .addon-stopped { color: #f44336; }

      .results { margin-top: 24px; }
      .results-header {
        display: flex; align-items: center; gap: 12px;
      }
      .results-header h3 { margin: 0; flex: 1; }
      .btn-clear {
        background: none; color: #999; border: 1px solid #ddd;
        border-radius: 16px; padding: 4px 12px; font-size: 0.8em;
      }
      .btn-clear:hover { background: #f5f5f5; color: #666; }
      .result-row {
        display: flex; align-items: center; gap: 8px;
        padding: 4px 0;
      }
      .summary { color: #666; font-size: 0.9em; margin: 8px 0; }
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
      <h1>
        <img src="https://raw.githubusercontent.com/KriVaTri/ESPHome-Update-Manager/refs/heads/main/logo/logo.png"
            style="height: 50px; vertical-align: middle; margin-right: 8px;">
        ESPHome Update Manager
      </h1>
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
              localStorage.setItem("esphome_update_manager_stop_addon", e.target.checked);
            }} />
          <span>Stop <span class="addon-name">${this._addonInfo.name}</span> during updates to free memory</span>
          ${this._addonInfo.running
            ? html`<span class="addon-status addon-running">● Running</span>`
            : html`<span class="addon-status addon-stopped">● Stopped</span>`
          }
        </div>
      ` : ""}

      ${selectableCount > 0 || this.running ? html`
        <div class="toolbar">
          ${this.running ? html`
            <button class="btn-cancel" @click=${this._cancelUpdates}>⏹ Cancel</button>
            <span class="toolbar-info">Updating…</span>
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
      if (inactiveTime > INACTIVE_THRESHOLD) {
        location.reload();
      }
    }
  });
})();
