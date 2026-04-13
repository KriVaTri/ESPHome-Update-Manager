# ESPHome Update Manager - Test Matrix

## Version: after refactor `_build_version_info_for_devices`
## Date: 

---

## Test (local device)

| # | Test | Auto-update | Selected | Service | Result |
|---|------|:-----------:|:--------:|:-------:|--------|
| 1 | Update firmware, no project version bump | ⭐ | | ⭐ | |
| 2 | Update firmware with project version bump via update | ⭐ | ✅ | ⭐ | |
| 3 | Update firmware with project version bump via manual force install | | | | |
| 4 | Force install without changes | | | | |
| 5 | Update firmware from no project to project version | | | | |
| 6 | Update firmware from project to no project version | | | | |
| 7 | Update firmware without project version | ⭐ | | ⭐ | |
| 8 | Manual force install from project to no project version | | | | |
| 9 | Manual force install | | | | |
| 10 | Auto force install: device offline → online | | | | |
| 11 | Auto force install after HA restart | | | | |

---

## Test (external device)

| # | Test | Auto-update | Selected | Service | Result |
|---|------|:-----------:|:--------:|:-------:|--------|
| 1 | Update firmware, no project version bump | ⭐ | | ⭐ | |
| 2 | Update firmware with project version bump via update | ⭐ | ✅ | ⭐ | |
| 3 | Update firmware with project version bump via manual force install | | | | |
| 4 | Force install without changes | | | | |
| 5 | Update firmware from no project to project version | | | | |
| 6 | Update firmware from project to no project version | | | | |
| 7 | Update firmware without project version | ⭐ | | ⭐ | |
| 8 | Manual force install from project to no project version | | | | |
| 9 | Manual force install | | | | |
| 10 | Auto force install: device offline → online | | | | |
| 11 | Auto force install: dashboard offline → online | | | | |
| 12 | Auto force install after HA restart | | | | |

---

## Test mixed setup - multiple devices forced install

| Scenario | Result |
|----------|--------|
| Success | |
| Canceled | |
| Failed | |

---

## Test mixed setup - multiple devices firmware update

| Scenario | Result |
|----------|--------|
| Success | |
| Canceled | |
| Failed | |

---

## Test project version downgrade

| # | Test | Result |
|---|------|--------|
| 1 | Project version downgrade local | |
| 2 | Project version downgrade external | |

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ⭐ | Critical for current refactor — must be tested |
| ✅ | Already tested and passed |
| ✔️ | Tested and passed |
| ❌ | Tested and failed |
| ⏭️ | Skipped |
| 🔄 | In progress |