# ESPHome Update Manager
ESPHome device update manager for Home Assistant
A custom Home Assistant integration that provides a dedicated panel for managing firmware updates across all your ESPHome devices.

## Features

- **Centralized dashboard** — View all ESPHome devices, their firmware versions, and online status in one place
- **Batch updates** — Select multiple devices and update them sequentially with a single click
- **Individual updates** — Update a single device directly from the panel
- **Enable firmware entities** — Disabled firmware update entities can be enabled directly from the panel
- **Smart error handling** — Compile errors, OTA failures, and offline devices are detected and reported immediately
- **VS Code Server add-on management** — Optionally stop the VS Code Server add-on during updates to free memory, and automatically restart it when updates are complete
- **Real-time status** — Live progress tracking with online/offline indicators for each device
- **Resilient queue** — If a device fails, the queue continues with the next device
- **Cancel anytime** — Cancel running updates at any time; remaining devices are marked as cancelled

## Requirements

- Home Assistant 2024.1 or newer
- ESPHome integration configured with one or more devices
- ESPHome Device Builder (Dashboard) add-on installed

## Recommendations

- add the following binary_sensor to your device yaml file
  ```
  binary_sensor:
    - platform: status
      name: "Status"
  ```
    
## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu (top right) → **Custom repositories**
3. Add the repository URL and select **Integration** as category
4. Search for **ESPHome Update Manager** and install it
5. Restart Home Assistant

### Manual installation

1. Copy the `custom_components/esphome_update_manager` folder to your Home Assistant `config/custom_components/` directory
2. Copy `esphome-update-panel.js` to `config/www/esphome-update-manager/esphome-update-panel.js`
3. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **ESPHome Update Manager**
3. Click to add — no configuration needed
4. A new **ESPHome Updates** panel appears in the sidebar

## Usage

### Device list

The panel shows all ESPHome devices with:

| Column | Description |
|--------|-------------|
| ☑️ Checkbox | Select devices for batch update |
| 🟢🔴🟡 Status | Online, offline, or unknown |
| Name | Device name |
| Version | Current version → available version |
| Button | Action button (see below) |

### Device buttons

| Button | Meaning |
|--------|---------|
| **Update** (blue) | Update available — click to start |
| **Up to date** (green) | Device is on the latest firmware |
| **Enable** (orange) | Firmware entity is disabled — click to enable |
| **Enabling…** (orange + spinner) | Entity is being enabled, waiting for HA to pick it up |
| **Updating…** (blue + spinner) | Update is in progress |
| **Offline** (grey) | Device is not reachable |
| **Unavailable** (light blue) | Firmware entity is unavailable |

### Batch updates

1. Select devices using the checkboxes (or click **Select all**)
2. Click **▶ Update selected (n)**
3. Devices are updated one by one sequentially
4. Progress and results are shown in real-time
5. Click **⏹ Cancel** to stop the queue at any time

### VS Code Server add-on

If the **VS Code Server** (Studio Code Server) add-on is installed, a checkbox appears:

> ☑️ Stop **Studio Code Server** during updates to free memory — ● Running / ● Stopped

- When checked, the add-on is automatically stopped before updates begin and restarted after all updates complete
- The add-on is always restarted, even if updates are cancelled or fail
- The current status (Running/Stopped) is polled every 30 seconds
- Your preference is saved and persists across browser refreshes

This is useful for systems with limited memory where the VS Code Server add-on can cause ESPHome compilations to fail due to insufficient RAM.

### Results

After updates complete, a results section shows the outcome for each device:

| Icon | Status | Description |
|------|--------|-------------|
| ✅ | Success | Update completed successfully |
| ❌ | Failed | Update failed (with error details) |
| ⏭️ | Skipped | Device was unavailable — skipped |
| ⛔ | Cancelled | Update was cancelled by user |
| ⏳ | Queued | Waiting in queue |
| 🔄 | Running | Currently updating |

Click **✕ Clear** to dismiss the results.

## Error handling

The integration handles various failure scenarios gracefully:

| Scenario | Detection | Action |
|----------|-----------|--------|
| YAML compile error | Immediate | Marked as failed, queue continues |
| OTA upload failure | Immediate | Marked as failed, queue continues |
| Insufficient memory | Immediate | Marked as failed, queue continues |
| Device offline before update | Immediate | Marked as skipped, queue continues |
| Device goes offline during update | ~2 minutes | Marked as failed, queue continues |
| Device does not recover after OTA | ~2 minutes | Marked as failed, queue continues |
| Update timeout | ~5 minutes | Marked as failed, queue continues |

**A failed device never blocks the rest of the queue.** Only an explicit cancel stops all remaining updates.

## File structure

```
config/
├── custom_components/
│   └── esphome_update_manager/
│       ├── __init__.py          # Integration setup, WS commands, Supervisor API
│       ├── manifest.json        # Integration manifest
│       ├── config_flow.py       # Config flow for UI setup
│       ├── const.py             # Constants
│       ├── strings.json         # UI strings
│       └── update_queue.py      # Sequential update queue manager
└── www/
    └── esphome-update-manager/
        └── esphome-update-panel.js  # Frontend panel
```

## Troubleshooting

### Panel does not appear in sidebar
- Make sure the integration is added via Settings → Devices & Services
- Check that `esphome-update-panel.js` exists in `config/www/esphome-update-manager/`
- Restart Home Assistant and clear your browser cache

### "Overwriting panel" error on reload
- This is handled automatically — the integration checks if the panel is already registered before creating it

### VS Code Server checkbox does not appear
- The add-on must be installed (it does not need to be running)
- Check Home Assistant logs for Supervisor API errors

### Updates fail with memory errors
- Enable the "Stop VS Code Server during updates" option
- Consider stopping other memory-heavy add-ons manually

## License

MIT License — see [LICENSE](LICENSE) for details.
