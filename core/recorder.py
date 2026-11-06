"""Interactive portal recorder with debounced dynamic-schema capture."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from config import (
    BROWSER_CHANNEL,
    CAPTURE_SENSITIVE_VALUES,
    DEBUG_CAPTURE_VALUES,
    DOM_STABILITY_QUIET_MS,
    DOM_STABILITY_TIMEOUT_MS,
    DYNAMIC_CAPTURE_DELAY,
    HEADLESS_MODE,
    NAVIGATION_TIMEOUT,
    POLL_INTERVAL,
    SESSIONS_DIR,
)
from core.event_listener import inject_event_listeners
from core.profile_manager import (
    ProfileLock,
    ensure_recorder_user_data_dir,
    validate_recorder_user_data_dir,
)
from core.schema_extractor import extract_form_schema, field_state, get_field_identity
from core.session_manager import SessionManager, get_session_manager
from core.utils import is_sensitive_field, safe_field_value
from core.validation import validate_recording

# Configure module logger
logger = logging.getLogger(__name__)


def normalize_category(category: Any) -> str:
    value = str(category or "").strip()
    return "Registration" if value.casefold() == "registeration" else value


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_json(path: str, value: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, default=str)


DOM_OBSERVER_SCRIPT = r"""
(() => {
    if (window.__recorderObserverInstalled) return;
    window.__recorderObserverInstalled = true;
    // Expanded to track all DOM elements, not just controls
    const controls = 'input, select, textarea, button';
    const staticElements = 'h1, h2, h3, h4, h5, h6, legend, p, li, dt, dd, caption, figcaption, [role="heading"], [role="alert"], [role="status"], [aria-live], table, dl, a[href], img';
    const state = { newFields: [], removedFields: [], visibilityChanges: [], rowChanges: [], panelChanges: [], newTextContent: [], removedTextContent: [] };
    window.__recorderDomChanges = state;
    // Keep the short name for integrations that consume the original observer API.
    window.domChanges = state;

    function xpath(element) {
        if (!element || !element.parentNode) return '';
        if (element.id) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
        if (element === document.body) return '/html/body';
        let index = 0;
        for (const sibling of element.parentNode.childNodes) {
            if (sibling === element) return xpath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (index + 1) + ']';
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) index++;
        }
        return '';
    }

    function hidden(element) {
        if (!element || element.nodeType !== 1) return true;
        for (let current = element; current; current = current.parentElement) {
            if (current.hidden || current.getAttribute('aria-hidden') === 'true' || current.hasAttribute('inert')) return true;
            if (current.classList.contains('hidden') || current.classList.contains('ng-hide') || current.classList.contains('d-none')) return true;
            const style = getComputedStyle(current);
            if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return true;
        }
        return element.matches('input[type="hidden"]') || element.getClientRects().length === 0;
    }

    function pushUnique(list, value, key) {
        if (!list.some(item => item[key] === value[key])) list.push(value);
    }

    function describe(element) {
        return { xpath: xpath(element), id: element.id || null, tag: element.tagName, name: element.name || null };
    }

    function describeStatic(element) {
        return { 
            xpath: xpath(element), 
            id: element.id || null, 
            tag: element.tagName, 
            textContent: (element.innerText || '').trim(),
            className: typeof element.className === 'string' ? element.className : null
        };
    }

    function controlsIn(node) {
        if (!node || node.nodeType !== 1) return [];
        const result = [];
        if (node.matches(controls)) result.push(node);
        node.querySelectorAll(controls).forEach(item => result.push(item));
        return result;
    }

    function staticElementsIn(node) {
        if (!node || node.nodeType !== 1) return [];
        const result = [];
        if (node.matches(staticElements)) result.push(node);
        node.querySelectorAll(staticElements).forEach(item => result.push(item));
        return result;
    }

    function rowFor(element) {
        return element.closest('[data-row-index], [data-index], [data-repeater-item], .repeatable-row, .repeater-item, .education-row, .work-row, .employment-row, .experience-row, .array-row, .dynamic-row');
    }

    function mutationSummary(mutation) {
        const target = mutation.target && mutation.target.nodeType === 1 ? mutation.target : null;
        if (mutation.type === 'attributes' && target) {
            controlsIn(target).forEach(control => {
                const item = describe(control);
                item.nowVisible = !hidden(control);
                pushUnique(state.visibilityChanges, item, 'xpath');
            });
            // Track visibility changes for static text elements too
            staticElementsIn(target).forEach(el => {
                const item = describeStatic(el);
                item.nowVisible = !hidden(el);
                pushUnique(state.visibilityChanges, item, 'xpath');
            });
            const panel = target.closest('.panel, .section, .form-group, [role="group"], [data-panel-id]');
            if (panel && (mutation.attributeName === 'class' || mutation.attributeName === 'style' || mutation.attributeName === 'hidden' || mutation.attributeName === 'aria-hidden')) {
                pushUnique(state.panelChanges, { panelId: panel.id || panel.getAttribute('data-panel-id') || null, panelXPath: xpath(panel), nowVisible: !hidden(panel) }, 'panelXPath');
            }
            const row = rowFor(target);
            if (row && (mutation.attributeName === 'class' || mutation.attributeName === 'style' || mutation.attributeName === 'hidden' || mutation.attributeName === 'aria-hidden')) {
                pushUnique(state.rowChanges, { containerXPath: xpath(row), index: row.getAttribute('data-row-index') || row.getAttribute('data-index') || null, rowKey: row.className || null, nowVisible: !hidden(row) }, 'containerXPath');
            }
        }
        if (mutation.type === 'childList') {
            mutation.addedNodes.forEach(node => {
                controlsIn(node).forEach(control => pushUnique(state.newFields, describe(control), 'xpath'));
                // Track new static text content
                staticElementsIn(node).forEach(el => {
                    const text = (el.innerText || '').trim();
                    if (text) {
                        pushUnique(state.newTextContent, describeStatic(el), 'xpath');
                    }
                });
                if (node.nodeType === 1) {
                    const row = rowFor(node) || (node.matches('[data-row-index], [data-index], [data-repeater-item], .repeatable-row, .repeater-item, .education-row, .work-row, .employment-row, .experience-row, .array-row, .dynamic-row') ? node : null);
                    if (row) pushUnique(state.rowChanges, { containerXPath: xpath(row), index: row.getAttribute('data-row-index') || row.getAttribute('data-index') || null, rowKey: row.className || null, nowVisible: !hidden(row) }, 'containerXPath');
                }
            });
            mutation.removedNodes.forEach(node => {
                controlsIn(node).forEach(control => pushUnique(state.removedFields, describe(control), 'xpath'));
                // Track removed static text content
                staticElementsIn(node).forEach(el => {
                    const text = (el.innerText || '').trim();
                    if (text) {
                        pushUnique(state.removedTextContent, describeStatic(el), 'xpath');
                    }
                });
                if (node.nodeType === 1) {
                    const row = rowFor(node) || (node.matches('[data-row-index], [data-index], [data-repeater-item], .repeatable-row, .repeater-item, .education-row, .work-row, .employment-row, .experience-row, .array-row, .dynamic-row') ? node : null);
                    if (row) pushUnique(state.rowChanges, { containerXPath: xpath(row), index: row.getAttribute('data-row-index') || row.getAttribute('data-index') || null, rowKey: row.className || null, nowVisible: false }, 'containerXPath');
                }
            });
        }
        if (mutation.type === 'characterData') {
            const parent = mutation.target.parentElement;
            if (parent) {
                const item = describeStatic(parent);
                item.changeType = 'text_changed';
                pushUnique(state.newTextContent, item, 'xpath');
            }
        }
    }

    const observer = new MutationObserver(mutations => mutations.forEach(mutationSummary));
    function install() {
        if (document.documentElement) observer.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['class', 'style', 'hidden', 'aria-hidden', 'disabled', 'readonly', 'required', 'inert'],
            childList: true,
            subtree: true,
            characterData: true
        });
    }
    install();
    if (!document.documentElement) document.addEventListener('DOMContentLoaded', install, { once: true });

    window.__recorderConsumeDomChanges = () => {
        const copy = JSON.parse(JSON.stringify(window.__recorderDomChanges || state));
        Object.keys(state).forEach(key => state[key].length = 0);
        return copy;
    };
})();
"""

WAIT_FOR_STABLE_DOM_SCRIPT = r"""
({ quietMs, timeoutMs }) => new Promise(resolve => {
    const started = performance.now();
    let lastMutation = performance.now();
    let mutationCount = 0;
    const root = document.documentElement;
    const observer = root ? new MutationObserver(mutations => {
        mutationCount += mutations.length;
        lastMutation = performance.now();
    }) : null;
    if (observer && root) observer.observe(root, { childList: true, subtree: true, attributes: true });
    const finish = timedOut => {
        if (observer) observer.disconnect();
        resolve({ stable: !timedOut, timed_out: timedOut, mutation_count: mutationCount,
            ready_state: document.readyState, waited_ms: Math.round(performance.now() - started) });
    };
    const poll = () => {
        const now = performance.now();
        const ready = document.readyState === 'interactive' || document.readyState === 'complete';
        if (ready && now - lastMutation >= quietMs) return finish(false);
        if (now - started >= timeoutMs) return finish(true);
        setTimeout(poll, Math.min(100, quietMs));
    };
    poll();
})
"""


class Recorder:
    def __init__(self, university, category, profile_path, profile_directory=None):
        self.university = university
        self.category = normalize_category(category)
        self.profile_path = profile_path
        self.user_data_dir, self.profile_directory = self._resolve_chrome_profile(
            profile_path, profile_directory
        )
        self.session_id = uuid.uuid4().hex[:8]
        self.actions: list[dict[str, Any]] = []
        self.raw_actions: list[dict[str, Any]] = []
        self.step_counter = 0
        self.current_step_name = "initial_load"
        self.session_dir = self._build_session_dir()
        self.browser = None
        self.page = None
        self._playwright = None
        self._profile_lock = ProfileLock(self.user_data_dir)
        self._running = False
        self._stop_event = threading.Event()
        self._page_lock = threading.Lock()  # Thread-safe page access
        self._pending_deadline: float | None = None
        self._pending_reasons: set[str] = set()
        self._pending_details: dict[str, Any] = {}
        self._last_signature: Counter[tuple[str, tuple[Any, ...]]] | None = None
        self.last_fields_set: set[str] = set()
        self.last_schema: dict[str, Any] | None = None
        self._last_url: str | None = None
        self._initial_captured = False
        self._recording_started_at = datetime.now(timezone.utc).isoformat()
        
        # Session management for cookies, tokens, and state persistence
        self.session_manager = get_session_manager()
        self.session_state = None
        
        # Memory management: circular buffer for DOM changes with max size
        self._max_dom_history = 1000  # Maximum number of DOM change entries per type
        self._dom_change_buffers: dict[str, list[Any]] = {
            key: [] for key in (
                "newFields", "removedFields", "visibilityChanges", "rowChanges",
                "panelChanges", "newTextContent", "removedTextContent",
            )
        }
        
        # Session recovery: periodic save configuration
        self._auto_save_interval = 30  # seconds between auto-saves
        self._last_auto_save: float = 0
        self._auto_save_counter = 0  # Track number of auto-saves
        
        # Error handling: retry configuration
        self._max_retries = 3
        self._retry_delay = 0.5  # seconds between retries
        
        # Performance: adaptive polling
        self._adaptive_poll_enabled = True
        self._consecutive_no_changes = 0
        self._current_poll_interval = POLL_INTERVAL
        self._min_poll_interval = 0.3
        self._max_poll_interval = 2.0
        self.debug_capture_values = DEBUG_CAPTURE_VALUES
        self.capture_sensitive_values = CAPTURE_SENSITIVE_VALUES
        self._pending_inputs: dict[str, dict[str, Any]] = {}
        self._focused_field_keys: dict[str, str] = {}
        self._active_scope: dict[str, Any] | None = None
        self._last_action_signature: tuple[Any, ...] | None = None
        self._last_action_timestamp = 0
        self._raw_sequence = 0
        self._semantic_sequence = 0
        self._pending_action_ids: list[str] = []
        self._last_raw_action_id: str | None = None
        self._unassigned_raw_action_ids: list[str] = []
        self._interaction_sequence = 0
        self._interactions: dict[str, dict[str, Any]] = {}
        self._active_interaction_by_key: dict[str, str] = {}
        self._current_interaction_id: str | None = None
        self._interaction_timeout_seconds = max(2.0, DYNAMIC_CAPTURE_DELAY * 4)
        self._captured_schemas: list[dict[str, Any]] = []
        self._last_state_audit = time.monotonic()
        self._state_audit_interval = 2.0

    @staticmethod
    def _resolve_chrome_profile(profile_path, profile_directory=None):
        """Resolve and validate a Recorder-owned Chrome user-data root."""
        if not isinstance(profile_path, str) or not profile_path.strip():
            raise ValueError("A Recorder Chrome user-data directory is required.")
        user_data_dir = validate_recorder_user_data_dir(profile_path.strip())
        folder = profile_directory or "Default"
        if folder != "Default" and not re.fullmatch(r"Profile \d+", folder):
            raise ValueError(f"Invalid Recorder Chrome profile directory: {folder!r}")
        return user_data_dir, folder

    def _ensure_profile_available(self):
        # Chrome creates Default and its Preferences file on the first launch.
        # This root is deliberately separate from Chrome's everyday User Data.
        self.user_data_dir = ensure_recorder_user_data_dir(self.user_data_dir)

    def _build_session_dir(self):
        university_name = (
            self.university.get("University Name")
            or self.university.get("university_name")
            or self.university.get("name")
            or "unknown"
        )
        uni_name = self._safe_name(str(university_name).replace(" ", "_"))
        return os.path.join(SESSIONS_DIR, self._safe_name(self.category), f"{uni_name}_{self.session_id}")

    def _portal_url(self):
        return (
            self.university.get("portal_url")
            or self.university.get("Portal Link")
            or self.university.get("portal_link")
            or self.university.get("Portal URL")
        )

    def start(self):
        ensure_dir(self.session_dir)
        try:
            self._ensure_profile_available()
            self._profile_lock.acquire()
            try:
                self._playwright = sync_playwright().start()
                launch_options = {
                    "user_data_dir": self.user_data_dir,
                    "headless": HEADLESS_MODE,
                    "viewport": {"width": 1280, "height": 800},
                    "channel": BROWSER_CHANNEL,
                }
                if self.profile_directory != "Default":
                    launch_options["args"] = [
                        f"--profile-directory={self.profile_directory}"
                    ]
                
                # Force Chrome binary path for real Chrome (not Chromium)
                import platform
                system = platform.system()
                chrome_paths = {
                    "Windows": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    "Darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "Linux": "/usr/bin/google-chrome"
                }
                executable_path = chrome_paths.get(system)
                if executable_path and os.path.isfile(executable_path):
                    launch_options["executable_path"] = executable_path
                
                self.browser = self._playwright.chromium.launch_persistent_context(
                    **launch_options
                )
            except PlaywrightError as exc:
                raise RuntimeError(
                    "Could not open the dedicated Recorder Chrome profile. "
                    "Close any Recorder Chrome window or stop the other Recorder session, "
                    f"then try again. Detail: {exc}"
                ) from exc
            
            # Get the first page from the context (persistent contexts may have existing pages)
            pages = self.browser.pages
            if pages:
                self.page = pages[0]
            else:
                self.page = self.browser.new_page()
            
            portal_url = self._portal_url()
            if not isinstance(portal_url, str) or not portal_url.strip():
                raise ValueError(
                    "The selected university has no portal URL. Expected one of: "
                    "portal_url, Portal Link, portal_link, or Portal URL."
                )
            portal_url = portal_url.strip()
            parsed_url = urlparse(portal_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError(f"Invalid portal URL for the selected university: {portal_url!r}")

            try:
                self.page.goto(portal_url, timeout=NAVIGATION_TIMEOUT, wait_until="domcontentloaded")
                print(f"Successfully navigated to {portal_url}")
            except PlaywrightTimeoutError as exc:
                # A slow portal may still have a usable DOM. Keep the browser
                # open so the operator can inspect it and continue manually.
                print(
                    f"Navigation timed out after {NAVIGATION_TIMEOUT/1000:.0f}s for {portal_url}. The browser remains open; "
                    "continue only if the intended page is available."
                )
                print(f"Timeout detail: {exc}")
            except PlaywrightError as exc:
                print(
                    f"Navigation did not complete for {portal_url}. The browser remains open; "
                    "continue only if the intended page is available."
                )
                print(f"Navigation detail: {exc}")

            print(f"Browser opened for {self.category} recording.")
            print(f"Complete login and navigate to the target section: {portal_url}")
            print("Press Enter to start recording. Authentication and positioning actions will be excluded.")
            input()

            self._install_recording_instrumentation()
            if self.capture_sensitive_values:
                print("WARNING: sensitive capture is enabled; passwords and tokens will be written to plain JSON.")
            self._running = True
            
            # Initialize session state for session management
            self.session_state = self.session_manager.create_session(
                session_id=self.session_id,
                university=self.university,
                category=self.category,
                base_url=portal_url,
            )
            
            # Extract and store initial cookies and storage
            self._capture_session_state()
            
            self._wait_for_stable_dom()
            initial = self._capture_schema("initial_load", "initial_load")
            self._set_baseline(initial)
            self._initial_captured = True
            self._clear_observer_changes()
            self._pending_deadline = None
            print(f"Recorder started. Session ID: {self.session_id}")
            print("Press Enter in this terminal when you have finished, or Ctrl+C to stop.")
            threading.Thread(target=self._wait_for_stop, daemon=True).start()
            self._poll_and_capture()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.stop()

    def _wait_for_stop(self):
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        self._stop_event.set()

    def _inject_dom_observer(self):
        self.page.add_init_script(DOM_OBSERVER_SCRIPT)
        # The operator may start on an already-loaded page. Init scripts only
        # run on future documents, so install the observer in current frames too.
        try:
            for frame in self.page.frames:
                try:
                    frame.evaluate(DOM_OBSERVER_SCRIPT)
                except Exception:
                    continue
        except Exception:
            try:
                self.page.evaluate(DOM_OBSERVER_SCRIPT)
            except Exception:
                pass

    def _install_recording_instrumentation(self):
        inject_event_listeners(self.page, self._on_event)
        self._inject_dom_observer()
        self.page.on("framenavigated", self._on_navigation)

    def _on_event(self, event_data):
        """Convert low-level browser events into replayable logical actions."""
        self._ensure_action_state()
        event = dict(event_data)
        field = dict(event.get("field") or {})
        field.update({
            key: event.get(key) for key in ("inputType", "name", "id")
            if event.get(key) is not None
        })
        if "value" in event:
            safe_value = safe_field_value(event.get("value"), field)
            if safe_value is None:
                event.pop("value", None)
            else:
                event["value"] = safe_value
        event.pop("text", None)
        self._raw_sequence += 1
        raw_action_id = f"raw_{self._raw_sequence:06d}"
        raw_event = {
            key: value for key, value in event.items()
            if key not in {"value", "text"} and value is not None
        }
        raw_event.update({
            "raw_action_id": raw_action_id, "raw_sequence": self._raw_sequence,
            "semantic_action_id": None, "step_id": None, "parent_interaction_id": None,
        })
        self.raw_actions.append(raw_event)
        self._last_raw_action_id = raw_action_id
        event_type = str(event.get("type") or "").lower()
        is_widget_controller = bool((event.get("field") or {}).get("widget_kind"))
        locator = self._event_locator(event)
        field_key = self._field_key_for_event(event)
        scope = self._scope_from_event(event)
        if scope:
            self._active_scope = scope

        interaction_key = self._interaction_key_for_event(event, field_key)
        self._route_raw_interaction(event, raw_event, interaction_key)

        if event.get("raw_only"):
            return

        if event_type == "focus":
            for pending_locator in list(self._pending_inputs):
                if pending_locator != locator:
                    self._flush_input(pending_locator)
            if field_key:
                self._focused_field_keys[locator] = field_key
            return

        if event_type == "input":
            if is_widget_controller:
                return
            if str(event.get("inputType") or "").lower() in {"checkbox", "radio"}:
                return
            pending = self._pending_inputs.get(locator)
            if pending is None:
                pending = dict(event)
                pending["started_at"] = event.get("timestamp")
                pending["raw_action_ids"] = [raw_action_id]
                self._pending_inputs[locator] = pending
            else:
                pending.update(event)
                pending.setdefault("raw_action_ids", []).append(raw_action_id)
            pending["last_received_at"] = time.monotonic()
            if field_key:
                pending["field_key"] = field_key
            return

        if event_type == "blur":
            if is_widget_controller:
                self._pending_inputs.pop(locator, None)
                interaction_key = self._interaction_key_for_event(event, field_key)
                interaction_id = self._active_interaction_by_key.get(interaction_key or "")
                interaction = self._interactions.get(interaction_id or "")
                if interaction and interaction.get("option_label"):
                    attributed_event = dict(event)
                    attributed_event["selected_option"] = {
                        "label": interaction.get("option_label"),
                        "value": interaction.get("option_value"),
                        "value_source": interaction.get("option_value_source") or "unavailable",
                        "widget_key": interaction.get("widget_key"),
                        "widget_label": interaction.get("widget_label") or (event.get("field") or {}).get("widget_label"),
                        "controller": event.get("field"),
                        "scoped_selector": {
                            "widget_key": interaction.get("widget_key"),
                            "option_label": interaction.get("option_label"),
                            "option_value": interaction.get("option_value"),
                        },
                    }
                    self._record_select_action(attributed_event, field_key, scope)
                return
            self._flush_input(locator, event)
            return

        if event_type == "change":
            input_type = str(event.get("inputType") or "").lower()
            if is_widget_controller and not event.get("selected_option"):
                self._pending_inputs.pop(locator, None)
                return
            if input_type in {"checkbox", "radio"}:
                self._pending_inputs.pop(locator, None)
                self._record_logical_action({
                    "type": "SET_CHECKBOX" if input_type == "checkbox" else "SET_RADIO",
                    "field_key": field_key,
                    "field": event.get("field"),
                    "checked": bool(event.get("checked")),
                    "event_occurred_at": event.get("timestamp"),
                    "raw_action_ids": [raw_action_id],
                    "immediate_post_state": event.get("immediate_post_state"),
                    "immediate_page_state": event.get("immediate_page_state"),
                }, scope)
            elif event.get("selected_option") or str(event.get("tag") or "").upper() == "SELECT":
                self._pending_inputs.pop(locator, None)
                self._record_select_action(event, field_key, scope)
            else:
                self._flush_input(locator, event)
            return

        if event_type == "click":
            if event.get("selected_option"):
                controller_locator = self._event_locator({"field": event["selected_option"].get("controller")})
                self._pending_inputs.pop(controller_locator, None)
                self._record_select_action(event, field_key, scope)
                return
            input_type = str(event.get("field", {}).get("inputType") or "").lower()
            tag = str(event.get("tag") or "").upper()
            if input_type in {"checkbox", "radio"} or tag in {"INPUT", "TEXTAREA", "SELECT"}:
                return
            if is_widget_controller:
                return
            self._record_logical_action({
                "type": "CLICK_ACTION",
                "field_key": field_key,
                "field": event.get("field"),
                "event_occurred_at": event.get("timestamp"),
                "raw_action_ids": [raw_action_id],
                "immediate_post_state": event.get("immediate_post_state"),
                "immediate_page_state": event.get("immediate_page_state"),
            }, scope)
            return

        if event_type == "submit":
            timestamp = event.get("timestamp") or 0
            submit_raw_ids = [raw_action_id]
            if self.actions and self.actions[-1].get("type") == "CLICK_ACTION" and timestamp - (self.actions[-1].get("event_occurred_at") or 0) < 1000:
                discarded = self.actions.pop()
                submit_raw_ids = list(discarded.get("raw_action_ids", [])) + submit_raw_ids
                discarded_id = discarded.get("semantic_action_id")
                self._pending_action_ids = [value for value in self._pending_action_ids if value != discarded_id]
                self._semantic_sequence -= 1
            self._record_logical_action({
                "type": "SUBMIT_FORM",
                "form": event.get("form"),
                "event_occurred_at": event.get("timestamp"),
                "raw_action_ids": submit_raw_ids,
            }, scope)

    def _ensure_action_state(self) -> None:
        """Initialize semantic-action state for normal and lightweight test instances."""
        if not hasattr(self, "_pending_inputs"):
            self._pending_inputs = {}
        if not hasattr(self, "_focused_field_keys"):
            self._focused_field_keys = {}
        if not hasattr(self, "_active_scope"):
            self._active_scope = None
        if not hasattr(self, "_last_action_signature"):
            self._last_action_signature = None
        if not hasattr(self, "_last_action_timestamp"):
            self._last_action_timestamp = 0
        if not hasattr(self, "_pending_reasons"):
            self._pending_reasons = set()
        if not hasattr(self, "_pending_details"):
            self._pending_details = {}
        if not hasattr(self, "_pending_deadline"):
            self._pending_deadline = None
        if not hasattr(self, "raw_actions"):
            self.raw_actions = []
        if not hasattr(self, "_raw_sequence"):
            self._raw_sequence = len(self.raw_actions)
        if not hasattr(self, "_semantic_sequence"):
            self._semantic_sequence = len(self.actions)
        if not hasattr(self, "_pending_action_ids"):
            self._pending_action_ids = []
        if not hasattr(self, "_last_raw_action_id"):
            self._last_raw_action_id = None
        if not hasattr(self, "_unassigned_raw_action_ids"):
            self._unassigned_raw_action_ids = []
        if not hasattr(self, "_interaction_sequence"):
            self._interaction_sequence = 0
        if not hasattr(self, "_interactions"):
            self._interactions = {}
        if not hasattr(self, "_active_interaction_by_key"):
            self._active_interaction_by_key = {}
        if not hasattr(self, "_current_interaction_id"):
            self._current_interaction_id = None
        if not hasattr(self, "_interaction_timeout_seconds"):
            self._interaction_timeout_seconds = max(2.0, DYNAMIC_CAPTURE_DELAY * 4)
        if not hasattr(self, "_captured_schemas"):
            self._captured_schemas = []
        if not hasattr(self, "_last_state_audit"):
            self._last_state_audit = time.monotonic()
        if not hasattr(self, "_state_audit_interval"):
            self._state_audit_interval = 2.0

    @staticmethod
    def _event_locator(event: dict[str, Any]) -> str:
        field = event.get("field") or event
        form = field.get("form") or {}
        form_key = form.get("id") or form.get("name") or form.get("action") or "document"
        field_id = str(field.get("id") or "").strip()
        if field_id and not re.fullmatch(r"(?i)(?:null|undefined|listbox-null)(?:-\d+)?|\d+", field_id):
            return f"{form_key}|id:{field_id}"
        if field.get("name"):
            return f"{form_key}|name:{field['name']}"
        return f"{form_key}|xpath:{field.get('xpath') or event.get('xpath') or 'unknown'}"

    def _interaction_key_for_event(self, event: dict[str, Any], field_key: str | None = None) -> str | None:
        option = event.get("selected_option") or {}
        field = option.get("controller") or event.get("field") or {}
        widget_key = option.get("widget_key") or field.get("widget_key")
        if widget_key:
            return f"widget:{widget_key}"
        if field_key:
            return f"field:{field_key}"
        form = event.get("form") or {}
        form_key = form.get("id") or form.get("name") or form.get("action")
        if form_key:
            return f"form:{form_key}"
        return None

    @staticmethod
    def _starts_interaction(event_type: str) -> bool:
        return event_type in {"pointerdown", "click", "focus", "keydown", "input", "change", "submit"}

    def _close_interaction(self, interaction_id: str, status: str = "cancelled") -> None:
        interaction = self._interactions.get(interaction_id)
        if not interaction or interaction.get("status") != "open":
            return
        interaction["status"] = status
        key = interaction.get("interaction_key")
        if self._active_interaction_by_key.get(key) == interaction_id:
            self._active_interaction_by_key.pop(key, None)
        if self._current_interaction_id == interaction_id:
            self._current_interaction_id = None

    def _route_raw_interaction(
        self, event: dict[str, Any], raw_event: dict[str, Any], interaction_key: str | None
    ) -> None:
        if not interaction_key:
            return
        event_type = str(event.get("type") or "").lower()
        current = self._interactions.get(self._current_interaction_id or "")
        if (
            current and current.get("interaction_key") != interaction_key
            and self._starts_interaction(event_type)
        ):
            self._close_interaction(current["interaction_id"], "cancelled")
        interaction_id = self._active_interaction_by_key.get(interaction_key)
        interaction = self._interactions.get(interaction_id or "")
        if interaction is None or interaction.get("status") != "open":
            # A trailing blur must not create a new interaction after an option
            # click has already completed the widget session.
            if event_type == "blur":
                return
            self._interaction_sequence += 1
            interaction_id = f"interaction_{self._interaction_sequence:06d}"
            interaction = {
                "interaction_id": interaction_id,
                "interaction_key": interaction_key,
                "widget_key": (event.get("selected_option") or {}).get("widget_key") or
                    (event.get("field") or {}).get("widget_key"),
                "field_key": None if interaction_key.startswith("widget:") else interaction_key.removeprefix("field:"),
                "started_at": event.get("timestamp"), "last_event_at": event.get("timestamp"),
                "last_received_at": time.monotonic(), "raw_action_ids": [], "status": "open",
            }
            self._interactions[interaction_id] = interaction
            self._active_interaction_by_key[interaction_key] = interaction_id
        interaction["last_event_at"] = event.get("timestamp")
        interaction["last_received_at"] = time.monotonic()
        interaction["raw_action_ids"].append(raw_event["raw_action_id"])
        option = event.get("selected_option") or {}
        if option:
            interaction["option_label"] = option.get("label")
            interaction["option_value"] = option.get("value")
            interaction["option_value_source"] = option.get("value_source") or "unavailable"
            interaction["widget_label"] = option.get("widget_label")
        raw_event["parent_interaction_id"] = interaction_id
        raw_event["interaction_key"] = interaction_key
        self._current_interaction_id = interaction_id
        if event_type == "blur" and not interaction.get("option_label"):
            self._close_interaction(interaction_id, "cancelled")

    def _action_interaction_key(self, action: dict[str, Any]) -> str | None:
        if action.get("widget_key"):
            return f"widget:{action['widget_key']}"
        if action.get("field_key"):
            return f"field:{action['field_key']}"
        form = action.get("form") or {}
        form_key = form.get("id") or form.get("name") or form.get("action")
        return f"form:{form_key}" if form_key else None

    def _take_interaction(self, action: dict[str, Any]) -> tuple[list[str], str | None]:
        key = action.pop("_interaction_key", None) or self._action_interaction_key(action)
        interaction_id = self._active_interaction_by_key.get(key or "")
        interaction = self._interactions.get(interaction_id or "")
        if not interaction or interaction.get("status") != "open":
            return [], None
        raw_ids = list(interaction.get("raw_action_ids", []))
        self._close_interaction(interaction["interaction_id"], "completed")
        return raw_ids, interaction["interaction_id"]

    def _expire_interactions(self) -> None:
        now = time.monotonic()
        for interaction_id, interaction in list(self._interactions.items()):
            if interaction.get("status") == "open" and now - interaction.get("last_received_at", now) >= self._interaction_timeout_seconds:
                self._close_interaction(interaction_id, "cancelled")

    def _serialized_interactions(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in interaction.items() if key != "last_received_at"}
            for interaction in self._interactions.values()
        ]

    def _recent_widget_evidence(self, widget_key: str, timestamp: int | float | None) -> dict[str, Any] | None:
        for interaction in reversed(list(self._interactions.values())):
            if interaction.get("widget_key") != widget_key:
                continue
            event_at = interaction.get("last_event_at") or 0
            if timestamp and event_at and timestamp - event_at > 3000:
                continue
            raw_ids = set(interaction.get("raw_action_ids", []))
            already_attributed = any(
                action.get("type") == "SELECT_OPTION" and action.get("widget_key") == widget_key
                and raw_ids.intersection(action.get("raw_action_ids", []))
                for action in self.actions
            )
            if not already_attributed:
                return interaction
        return None

    @staticmethod
    def _scope_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
        option = event.get("selected_option") or {}
        field = option.get("controller") or event.get("field") or {}
        form = field.get("form") or event.get("form")
        if not isinstance(form, dict) or not any(form.get(key) for key in ("id", "name", "action")):
            return None
        return {
            "formId": form.get("id"),
            "formName": form.get("name"),
            "formAction": form.get("action"),
        }

    def _flush_input(self, locator: str, final_event: dict[str, Any] | None = None) -> None:
        pending = self._pending_inputs.pop(locator, None)
        if pending is None:
            return
        if final_event:
            pending.update(final_event)
        field_key = pending.get("field_key") or self._focused_field_keys.get(locator) or self._field_key_for_event(pending)
        self._record_logical_action({
            "type": "FILL_FIELD",
            "field_key": field_key,
            "field": pending.get("field"),
            "started_at": pending.get("started_at"),
            "event_occurred_at": pending.get("timestamp"),
            "value": safe_field_value(pending.get("value"), pending.get("field") or pending),
            "value_fingerprint": pending.get("value_fingerprint"),
            "raw_action_ids": pending.get("raw_action_ids") or ([self._last_raw_action_id] if self._last_raw_action_id else []),
            "immediate_post_state": pending.get("immediate_post_state"),
            "immediate_page_state": pending.get("immediate_page_state"),
        }, self._scope_from_event(pending))

    def _flush_pending_inputs(self) -> None:
        for locator in list(self._pending_inputs):
            self._flush_input(locator)

    def _flush_expired_inputs(self) -> None:
        now = time.monotonic()
        for locator, pending in list(self._pending_inputs.items()):
            if now - pending.get("last_received_at", now) >= DYNAMIC_CAPTURE_DELAY:
                self._flush_input(locator)

    def _record_select_action(self, event: dict[str, Any], field_key: str | None,
                              scope: dict[str, Any] | None) -> None:
        option = dict(event.get("selected_option") or {})
        controller_event = {"field": option.get("controller")} if option.get("controller") else event
        controller_key = self._field_key_for_event(controller_event) or field_key
        option_label = option.get("label")
        option_value = option.get("value")
        scoped_selector = dict(option.get("scoped_selector") or {})
        if option_value is not None and not scoped_selector.get("option_value"):
            scoped_selector["option_value"] = option_value
        self._record_logical_action({
            "type": "SELECT_OPTION",
            "field_key": controller_key,
            "field": option.get("controller") or event.get("field"),
            "widget_key": option.get("widget_key"),
            "option_label": option_label,
            "option_value": option_value,
            "option_value_source": option.get("value_source") or ("dom" if option.get("value") is not None else "unavailable"),
            "source": "human_interaction",
            "scoped_selector": scoped_selector,
            "event_occurred_at": event.get("timestamp"),
            "raw_action_ids": [self._last_raw_action_id] if self._last_raw_action_id else [],
            "immediate_post_state": event.get("immediate_post_state"),
            "immediate_page_state": event.get("immediate_page_state"),
        }, scope)

    def _record_logical_action(self, action: dict[str, Any], scope: dict[str, Any] | None) -> None:
        action = {key: value for key, value in action.items() if value is not None}
        action["recorded_at"] = datetime.now(timezone.utc).isoformat()
        signature = (
            action.get("type"), action.get("field_key"), action.get("widget_key"),
            action.get("option_label"), action.get("option_value"), action.get("checked"),
        )
        timestamp = action.get("event_occurred_at") or 0
        if signature == self._last_action_signature and timestamp - self._last_action_timestamp <= 350:
            if self.actions:
                previous = self.actions[-1]
                interaction_ids, _interaction_id = self._take_interaction(action)
                previous_ids = previous.setdefault("raw_action_ids", [])
                for raw_id in interaction_ids:
                    if raw_id not in previous_ids:
                        previous_ids.append(raw_id)
                self._link_raw_actions(previous, interaction_ids)
            return
        interaction_ids, interaction_id = self._take_interaction(action)
        explicit_ids = list(action.get("raw_action_ids") or [])
        if interaction_id is None and explicit_ids:
            parents = {
                raw.get("parent_interaction_id") for raw in self.raw_actions
                if raw.get("raw_action_id") in explicit_ids and raw.get("parent_interaction_id")
            }
            if len(parents) == 1:
                interaction_id = next(iter(parents))
        self._semantic_sequence += 1
        action["semantic_sequence"] = self._semantic_sequence
        action["semantic_action_id"] = f"action_{self._semantic_sequence:06d}"
        action["step_id"] = f"step_{self._semantic_sequence:06d}"
        action["parent_interaction_id"] = interaction_id or f"interaction_semantic_{self._semantic_sequence:06d}"
        action["semantic_name"] = self._action_step_name(action)
        action["raw_action_ids"] = list(dict.fromkeys(interaction_ids + explicit_ids))
        action["outcome"] = "unverified"
        action.setdefault("before", self._before_state_for_action(action))
        self.actions.append(action)
        self._link_raw_actions(action, action["raw_action_ids"])
        self._last_action_signature = signature
        self._last_action_timestamp = timestamp
        if action.get("field_key") and getattr(self, "session_state", None):
            action_field = action.get("field") or {}
            value = safe_field_value(action.get("value"), action_field)
            self.session_state.update_form_data(
                action["field_key"], value,
                is_sensitive=is_sensitive_field(action_field),
            )
        self._pending_action_ids.append(action["semantic_action_id"])
        self._schedule_capture("action")

    def _before_state_for_action(self, action: dict[str, Any]) -> dict[str, Any]:
        schema = self.last_schema or {}
        widget_key = action.get("widget_key")
        if widget_key:
            widget = next((item for item in schema.get("widgets", []) if item.get("widget_key") == widget_key), None)
            if widget:
                return {
                    "selected_values": widget.get("selected_values") or [],
                    "semantic_value": widget.get("semantic_value") or widget.get("selected_labels") or [],
                    "expanded": widget.get("expanded"),
                    "value_present": widget.get("value_present", bool(widget.get("selected_labels"))),
                }
        field_key = action.get("field_key")
        field = next((item for item in schema.get("fields", []) if item.get("field_key") == field_key), None)
        if field:
            return {
                "value_present": field.get("value_present", False),
                "checked": field.get("checked"),
                "selected_values": field.get("selected_values") or [],
            }
        return {}

    def _link_raw_actions(self, action: dict[str, Any], raw_ids: list[str]) -> None:
        wanted = set(raw_ids)
        for raw in reversed(self.raw_actions):
            if raw.get("raw_action_id") in wanted:
                raw["semantic_action_id"] = action.get("semantic_action_id")
                raw["step_id"] = action.get("step_id")
                raw["parent_interaction_id"] = action.get("parent_interaction_id")
                wanted.discard(raw.get("raw_action_id"))
                if not wanted:
                    break

    def _field_key_for_event(self, event: dict[str, Any]) -> str | None:
        """Link an interaction to the latest known schema field when possible."""
        fields = (getattr(self, "last_schema", None) or {}).get("fields", [])
        field_hint = event.get("field") or {}
        xpath = field_hint.get("xpath") or event.get("xpath")
        if xpath:
            matches = [field for field in fields if field.get("xpath") == xpath]
            if len(matches) == 1:
                return matches[0].get("field_key") or get_field_identity(matches[0])
        for attribute in ("id", "name"):
            value = field_hint.get(attribute) or event.get(attribute)
            if value:
                matches = [field for field in fields if field.get(attribute) == value]
                if len(matches) == 1:
                    return matches[0].get("field_key") or get_field_identity(matches[0])
        if field_hint:
            form = field_hint.get("form") or {}
            provisional = {
                "tag": str(field_hint.get("tag") or event.get("tag") or "").lower(),
                "inputType": field_hint.get("inputType") or event.get("inputType"),
                "id": field_hint.get("id"), "name": field_hint.get("name"),
                "label": field_hint.get("label"), "xpath": xpath, "frame_path": "main",
                "form": {
                    "formId": form.get("id"), "formName": form.get("name"),
                    "formAction": form.get("action"),
                },
            }
            return get_field_identity(provisional)
        return None

    def _on_navigation(self, frame):
        if self.page is not None and frame == self.page.main_frame and self._initial_captured:
            # Update session navigation history
            if self.session_state:
                self.session_state.add_navigation(frame.url)
                self.session_manager.persist_session(self.session_id)
            self._schedule_capture("navigation", {"url": frame.url})

    @staticmethod
    def _safe_name(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._")
        return value[:80] or "unknown"

    def _collect_existing_data(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Summarize safe, currently visible values already present on the page.

        This keeps the action stream separate while still preserving the page's
        current state in the same recording timeline. Sensitive values are
        intentionally excluded using the same field classification rules used for
        action capture.
        """
        values: list[dict[str, Any]] = []
        for field in schema.get("fields", []) or []:
            if not isinstance(field, dict):
                continue
            if not field.get("value_present"):
                continue
            if is_sensitive_field(field) and not getattr(self, "capture_sensitive_values", False):
                continue
            field_key = field.get("field_key") or field.get("xpath")
            if not field_key:
                continue
            value = field.get("value")
            if value is None:
                continue
            values.append({
                "field_key": field_key,
                "label": field.get("label") or field.get("semantic_label") or field.get("name") or field.get("id"),
                "input_type": field.get("inputType") or field.get("type"),
                "value": value,
                "value_fingerprint": field.get("value_fingerprint"),
                "visible": bool(field.get("visible")),
                "scope": field.get("form") or schema.get("scope"),
            })

        widgets: list[dict[str, Any]] = []
        for widget in schema.get("widgets", []) or []:
            if not isinstance(widget, dict):
                continue
            selected_labels = widget.get("selected_labels") or widget.get("semantic_value") or []
            if not selected_labels:
                continue
            widgets.append({
                "widget_key": widget.get("widget_key"),
                "label": widget.get("label") or widget.get("widget_label"),
                "selected_labels": selected_labels,
                "selected_values": widget.get("selected_values", []),
                "controller_field_key": widget.get("controller_field_key"),
            })

        return {
            "captured_at": schema.get("timestamp"),
            "step": schema.get("step"),
            "change_type": schema.get("change_type"),
            "total_values": len(values),
            "total_widgets": len(widgets),
            "values": values,
            "widgets": widgets,
        }

    def _capture_schema(self, step_name, change_type=None, change_details=None):
        """Capture schema with retry logic for error handling."""
        schema = self._execute_with_retry(
            lambda: extract_form_schema(
                self.page, step_name, self.page.url, change_type, change_details,
                debug_capture_values=self.debug_capture_values,
                capture_sensitive_values=self.capture_sensitive_values,
                active_scope=self._active_scope,
                prefer_form_scope=self.category.casefold() == "registration",
            ),
            operation_name=f"capture_schema:{step_name}"
        )
        if schema is None:
            return None
        action = (change_details or {}).get("action") if isinstance(change_details, dict) else None
        if isinstance(action, dict):
            self._apply_action_result(schema, action)
            schema["step_id"] = action.get("step_id")
            schema["semantic_action_id"] = action.get("semantic_action_id")
            schema["semantic_sequence"] = action.get("semantic_sequence")
            schema["action"] = action

        self._annotate_page_content(schema)
        schema["existing_data"] = self._collect_existing_data(schema)
        filename = f"step_{self.step_counter:02d}_{self._safe_name(step_name)}.json"
        write_json(os.path.join(self.session_dir, filename), schema)
        self._captured_schemas.append(schema)
        fields = schema.get("fields", [])
        visible = sum(1 for field in fields if field.get("visible"))
        
        # Update session state with form data (non-sensitive only)
        if self.session_state and fields:
            self._update_session_form_data(fields)
        
        print(
            f"[Progress] Step {self.step_counter:02d}: {step_name} | "
            f"change={schema.get('change_type')} | fields={len(fields)} | "
            f"visible={visible} | hidden={len(fields) - visible} | "
            f"total_actions={len(self.actions)}"
        )
        self.step_counter += 1
        self.current_step_name = step_name
        
        # Capture session state after schema capture
        self._capture_session_state()
        
        return schema

    def _annotate_page_content(self, schema: dict[str, Any]) -> None:
        """Attach deterministic initial/add/change/remove semantics to content."""
        content = schema.get("page_content")
        if not isinstance(content, dict):
            return
        records = [item for item in content.get("records", []) if isinstance(item, dict)]
        previous_content = (self.last_schema or {}).get("page_content") or {}
        same_page = (self.last_schema or {}).get("url") == schema.get("url")
        previous_records = {
            item.get("content_key"): item
            for item in previous_content.get("records", [])
            if isinstance(item, dict) and item.get("content_key")
        } if same_page else {}
        current_records = {
            item.get("content_key"): item for item in records if item.get("content_key")
        }
        changes: list[dict[str, Any]] = []
        for record in records:
            key = record.get("content_key")
            before = previous_records.get(key)
            if not same_page:
                record["change_status"] = "initial"
                changes.append(dict(record))
            elif before is None:
                record["change_status"] = "added"
                changes.append(dict(record))
            elif self._content_record_state(before) != self._content_record_state(record):
                record["change_status"] = "changed"
                record["previous_text"] = before.get("text")
                changed = dict(record)
                changed["previous"] = self._content_record_state(before)
                changed["current"] = self._content_record_state(record)
                changes.append(changed)
            else:
                record["change_status"] = "unchanged"
        for key, before in previous_records.items():
            if key not in current_records:
                removed = dict(before)
                removed["change_status"] = "removed"
                removed["removed_at"] = content.get("captured_at")
                changes.append(removed)
        content["records"] = records
        content["changes"] = changes
        content["content_changed"] = bool(changes)

    @staticmethod
    def _content_record_state(record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            record.get("text"), record.get("headers"), record.get("rows"),
            record.get("items"), record.get("url"), record.get("src"),
            record.get("alt"), record.get("title"),
        )

    def _page_content_timeline(self) -> list[dict[str, Any]]:
        """Return content snapshots once per distinct URL/content state."""
        timeline: list[dict[str, Any]] = []
        previous_identity: tuple[Any, Any] | None = None
        for schema in self._captured_schemas:
            content = schema.get("page_content") if isinstance(schema, dict) else None
            if not isinstance(content, dict):
                continue
            identity = (content.get("url"), content.get("fingerprint"))
            if identity == previous_identity:
                continue
            timeline.append({
                **content,
                "step": schema.get("step"),
                "change_type": schema.get("change_type"),
            })
            previous_identity = identity
        return timeline

    @staticmethod
    def _normalise_option(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("value") if value.get("value") is not None else value.get("label")
        return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

    def _apply_action_result(self, schema: dict[str, Any], action: dict[str, Any]) -> None:
        """Verify an action and make its event-time state authoritative.

        The event listener captures this compact state in the browser microtask
        belonging to the physical interaction.  It therefore cannot contain a
        later human action, even when the full stable snapshot is taken later.
        """
        immediate = action.get("immediate_post_state") or {}
        immediate_page = action.get("immediate_page_state") or {}
        next_human = self._next_human_raw_boundary(action)
        stable_timestamp = schema.get("timestamp")
        contaminated = bool(
            next_human and stable_timestamp is not None
            and stable_timestamp >= (next_human.get("timestamp") or float("inf"))
        )
        target_widget = next(
            (widget for widget in schema.get("widgets", [])
             if action.get("widget_key") and widget.get("widget_key") == action.get("widget_key")),
            None,
        )
        stable_selected = list(
            (target_widget or {}).get("selected_values")
            or (target_widget or {}).get("selected_labels")
            or []
        )
        field_states = {
            field.get("xpath"): field for field in immediate_page.get("fields", []) if field.get("xpath")
        }
        for field in schema.get("fields", []):
            owned_state = field_states.get(field.get("xpath"))
            if not owned_state:
                continue
            for key in ("value", "value_present", "value_fingerprint", "checked", "selected_values"):
                if key in owned_state:
                    field[key] = owned_state[key]
            field["state_source"] = "immediate_event_state"
        widget_states = {
            widget.get("controller_xpath"): widget
            for widget in immediate_page.get("widgets", []) if widget.get("controller_xpath")
        }
        for widget in schema.get("widgets", []):
            owned_state = widget_states.get(widget.get("controller_xpath"))
            if owned_state:
                widget["stable_observed_selected_labels"] = widget.get("selected_labels", [])
                owned_selected = owned_state.get("selected_values", [])
                widget["selected_values"] = owned_selected
                widget["selected_labels"] = [
                    item.get("label") or item.get("value") if isinstance(item, dict) else item
                    for item in owned_selected
                ]
                widget["semantic_value"] = owned_state.get("semantic_value") or widget["selected_labels"]
                widget["value_present"] = owned_state.get("value_present", False)
                widget["expanded"] = owned_state.get("expanded", False)
                widget["state_source"] = "immediate_event_state"
        action["before"] = action.get("before") or {}
        action["after"] = immediate
        action_type = action.get("type")
        expected: Any = None
        actual: Any = None
        method = "immediate_post_state"
        if action_type == "SELECT_OPTION":
            expected = action.get("option_value") if action.get("option_value") is not None else action.get("option_label")
            selected = immediate.get("selected_values") or []
            used_stable_state = False
            if not selected and stable_selected and not contaminated:
                selected = stable_selected
                used_stable_state = True
                action["after"] = {
                    **immediate,
                    "selected_values": selected,
                    "semantic_value": [
                        item.get("label") or item.get("value") if isinstance(item, dict) else item
                        for item in selected
                    ],
                    "value_present": True,
                    "state_source": "stable_post_state",
                }
            actual_values: list[str] = []
            for value in selected:
                if isinstance(value, dict):
                    actual_values.extend(
                        self._normalise_option(candidate)
                        for candidate in (value.get("value"), value.get("label")) if candidate is not None
                    )
                else:
                    actual_values.append(self._normalise_option(value))
            expected_values = {
                self._normalise_option(candidate)
                for candidate in (action.get("option_value"), action.get("option_label")) if candidate is not None
            }
            matched = bool(expected_values.intersection(actual_values))
            if matched:
                action["outcome"] = "success"
            elif "selected_values" in immediate:
                action["outcome"] = "failed"
            else:
                action["outcome"] = "unverified"
            actual = selected
            method = "stable_widget_selected_values" if used_stable_state else "widget_selected_values"
            for widget in schema.get("widgets", []):
                if action.get("widget_key") and widget.get("widget_key") == action.get("widget_key"):
                    widget.setdefault("stable_observed_selected_labels", widget.get("selected_labels", []))
                    widget["selected_values"] = selected
                    widget["selected_labels"] = [
                        item.get("label") or item.get("value") if isinstance(item, dict) else item
                        for item in selected
                    ]
                    widget["semantic_value"] = widget["selected_labels"]
                    widget["value_present"] = bool(selected)
                    widget["state_source"] = "immediate_event_state"
        elif action_type in {"SET_CHECKBOX", "SET_RADIO"}:
            expected = bool(action.get("checked"))
            actual = immediate.get("checked")
            action["outcome"] = "success" if actual is expected else ("failed" if actual is not None else "unverified")
            method = "checked_state"
        elif action_type == "FILL_FIELD":
            expected = True
            actual = immediate.get("value_present")
            action["outcome"] = "success" if actual else ("failed" if actual is False else "unverified")
            method = "value_present"
        elif action_type in {"SUBMIT_FORM", "CLICK_ACTION", "OPEN_WIDGET", "CLICK"}:
            action["outcome"] = "unverified"
        elif action_type in {"STATE_CHANGE", "FORM_TRANSITION", "PAGE_TRANSITION"}:
            expected = action.get("change_kind")
            actual = action.get("change_kind") if immediate else None
            action["outcome"] = "success" if actual else "unverified"
            method = "state_diff"
        action["verification"] = {"method": method, "expected": expected, "actual": actual}
        schema["immediate_post_state"] = immediate
        schema["snapshot_authority"] = "immediate_event_state" if immediate_page else "stable_dom"
        schema["stable_post_state"] = {
            "captured": not contaminated,
            "source": "stable_dom",
            "may_include_later_ui_state": contaminated,
            "causal_boundary": {
                "action_timestamp": action.get("event_occurred_at"),
                "next_human_raw_action_id": (next_human or {}).get("raw_action_id"),
                "next_human_timestamp": (next_human or {}).get("timestamp"),
                "stable_state_timestamp": stable_timestamp,
            },
        }
        classification = (action.get("field") or {}).get("classification")
        if classification is None:
            target_keys = {action.get("field_key"), action.get("widget_key")}
            target_field = next(
                (field for field in schema.get("fields", []) if field.get("field_key") in target_keys), None
            )
            classification = (target_field or {}).get("classification")
        supported_matcher_types = {"FILL_FIELD", "SELECT_OPTION", "SET_CHECKBOX", "SET_RADIO", "UPLOAD_FILE"}
        required_value_known = {
            "FILL_FIELD": actual is True,
            "SELECT_OPTION": bool(action.get("option_label") or action.get("option_value")),
            "SET_CHECKBOX": action.get("checked") is not None,
            "SET_RADIO": action.get("checked") is not None,
            "UPLOAD_FILE": bool(action.get("file_present")),
        }.get(action_type, False)
        action["matcher_training_eligible"] = bool(
            action_type in supported_matcher_types
            and action.get("outcome") == "success"
            and actual is not None
            and classification == "user_field"
            and (action.get("field_key") or action.get("widget_key"))
            and required_value_known
            and not contaminated
        )
        action["page_state_training_eligible"] = bool(
            action_type in {"FORM_TRANSITION", "PAGE_TRANSITION"} and action.get("outcome") == "success"
            and not contaminated
        )

    def _next_human_raw_boundary(self, action: dict[str, Any]) -> dict[str, Any] | None:
        human_types = {"pointerdown", "click", "focus", "keydown", "input", "change"}
        action_raw_ids = set(action.get("raw_action_ids", []))
        action_sequences = [
            raw.get("raw_sequence") or 0 for raw in self.raw_actions
            if raw.get("raw_action_id") in action_raw_ids
        ]
        max_sequence = max(action_sequences, default=0)
        action_timestamp = action.get("event_occurred_at") or 0
        for raw in self.raw_actions:
            if raw.get("type") not in human_types or raw.get("raw_action_id") in action_raw_ids:
                continue
            if max_sequence and (raw.get("raw_sequence") or 0) > max_sequence:
                return raw
            if not max_sequence and (raw.get("timestamp") or 0) > action_timestamp:
                return raw
        return None

    @staticmethod
    def _freeze_signature_value(value: Any) -> Any:
        """Convert nested browser state into deterministic hashable values."""
        if isinstance(value, dict):
            return tuple(sorted(
                (str(key), Recorder._freeze_signature_value(item))
                for key, item in value.items()
            ))
        if isinstance(value, (list, tuple)):
            return tuple(Recorder._freeze_signature_value(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(Recorder._freeze_signature_value(item) for item in value))
        return value

    @staticmethod
    def _signature(schema):
        """Generate a schema signature for change detection.
        
        Excludes step name to enable cross-step field comparisons.
        Includes captured value state so autofill and hydration changes are
        captured even when no browser interaction event is emitted.
        """
        signature = Counter((get_field_identity(field), (
            *field_state(field),
            bool(field.get("value_present")),
            field.get("value_fingerprint"),
            bool(field.get("checked")) if field.get("checked") is not None else None,
            Recorder._freeze_signature_value(field.get("semantic_value") or []),
        )) for field in schema.get("fields", []))
        for widget in schema.get("widgets", []):
            identity = "widget|{}|{}".format(
                widget.get("frame_path"), widget.get("widget_key"),
            )
            signature[(identity, (
                bool(widget.get("expanded")), bool(widget.get("disabled")),
                Recorder._freeze_signature_value(
                    widget.get("semantic_value") or widget.get("selected_labels") or []
                ),
            ))] += 1
        page_content = schema.get("page_content") or {}
        signature[("page_content", schema.get("url")), (
            page_content.get("fingerprint"),
        )] += 1
        return signature

    def _wait_for_stable_dom(self) -> dict[str, Any] | None:
        """Wait for document readiness and a quiet mutation window."""
        if self.page is None:
            return None
        try:
            result = self.page.evaluate(WAIT_FOR_STABLE_DOM_SCRIPT, {
                "quietMs": DOM_STABILITY_QUIET_MS,
                "timeoutMs": DOM_STABILITY_TIMEOUT_MS,
            })
            if result and result.get("timed_out"):
                logger.warning("DOM stability wait timed out: %s", result)
            return result
        except Exception as exc:
            # Navigation may replace the execution context once more. The
            # capture retry still provides a safe fallback.
            logger.debug("DOM stability wait was interrupted: %s", exc)
            return None

    def _set_baseline(self, schema):
        self._last_signature = self._signature(schema)
        self.last_fields_set = {identity for identity, _state in self._last_signature}
        self.last_schema = schema
        self._last_url = schema.get("url")
        
        # Update session state with current URL
        if self.session_state and schema.get("url"):
            self.session_state.current_url = schema["url"]

    @staticmethod
    def _form_fingerprint(schema: dict[str, Any] | None) -> tuple[Any, ...]:
        schema = schema or {}
        scope = schema.get("scope") or {}
        eligible_fields = []
        for field in schema.get("fields", []):
            if field.get("classification") not in {None, "user_field"}:
                continue
            if field.get("type") == "static" or not field.get("visible", True):
                continue
            eligible_fields.append(field)
        form_identities = []
        for field in eligible_fields:
            form = field.get("form") or {}
            identity = (form.get("formId"), form.get("formName"), form.get("formAction"))
            if any(identity):
                form_identities.append(identity)
        primary_form = Counter(form_identities).most_common(1)[0][0] if form_identities else (
            scope.get("formId"), scope.get("formName"), scope.get("formAction")
        )
        user_fields = sorted({
            re.sub(r"\s+", " ", str(
                field.get("semantic_label") or field.get("label") or field.get("name") or field.get("field_key")
            ).strip()).casefold()
            for field in eligible_fields
            if field.get("semantic_label") or field.get("label") or field.get("name") or field.get("field_key")
        })
        headings = sorted({
            re.sub(r"\s+", " ", str(field.get("textContent") or "").strip()).casefold()
            for field in schema.get("fields", [])
            if field.get("type") == "static" and field.get("visible", True) and field.get("textContent")
        })
        return (
            *primary_form, tuple(user_fields), tuple(headings)
        )

    @classmethod
    def detect_state_transition(
        cls, previous: dict[str, Any] | None, current: dict[str, Any]
    ) -> str | None:
        """Detect major same-URL form changes independently of navigation."""
        if not previous or previous.get("url") != current.get("url"):
            return None
        before = cls._form_fingerprint(previous)
        after = cls._form_fingerprint(current)
        if before == after:
            return None
        before_fields, after_fields = set(before[3]), set(after[3])
        union = before_fields | after_fields
        similarity = len(before_fields & after_fields) / len(union) if union else 1.0
        identity_changed = before[:3] != after[:3]
        structural_delta = len(before_fields ^ after_fields)
        if identity_changed and structural_delta > 0 and similarity < 0.8:
            return "FORM_TRANSITION"
        if min(len(before_fields), len(after_fields)) >= 2 and structural_delta >= 3 and similarity < 0.35:
            return "FORM_TRANSITION"
        return None

    def _record_implicit_changes(self, candidate: dict[str, Any], triggering_action: dict[str, Any] | None) -> None:
        previous = self.last_schema
        if not previous:
            return
        transition = self.detect_state_transition(previous, candidate)
        if transition and not (triggering_action or {}).get("change_kind"):
            self._record_logical_action({
                "type": transition, "change_kind": transition,
                "raw_action_ids": [], "event_occurred_at": candidate.get("timestamp"),
                "immediate_post_state": {"before": self._form_fingerprint(previous), "after": self._form_fingerprint(candidate)},
            }, candidate.get("scope"))

        previous_fields = {field.get("field_key"): field for field in previous.get("fields", []) if field.get("field_key")}
        action_field_key = (triggering_action or {}).get("field_key")
        action_widget_key = (triggering_action or {}).get("widget_key")
        from core.utils import is_sensitive_field
        for field in candidate.get("fields", []):
            field_key = field.get("field_key")
            before = previous_fields.get(field_key)
            if not before or field_key in {action_field_key, action_widget_key}:
                continue
            if field.get("classification") not in {None, "user_field"}:
                continue
            if field.get("type") == "widget_reference":
                before_values = before.get("semantic_value") or []
                after_values = field.get("semantic_value") or []
                if before_values != after_values:
                    evidence = self._recent_widget_evidence(field.get("widget_key") or field_key, candidate.get("timestamp"))
                    if evidence:
                        new_labels = [
                            value.get("label") if isinstance(value, dict) else value
                            for value in (field.get("selected_values") or after_values)
                        ]
                        if not evidence.get("option_label") and not new_labels:
                            continue
                        option_label = evidence.get("option_label") or next(
                            (label for label in new_labels if label not in before_values),
                            new_labels[-1] if new_labels else None,
                        )
                        self._record_logical_action({
                            "type": "SELECT_OPTION", "source": "human_interaction",
                            "widget_key": field.get("widget_key") or field_key,
                            "field_key": field_key,
                            "field": {
                                "label": field.get("semantic_label") or field.get("label"),
                                "classification": "user_field",
                            },
                            "option_label": option_label,
                            "option_value": evidence.get("option_value"),
                            "option_value_source": evidence.get("option_value_source") or "unavailable",
                            "raw_action_ids": list(evidence.get("raw_action_ids", [])),
                            "event_occurred_at": evidence.get("last_event_at") or candidate.get("timestamp"),
                            "immediate_post_state": {
                                "kind": "multiselect", "selected_values": field.get("selected_values") or
                                    [{"label": label, "value": None, "value_source": "unavailable"} for label in after_values],
                                "semantic_value": after_values, "value_present": bool(after_values),
                            },
                        }, candidate.get("scope"))
                        evidence["status"] = "completed"
                        evidence["attribution"] = "semantic_state_diff"
                        continue
            if not before.get("value_present") and field.get("value_present"):
                sensitive = is_sensitive_field(field)
                self._record_logical_action({
                    "type": "STATE_CHANGE", "change_kind": "VALUE_CHANGED",
                    "source": "autofill_or_programmatic", "field_key": field_key,
                    "field": {
                        "label": field.get("semantic_label") or field.get("label"),
                        "name": field.get("name"), "inputType": field.get("inputType"),
                    },
                    "sensitive": sensitive, "raw_action_ids": [],
                    "event_occurred_at": candidate.get("timestamp"),
                    "immediate_post_state": {
                        "value_present": True, "sensitive": sensitive,
                        **({} if sensitive else {"value_fingerprint": field.get("value_fingerprint")}),
                    },
                }, candidate.get("scope"))

    def _get_field_identity(self, field):
        """Compatibility wrapper for callers using the recorder helper API."""
        return get_field_identity(field)
    
    def _execute_with_retry(self, operation, operation_name="operation", max_retries=None, delay=None):
        """Execute an operation with retry logic for error handling.
        
        Args:
            operation: Callable to execute
            operation_name: Name for logging purposes
            max_retries: Maximum number of retries (uses instance default if None)
            delay: Delay between retries in seconds (uses instance default if None)
            
        Returns:
            Result of the operation or None if all retries failed
        """
        max_retries = max_retries if max_retries is not None else self._max_retries
        delay = delay if delay is not None else self._retry_delay
        
        last_exception = None
        for attempt in range(max_retries + 1):
            try:
                return operation()
            except Exception as exc:
                last_exception = exc
                logger.warning(
                    f"{operation_name} failed (attempt {attempt + 1}/{max_retries + 1}): {exc}"
                )
                if attempt < max_retries:
                    time.sleep(delay)
        
        logger.error(f"{operation_name} failed after {max_retries + 1} attempts: {last_exception}")
        return None
    
    def _capture_session_state(self):
        """Capture cookies, localStorage, and sessionStorage from the page."""
        if not self.page or not self.session_state:
            return
        
        try:
            # Extract cookies
            self.session_manager.extract_cookies_from_playwright(self.page, self.session_id)
            
            # Extract storage
            self.session_manager.extract_storage_from_playwright(self.page, self.session_id)
            
            logger.debug(f"Captured session state for {self.session_id}")
        except Exception as exc:
            logger.warning(f"Failed to capture session state: {exc}")
    
    def _update_session_form_data(self, fields):
        """Update session form data from schema fields (non-sensitive only).
        
        Args:
            fields: List of field dictionaries from schema
        """
        if not self.session_state or not fields:
            return
        
        for field in fields:
            field_key = field.get("field_key")
            if not field_key:
                continue
            
            # Check if field has a value present
            if field.get("value_present"):
                is_sensitive = is_sensitive_field(field)
                captured_value = (
                    field.get("value") if getattr(self, "capture_sensitive_values", False)
                    else safe_field_value(field.get("value"), field)
                )
                self.session_state.update_form_data(
                    field_key,
                    captured_value,
                    is_sensitive=is_sensitive,
                    persist_sensitive=getattr(self, "capture_sensitive_values", False),
                )

    def _schedule_capture(self, change_type=None, details=None):
        self._pending_reasons.add(change_type or "dynamic")
        if details:
            for key, value in details.items():
                self._pending_details[key] = value
        self._pending_deadline = time.monotonic() + DYNAMIC_CAPTURE_DELAY

    def _change_type(self) -> str:
        priority = ["navigation", "action", "row_add", "row", "new_fields", "dynamic"]
        return next((name for name in priority if name in self._pending_reasons), "dynamic")

    def _next_pending_action(self) -> dict[str, Any] | None:
        if not self._pending_action_ids:
            return None
        action_id = self._pending_action_ids[0]
        return next((action for action in self.actions if action.get("semantic_action_id") == action_id), None)

    @staticmethod
    def _action_step_name(action: dict[str, Any]) -> str:
        action_type = str(action.get("type") or "action").lower()
        field = action.get("field") or {}
        field_key = str(action.get("field_key") or "")
        semantic = (
            field.get("label") or field.get("name") or field.get("id") or
            action.get("widget_key") or action.get("option_label")
        )
        if not semantic and field_key:
            locator = next((part for part in field_key.split("|") if part.startswith(("id:", "name:"))), "")
            semantic = locator.split(":", 1)[-1] if locator else "field"
        verbs = {
            "fill_field": "enter", "select_option": "select", "set_checkbox": "set",
            "set_radio": "set", "click_action": "click", "open_widget": "open", "submit_form": "submit",
        }
        semantic_slug = re.sub(r"[^a-z0-9]+", "_", str(semantic or "form").casefold()).strip("_")
        return f"{verbs.get(action_type, action_type)}_{semantic_slug or 'form'}"

    def _step_name(self, change_type, details):
        action = details.get("action") if isinstance(details, dict) else None
        if isinstance(action, dict):
            base = self._action_step_name(action)
        elif change_type == "navigation":
            base = f"nav_{self.step_counter}"
        elif change_type in {"row", "row_add"}:
            row_changes = details.get("changes") or details.get("rowChanges") or []
            index = row_changes[0].get("index") if row_changes and isinstance(row_changes[0], dict) else details.get("row_index")
            key = row_changes[0].get("rowKey") if row_changes and isinstance(row_changes[0], dict) else None
            prefix = "row_add" if change_type == "row_add" else "row"
            suffix = "_".join(str(part) for part in (key, index) if part not in (None, ""))
            base = f"{prefix}_{suffix or self.step_counter}"
        elif change_type == "panel":
            base = f"panel_{details.get('panel_id') or details.get('panelId') or self.step_counter}"
        elif change_type == "visibility":
            changes = details.get("changes") or details.get("visibilityChanges") or []
            field_id = changes[0].get("id") if changes and isinstance(changes[0], dict) else details.get("field_id")
            base = f"field_{field_id or self.step_counter}"
        else:
            base = f"dynamic_{self.step_counter}"
        return self._safe_name(base)

    def _check_and_capture_schema(self, change_type=None, change_details=None, force=False):
        if self.page is None:
            return False
        details = change_details or dict(self._pending_details)
        stability = None if details.get("source") == "state_audit" else self._wait_for_stable_dom()
        if stability:
            details = dict(details)
            details["dom_stability"] = stability
        schema = extract_form_schema(
            self.page, "temp", self.page.url,
            debug_capture_values=self.debug_capture_values,
            capture_sensitive_values=self.capture_sensitive_values,
            active_scope=self._active_scope,
            prefer_form_scope=self.category.casefold() == "registration",
        )
        triggering_action = details.get("action") if isinstance(details, dict) else None
        self._record_implicit_changes(schema, triggering_action if isinstance(triggering_action, dict) else None)
        signature = self._signature(schema)
        changed = (
            force or change_type == "action" or "action" in details or
            self._last_signature is None or signature != self._last_signature or schema["url"] != self._last_url
        )
        if not changed:
            return False
        change_type = change_type or self._change_type()
        step_name = self._step_name(change_type, details)
        captured = self._capture_schema(step_name, change_type, details)
        if captured is None:
            return False
        self._set_baseline(captured)
        action = details.get("action") if isinstance(details, dict) else None
        if isinstance(action, dict) and self._pending_action_ids:
            if self._pending_action_ids[0] == action.get("semantic_action_id"):
                self._pending_action_ids.pop(0)
        return True

    def _consume_observer_changes(self):
        merged: dict[str, list[Any]] = {key: [] for key in (
            "newFields", "removedFields", "visibilityChanges", "rowChanges",
            "panelChanges", "newTextContent", "removedTextContent",
        )}
        if self.page is None:
            return merged
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = [self.page]
        for frame in frames:
            try:
                changes = frame.evaluate("() => window.__recorderConsumeDomChanges ? window.__recorderConsumeDomChanges() : null")
            except Exception:
                continue
            if not changes:
                continue
            for key in merged:
                for item in changes.get(key, []):
                    # Use circular buffer to limit memory growth
                    buffer = self._dom_change_buffers[key]
                    if len(buffer) >= self._max_dom_history:
                        # Remove oldest entry (circular buffer behavior)
                        buffer.pop(0)
                    if item not in buffer:
                        buffer.append(item)
                        merged[key].append(item)
        return merged

    def _clear_observer_changes(self):
        self._consume_observer_changes()

    def _drain_events(self):
        """Compatibility hook; sync Playwright delivers exposed callbacks while pumping."""
        return None

    def _poll_and_capture(self):
        """Poll for DOM changes and capture schemas with thread-safe page access.
        
        Implements periodic auto-save to prevent data loss on crashes.
        Uses adaptive polling to optimize performance based on activity level.
        """
        start_time = time.monotonic()
        while self._running and not self._stop_event.is_set():
            self._drain_events()
            self._flush_expired_inputs()
            self._expire_interactions()
            now = time.monotonic()
            if now - self._last_state_audit >= self._state_audit_interval and self.page is not None:
                self._check_and_capture_schema("dynamic", {"source": "state_audit"})
                self._last_state_audit = now
            with self._page_lock:  # Thread-safe page access
                changes = self._consume_observer_changes()
            
            has_changes = any([
                changes["newFields"],
                changes["removedFields"],
                changes["visibilityChanges"],
                changes["panelChanges"],
                changes["rowChanges"],
                changes["newTextContent"],
                changes["removedTextContent"],
            ])
            
            if changes["newFields"]:
                self._schedule_capture("new_fields", {"count": len(changes["newFields"]), "fields": changes["newFields"]})
            if changes["removedFields"]:
                self._schedule_capture("dynamic", {"removed": changes["removedFields"]})
            if changes["rowChanges"]:
                self._schedule_capture("row_add", {"changes": changes["rowChanges"]})
            if self._pending_deadline is not None and time.monotonic() >= self._pending_deadline:
                reason = self._change_type()
                details = dict(self._pending_details)
                pending_action = self._next_pending_action()
                if pending_action is not None:
                    reason = "action"
                    details["action"] = pending_action
                self._pending_deadline = None
                self._pending_reasons.clear()
                self._pending_details.clear()
                self._check_and_capture_schema(reason, details, force=reason == "action")
                if self._pending_action_ids:
                    self._pending_reasons.add("action")
                    self._pending_deadline = time.monotonic()
            
            # Session recovery: periodic incremental save of actions.json
            elapsed = time.monotonic() - start_time
            if elapsed - self._last_auto_save >= self._auto_save_interval and self.actions:
                self._save_actions_incremental()
                self._last_auto_save = elapsed
            
            # Adaptive polling: adjust interval based on activity
            if self._adaptive_poll_enabled:
                if has_changes:
                    # More activity = faster polling
                    self._consecutive_no_changes = 0
                    self._current_poll_interval = max(self._min_poll_interval, 
                                                       self._current_poll_interval * 0.8)
                else:
                    # No activity = slower polling to save resources
                    self._consecutive_no_changes += 1
                    if self._consecutive_no_changes > 5:
                        self._current_poll_interval = min(self._max_poll_interval,
                                                           self._current_poll_interval * 1.1)
            
            self.page.wait_for_timeout(max(1, int(self._current_poll_interval * 1000)))
    
    def _save_actions_incremental(self):
        """Save actions incrementally to prevent data loss on crashes.
        
        This creates a backup file that can be recovered if the recorder crashes
        before the final save.
        """
        try:
            backup_path = os.path.join(self.session_dir, "actions_backup.json")
            end_time = datetime.now(timezone.utc).isoformat()
            recording_duration = (datetime.fromisoformat(end_time.replace('Z', '+00:00')) - 
                                 datetime.fromisoformat(self._recording_started_at.replace('Z', '+00:00'))).total_seconds()
            
            write_json(backup_path, {
                "session_id": self.session_id,
                "category": self.category,
                "university": self.university,
                "start_time": self._recording_started_at,
                "last_backup_time": end_time,
                "recording_duration_seconds": recording_duration,
                "total_actions": len(self.actions),
                "total_steps": self.step_counter,
                "backup_number": self._auto_save_counter,
                "actions": self.actions,
                "raw_actions": self.raw_actions,
                "interactions": self._serialized_interactions(),
                "page_content": self._page_content_timeline(),
            })
            self._auto_save_counter += 1
            logger.debug(f"Auto-saved {len(self.actions)} actions to {backup_path}")
        except Exception as exc:
            logger.warning(f"Failed to auto-save actions: {exc}")

    def stop(self):
        self._ensure_action_state()
        self._flush_pending_inputs()
        for interaction_id in list(self._interactions):
            self._close_interaction(interaction_id, "cancelled")
        has_page_content = any(
            (schema.get("page_content") or {}).get("records")
            for schema in getattr(self, "_captured_schemas", [])
            if isinstance(schema, dict)
        )
        if not self._running and self.browser is None and self._playwright is None:
            self._profile_lock.release()
            if not self.actions and not has_page_content and os.path.isdir(self.session_dir):
                self._discard_session()
            return
        self._running = False
        self._stop_event.set()
        if not self.actions and not has_page_content:
            self._close_runtime()
            self._discard_session()
            print("No actions or page content recorded; empty session discarded.")
            return
        
        # Capture any pending schema changes before saving
        with self._page_lock:
            if self._pending_deadline is not None and self.page is not None:
                try:
                    while self._pending_action_ids:
                        action = self._next_pending_action()
                        if action is None:
                            self._pending_action_ids.pop(0)
                            continue
                        if not self._check_and_capture_schema("action", {"action": action}, force=True):
                            break
                    if self._pending_reasons - {"action"}:
                        reason = self._change_type()
                        self._check_and_capture_schema(reason, dict(self._pending_details))
                except Exception as exc:
                    logger.warning(f"Failed to capture final schema: {exc}")
        
        # Save actions with metadata
        end_time = datetime.now(timezone.utc).isoformat()
        recording_duration = (datetime.fromisoformat(end_time.replace('Z', '+00:00')) - 
                             datetime.fromisoformat(self._recording_started_at.replace('Z', '+00:00'))).total_seconds()
        
        try:
            action_step_ids = [
                schema.get("step_id") for schema in self._captured_schemas
                if schema.get("semantic_action_id") and schema.get("step_id")
            ]
            validation = validate_recording(
                self.raw_actions, self.actions, action_step_ids, self._captured_schemas
            )
            existing_data = [
                {"step": schema.get("step"), "change_type": schema.get("change_type"), "existing_data": schema.get("existing_data")}
                for schema in self._captured_schemas
                if isinstance(schema, dict) and schema.get("existing_data")
            ]
            page_content = self._page_content_timeline()
            common_metadata = {
                "session_id": self.session_id,
                "category": self.category,
                "university": self.university,
                "start_time": self._recording_started_at,
                "end_time": end_time,
                "recording_duration_seconds": recording_duration,
                "total_steps": self.step_counter,
                "auto_backups_created": self._auto_save_counter,
                "sensitive_values_captured": self.capture_sensitive_values,
                "validation": validation,
            }
            write_json(os.path.join(self.session_dir, "raw_actions.json"), {
                **common_metadata, "total_raw_actions": len(self.raw_actions),
                "raw_actions": self.raw_actions,
            })
            write_json(os.path.join(self.session_dir, "semantic_actions.json"), {
                **common_metadata, "total_semantic_actions": len(self.actions),
                "semantic_actions": self.actions,
                "page_content": page_content,
            })
            write_json(os.path.join(self.session_dir, "interactions.json"), {
                **common_metadata, "total_interactions": len(self._interactions),
                "interactions": self._serialized_interactions(),
            })
            write_json(os.path.join(self.session_dir, "actions.json"), {
                **common_metadata,
                "total_actions": len(self.actions),
                "actions": self.actions,
                "existing_data": existing_data,
                "page_content": page_content,
            })
            write_json(os.path.join(self.session_dir, "validation.json"), validation)
            if not validation["passed"]:
                logger.error("Recorder validation failed: %s", validation["errors"])
                print(f"Recording integrity validation failed with {len(validation['errors'])} error(s).")
            
            # Clean up backup file after successful final save
            backup_path = os.path.join(self.session_dir, "actions_backup.json")
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
        except Exception as exc:
            logger.error(f"Failed to save actions.json: {exc}")
            raise
        finally:
            self._close_runtime()
        
        # Persist final session state before closing
        if self.session_state:
            self.session_manager.persist_session(self.session_id)
        
        print(f"Recording saved to {self.session_dir}")
        print(f"Session summary: {len(self.actions)} actions, {self.step_counter} steps, {recording_duration:.1f}s duration")
        if self._auto_save_counter > 0:
            print(f"Session recovery: {self._auto_save_counter} auto-backups were created during recording")

    def _close_runtime(self):
        try:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        finally:
            self._profile_lock.release()

    def _discard_session(self):
        """Remove only this recorder's empty session directory."""
        if os.path.isdir(self.session_dir):
            shutil.rmtree(self.session_dir, ignore_errors=True)
