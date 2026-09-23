import shutil
from dataclasses import dataclass
from pathlib import Path

from apps.oms.shared.logger import get_logger
from apps.oms.shared.exceptions import ConfigurationException

log = get_logger(__name__)


@dataclass
class ProfileInfo:
    '''
    Information about a browser profile directory.
    Returned by BrowserProfile.inspect() for diagnostics.
    '''
    path:       Path
    exists:     bool
    size_mb:    float    # Approximate size of profile on disk
    has_cookies: bool    # Whether the profile appears to have a saved session


class BrowserProfile:
    '''
    Manages the persistent Chromium browser profile for the OMS.

    A Chromium persistent profile is a directory on disk that stores
    browser state: cookies, localStorage, preferences, cache.
    When Playwright launches with user_data_dir pointing to this
    directory, it restores the previous browser state — including
    any WhatsApp Web login session.

    This is how the OMS stays logged into WhatsApp Web between
    restarts without needing to scan the QR code every time.

    The OMS profile is COMPLETELY SEPARATE from:
      - Your personal Chrome browser profile
      - The review automation's WhatsApp session
      - Any other browser profile on the system

    Usage:
        profile = BrowserProfile(session_dir=".sessions/oms_session")
        profile.ensure_exists()
        path = profile.path   # Pass to Playwright's user_data_dir
    '''

    # Minimum profile size in bytes that suggests a saved session exists
    # A fresh empty profile is ~50KB. A profile with WhatsApp session is ~5MB+
    SESSION_SIZE_THRESHOLD_BYTES = 1_000_000  # 1 MB

    def __init__(self, session_dir: str):
        '''
        Args:
            session_dir: Path to the profile directory.
                         Created automatically if it does not exist.
                         Example: ".sessions/oms_session"
        '''
        if not session_dir:
            raise ConfigurationException(
                "Browser session_dir cannot be empty. "
                "Set OMS_BROWSER_SESSION_DIR or configure in settings.py"
            )

        self._path = Path(session_dir).resolve()
        log.debug(f"BrowserProfile initialised: {self._path}")

    @property
    def path(self) -> Path:
        '''Absolute path to the profile directory.'''
        return self._path

    @property
    def path_str(self) -> str:
        '''String version of the profile path for Playwright.'''
        return str(self._path)

    def ensure_exists(self) -> None:
        '''
        Create the profile directory if it does not exist.
        Safe to call multiple times — uses exist_ok=True.
        '''
        self._path.mkdir(parents=True, exist_ok=True)
        log.debug(f"Profile directory ready: {self._path}")

    def exists(self) -> bool:
        '''True if the profile directory exists on disk.'''
        return self._path.exists()

    def appears_to_have_session(self) -> bool:
        '''
        Heuristic check: does this profile likely have a saved WhatsApp session?

        A fresh profile has very little data. A profile with a WhatsApp
        login session will be at least 1MB due to stored cookies and
        localStorage. This is a best-effort check — the only reliable
        way to confirm login is to load WhatsApp Web and check the UI.

        Returns:
            True if the profile appears to contain saved session data.
            False if the profile is empty or very small (likely fresh).
        '''
        if not self._path.exists():
            return False

        try:
            total_size = sum(
                f.stat().st_size
                for f in self._path.rglob("*")
                if f.is_file()
            )
            return total_size > self.SESSION_SIZE_THRESHOLD_BYTES
        except Exception as e:
            log.debug(f"Could not calculate profile size: {e}")
            return False

    def inspect(self) -> ProfileInfo:
        '''
        Return diagnostic information about the profile.
        Used for health checks and startup logging.
        '''
        exists = self._path.exists()
        size_bytes = 0
        has_cookies = False

        if exists:
            try:
                size_bytes = sum(
                    f.stat().st_size
                    for f in self._path.rglob("*")
                    if f.is_file()
                )
                # Chromium stores cookies at Default/Cookies (older)
                # or Default/Network/Cookies (newer, Windows confirmed).
                # Check both — whichever exists and has data counts.
                cookies_old = self._path / "Default" / "Cookies"
                cookies_new = self._path / "Default" / "Network" / "Cookies"
                has_cookies = (
                    (cookies_old.exists() and cookies_old.stat().st_size > 1024)
                    or
                    (cookies_new.exists() and cookies_new.stat().st_size > 1024)
                )
                
            except Exception as e:
                log.debug(f"Profile inspection error: {e}")

        return ProfileInfo(
            path        =self._path,
            exists      =exists,
            size_mb     =round(size_bytes / (1024 * 1024), 2),
            has_cookies =has_cookies,
        )

    def clear(self) -> None:
        '''
        Delete the profile directory completely.
        Used when a session is corrupt and needs a fresh start.

        WARNING: This logs out of WhatsApp Web permanently.
        The next launch will require scanning the QR code again.
        '''
        if self._path.exists():
            shutil.rmtree(self._path)
            log.warning(
                f"Profile cleared: {self._path}\n"
                f"WhatsApp Web will require QR scan on next launch."
            )
        else:
            log.debug("Profile does not exist — nothing to clear.")

    def __repr__(self):
        info = self.inspect()
        return (
            f"BrowserProfile("
            f"path={self._path}, "
            f"exists={info.exists}, "
            f"size={info.size_mb}MB, "
            f"has_session={info.has_cookies}"
            f")"
        )