# <img width="50" height="50" align="absmiddle" alt="logo" src="https://github.com/user-attachments/assets/402670fd-f94c-4b9e-a895-66d8e41a5c6e" /> ESPHome Update Manager

[![Latest Release](https://img.shields.io/github/v/release/KriVaTri/esphome-update-manager?include_prereleases&label=latest%20release)](https://github.com/KriVaTri/ESPHome-Update-Manager/releases)

ESPHome device update manager for Home Assistant.

A custom Home Assistant integration that provides a dedicated panel and lovelace card for managing ESPHome firmware updates across all your ESPHome devices.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Recommendations](#recommendations)
- [Installation](#installation)
- [Configuration](#configuration)
  - [External Dashboard Setup](#external-dashboard-setup)
- [Dashboard Card](#dashboard-card)
- [Usage](#usage)
  - [Toolbar](#toolbar)
  - [Device list](#device-list)
  - [Device button](#device-button)
  - [Batch updates](#batch-updates)
  - [Force Install](#force-install)
    - [Via frontend](#via-frontend)
    - [Pending force install for offline devices](#pending-force-install-for-offline-devices)
    - [Via service](#via-service-esphome-yaml-force-install)
    - [Project version auto-install](#project-version-auto-install)
  - [Exclude devices from auto-install](#exclude-devices-from-auto-install)
  - [Auto-install](#auto-install)
  - [Service: Start Updates](#service-start-updates)
  - [Service: Refresh project versions](#service-refresh-project-versions)
  - [Granular build services](#granular-build-services)
  - [VS Code Server add-on](#vs-code-server-add-on)
  - [Results](#results)
  - [Update log](#update-log)
  - [Failure notifications](#failure-notifications)
- [Deep sleep devices](#deep-sleep-devices)
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
- **Force Install** — Recompile and upload via the ESPHome dashboard, even when no update is available. Also used for project version updates
- **Pending Force Install for offline devices** — Queue offline (e.g. deep sleep) devices for force install; they are automatically installed as soon as they come online
- **Auto-install** — automatically start updates when new firmware or a new project version becomes available
- **Manual refresh** — refresh button and service to re-check project versions, dashboards, and pending devices on demand or from automations
- **Enable firmware entities** — Disabled firmware update entities can be enabled directly from the panel
- **Exclude devices from auto-install** — Mark devices to be skipped by auto-install while still allowing manual force install
- **Skipped update detection** — Devices with updates skipped via Home Assistant are clearly marked
- **Smart error handling** — Compile errors, OTA failures, and offline devices are detected and reported immediately
- **Failure notifications** — Persistent notifications alert you when updates fail, with a link to the update log
- **Update log** — Detailed log of all update results, viewable directly in the panel
- **Log history** — Access previous update logs via the 3-dots menu (configurable backup count)
- **VS Code Server add-on management** — Optionally stop the VS Code Server add-on during updates to free memory, and automatically restart it when updates are complete
- **Real-time status** — Live progress tracking with online/offline indicators for each device
- **Resilient queue** — If a device fails, the queue continues with the next device
- **Cancel anytime** — Cancel running updates at any time; remaining devices are marked as cancelled
- **Granular build services** — Trigger individual `clean_build_files`, `compile`, or `upload` operations via service calls for use in scripts and automations

## Requirements

- Home Assistant 2024.1 or newer
- ESPHome integration configured with your devices
- ESPHome Device Builder (Dashboard) app (add-on) installed and populated with your devices, **or** an external ESPHome dashboard accessible via HTTP. (ESPHome 2021.8.0 or newer)

## Recommendations

- Although not required, adding the following to the device's YAML gives **fast and reliable online status detection**:
  
  ```yaml
  binary_sensor:
    - platform: status
      name: "Status"
  ```

  This sensor reports the device's online state instantly to Home Assistant.

> **Note on online status detection:**
>
> The integration uses a fallback chain to determine each device's online status:
>
> 1. **`status` binary_sensor** *(recommended)* — instant and reliable detection of online/offline transitions.
> 2. **ESPHome integration fallback** — if no status sensor is configured, the integration falls back to the ESPHome native API connection state, but is **significantly slower and less reliable**: it can take up to several minutes before a disconnected device is detected as offline, due to the keepalive timeout of the ESPHome API.
> 3. **Unknown** 🟡 — shown when neither source can determine the status.
>
> For best results, always add the `status` binary_sensor to your devices.

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

## Dashboard Card

ESPHome Update Manager can also be used as a Lovelace dashboard card:

```yaml
type: custom:esphome-update-card
```

### Card Configuration Options

All options are optional — the card works out of the box without any configuration.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `title` | string | *none* | Display a custom title at the top of the card |
| `show_header` | boolean | `false` | Show the full panel header with logo inside the card |
| `compact` | boolean | `false` | Use smaller fonts and padding for a more compact layout |
| `hide_addon_option` | boolean | `false` | Hide the add-on stop/start option |
| `hide_auto_update` | boolean | `false` | Hide the automatic update toggle |
| `hide_results` | boolean | `false` | Hide the update results section |
| `max_width` | string | *none* | Maximum width of the card (e.g. `900px`) |
| `max_height` | string | *none* | Maximum height of the card — content will scroll (e.g. `600px`) |
| `align` | string | `left` | Card alignment: `left`, `center`, or `right` |

### Example

```yaml
type: custom:esphome-update-card
title: "ESP Updates"
show_header: false
compact: true
hide_addon_option: true
hide_auto_update: true
hide_results: true
max_width: 900px
max_height: 600px
align: center
```

> **Note:**
> The card shares all functionality with the panel — updates, force install, cancel, and log viewing all work the same way.
> The log menu (⋮) is accessible via the toolbar inside the card when the header is hidden.

## Usage

### Toolbar

The toolbar above the device list contains four mode buttons and a log shortcut:

| Button | Color | Description |
|--------|-------|-------------|
| **UPD** | Blue | Enter Firmware Update mode — select devices to update |
| **FRC** | Green | Enter Force Install mode — select devices to recompile and reinstall |
| **EXC** | Purple | Enter Exclude mode — select devices to exclude from auto-update |
| **LOG** | Teal | Open the latest update log |

Once a mode is active, the toolbar switches to a confirm/cancel view:

| Button | State | Description |
|--------|-------|-------------|
| **▶ Firmware Update (n)** (blue) | Update mode | Starts the update queue for the selected devices. Click with 0 selected to exit the mode. |
| **▶ Force Install (n)** (green) | Force Install mode | Starts force install for the selected devices, or saves the pending list when only offline devices are selected. Click with 0 selected to exit the mode. |
| **▶ Save Excluded (n)** (purple) | Exclude mode | Saves the current selection as excluded devices. Saving with 0 selected clears the exclude list. |
| **✕** | Any mode active | Exits the current mode without applying changes |
| **⏹ Cancel** (red) | Updates running | Cancels the running update queue |

Only one mode can be active at a time. The **UPD** button is disabled when no updates are available.

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
| **Update** (blue) | Firmware update ready to install — click to start |
| **Install** (blue) | Project version bump ready to install — click to start |
| **Up to date** (green) | Device is on the latest firmware |
| **Excluded** (purple) | Device is excluded from auto-update and has an update available — manage via the **EXC** button |
| **Skipped** (purple) | Update was skipped via Home Assistant — clear skip in HA to update |
| **Enable** (orange) | Firmware entity is disabled — click to enable |
| **Enabling…** (orange + spinner) | Entity is being enabled, waiting for HA to pick it up |
| **Updating…** (blue + spinner) | Update is in progress |
| **Installing…** (blue + spinner) | Force install is in progress |
| **Compiling…** (blue + spinner) | Compiling only is in progress |
| **Uploading…** (blue + spinner) | Uploading only is in progress |
| **Pending** (grey) | Device is queued for force install — will start automatically when device comes online |
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
- You want to update multiple devices in one batch — Force Install compiles and uploads all selected devices sequentially, without needing a firmware version bump.
- You want to install a modified yaml to an offline device when it comes online

#### Via frontend

1. Click **Force Install** in the toolbar — the panel switches to Force Install mode and **all** devices become selectable, including offline and unavailable ones
2. Select one or more devices using the checkboxes (or click the **Select all** checkbox in the toolbar)
3. Click **▶ Force Install (n)** to start
4. **Online devices** are recompiled and uploaded immediately, sequentially
5. **Offline devices** are added to the **pending** list and will be force installed automatically as soon as they come online (see [Pending force install for offline devices](#pending-force-install-for-offline-devices))
6. Progress and results are shown in real-time, just like a regular update
7. Click **✕** to exit Force Install mode without starting

> **Note:** While in Force Install mode, the **Firmware Update** button is disabled (and vice versa) — you can only be in one mode at a time.
> 
> Force Install always recompiles the firmware, even if nothing has changed.

#### Pending force install for offline devices

Devices that are offline (e.g., deep sleep devices) when you trigger a Force Install are not skipped — they are added to a persistent **pending list**. The integration watches for these devices to come online and automatically starts the force install at that moment.

**How it works:**

1. Select one or more offline devices in Force Install mode (or pass them to the `esphome_update_manager.force_install` service) and confirm (Click **▶ Force Install (n)**)
2. Each offline device is shown with the **Pending** label in the panel. To remove a device from the pending list, enter **FRC** mode again — pending devices appear pre-selected — uncheck them and click **▶ Force Install** to confirm.
3. When a pending device comes online (status sensor goes `on` or ESPHome native API reconnects), the integration waits a short debounce window of ~15 seconds so multiple devices waking up at the same time can be batched
4. Once ready, the force install starts automatically for all online pending devices in a single batch
5. After the force install completes, the device is removed from the pending list — regardless of success or failure

**Properties:**
- The pending list is persisted across Home Assistant restarts
- A pending device that is removed from Home Assistant is automatically cleaned up
- If a normal update queue is already running when a pending device comes online, the integration retries every 60 seconds until the queue is free
- Pending state is independent from auto-update — pending devices are always force installed, even when auto-update is disabled

> **Tip:** Combine pending force install with the [Deep sleep devices](#deep-sleep-devices) automation to fully automate firmware updates for battery-powered devices.

#### Via service: ESPHome Yaml Force Install

Force Install can also be triggered via a Home Assistant service call, useful for automations:

**Service:** `esphome_update_manager.force_install`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `device_id` | Yes | One or more Home Assistant device IDs to force install. Can be a single string or a list. |

**Behavior:**
- **Online devices** are force installed immediately
- **Offline devices** are added to the pending list and will be installed automatically when they come online
- If the queue is already running, all selected devices are added to the pending list and will be installed when the queue is free

> **Note:** The force_install service respects the "Stop VS Code Server" setting — if enabled, the VS Code Server add-on will be stopped before the update and restarted afterwards.

**Example automation — force install a specific device:**

```yaml
actions:
  - action: esphome_update_manager.force_install
    data:
      device_id: "abc123def456"
```

**Example automation — force install multiple devices (mix of online + offline allowed):**

```yaml
actions:
  - action: esphome_update_manager.force_install
    data:
      device_id:
        - "abc123def456"
        - "789xyz000111"
```

> **Note:** The `device_id` is the Home Assistant device ID, not the entity ID. You can find the device ID in **Settings → Devices & Services → ESPHome → [your device] → ⋮ → Device info → ID**.

#### Project version auto-install

The integration automatically checks whether the project version defined in the YAML matches the version installed on the device. When a higher project version is found in the YAML, the device is shown in the panel with the new version and — depending on your settings — automatically force installed.

**User control via scope dropdown:**

In the Auto install bar you can choose **what** the integration should install automatically:

| Scope | Behavior |
|-------|----------|
| **Firmware only** | Only ESPHome firmware updates are installed automatically. Project bumps are still detected and shown in the panel, but require a manual click on **Install**. |
| **Project only** | Only project version bumps from the YAML are installed automatically. Firmware updates are still detected and shown, but require a manual click on **Update**. |
| **Firmware + Project** | Both firmware updates and project bumps are installed automatically. |

> **Note:** Detection of project bumps always runs, regardless of the scope. The scope only controls whether the integration **acts** on it automatically. Devices excluded via **EXC** are never installed automatically, but their pending bump is still visible.

**This check runs in the following situations:**

| Trigger | Description |
|---------|-------------|
| **Device comes online** | When a device transitions from offline to online, its project version is checked after a short delay |
| **HA restarts** | A short while after Home Assistant has fully started, all currently online devices are checked |
| **External dashboard comes online** | When the external dashboard reconnects, all online devices are re-checked |
| **Auto install settings change** | When you toggle auto install or change the scope, a full re-check runs |
| **Refresh button** | Click the ↻ refresh button next to the scope dropdown to re-check now |
| **Refresh service** | Call `esphome_update_manager.refresh_project_versions` from an automation |

**How it works:**
1. The integration reads the `sw_version` from the HA device registry (e.g., `1.0.2 (ESPHome 2026.3.1)`)
2. It fetches the YAML config from the ESPHome dashboard and reads the `project.version` field
3. If the YAML version is higher than the installed version, the bump is cached and shown in the panel
4. If auto install is enabled with a scope that includes project bumps, a Force Install is queued automatically
5. Multiple devices detected at the same time are grouped and started as a single batch (~15 second debounce)
6. If the update queue is already running, the Force Install is retried until the queue is free

**Online vs offline devices:**
- **Online devices** with a project bump are installed automatically (if scope allows it) or can be installed manually via the **Install** button
- **Offline devices** with a project bump are shown in the panel with their new version visible — they are not auto-installed until they come back online

**Requirements for project version check:**
- The device YAML must contain a `project` block with a `version` field:

  ```yaml
  esphome:
    project:
      name: "mycompany.mydevice"
      version: "1.0.3"
  ```

- The ESPHome dashboard (local or external) must be accessible

### Exclude devices from auto-install

Sometimes you want certain devices to **not** be installed automatically — for example a critical device that you only want to update manually after testing, or a device with an unstable firmware/project version where you want to wait.

**How it works:**

1. Click **EXC** in the toolbar — the panel switches to Exclude mode and every device becomes selectable
2. Devices that are already excluded appear **pre-selected**
3. Check the devices you want to exclude (or uncheck devices you no longer want to exclude)
4. Click **▶ Save Excluded (n)** to apply

**Effects of excluding a device:**
- The device is shown with a purple **Excluded** button instead of the usual **Update** or **Install** button when something is available
- The device is **not** selectable in Firmware Update mode (UPD)
- **Auto install** skips the device for both firmware updates **and** project version bumps, even when the scope includes them
- The pending project bump is still detected and visible in the panel (with the new version shown), so you can decide to install it manually
- **Force Install (FRC)** still works — exclude only applies to automatic installs, not to manual or service-triggered force installs

**Up-to-date excluded devices** show the regular **Up to date** button — the **Excluded** indication is only visible when an update or project bump is available, since that is when the exclusion actually has an effect.

When you remove devices from the exclude list and confirm with **▶ Save Excluded**, the integration immediately re-evaluates auto install for the newly un-excluded devices — both firmware updates and project bumps are queued if applicable (and the scope allows it).

### Auto install

The Auto install bar contains:

> ☑️ **Auto install trigger:** [Firmware only ▾] ↻  ● Enabled / ● Disabled

- **Checkbox** — enables or disables automatic installation
- **Scope dropdown** — controls what is installed automatically: `Firmware only`, `Project only`, or `Firmware + Project` (see [Project version auto-install](#project-version-auto-install))
- **Refresh button (↻)** — immediately re-checks project versions, dashboards, and pending force installs without waiting for the next poll cycle

When enabled, the integration monitors all ESPHome device update entities and project version bumps according to the selected scope. Installs start automatically on:

- A device that comes online with a pending update or project bump
- ESPHome being updated (new firmware available)
- Home Assistant restarting with pending updates
- An external dashboard coming online with pending updates
- Settings changes (enabling auto install or changing scope re-evaluates all devices)

The "Stop VS Code Server" setting is respected for every auto install.

> **Note:** Excluded devices (see [Exclude devices from auto-install](#exclude-devices-from-auto-update)) are always skipped by auto install, even when they have an update available.

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

### Service: Refresh project versions

Manually re-checks all ESPHome devices for project version bumps and refreshes the dashboard state. Useful for periodic automations.

**Service:** `esphome_update_manager.refresh_project_versions`

No parameters.

**Behavior:**
- Refreshes local and external dashboard data
- Re-checks all ESPHome devices for project version bumps
- Re-evaluates the pending force install list
- Kicks off auto install for any newly detected updates (if auto install is enabled)

**Example automation — re-check project versions every hour:**

```yaml
alias: ESPHome refresh project versions
triggers:
  - trigger: time_pattern
    hours: "/1"
actions:
  - action: esphome_update_manager.refresh_project_versions
mode: single
```

### Granular build services

In addition to `start_updates` and `force_install`, the integration provides three services that expose individual steps of the ESPHome build pipeline. Useful for scripts, automations, and advanced workflows.

**Services:**

| Service | Description |
|---------|-------------|
| `esphome_update_manager.clean_build_files` | Clean build files (equivalent to *"Clean Build Files"* in the ESPHome dashboard) |
| `esphome_update_manager.compile` | Compile firmware **without** uploading (equivalent to *"Install → Manual download → Cancel"* in the ESPHome dashboard) |
| `esphome_update_manager.upload` | OTA upload **pre-compiled** firmware, without compiling first |

**Parameters (all three):**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `device_id` | No | One or more Home Assistant device IDs. Single string or list. **Leave empty to target all ESPHome devices.** |

**Behavior:**
- All three work with both **local** and **external** dashboards (the integration auto-selects per device, just like Force Install)
- Operations run sequentially via the same update queue used for firmware updates
- Progress is visible in the panel and update log (with operation-specific labels)
- For `compile` and `upload`, the "Stop VS Code Server" setting is respected
- For `clean`, the add-on is not stopped (cleaning is fast)

**Typical use cases:**

| Use case | Workflow |
|----------|----------|
| Pre-compile before a sleep window | `compile` ahead of time, then `upload` the moment the device wakes |
| Recover from a corrupted build | `clean_build_files` followed by `compile` |
| Batch OTA during a maintenance window | `compile` all devices overnight, then `upload` later |

**Example automation — pre-compile before a sleeping sensor wakes up:**

```yaml
alias: Pre-compile sleeping sensor
triggers:
  - trigger: time
    at: "06:55:00"
actions:
  - action: esphome_update_manager.compile
    data:
      device_id: "abc123def456"
  # Device wakes around 07:00 — OTA upload is now instant
```

**Example automation — clean all devices weekly:**

```yaml
alias: Weekly clean of all ESPHome build files
triggers:
  - trigger: time
    at: "04:00:00"
conditions:
  - condition: time
    weekday:
      - sun
actions:
  - action: esphome_update_manager.clean_build_files
    # No device_id → cleans all ESPHome devices
```

**Example script — clean + compile + upload chain for one device:**

```yaml
sequence:
  - action: esphome_update_manager.clean_build_files
    data:
      device_id: "abc123def456"
  - wait_for_trigger:
      - trigger: event
        event_type: esphome_update_manager_finished
        event_data:
          operation: clean
    timeout: "00:05:00"
  - action: esphome_update_manager.compile
    data:
      device_id: "abc123def456"
  - wait_for_trigger:
      - trigger: event
        event_type: esphome_update_manager_finished
        event_data:
          operation: compile
    timeout: "00:30:00"
  - action: esphome_update_manager.upload
    data:
      device_id: "abc123def456"
  - wait_for_trigger:
      - trigger: event
        event_type: esphome_update_manager_finished
        event_data:
          operation: upload
    timeout: "00:10:00"
```

> **Note:** Only one operation can run at a time — if the update queue is already busy when you call one of these services, the call fails with an error. Wait for the current operation to finish, or chain them sequentially as shown above.

### VS Code Server add-on

If the **VS Code Server** (Studio Code Server) add-on is installed, a checkbox appears:

> ☑️ Stop **Studio Code Server** during jobs — ● Running / ● Stopped

- When checked, the add-on is automatically stopped before updates/force installs begin and restarted after all updates/force installs complete
- The add-on is always restarted, even if updates are cancelled or fail
- The current status (Running/Stopped) is polled every 30 seconds

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
- Timestamp of the job run and integration version used
- Job type (Firmware Update, Force Install, Clean Build Files, Compile Only, or OTA Upload Only)
- Summary with success/failed/skipped/cancelled counts
- Details per device including status, version (from → to), start time, finish time, and any error messages

Logs are stored at:
- Current log: `config/esphome-update-manager/update_log.txt`
- Backups: `config/esphome-update-manager/log-backups/`

A new backup is created automatically after each update batch completes. The number of backups kept can be configured in the integration settings (set to 0 to disable backups).

### Failure notifications

When one or more updates fail, a persistent notification is created in Home Assistant:

> **ESPHome Update Failed**  
> Update for X ESPHome device(s) has failed.  
> *View update log* (clickable link)

Clicking the link opens the panel and automatically displays the latest update log.

<img width="500" height="389" alt="log" src="https://github.com/user-attachments/assets/c3f5c8c2-d1f0-4ed4-8224-71bf30eaf17f" />

## Deep sleep devices

By combining the integration with a small Home Assistant automation, firmware updates and force installs can be fully automated for these devices: the next time they wake up they will be flashed and put back to sleep automatically, respecting the awake window configured in the automation.

### Requirements per device

Each deep sleep device must expose:

1. **A status binary sensor** (used by the automation to detect when the device is online):

   ```yaml
   binary_sensor:
     - platform: status
       name: "Status"
   ```

2. **A deep sleep button** the automation can press. The entity_id must contain `deep_sleep`, `deepsleep`, or have `sleep` in its friendly name. The deep sleep block must **not** define a `run_duration`, so the device stays awake until the button is pressed by the automation (the awake window must be configured in the automation instead):

   ```yaml
   deep_sleep:
     id: deep_sleep_control
     sleep_duration: 60min

   button:
     - platform: template
       name: "Enter Deep Sleep"
       icon: mdi:sleep
       on_press:
         - deep_sleep.enter:
             id: deep_sleep_control
   ```

> **Note:** Without a status binary sensor, the automation cannot reliably detect when the device is back online after the OTA reboot, and the deep sleep button will not be pressed.

### Automation

This single automation handles **all** deep sleep devices simultaneously.

```yaml
alias: Deep sleep after wake-up or update
description: >
  Handles ALL deep sleep devices. On every wake-up (normal wake AND the
  reboot after an install), the device gets its full awake_window_seconds
  before being put back to sleep — unless an update is pending/running,
  in which case it stays awake until that update finishes (then still
  gets its awake_window_seconds before sleeping).
mode: parallel
max: 10   # bump this if more than 10 deep sleep devices could wake up at once
max_exceeded: silent
variables:
  awake_window_seconds: 120   # <- your normal "awake to do tasks" window,
                               # applied both on a normal wake-up and after
                               # an install + reboot
triggers:
  - id: wake
    trigger: state
    entity_id:
      - binary_sensor.device1_status # change to your device status sensor
      - binary_sensor.device2_status # change to your device status sensor
      # add one line per deep sleep device's status sensor here
    to: "on"
  - id: finished
    trigger: event
    event_type: esphome_update_manager_finished
actions:
  - choose:
      # ── Branch 1: device just woke up ──────────────────────────────
      - conditions:
          - condition: trigger
            id: wake
        sequence:
          - variables:
              wake_last_changed: "{{ trigger.to_state.last_changed }}"
          - delay: "{{ awake_window_seconds }}"
          - condition: template
            value_template: >
              {{ states(trigger.entity_id) is not none
                 and state_attr(trigger.entity_id, 'friendly_name') is not none
                 and (states[trigger.entity_id].last_changed | string) == (wake_last_changed | string) }}
            # ^ if this is false, the sensor flipped again during our wait
            #   (a reboot happened) — a newer run or the finished-tak is
            #   already handling this device, so we stop here silently
          - action: esphome_update_manager.get_device_status
            data:
              device_id: "{{ device_id(trigger.entity_id) }}"
            response_variable: dev_status
          - if:
              - condition: template
                value_template: >
                  {{ not dev_status.devices.get(device_id(trigger.entity_id), {}).get('active', false) }}
            then:
              - variables:
                  deep_sleep_btn: >
                    {% set ns = namespace(found=none) %}
                    {% for e in states.button
                       if device_id(e.entity_id) == device_id(trigger.entity_id)
                       and ('deep_sleep' in e.entity_id
                            or 'deepsleep' in e.entity_id
                            or 'sleep' in (e.attributes.friendly_name | lower)) %}
                      {% set ns.found = e.entity_id %}
                    {% endfor %}
                    {{ ns.found }}
              - if:
                  - condition: template
                    value_template: "{{ deep_sleep_btn != none }}"
                then:
                  - target:
                      entity_id: "{{ deep_sleep_btn }}"
                    action: button.press

      # ── Branch 2: an update batch just finished (force install OR
      #    regular auto-update / manual update) ───────────────────────
      - conditions:
          - condition: trigger
            id: finished
        sequence:
          - variables:
              finished_devices: |
                {{ trigger.event.data.results
                   | selectattr('device_id', 'defined')
                   | list }}
          - if:
              - condition: template
                value_template: "{{ finished_devices | length > 0 }}"
            then:
              - repeat:
                  for_each: "{{ finished_devices }}"
                  sequence:
                    # Wait for the device to come back online after reboot
                    - wait_template: >
                        {% set ns = namespace(online=false) %}
                        {% for e in states.binary_sensor
                           if e.entity_id.endswith('_status')
                           and device_id(e.entity_id) == repeat.item.device_id %}
                          {% if e.state == 'on' %}{% set ns.online = true %}{% endif %}
                        {% endfor %}
                        {{ ns.online }}
                      timeout: "00:03:00"
                      continue_on_timeout: true
                    # Give it its normal awake window (device does its
                    # tasks now, on this reboot's wake cycle)
                    - delay: "{{ awake_window_seconds }}"
                    # Final safety check before sleeping — in case
                    # something new got queued for it in the meantime
                    - action: esphome_update_manager.get_device_status
                      data:
                        device_id: "{{ repeat.item.device_id }}"
                      response_variable: dev_status2
                    - if:
                        - condition: template
                          value_template: >
                            {{ not dev_status2.devices.get(repeat.item.device_id, {}).get('active', false) }}
                      then:
                        - variables:
                            deep_sleep_btn: >
                              {% set ns = namespace(found=none) %}
                              {% for e in states.button
                                 if device_id(e.entity_id) == repeat.item.device_id
                                 and ('deep_sleep' in e.entity_id
                                      or 'deepsleep' in e.entity_id
                                      or 'sleep' in (e.attributes.friendly_name | lower)) %}
                                {% set ns.found = e.entity_id %}
                              {% endfor %}
                              {{ ns.found }}
                        - if:
                            - condition: template
                              value_template: "{{ deep_sleep_btn != none }}"
                            then:
                              - target:
                                  entity_id: "{{ deep_sleep_btn }}"
                                action: button.press
```

**Operation of deep sleep devices in combination with the automation:**

- If the device wakes and there is no update or force-install pending → enter deep sleep after the time configured in the automation.
- If the device wakes and there is a firmware update, but the device is excluded → enter deep sleep after the time configured in the automation.
- If the device wakes and there is a firmware update and the device is not excluded → the update is performed; after reboot, return to deep sleep after the time configured in the automation.
- If the device wakes and a force-install is pending → the install is performed; after reboot, return to deep sleep after the time configured in the automation.
- When the update or install fails -> return to deep sleep after the time configured in the automation.

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

<img width="500" height="552" alt="update-1" src="https://github.com/user-attachments/assets/302d8e7b-598d-4f40-b8ec-5a583b22b8cf" />

### Update in progress

<img width="500" height="552" alt="update-2" src="https://github.com/user-attachments/assets/676f1fb0-42f7-48d6-aab5-f24f37e7acf1" />

### Update successful

<img width="500" height="552" alt="update-5" src="https://github.com/user-attachments/assets/920a6c20-1fb1-4ab4-a559-789be46034bf" />

## Troubleshooting

## Migration: switching devices between local and external dashboard

**Behavior:**

| Scenario | Result |
|----------|--------|
| No external URL configured | Always local |
| External URL configured | External has priority |
| YAML on both dashboards + URL configured | External wins |
| No YAML anywhere | Unavailable |

**How to switch:**
1. Add or remove the YAML from the desired dashboard
2. Remove the device and re-add the device entry in the ESPHome integration (it is recommended to restart HA before re-adding the device)

> **Note:** After adding a YAML to the local dashboard, it may take a moment before HA creates the update entity.
>
> **Tip:** If the above steps do not give the expected result, try reloading the ESPHome integration or restarting HA.

### Panel does not appear in sidebar
- Make sure the integration is added via Settings → Devices & Services
- Restart Home Assistant and clear your browser cache

### VS Code Server checkbox does not appear
- The add-on must be installed (it does not need to be running)
- Check Home Assistant logs for Supervisor API errors

### Updates fail with memory errors
- Enable the "Stop VS Code Server during updates" option
- Consider stopping other memory-heavy add-ons manually

### Auto-install does not trigger
- Ensure the "Auto install trigger" checkbox is enabled
- Auto-install only triggers on state transitions (e.g., device coming online), not when already in "update available" state
- Check Home Assistant logs for `esphome_update_manager` entries

### Project version auto-install does not trigger
- Ensure the device YAML contains a `project` block with a `version` field
- Check that the auto install scope includes project bumps (`Project only` or `Firmware + Project`)
- Check that the device is not excluded (see [Exclude devices from auto-install](#exclude-devices-from-auto-update))
- Check that the ESPHome dashboard is accessible
- Try clicking the ↻ refresh button next to the scope dropdown
- Check Home Assistant logs for `esphome_update_manager` entries

### Force Install fails
- Verify the ESPHome dashboard is accessible and the device YAML exists
- Check the ESPHome dashboard logs for compile or upload errors
- Ensure the device is online and reachable via OTA
- Check Home Assistant logs for `esphome_update_manager` entries

### Pending force install does not trigger when the device wakes up
- Verify the device is still in the pending list (visible as **Pending** in the panel)
- If the regular update queue is running, pending installs are retried every 60 seconds until the queue is free
- Check Home Assistant logs for `esphome_update_manager` entries

### Deep sleep automation does not put devices back to sleep
- Verify the device has a button entity whose entity_id contains `deep_sleep`, `deepsleep`, or whose friendly name contains `sleep`
- Verify the device's deep sleep block in YAML does **not** set `run_duration` (otherwise the device sleeps before the button is pressed)
- Check the automation traces (Settings → Automations → Deep sleep after force install → Traces) to see which path was taken
- Confirm the `esphome_update_manager_finished` event contains entries with `is_force_install: true` (Developer Tools → Events → listen)

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
