import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv  # <-- NEW: import for .env support

from apps.oms.shared.exceptions import ConfigurationException
from apps.oms.shared.logger import get_logger


log = get_logger("oms.runner")
# Load .env file from the current working directory (or parent, etc.)
# This looks for a .env file in the same folder as the running script,
# or you can specify an absolute path: load_dotenv("/path/to/.env")
# load_dotenv()  # <-- NEW: loads variables into os.environ

env_file = Path(__file__).parent.parent.parent / '.env'
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
    log.warning(f"Found .env file at {env_file}, using system environment variables")

else:
    log.warning(f"No .env file found at {env_file}, using system environment variables")



# Module-level singleton — settings loaded once, reused everywhere
_settings_instance: Optional["OMSSettings"] = None

# ... (all the dataclass definitions remain exactly as before) ...

@dataclass
class WhatsAppSettings:
    group_name:       str   = ""
    staff_number:     str   = ""
    monitor_number:   str   = ""
    poll_interval:    int   = 15
    message_lookback: int   = 20

@dataclass
class BrowserSettings:
    session_dir: str = ".sessions/oms_session"
    headless:    bool = False
    viewport_w:  int  = 1280
    viewport_h:  int  = 800
    timezone:    str  = "Africa/Lagos"
    locale:      str  = "en-US"

@dataclass
class StorageSettings:
    database_url: str = "sqlite:///data/oms.db"
    orders_file:  str = "data/orders_export.xlsx"

@dataclass
class GoogleSettings:
    '''
    Google Sheets sync configuration — credential-file-free.

    Every field a service account JSON key normally holds is instead
    its own env var, so nothing resembling a downloadable credentials
    file ever needs to exist on disk or get committed to git. The
    JSON key is only ever touched once, locally, to read these values
    out of it — see scripts/json_to_env.py — then it can be deleted.

    private_key is the one tricky field: PEM keys are multi-line, and
    .env values are conventionally single-line. Store it with literal
    "\\n" sequences (not real newlines) inside the quoted value; it's
    unescaped back into real newlines in GoogleSheetsProvider.connect().
    '''
    enabled:              bool  = False

    # Fields lifted 1:1 from the service account JSON key
    project_id:           str   = ""
    private_key_id:       str   = ""
    private_key:          str   = ""   # literal "\n" sequences, not real newlines
    client_email:         str   = ""
    client_id:            str   = ""
    auth_uri:              str  = "https://accounts.google.com/o/oauth2/auth"
    token_uri:              str = "https://oauth2.googleapis.com/token"
    auth_provider_cert_url: str = "https://www.googleapis.com/oauth2/v1/certs"

    # Sheet targeting + sync tuning — unchanged from before
    spreadsheet_id:   str   = ""
    sheet_name:       str   = "Orders"
    retry_limit:      int   = 5
    retry_interval:   float = 5.0
    batch_size:       int   = 50
    queue_max_size:   int   = 500

    def is_configured(self) -> bool:
        '''True only if every field connect() actually needs is present.'''
        return bool(
            self.project_id and self.private_key_id and self.private_key
            and self.client_email and self.client_id and self.spreadsheet_id
        )

@dataclass
class RetrySettings:
    max_retries: int   = 3
    retry_delay: float = 5.0

@dataclass
class ObserverSettings:
    poll_interval_seconds:    float = 2.0
    recovery_max_age_hours:   int   = 24
    recovery_max_messages:    int   = 300
    recovery_max_scrolls:     int   = 20
    message_cache_size:       int   = 1000
    checkpoint_history_size:  int   = 5

@dataclass
class LoggingSettings:
    log_dir:   str = "logs"
    log_level: str = "INFO"

