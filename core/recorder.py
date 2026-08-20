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

from config import BROWSER_CHANNEL, DYNAMIC_CAPTURE_DELAY, HEADLESS_MODE, NAVIGATION_TIMEOUT, POLL_INTERVAL, SESSIONS_DIR
from core.event_listener import inject_event_listeners
from core.profile_manager import (
    ProfileLock,
    ensure_recorder_user_data_dir,
    validate_recorder_user_data_dir,
)
from core.schema_extractor import extract_form_schema, field_state, get_field_identity

# Configure module logger
logger = logging.getLogger(__name__)


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
    const staticElements = 'p, span, div, label, h1, h2, h3, h4, h5, h6, li, td, th, a, strong, em, b, i, u, small, mark, del, ins, sub, sup, code, pre, blockquote, figcaption, legend, dt, dd, summary, caption';
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
    }

    const observer = new MutationObserver(mutations => mutations.forEach(mutationSummary));
    function install() {
        if (document.documentElement) observer.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['class', 'style', 'hidden', 'aria-hidden', 'disabled', 'readonly', 'required', 'inert'],
            childList: true,
            subtree: true
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


class Recorder:
    def __init__(self, university, category, profile_path, profile_directory=None):
        self.university = university
        self.category = category
        self.profile_path = profile_path
        self.user_data_dir, self.profile_directory = self._resolve_chrome_profile(
            profile_path, profile_directory
        )
        self.session_id = uuid.uuid4().hex[:8]
        self.actions: list[dict[str, Any]] = []
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
        
        # Memory management: circular buffer for DOM changes with max size
        self._max_dom_history = 1000  # Maximum number of DOM change entries per type
        self._dom_change_buffers: dict[str, list[Any]] = {
            key: [] for key in ("newFields", "removedFields", "visibilityChanges", "rowChanges", "panelChanges")
        }
        
        # Session recovery: periodic save configuration
        self._auto_save_interval = 30  # seconds between auto-saves
        self._last_auto_save: float = 0
        self._auto_save_counter = 0  # Track number of auto-saves

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
            self._running = True
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
        """Record a browser event with accurate timestamps.
        
        Uses the client-side timestamp from JavaScript for event_occurred_at,
        and records when Python received it in recorded_at for debugging.
        """
        event = dict(event_data)
        # Preserve the original JS timestamp as when the event actually occurred
        event["event_occurred_at"] = event.get("timestamp")
        # Record when Python received/dran the event (for debugging latency)
        event["recorded_at"] = datetime.now(timezone.utc).isoformat()
        self.actions.append(event)

    def _on_navigation(self, frame):
        if self.page is not None and frame == self.page.main_frame and self._initial_captured:
            self._schedule_capture("navigation", {"url": frame.url})

    @staticmethod
    def _safe_name(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._")
        return value[:80] or "unknown"

    def _capture_schema(self, step_name, change_type=None, change_details=None):
        schema = extract_form_schema(self.page, step_name, self.page.url, change_type, change_details)
        filename = f"step_{self.step_counter:02d}_{self._safe_name(step_name)}.json"
        write_json(os.path.join(self.session_dir, filename), schema)
        fields = schema.get("fields", [])
        visible = sum(1 for field in fields if field.get("visible"))
        print(
            f"[Progress] Step {self.step_counter:02d}: {step_name} | "
            f"change={schema.get('change_type')} | fields={len(fields)} | "
            f"visible={visible} | hidden={len(fields) - visible} | "
            f"total_actions={len(self.actions)}"
        )
        self.step_counter += 1
        self.current_step_name = step_name
        return schema

    @staticmethod
    def _signature(schema):
        """Generate a schema signature for change detection.
        
        Excludes step name to enable cross-step field comparisons.
        Only includes field identity and state, not presentation metadata.
        """
        return Counter((get_field_identity(field), field_state(field)) for field in schema.get("fields", []))

    def _set_baseline(self, schema):
        self._last_signature = self._signature(schema)
        self.last_fields_set = {identity for identity, _state in self._last_signature}
        self.last_schema = schema
        self._last_url = schema.get("url")

    def _get_field_identity(self, field):
        """Compatibility wrapper for callers using the recorder helper API."""
        return get_field_identity(field)

    def _schedule_capture(self, change_type=None, details=None):
        self._pending_reasons.add(change_type or "dynamic")
        if details:
            for key, value in details.items():
                self._pending_details[key] = value
        self._pending_deadline = time.monotonic() + DYNAMIC_CAPTURE_DELAY

    def _change_type(self) -> str:
        priority = ["navigation", "row_add", "row", "panel", "visibility", "new_fields", "dynamic"]
        return next((name for name in priority if name in self._pending_reasons), "dynamic")

    def _step_name(self, change_type, details):
        if change_type == "navigation":
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
        schema = extract_form_schema(self.page, "temp", self.page.url)
        signature = self._signature(schema)
        changed = force or self._last_signature is None or signature != self._last_signature or schema["url"] != self._last_url
        if not changed:
            return False
        change_type = change_type or self._change_type()
        details = change_details or dict(self._pending_details)
        step_name = self._step_name(change_type, details)
        captured = self._capture_schema(step_name, change_type, details)
        self._set_baseline(captured)
        return True

    def _consume_observer_changes(self):
        merged: dict[str, list[Any]] = {key: [] for key in ("newFields", "removedFields", "visibilityChanges", "rowChanges", "panelChanges")}
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
        """
        start_time = time.monotonic()
        while self._running and not self._stop_event.is_set():
            self._drain_events()
            with self._page_lock:  # Thread-safe page access
                changes = self._consume_observer_changes()
            if changes["newFields"]:
                self._schedule_capture("new_fields", {"count": len(changes["newFields"]), "fields": changes["newFields"]})
            if changes["removedFields"]:
                self._schedule_capture("dynamic", {"removed": changes["removedFields"]})
            if changes["visibilityChanges"]:
                self._schedule_capture("visibility", {"changes": changes["visibilityChanges"]})
            if changes["panelChanges"]:
                self._schedule_capture("panel", {"changes": changes["panelChanges"], "panel_id": changes["panelChanges"][0].get("panelId")})
            if changes["rowChanges"]:
                self._schedule_capture("row_add", {"changes": changes["rowChanges"]})
            if self._pending_deadline is not None and time.monotonic() >= self._pending_deadline:
                reason = self._change_type()
                details = dict(self._pending_details)
                self._pending_deadline = None
                self._pending_reasons.clear()
                self._pending_details.clear()
                self._check_and_capture_schema(reason, details)
            
            # Session recovery: periodic incremental save of actions.json
            elapsed = time.monotonic() - start_time
            if elapsed - self._last_auto_save >= self._auto_save_interval and self.actions:
                self._save_actions_incremental()
                self._last_auto_save = elapsed
            
            self.page.wait_for_timeout(max(1, int(POLL_INTERVAL * 1000)))
    
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
            })
            self._auto_save_counter += 1
            logger.debug(f"Auto-saved {len(self.actions)} actions to {backup_path}")
        except Exception as exc:
            logger.warning(f"Failed to auto-save actions: {exc}")

    def stop(self):
        if not self._running and self.browser is None and self._playwright is None:
            self._profile_lock.release()
            if not self.actions and os.path.isdir(self.session_dir):
                self._discard_session()
            return
        self._running = False
        self._stop_event.set()
        if not self.actions:
            self._close_runtime()
            self._discard_session()
            print("No actions recorded; empty session discarded.")
            return
        
        # Capture any pending schema changes before saving
        with self._page_lock:
            if self._pending_deadline is not None and self.page is not None:
                try:
                    reason = self._change_type()
                    details = dict(self._pending_details)
                    self._check_and_capture_schema(reason, details)
                except Exception as exc:
                    logger.warning(f"Failed to capture final schema: {exc}")
        
        # Save actions with metadata
        end_time = datetime.now(timezone.utc).isoformat()
        recording_duration = (datetime.fromisoformat(end_time.replace('Z', '+00:00')) - 
                             datetime.fromisoformat(self._recording_started_at.replace('Z', '+00:00'))).total_seconds()
        
        try:
            write_json(os.path.join(self.session_dir, "actions.json"), {
                "session_id": self.session_id,
                "category": self.category,
                "university": self.university,
                "start_time": self._recording_started_at,
                "end_time": end_time,
                "recording_duration_seconds": recording_duration,
                "total_actions": len(self.actions),
                "total_steps": self.step_counter,
                "auto_backups_created": self._auto_save_counter,
                "actions": self.actions,
            })
            
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
