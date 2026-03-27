DOMAIN = "esphome_update_manager"

# Timeout per device (seconds) — OTA can take a while
DEFAULT_UPDATE_TIMEOUT = 600

# Delay between updates (seconds)
DEFAULT_DELAY_BETWEEN = 1

# Device status
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

# Default max log updates to store
DEFAULT_MAX_LOG_BACKUPS = 5

# Dashboard configuration
CONF_DASHBOARD_URL = "dashboard_url"
CONF_DASHBOARD_MODE = "dashboard_mode"
DASHBOARD_MODE_LOCAL = "local"
DASHBOARD_MODE_EXTERNAL = "external"
CONF_DASHBOARD_USERNAME = "dashboard_username"
CONF_DASHBOARD_PASSWORD = "dashboard_password"
