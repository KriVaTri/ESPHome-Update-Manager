# <img width="50" height="50" align="absmiddle" alt="logo" src="https://github.com/user-attachments/assets/402670fd-f94c-4b9e-a895-66d8e41a5c6e" /> ESPHome Update Manager

[![Latest Release](https://img.shields.io/github/v/release/KriVaTri/esphome-update-manager?include_prereleases&label=latest%20release)](https://github.com/KriVaTri/ESPHome-Update-Manager/releases)

ESPHome device update manager for Home Assistant.

A custom Home Assistant integration that provides a dedicated panel for managing ESPHome firmware updates across all your ESPHome devices.

> **Note:** Since version 1.4.0 the integration supports both local and external ESPHome dashboards. Earlier versions only support the local ESPHome add-on/app.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Recommendations](#recommendations)
- [Installation](#installation)
- [Configuration](#configuration)
  - [External Dashboard Setup](#external-dashboard-setup)
- [Usage](#usage)
  - [Device list](#device-list)
  - [Device button](#device-button)
  - [Batch updates](#batch-updates)
  - [Force Install](#force-install)
    - [Via frontend](#via-frontend)
    - [Via service](#via-service-force_install)
    - [Project version auto-check](#project-version-auto-check)
  - [Auto-update](#auto-update)
  - [VS Code Server add-on](#vs-code-server-add-on)
  - [Results](#results)
  - [Update log](#update-log)
  - [Failure notifications](#failure-notifications)
- [Error handling](#error-handling)
  - [Failed update](#failed-update)
- [Troubleshooting](#troubleshooting)
- [Uninstallation](#uninstallation)
- [License](#license)

## Features

- **Centralized dashboard** — View all ESPHome devices, their firmware versions, and status in one place
- **External dashboard support** — Connect to an ESPHome dashboard running on another machine (e.g., a separate build server)
- **Dashboard authentication** — Support for username/password authentication on external dashboards
- **Mixed setup support** — Use both local ESPHome add-on and external dashboard simultaneously
- **Batch updates** — Select multiple devices and update them sequentially with a single click
- **Individual updates** — Update a single device directly from the panel
- **Force Install** — Recompile and upload firmware via the ESPHome dashboard, even when no update is available. Also used for project version updates
- **Project version auto-check** — Automatically detects and installs project version updates when a device comes online, HA restarts, or the external dashboard comes online
- **Auto-update** — Automatically start updates when new firmware becomes available
- **Enable firmware entities** — Disabled firmware update entities can be enabled directly from the panel
- **Skipped update detection** — Devices with updates skipped via Home Assistant are clearly marked
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
- ESPHome Device Builder (Dashboard) app (add-on) installed and populated with your devices, **or** an external ESPHome dashboard accessible via HTTP

## Recommendations

- Add the following binary_sensor to your device yaml file for improved integration performance and functionality (online status):
  
  ```yaml
  binary_sensor:
    - platform: status
      name: "Status"
  ```

## Installation

1. Via HACS: Search for **ESPHome Update Manager**, download and restart Home Assistant

   Or manual: Copy the `custom_components/esphome_update_manager` folder to your Home Assistant `config/custom_components/` directory and restart Home Assistant
2. Add integration: Home Assistant → Settings → Devices & Services → Add Integration → search **ESPHome Update Manager** → submit
3. Optionally configure an external ESPHome dashboard URL (see Configuration) and log backup count
4. A new **ESPHome Updates** panel appears in the sidebar

## Configuration

The integration can be configured via **Settings → Devices & Services → ESPHome Update Manager → Configure**:

| Option | Default | Description |
|--------|---------|-------------|
| External Dashboard URL | *(empty)* | URL of an external ESPHome dashboard (e.g., `http://192.168.1.100:6052`). Leave empty to use only the local ESPHome add-on. |
| Username | *(empty)* | Username for dashboard authentication (optional) |
| Password | *(empty)* | Password for dashboard authentication (optional) |
| Number of log backups to keep | 5 | Number of previous update logs to retain (0 = disable backups) |

### External Dashboard Setup

If you run ESPHome on a separate machine (e.g., a dedicated build server), you can configure the integration to use that dashboard for compiling and uploading firmware:

1. Go to **Settings → Devices & Services → ESPHome Update Manager → Configure**
2. Enter the URL of your external ESPHome dashboard (e.g., `http://192.168.1.100:6052`)
3. If your dashboard requires authentication, enter the username and password
4. Click **Submit**

The integration will automatically reload and connect to the external dashboard.

**Requirements for external dashboard:**
- The dashboard must be accessible from Home Assistant via HTTP
- The devices must also exist in Home Assistant's ESPHome integration

**Authentication:**
- If your external dashboard is protected with basic authentication, enter the username and password in the configuration
- The credentials are used for both HTTP requests and WebSocket connections (compile/upload)
- When updating credentials in the options flow, leave the password field empty to keep the current password

**To disconnect from an external dashboard:**
- Clear the URL field or set it to `http://`
- This will remove the external dashboard configuration and credentials

**Mixed setup:**
When using an external dashboard, you can have devices managed by both the local ESPHome add-on and the external dashboard. In mixed setups, external devices are marked with `(ext)` after their name in the panel.

**Dashboard offline handling:**
- If the external dashboard becomes unreachable, affected devices show "Unavailable" status
- The panel automatically updates within ~1 minute when the dashboard comes back online
- Auto-update triggers automatically when the dashboard comes online with pending updates

## Usage

### Device list

The panel shows all ESPHome devices with:

| Column | Description |
|--------|-------------|
| ☑️ Checkbox | Select devices for batch update |
| 🟢🔴🟡 Status | Online, offline, or unknown |
| Name | Device name (with `(ext)` suffix in mixed setups for external devices) |
| Version | Current version → available version `or` Current version only if up-to-date|
| Button | Action button (see below) |

### Device button

| Button | Meaning |
|--------|---------|
| **Update** (blue) | Update ready to install — click to start |
| **Up to date** (green) | Device is on the latest firmware |
| **Skipped** (purple) | Update was skipped via Home Assistant — clear skip in HA to update |
| **Enable** (orange) | Firmware entity is disabled — click to enable |
| **Enabling…** (orange + spinner) | Entity is being enabled, waiting for HA to pick it up |
| **Updating…** (blue + spinner) | Update is in progress |
| **Installing…** (blue + spinner) | Force install is in progress |
| **Offline** (grey) | Device is not reachable |
| **Unavailable** (light blue) | Firmware entity is unavailable (or external dashboard offline) |

### Batch updates

1. Select devices using the checkboxes (or click **Select all**)
2. Click **▶ Update selected (n)**
3. Devices are updated sequentially
4. Progress and results are shown in real-time
5. Click **⏹ Cancel** to stop the queue at any time

### Force Install

Force Install recompiles the firmware via the ESPHome dashboard and uploads it via OTA, regardless of whether an update is available. This is useful when:

- You want to push a configuration change that does not change the firmware version
- A device's project version in the YAML is higher than what is installed on the device
- You want to force a clean reinstall of the current firmware
- You want to update multiple devices in one batch — Force Install compiles and uploads all selected devices sequentially, without needing a firmware version bump. This is the most powerful feature of Force Install: push any change to any number of devices in one single operation.

#### Via frontend

1. Click **Force Install** in the toolbar (only visible when no update is running)
2. The panel switches to Force Install mode — all online devices become selectable
3. Select one or more devices using the checkboxes (or click **Select all**)
4. Click **▶ Force Install (n)**
5. The integration compiles the firmware and uploads it via OTA for each selected device sequentially
6. Progress and results are shown in real-time, just like a regular update
7. Click **✕ Cancel** to exit Force Install mode without starting

> **Note:** Force Install always recompiles the firmware, even if nothing has changed. This may take a few minutes per device depending on your hardware.

#### Via service: `force_install`

Force Install can also be triggered via a Home Assistant service call, useful for automations:

**Service:** `esphome_update_manager.force_install`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `device_id` | Yes | One or more Home Assistant device IDs to force install. Can be a single string or a list. |

**Example automation — force install a specific device:**

```yaml
actions:
  - action: esphome_update_manager.force_install
    data:
      device_id: "abc123def456"
```

**Example automation — force install multiple devices:**

```yaml
actions:
  - action: esphome_update_manager.force_install
    data:
      device_id:
        - "abc123def456"
        - "789xyz000111"
```

> **Note:** The `device_id` is the Home Assistant device ID, not the entity ID. You can find the device ID in **Settings → Devices & Services → ESPHome → [your device] → ⋮ → Device info → ID**.

#### Project version auto-check

The integration automatically checks whether the project version defined in the YAML matches the version installed on the device. If the YAML contains a higher project version, a Force Install is triggered automatically.

**This check runs in the following situations:**

| Trigger | Description |
|---------|-------------|
| **Device comes online** | When a device transitions from offline to online, its project version is checked after a 5 second delay |
| **HA restarts** | 30 seconds after Home Assistant has fully started, all currently online devices are checked |
| **External dashboard comes online** | When the external dashboard reconnects, all currently online devices are checked |

**How it works:**
1. The integration reads the `sw_version` from the HA device registry (e.g., `1.0.2 (ESPHome 2026.3.1)`)
2. It fetches the YAML config from the ESPHome dashboard and reads the `project.version` field
3. If the YAML version is higher than the installed version, a Force Install is queued
4. Multiple devices detected at the same time are grouped and started as a single batch (15 second debounce)
5. If the update queue is already running, the Force Install is retried every 60 seconds until the queue is finished

**Requirements for project version check:**
- The device YAML must contain a `project` block with a `version` field:

  ```yaml
  esphome:
    project:
      name: "mycompany.mydevice"
      version: "1.0.3"
  ```

- The device must have a `binary_sensor` status entity (see [Recommendations](#recommendations))
- The ESPHome dashboard (local or external) must be accessible

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
- An external dashboard comes online and devices have pending updates

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
- Timestamp of the update run and integration version used
- Summary with success/failed/skipped/cancelled counts
- Details per device including status, version (from → to), start time, finish time, and any error messages

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
| External dashboard unreachable | Immediate | Marked as failed, queue continues |
| Authentication failed | Immediate | Marked as failed, queue continues |

`A failed device never blocks the rest of the queue. Only an explicit cancel stops all remaining updates.`

### Failed update
- The device failed during a previous auto-update
- Auto-update will skip this device until you manually update it successfully
- Click the **Update** button to manually update the device or select multiple failed devices to update in sequence
- After a successful update, the red indication is removed and auto-update resumes for this device

## Examples Panel

### Update ready to install

<img width="700" height="460" alt="update1" src="https://github.com/user-attachments/assets/0b44677e-5030-4424-8bca-ff5f369c8b76" />

### Update in progress

<img width="700" height="460" alt="update2" src="https://github.com/user-attachments/assets/fd477355-7e30-43c3-bebd-8205e7cf50e8" />

### Update successful

<img width="700" height="460" alt="update3" src="https://github.com/user-attachments/assets/74513810-cfb9-4810-abb6-08f32e90a7c6" />

## Troubleshooting

### Switching devices between local and external dashboard
To move a device from one dashboard to another, follow these steps:

**From local to external:**
1. Remove the YAML file from the local ESPHome add-on
2. Add the YAML file to the external ESPHome dashboard
3. Remove the device from the ESPHome integration in Home Assistant
4. Restart HA
5. Re-add the device to the ESPHome integration
6. The device will now be managed by the external dashboard

**From external to local:**
1. Remove the YAML file from the external ESPHome dashboard
2. Add the YAML file to the local ESPHome add-on
3. Remove the device from the ESPHome integration in Home Assistant
4. Restart HA
5. Re-add the device to the ESPHome integration
6. The device will now be managed by the local ESPHome add-on

> **Note:** Removing and re-adding the device ensures that firmware entities are properly recreated and the integration correctly detects the dashboard source.

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

### Project version auto-check does not trigger
- Ensure the device YAML contains a `project` block with a `version` field
- Ensure the device has a `binary_sensor.status` entity (see [Recommendations](#recommendations))
- Check that the ESPHome dashboard is accessible
- Check Home Assistant logs for `esphome_update_manager` entries

### Force Install fails
- Verify the ESPHome dashboard is accessible and the device YAML exists
- Check the ESPHome dashboard logs for compile or upload errors
- Ensure the device is online and reachable via OTA
- Check Home Assistant logs for `esphome_update_manager` entries

### External dashboard not connecting
- Verify the URL is correct and accessible from Home Assistant (e.g., `http://192.168.1.100:6052`)
- Check that the dashboard is running
- If authentication is enabled on the dashboard, ensure you have entered the correct username and password
- Check Home Assistant logs for connection errors
- The dashboard status updates every ~1 minute

### Authentication failed
- Verify your username and password are correct
- Check that basic authentication is enabled on your external dashboard
- The integration uses HTTP Basic Authentication — other authentication methods are not supported
- Check Home Assistant logs for `401` errors

### External dashboard devices show "Unavailable"
- The external dashboard may be offline — check the dashboard status
- Authentication may have failed — verify your credentials
- Devices will automatically become available when the dashboard reconnects

### Local dashboard devices show "Unavailable"
- Local dashboard devices depend on the firmware entity created by HA (update.<your_device>_firmware)
- A yaml per device must exist in the ESPHome builder app
- This yaml must contain the OTA component
- The device must be added to the ESPHome integration
- When there still is no update entity, backup your yaml and delete it from the builder dashboard, delete the device from the esphome integration, restart HA and add yaml and device again.

### Notification link does not open the log
- Clear your browser cache and reload the panel
- Ensure the panel is accessible at `/esphome-update-manager`

### Device shows "Skipped" but I want to update it
- To clear the skipped update go to Settings → System → Updates → ⋮ menu

## Uninstallation

To completely remove the integration, follow these steps in order:

1. Go to **Settings** → **Devices & Services** → **ESPHome Update Manager** → **Delete**
2. Go to **HACS** → **ESPHome Update Manager** → **Remove**
3. Restart Home Assistant

⚠️ **Important:** Always remove the integration from Settings first, before removing it from HACS. This ensures all files and settings are properly cleaned up.

## License

MIT License — see [LICENSE](LICENSE) for details.
