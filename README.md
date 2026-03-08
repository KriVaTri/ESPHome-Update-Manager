# <img width="50" height="50" align="absmiddle" alt="logo" src="https://github.com/user-attachments/assets/402670fd-f94c-4b9e-a895-66d8e41a5c6e" /> ESPHome Update Manager

[![Latest Release](https://img.shields.io/github/v/release/KriVaTri/esphome-update-manager?include_prereleases&label=release)](https://github.com/KriVaTri/ESPHome-Update-Manager/releases)


ESPHome device update manager for Home Assistant.

A custom Home Assistant integration that provides a dedicated panel for managing firmware updates across all your ESPHome devices.

> **Note:** This integration is intended for users who compile and flash ESPHome updates directly from Home Assistant (using the ESPHome app (formerly known as add-on) or Device Builder).

## Features

- **Centralized dashboard** — View all ESPHome devices, their firmware versions, and online status in one place
- **Batch updates** — Select multiple devices and update them sequentially with a single click
- **Individual updates** — Update a single device directly from the panel
- **Auto-update** — Automatically start updates when new firmware becomes available
- **Enable firmware entities** — Disabled firmware update entities can be enabled directly from the panel
- **Smart error handling** — Compile errors, OTA failures, and offline devices are detected and reported immediately
- **Failure notifications** — Persistent notifications alert you when updates fail, with a link to the update log
- **Update log** — Detailed log of all update results, viewable directly in the panel
- **Log history** — Access previous update logs via the 3-dots menu (configurable backup count)
- **VS Code Server add-on management** — Optionally stop the VS Code Server add-on during updates to free memory, and automatically restart it when updates are complete
- **Real-time status** — Live progress tracking with online/offline indicators for each device
- **Resilient queue** — If a device fails, the queue continues with the next device
- **Cancel anytime** — Cancel running updates at any time; remaining devices are marked as cancelled

## Requirements

- Home Assistant 2024.1 or newer
- ESPHome integration configured with your devices
- ESPHome Device Builder (Dashboard) add-on installed and populated with your devices

## Recommendations

- Add the following binary_sensor to your device yaml file for improved integration performance and functionality:
  
  ```yaml
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
2. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **ESPHome Update Manager**
3. Click to add — no configuration needed
4. A new **ESPHome Updates** panel appears in the sidebar

## Configuration

The integration can be configured via **Settings → Devices & Services → ESPHome Update Manager → Configure**:

| Option | Default | Description |
|--------|---------|-------------|
| Maximum number of log backups to keep | 5 | Number of previous update logs to retain (0 = disable backups) |

## Usage

### Device list

The panel shows all ESPHome devices with:

| Column | Description |
|--------|-------------|
| ☑️ Checkbox | Select devices for batch update |
| 🟢🔴🟡 Status | Online, offline, or unknown |
| Name | Device name |
| Version | Current version → available version `or` Current version only if up-to-date|
| Button | Action button (see below) |

### Device buttons

| Button | Meaning |
|--------|---------|
| **Update** (blue) | Update ready to install — click to start |
| **Up to date** (green) | Device is on the latest firmware |
| **Enable** (orange) | Firmware entity is disabled — click to enable |
| **Enabling…** (orange + spinner) | Entity is being enabled, waiting for HA to pick it up |
| **Updating…** (blue + spinner) | Update is in progress |
| **Offline** (grey) | Device is not reachable |
| **Unavailable** (light blue) | Firmware entity is unavailable |

### Batch updates

1. Select devices using the checkboxes (or click **Select all**)
2. Click **▶ Update selected (n)**
3. Devices are updated sequentially
4. Progress and results are shown in real-time
5. Click **⏹ Cancel** to stop the queue at any time

### Auto-update

A checkbox enables automatic updates:

> ☑️ Automatically start updates when available — ● Enabled / ● Disabled

- When enabled, the integration monitors all ESPHome device update entities
- When a device's firmware state changes to "update available" (e.g., after coming online or after ESPHome is updated), the update starts automatically
- Auto-updates respect the "Stop VS Code Server" setting
- Auto-updates work even when the panel is not open
- The setting persists across Home Assistant restarts

**Note:** Auto-update triggers when a device transitions to having an update available. This happens when:
- The auto-update option is enabled and devices have pending updates
- A device comes online and has a pending update
- ESPHome is updated and devices now have newer firmware available
- Home Assistant restarts and devices have pending updates

### Service: Start Updates

The integration provides a service that can be used in automations:

**Service:** `esphome_update_manager.start_updates`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `entity_ids` | No | List of specific entity IDs to update. If not provided, all devices with available updates will be updated. |
| `stop_addon` | No | Whether to stop VS Code Server during updates. If not provided, uses the saved panel setting. |

#### Example automations

**Update all devices at 3 AM:**

```yaml
alias: ESPHome start devices update
description: time to start updating esphome devices
triggers:
  - trigger: time
    at: "03:00:00"
conditions: []
actions:
  - action: esphome_update_manager.start_updates
    data: {}
mode: single
```

### VS Code Server add-on

If the **VS Code Server** (Studio Code Server) add-on is installed, a checkbox appears:

> ☑️ Stop **Studio Code Server** during updates to free memory — ● Running / ● Stopped

- When checked, the add-on is automatically stopped before updates begin and restarted after all updates complete
- The add-on is always restarted, even if updates are cancelled or fail
- The current status (Running/Stopped) is polled every 30 seconds
- This setting applies to both manual and auto-updates

`This is useful for systems with limited memory where the VS Code Server add-on can cause ESPHome compilations to fail due to insufficient RAM.`

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

### Update log

Access update logs via the **⋮** menu in the top-right corner of the panel:

- **Latest Log** — View the most recent update log
- **Previous Logs** — Browse previous update logs (configurable, default: 5)

Each log includes:
- Timestamp of the update run
- Summary with success/failed/skipped/cancelled counts
- Details per device including status, start time, finish time, and any error messages

Logs are stored at:
- Current log: `config/www/esphome-update-manager/update_log.txt`
- Backups: `config/www/esphome-update-manager/log-backups/`

A new backup is created automatically after each update batch completes. The number of backups kept can be configured in the integration settings (set to 0 to disable backups).

### Failure notifications

When one or more updates fail, a persistent notification is created in Home Assistant:

> **ESPHome Update Failed**  
> Update for X ESPHome device(s) has failed.  
> *View update log* (clickable link)

Clicking the link opens the panel and automatically displays the latest update log.

<img width="500" height="401" alt="log" src="https://github.com/user-attachments/assets/6d978086-a895-42e7-a0d2-6824f66563af" />

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

`A failed device never blocks the rest of the queue. Only an explicit cancel stops all remaining updates.`


## Examples Panel

### Update ready to install

<img width="700" height="460" alt="update1" src="https://github.com/user-attachments/assets/0b44677e-5030-4424-8bca-ff5f369c8b76" />



### Update in progress

<img width="700" height="460" alt="update2" src="https://github.com/user-attachments/assets/fd477355-7e30-43c3-bebd-8205e7cf50e8" />



### Update successful

<img width="700" height="460" alt="update3" src="https://github.com/user-attachments/assets/74513810-cfb9-4810-abb6-08f32e90a7c6" />



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

### Auto-update does not trigger
- Ensure the "Automatically start updates when available" checkbox is enabled
- Check that your devices have the `binary_sensor.status` entity (see Recommendations)
- Auto-update only triggers on state transitions (e.g., device coming online), not when already in "update available" state
- Check Home Assistant logs for `esphome_update_manager` entries

### Notification link does not open the log
- Clear your browser cache and reload the panel
- Ensure the panel is accessible at `/esphome-update-manager`

## License

MIT License — see [LICENSE](LICENSE) for details.
