"""Recorder-owned Chrome profile paths and process-safe locking."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from typing import BinaryIO


PROFILE_ENV_VAR = "PORTAL_RECORDER_USER_DATA_DIR"
LOCK_FILE_NAME = ".recorder.lock"
# Only Chrome channels supported - no Chromium or Edge
CHANNEL_DIRECTORY_NAMES = {
    "chrome": "chrome",
    "chrome-beta": "chrome-beta",
}
# Profile retention: auto-cleanup profiles older than 30 days
PROFILE_RETENTION_DAYS = 30


class ProfileInUseError(RuntimeError):
    """Raised when another Recorder instance owns the browser profile."""


def _home_directory(environ: Mapping[str, str]) -> str:
    return environ.get("HOME") or environ.get("USERPROFILE") or os.path.expanduser("~")


def _normalized_path(path: str) -> str:
    expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    # Resolve existing symlinks/junctions so an alias cannot accidentally point
    # the Recorder back into Chrome's protected everyday profile.
    return os.path.realpath(expanded)


def get_standard_browser_user_data_dirs(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> list[str]:
    """Return ordinary browser user-data roots that must never be automated."""
    env = os.environ if environ is None else environ
    current_system = system or platform.system()
    home = _home_directory(env)

    if current_system == "Windows":
        local_app_data = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        relative_paths = [
            ("Google", "Chrome", "User Data"),
            ("Google", "Chrome Beta", "User Data"),
            ("Google", "Chrome Dev", "User Data"),
            ("Google", "Chrome SxS", "User Data"),
            ("Chromium", "User Data"),
            ("Microsoft", "Edge", "User Data"),
            ("Microsoft", "Edge Beta", "User Data"),
            ("Microsoft", "Edge Dev", "User Data"),
            ("Microsoft", "Edge SxS", "User Data"),
        ]
        return [
            _normalized_path(os.path.join(local_app_data, *relative_path))
            for relative_path in relative_paths
        ]
    if current_system == "Darwin":
        application_support = os.path.join(home, "Library", "Application Support")
        relative_paths = [
            ("Google", "Chrome"),
            ("Google", "Chrome Beta"),
            ("Google", "Chrome Dev"),
            ("Google", "Chrome Canary"),
            ("Chromium",),
            ("Microsoft Edge",),
            ("Microsoft Edge Beta",),
            ("Microsoft Edge Dev",),
            ("Microsoft Edge Canary",),
        ]
        return [
            _normalized_path(os.path.join(application_support, *relative_path))
            for relative_path in relative_paths
        ]

    config_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    directory_names = [
        "google-chrome",
        "google-chrome-beta",
        "google-chrome-unstable",
        "chromium",
        "microsoft-edge",
        "microsoft-edge-beta",
        "microsoft-edge-dev",
    ]
    return [
        _normalized_path(os.path.join(config_home, directory_name))
        for directory_name in directory_names
    ]


def get_standard_chrome_user_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> str:
    """Return stable Chrome's ordinary user-data root."""
    return get_standard_browser_user_data_dirs(
        environ=environ, system=system
    )[0]


