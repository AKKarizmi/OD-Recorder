import os
import platform
from collections.abc import Mapping
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
# Only support real Chrome channels - no Chromium or Edge
SUPPORTED_BROWSER_CHANNELS = {"chrome", "chrome-beta"}


def _optional_bool(value: Any, *, setting_name: str) -> bool | None:
    """Parse an optional boolean setting and reject ambiguous values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return None
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
    raise ValueError(
        f"{setting_name} must be one of: true, false, 1, 0, yes, no, on, off."
    )


def get_available_browser_channel(
    configured: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the browser channel, with environment taking precedence."""
    env = os.environ if environ is None else environ
    environment_value = env.get("PORTAL_BROWSER_CHANNEL", "").strip()
    if environment_value:
        selected = environment_value
    elif isinstance(configured, str) and configured.strip():
        selected = configured.strip()
    else:
        selected = "chrome"
    if selected not in SUPPORTED_BROWSER_CHANNELS:
        print(
            f"Warning: Unsupported browser channel {selected!r}; using 'chrome'. "
            f"Supported channels: {', '.join(sorted(SUPPORTED_BROWSER_CHANNELS))}."
        )
        return "chrome"
    return selected


def is_headless_environment(
    configured: bool | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> bool:
    """Resolve headed/headless mode from env, config, and the host platform.

    ``DISPLAY`` is meaningful on Unix-like systems, but is normally absent on
    Windows and macOS. Explicit environment values intentionally override CI
    and automatic detection.
    """
    env = os.environ if environ is None else environ

    for variable in ("PORTAL_HEADLESS", "HEADLESS"):
        if variable in env and env[variable].strip():
            return bool(_optional_bool(env[variable], setting_name=variable))

    configured_value = _optional_bool(configured, setting_name="headless")
    if configured_value is not None:
        return configured_value

    ci_value = _optional_bool(env.get("CI"), setting_name="CI")
    if ci_value is True:
        return True

    current_system = system or platform.system()
    if current_system in {"Windows", "Darwin"}:
        return False
    return not bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


# Default categories used when no config file is provided
DEFAULT_CATEGORIES = ["AUTH", "Registration", "Application", "Data Entry", "Test"]


SESSIONS_DIR = os.path.join(os.path.dirname(__file__), 'recordings', 'sessions')
DEFAULT_DURATION = 3600  # 1 hour

# Configuration file path for loading categories and other settings
DEFAULT_CONFIG_FILE_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(path: str | None = None) -> dict:
    """Load configuration from config file if it exists.
    
    Returns a dictionary with configuration values. If the config file
    doesn't exist or is invalid, returns default values.
    """
    import json
    
    defaults = {
        "categories": DEFAULT_CATEGORIES,
        "browser_channel": "chrome",
        # None means auto-detect. An explicit JSON boolean overrides detection.
        "headless": None,
    }
    config = defaults.copy()

    config_path = path or os.environ.get("PORTAL_CONFIG_FILE", DEFAULT_CONFIG_FILE_PATH)
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                if not isinstance(file_config, dict):
                    raise ValueError("the top-level JSON value must be an object")
                candidate = defaults.copy()
                # Merge with defaults
                if "categories" in file_config:
                    candidate["categories"] = file_config["categories"]
                if isinstance(file_config.get("browser_channel"), str):
                    candidate["browser_channel"] = file_config["browser_channel"]
                if "headless" in file_config:
                    candidate["headless"] = _optional_bool(
                        file_config["headless"], setting_name="headless"
                    )
                config = candidate
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            print(f"Warning: Could not load config file {config_path}: {e}")
            print("Using default configuration.")
            config = defaults.copy()
    
    return config

# Recorder polling/debounce values are expressed in seconds.
# DYNAMIC_CAPTURE_DELAY: time to wait after DOM changes before capturing (balances responsiveness vs noise)
DYNAMIC_CAPTURE_DELAY = 0.8
# POLL_INTERVAL: how frequently to check for DOM changes (500ms provides good responsiveness)
POLL_INTERVAL = 0.5
# Playwright timeout values are expressed in milliseconds.
NAVIGATION_TIMEOUT = 60_000
# Load configuration from file (if available)
CONFIG = load_config()
# Environment variables override config.json; otherwise use platform-aware
# automatic detection. Windows and macOS are headed by default.
BROWSER_CHANNEL = get_available_browser_channel(CONFIG.get("browser_channel"))
HEADLESS_MODE = is_headless_environment(CONFIG.get("headless"))