@dataclass
class OMSSettings:
    app_name:    str = "Windwhirl OMS"
    environment: str = "development"

    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)
    browser:  BrowserSettings  = field(default_factory=BrowserSettings)
    storage:  StorageSettings  = field(default_factory=StorageSettings)
    google:   GoogleSettings   = field(default_factory=GoogleSettings)
    retry:    RetrySettings    = field(default_factory=RetrySettings)
    observer: ObserverSettings = field(default_factory=ObserverSettings)
    logging:  LoggingSettings  = field(default_factory=LoggingSettings)

    def __post_init__(self):
        '''Load from environment variables (already populated by .env if used).'''
        self._load_from_env()
        log.debug(f"OMSSettings loaded: env={self.environment}")

    def _load_from_env(self):
        env_map = {
            "OMS_APP_NAME":                 ("app_name",                  str),
            "OMS_ENVIRONMENT":              ("environment",               str),
            "OMS_WHATSAPP_GROUP_NAME":      ("whatsapp.group_name",       str),
            "OMS_WHATSAPP_STAFF_NUMBER":    ("whatsapp.staff_number",     str),
            "OMS_WHATSAPP_MONITOR_NUMBER":  ("whatsapp.monitor_number",   str),
            "OMS_WHATSAPP_POLL_INTERVAL":   ("whatsapp.poll_interval",    int),
            "OMS_BROWSER_HEADLESS":         ("browser.headless",          bool),
            "OMS_BROWSER_SESSION_DIR":      ("browser.session_dir",       str),
            "OMS_BROWSER_TIMEZONE":         ("browser.timezone",          str),
            "OMS_STORAGE_DATABASE_URL":     ("storage.database_url",      str),
            "OMS_RETRY_MAX_RETRIES":        ("retry.max_retries",         int),
            "OMS_RETRY_DELAY":              ("retry.retry_delay",         float),
            "OMS_LOGGING_LOG_DIR":          ("logging.log_dir",           str),
            "OMS_LOGGING_LOG_LEVEL":        ("logging.log_level",         str),
            "OMS_OBSERVER_POLL_INTERVAL":      ("observer.poll_interval_seconds",   float),
            "OMS_OBSERVER_MAX_AGE_HOURS":      ("observer.recovery_max_age_hours",  int),
            "OMS_OBSERVER_MAX_MESSAGES":       ("observer.recovery_max_messages",   int),
            "OMS_OBSERVER_MAX_SCROLLS":        ("observer.recovery_max_scrolls",    int),
            "OMS_OBSERVER_CACHE_SIZE":         ("observer.message_cache_size",      int),
            "OMS_OBSERVER_CHECKPOINT_HISTORY": ("observer.checkpoint_history_size", int),

            # --- Google Sheets: credential fields, .env-only, no JSON file ---
            "OMS_GOOGLE_ENABLED":            ("google.enabled",                bool),
            "OMS_GOOGLE_PROJECT_ID":         ("google.project_id",             str),
            "OMS_GOOGLE_PRIVATE_KEY_ID":     ("google.private_key_id",         str),
            "OMS_GOOGLE_PRIVATE_KEY":        ("google.private_key",            str),
            "OMS_GOOGLE_CLIENT_EMAIL":       ("google.client_email",           str),
            "OMS_GOOGLE_CLIENT_ID":          ("google.client_id",              str),
            "OMS_GOOGLE_AUTH_URI":           ("google.auth_uri",               str),
            "OMS_GOOGLE_TOKEN_URI":          ("google.token_uri",              str),
            "OMS_GOOGLE_AUTH_PROVIDER_CERT_URL": ("google.auth_provider_cert_url", str),

            # --- Google Sheets: sheet targeting + sync tuning ---
            "OMS_GOOGLE_SPREADSHEET_ID":    ("google.spreadsheet_id",   str),
            "OMS_GOOGLE_SHEET_NAME":        ("google.sheet_name",       str),
            "OMS_GOOGLE_RETRY_LIMIT":       ("google.retry_limit",      int),
            "OMS_GOOGLE_RETRY_INTERVAL":    ("google.retry_interval",   float),
            "OMS_GOOGLE_BATCH_SIZE":        ("google.batch_size",       int),
            "OMS_GOOGLE_QUEUE_MAX_SIZE":    ("google.queue_max_size",   int),
        }

        for env_key, (attr_path, cast) in env_map.items():
            value = os.environ.get(env_key)
            if value is not None:
                parts = attr_path.split(".")
                if len(parts) == 1:
                    setattr(self, parts[0], cast(value))
                elif len(parts) == 2:
                    section = getattr(self, parts[0])
                    if cast == bool:
                        value = value.lower() in ("true", "1", "yes")
                    else:
                        value = cast(value)
                    setattr(section, parts[1], value)

    def validate(self):
        errors = []
        if not self.whatsapp.group_name:
            errors.append(
                "whatsapp.group_name is required. "
                "Set OMS_WHATSAPP_GROUP_NAME or edit settings.py."
            )
        if not self.whatsapp.staff_number:
            errors.append(
                "whatsapp.staff_number is required. "
                "Set OMS_WHATSAPP_STAFF_NUMBER or edit settings.py."
            )
        if self.whatsapp.staff_number:
            num = self.whatsapp.staff_number.replace("+", "").replace(" ", "")
            if not num.startswith("234") or len(num) != 13:
                errors.append(
                    f"whatsapp.staff_number must be 13 digits starting with 234. "
                    f"Got: {self.whatsapp.staff_number!r}"
                )

        # Only validate Google credentials if sync is actually turned on —
        # an app with sync disabled shouldn't be forced to configure it.
        if self.google.enabled and not self.google.is_configured():
            missing = [
                name for name, val in [
                    ("OMS_GOOGLE_PROJECT_ID",     self.google.project_id),
                    ("OMS_GOOGLE_PRIVATE_KEY_ID", self.google.private_key_id),
                    ("OMS_GOOGLE_PRIVATE_KEY",    self.google.private_key),
                    ("OMS_GOOGLE_CLIENT_EMAIL",   self.google.client_email),
                    ("OMS_GOOGLE_CLIENT_ID",      self.google.client_id),
                    ("OMS_GOOGLE_SPREADSHEET_ID", self.google.spreadsheet_id),
                ] if not val
            ]
            errors.append(
                "google.enabled is true but required credential env vars are "
                f"missing: {', '.join(missing)}. Run scripts/json_to_env.py "
                "against your service account key once to generate them."
            )

        if errors:
            raise ConfigurationException(
                f"OMS configuration has {len(errors)} error(s):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )
        log.info(
            f"Configuration validated. "
            f"Group: {self.whatsapp.group_name!r}, "
            f"Staff: +{self.whatsapp.staff_number}"
        )

    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def has_sheets(self) -> bool:
        return bool(self.google.spreadsheet_id)

    def __repr__(self):
        return (
            f"OMSSettings("
            f"env={self.environment!r}, "
            f"group={self.whatsapp.group_name!r}, "
            f"headless={self.browser.headless}"
            f")"
        )

def get_settings() -> OMSSettings:
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = OMSSettings()
    return _settings_instance

def reset_settings():
    global _settings_instance
    _settings_instance = None