def get_recorder_user_data_dir(
    *,
    browser_channel: str = "chrome",
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> str:
    """Return the persistent, app-owned Chrome user-data directory."""
    env = os.environ if environ is None else environ
    try:
        channel_directory = CHANNEL_DIRECTORY_NAMES[browser_channel]
    except KeyError as exc:
        raise ValueError(f"Unsupported browser channel: {browser_channel!r}") from exc

    override = env.get(PROFILE_ENV_VAR, "").strip()
    if override:
        return _normalized_path(os.path.join(override, channel_directory))

    current_system = system or platform.system()
    home = _home_directory(env)
    if current_system == "Windows":
        local_app_data = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        root = os.path.join(
            local_app_data, "PortalRecorder", "BrowserProfiles", channel_directory
        )
    elif current_system == "Darwin":
        root = os.path.join(
            home,
            "Library",
            "Application Support",
            "PortalRecorder",
            "BrowserProfiles",
            channel_directory,
        )
    else:
        state_home = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        root = os.path.join(
            state_home, "portal-recorder", "browser-profiles", channel_directory
        )
    return _normalized_path(root)


def _is_within(path: str, parent: str, *, system: str) -> bool:
    normalized_path = _normalized_path(path)
    normalized_parent = _normalized_path(parent)
    if system in {"Windows", "Darwin"}:
        comparable_path = normalized_path.replace("\\", "/").rstrip("/").casefold()
        comparable_parent = (
            normalized_parent.replace("\\", "/").rstrip("/").casefold()
        )
        return comparable_path == comparable_parent or comparable_path.startswith(
            comparable_parent + "/"
        )
    try:
        common = os.path.commonpath([normalized_path, normalized_parent])
    except ValueError:
        # Different Windows drives cannot contain one another.
        return False
    return common == normalized_parent


def _is_within_by_file_identity(path: str, parent: str) -> bool:
    """Detect existing symlink, junction, 8.3, and extended-path aliases."""
    if not os.path.exists(parent):
        return False

    candidate = path
    while True:
        if os.path.exists(candidate):
            try:
                if os.path.samefile(candidate, parent):
                    return True
            except OSError:
                pass
        ancestor = os.path.dirname(candidate)
        if ancestor == candidate:
            return False
        candidate = ancestor


def validate_recorder_user_data_dir(
    user_data_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> str:
    """Reject Chrome's protected everyday profile and return a normalized path."""
    if not isinstance(user_data_dir, str) or not user_data_dir.strip():
        raise ValueError("A Recorder Chrome user-data directory is required.")

    normalized = _normalized_path(user_data_dir.strip())
    current_system = system or platform.system()
    standard_directories = get_standard_browser_user_data_dirs(
        environ=environ, system=current_system
    )
    if any(
        _is_within(normalized, standard, system=current_system)
        or _is_within_by_file_identity(normalized, standard)
        for standard in standard_directories
    ):
        raise ValueError(
            "Recorder cannot automate Chrome's everyday User Data directory. "
            f"Choose a separate directory with {PROFILE_ENV_VAR}."
        )
    return normalized


def ensure_recorder_user_data_dir(
    user_data_dir: str | None = None,
    *,
    browser_channel: str = "chrome",
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> str:
    """Validate and create the persistent Recorder browser profile root."""
    selected = user_data_dir or get_recorder_user_data_dir(
        browser_channel=browser_channel, environ=environ, system=system
    )
    normalized = validate_recorder_user_data_dir(
        selected, environ=environ, system=system
    )
    os.makedirs(normalized, mode=0o700, exist_ok=True)
    return normalized


class ProfileLock:
    """An OS-backed lock held for the lifetime of a Recorder browser session."""

    def __init__(self, user_data_dir: str):
        self.user_data_dir = user_data_dir
        self.path = os.path.join(user_data_dir, LOCK_FILE_NAME)
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "ProfileLock":
        if self.acquired:
            return self

        os.makedirs(self.user_data_dir, mode=0o700, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise ProfileInUseError(
                "The Recorder Chrome profile is already in use. Close the other "
                "Recorder browser window or stop the other Recorder session."
            ) from exc

        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        try:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor also releases an OS-backed lock.
                pass
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def cleanup_old_profiles(
    *,
    older_than_days: int = PROFILE_RETENTION_DAYS,
    browser_channel: str = "chrome",
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Remove Recorder browser profiles older than the specified age.
    
    Args:
        older_than_days: Remove profiles not modified within this many days
        browser_channel: Chrome channel to clean up (default: "chrome")
        environ: Environment variables (default: os.environ)
        system: Platform system name (default: auto-detect)
        dry_run: If True, only report what would be deleted without deleting
        
    Returns:
        List of removed (or would-be-removed in dry_run) profile paths
    """
    import time
    
    env = os.environ if environ is None else environ
    current_system = system or platform.system()
    home = _home_directory(env)
    
    # Get the base directory for recorder profiles
    try:
        channel_directory = CHANNEL_DIRECTORY_NAMES[browser_channel]
    except KeyError as exc:
        raise ValueError(f"Unsupported browser channel: {browser_channel!r}") from exc
    
    if current_system == "Windows":
        local_app_data = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        base_dir = os.path.join(local_app_data, "PortalRecorder", "BrowserProfiles", channel_directory)
    elif current_system == "Darwin":
        base_dir = os.path.join(
            home,
            "Library",
            "Application Support",
            "PortalRecorder",
            "BrowserProfiles",
            channel_directory,
        )
    else:
        state_home = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        base_dir = os.path.join(state_home, "portal-recorder", "browser-profiles", channel_directory)
    
    if not os.path.isdir(base_dir):
        return []
    
    cutoff_time = time.time() - (older_than_days * 24 * 60 * 60)
    removed = []
    
    for entry in os.listdir(base_dir):
        profile_path = os.path.join(base_dir, entry)
        if not os.path.isdir(profile_path):
            continue
        
        # Check last modification time
        try:
            mtime = os.path.getmtime(profile_path)
        except OSError:
            continue
        
        if mtime < cutoff_time:
            if dry_run:
                removed.append(profile_path)
                print(f"[DRY RUN] Would remove old profile: {profile_path}")
            else:
                try:
                    import shutil
                    shutil.rmtree(profile_path)
                    removed.append(profile_path)
                    print(f"Removed old profile: {profile_path}")
                except OSError as e:
                    print(f"Failed to remove {profile_path}: {e}")
    
    return removed
