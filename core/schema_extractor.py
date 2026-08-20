from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Any

from core.utils import mask_sensitive_value, normalize_xpath

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def generate_multi_strategy_selector(field: dict[str, Any]) -> dict[str, str | None]:
    """Generate multiple selector strategies for robust element identification.
    
    Priority order: id > name > CSS > XPath
    This provides fallback options when one selector strategy fails.
    
    Args:
        field: Field dictionary containing id, name, xpath, and other attributes
        
    Returns:
        Dictionary with selector strategies: id, name, css, xpath (all nullable)
    """
    selectors = {
        "id": None,
        "name": None,
        "css": None,
        "xpath": None,
    }
    
    # Priority 1: ID selector (most stable)
    field_id = field.get("id")
    if field_id and isinstance(field_id, str) and field_id.strip():
        selectors["id"] = field_id.strip()
    
    # Priority 2: Name selector (stable for form fields)
    field_name = field.get("name")
    if field_name and isinstance(field_name, str) and field_name.strip():
        selectors["name"] = field_name.strip()
    
    # Priority 3: CSS selector (combine id/name or use tag + attributes)
    tag = field.get("tag") or field.get("type") or "*"
    tag = str(tag).lower()
    
    if selectors["id"]:
        selectors["css"] = f"#{selectors['id']}"
    elif selectors["name"]:
        selectors["css"] = f'{tag}[name="{selectors["name"]}"]'
    else:
        # Build CSS from available attributes
        css_parts = [tag]
        
        # Add class if available
        attrs = field.get("attributes", {})
        css_class = attrs.get("class")
        if css_class:
            # Use first class only for simplicity
            first_class = css_class.split()[0] if css_class else None
            if first_class:
                css_parts.append(f".{first_class}")
        
        # Add aria-label or title as attribute selector
        aria_label = attrs.get("aria-label") or field.get("label")
        if aria_label:
            # Sanitize for CSS selector
            safe_label = re.sub(r'[^\w\s-]', '', str(aria_label)[:30])
            if safe_label.strip():
                css_parts.append(f'[aria-label*="{safe_label.strip()}"]')
        
        selectors["css"] = "".join(css_parts) if len(css_parts) > 1 else tag
    
    # Priority 4: XPath (fallback, most fragile but most expressive)
    xpath = field.get("xpath") or field.get("normalized_xpath")
    if xpath:
        selectors["xpath"] = normalize_xpath(xpath)
    
    return selectors


def normalize_xpath(xpath: Any) -> str:
    """Remove positional XPath predicates while retaining the DOM shape.
    
    Reuses the utility function for consistency across the codebase.
    """
    value = str(xpath or "")
    return re.sub(r"\[\d+\]", "[]", value)


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def get_field_identity(field: dict[str, Any]) -> str:
    """Return a stable logical identity for a field across dynamic snapshots.

    Values and presentation state are intentionally excluded.  ``row_index`` is
    included when available so two otherwise identical repeated-row controls do
    not collapse into one occurrence.
    """
    tag = field.get("tag") or field.get("type") or ""
    field_id = str(field.get("id") or "").strip()
    field_name = str(field.get("name") or "").strip()
    locator = f"id:{field_id}" if field_id else (f"name:{field_name}" if field_name else f"xpath:{normalize_xpath(field.get('xpath'))}")
    parts = [
        str(field.get("frame_path") or "main"),
        str(tag).casefold(),
        str(field.get("inputType") or "").casefold(),
        locator,
        _normalise_text(field.get("label")),
    ]
    row_index = field.get("row_index")
    if row_index is not None:
        parts.append(f"row:{row_index}")
    return "|".join(parts)


def field_state(field: dict[str, Any]) -> tuple[Any, ...]:
    """State that can alter a schema without making ordinary typing a change."""
    return (
        bool(field.get("visible")),
        bool(field.get("disabled")),
        bool(field.get("readonly")),
        bool(field.get("required")),
    )


def field_identity_counts(fields: list[dict[str, Any]]) -> Counter[str]:
    return Counter(get_field_identity(field) for field in fields)


_EXTRACT_FIELDS_SCRIPT = r"""
(framePath) => {
    // Expanded selector to capture ALL elements with data, not just form controls
    const controlSelector = 'input, textarea, select, button';
    
    function xpath(element) {
        if (!element || !element.parentNode) return '';
        if (element.id) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
        if (element === document.body) return '/html/body';
        let index = 0;
        for (const sibling of element.parentNode.childNodes) {
            if (sibling === element) {
                return xpath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (index + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) index++;
        }
        return '';
    }

    function isHidden(element) {
        if (!element || element.nodeType !== 1) return true;
        for (let current = element; current && current.nodeType === 1; current = current.parentElement) {
            if (current.hidden || current.getAttribute('aria-hidden') === 'true' || current.hasAttribute('inert')) return true;
            const classes = current.classList;
            if (classes.contains('hidden') || classes.contains('ng-hide') || classes.contains('d-none')) return true;
            const style = getComputedStyle(current);
            if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return true;
        }
        return element.matches('input[type="hidden"]') || element.getClientRects().length === 0;
    }

    function labelFor(element) {
        if (element.labels && element.labels.length) return element.labels[0].innerText.trim();
        const labelledBy = element.getAttribute('aria-labelledby');
        if (labelledBy) {
            const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
            if (text) return text;
        }
        const aria = element.getAttribute('aria-label');
        if (aria) return aria.trim();
        const title = element.getAttribute('title');
        if (title) return title.trim();
        const placeholder = element.getAttribute('placeholder');
        if (placeholder) return placeholder.trim();
        return '';
    }

    function hasControl(node) {
        return node && node.nodeType === 1 && (node.matches(controlSelector) || node.querySelector(controlSelector));
    }

    function rowContainer(element) {
        for (let current = element.parentElement; current; current = current.parentElement) {
            const cls = typeof current.className === 'string' ? current.className : '';
            const explicit = current.hasAttribute('data-row-index') || current.hasAttribute('data-index') ||
                current.matches('[data-repeater-item], .repeatable-row, .repeater-item, .education-row, .work-row, .employment-row, .experience-row, .array-row, .dynamic-row');
            if (explicit || /(?:repeatable|repeater|education-row|work-row|employment-row|experience-row|array-row|dynamic-row)/i.test(cls)) return current;
        }
        return null;
    }

    function rowInfo(element) {
        const row = rowContainer(element);
        if (!row) return { rowIndex: null, rowKey: null, rowXPath: null };
        const raw = row.getAttribute('data-row-index') ?? row.getAttribute('data-index');
        const numeric = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
        // Count only sibling rows that are actual repeating rows with the same structure
        const siblings = Array.from(row.parentElement?.children || []).filter(sibling => {
            if (!hasControl(sibling)) return false;
            const a = typeof sibling.className === 'string' ? sibling.className : '';
            const b = typeof row.className === 'string' ? row.className : '';
            return sibling.tagName === row.tagName && a === b;
        });
        // Prefer explicit data attribute; fall back to computed index among siblings
        const index = numeric !== null ? numeric : Math.max(0, siblings.indexOf(row));
        return {
            rowIndex: index,
            rowKey: raw ?? (typeof row.className === 'string' ? row.className.trim() : null),
            rowXPath: xpath(row)
        };
    }
    
    // Extract data attributes from an element
    function getDataAttributes(element) {
        const dataAttrs = {};
        for (const attr of element.attributes) {
            if (attr.name.startsWith('data-')) {
                dataAttrs[attr.name] = attr.value;
            }
        }
        return dataAttrs;
    }

    const fields = [];
    const seen = new Set();
    
    // Collect form controls (existing functionality)
    function collectControls(root) {
        if (!root || !root.querySelectorAll) return;
        root.querySelectorAll(controlSelector).forEach(el => {
            if (seen.has(el)) return;
            seen.add(el);
            const row = rowInfo(el);
            fields.push({
                type: 'control',
                tag: el.tagName.toLowerCase(),
                inputType: el.type || null,
                name: el.name || null,
                id: el.id || null,
                label: labelFor(el),
                xpath: xpath(el),
                frame_path: framePath,
                visible: !isHidden(el),
                disabled: !!el.disabled,
                readonly: !!el.readOnly,
                required: !!el.required,
                value: el.value || null,
                row_index: row.rowIndex,
                row_key: row.rowKey,
                row_xpath: row.rowXPath,
                attributes: Object.fromEntries(Array.from(el.attributes).map(a => [a.name, a.value]))
            });
        });
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collectControls(el.shadowRoot); });
    }
    
    // NEW: Collect static text content and structural elements
    function collectStaticContent(root) {
        if (!root || !root.querySelectorAll) return;
        
        // Target elements that typically contain meaningful text
        const textSelectors = 'p, span, div, label, h1, h2, h3, h4, h5, h6, li, td, th, a, strong, em, b, i, u, small, mark, del, ins, sub, sup, code, pre, blockquote, figcaption, legend, dt, dd, summary, caption';
        
        root.querySelectorAll(textSelectors).forEach(el => {
            // Skip elements already captured as controls
            if (el.matches(controlSelector)) return;
            
            const textContent = (el.innerText || '').trim();
            const ariaLabel = el.getAttribute('aria-label') || '';
            const ariaDescribedby = el.getAttribute('aria-describedby') || '';
            const role = el.getAttribute('role') || '';
            const dataAttrs = getDataAttributes(el);
            const hasDataAttrs = Object.keys(dataAttrs).length > 0;
            
            // Only include if there's meaningful content
            if (textContent || ariaLabel || ariaDescribedby || role || hasDataAttrs) {
                const row = rowInfo(el);
                fields.push({
                    type: 'static',
                    tag: el.tagName.toLowerCase(),
                    textContent: textContent,
                    ariaLabel: ariaLabel || null,
                    ariaDescribedby: ariaDescribedby || null,
                    role: role || null,
                    id: el.id || null,
                    xpath: xpath(el),
                    frame_path: framePath,
                    visible: !isHidden(el),
                    className: typeof el.className === 'string' ? el.className.trim() : null,
                    row_index: row.rowIndex,
                    row_key: row.rowKey,
                    row_xpath: row.rowXPath,
                    dataAttributes: dataAttrs
                });
            }
        });
        
        // Recurse into shadow DOM
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collectStaticContent(el.shadowRoot); });
    }
    
    // Collect hidden inputs with values (important for CSRF tokens, etc.)
    function collectHiddenInputs(root) {
        if (!root || !root.querySelectorAll) return;
        
        root.querySelectorAll('input[type="hidden"]').forEach(el => {
            if (seen.has(el)) return;
            seen.add(el);
            
            fields.push({
                type: 'hidden_data',
                tag: 'input',
                inputType: 'hidden',
                name: el.name || null,
                id: el.id || null,
                value: el.value || null,
                xpath: xpath(el),
                frame_path: framePath,
                visible: false,
                attributes: Object.fromEntries(Array.from(el.attributes).map(a => [a.name, a.value]))
            });
        });
        
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collectHiddenInputs(el.shadowRoot); });
    }
    
    collectControls(document);
    collectStaticContent(document);
    collectHiddenInputs(document);
    
    return fields;
}
"""


def _frame_path(frame: Any) -> str:
    parts: list[str] = []
    current = frame
    while current is not None:
        parent = getattr(current, "parent_frame", None)
        if parent is None:
            parts.append("main")
            break
        try:
            index = list(parent.child_frames).index(current)
        except (AttributeError, ValueError):
            index = 0
        parts.append(f"frame[{index}]")
        current = parent
    return "/".join(reversed(parts))


def extract_form_schema(page: Any, step_name: str, url: str, change_type: str | None = None,
                        change_details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract controls from the page, child frames, and open shadow roots.
    
    Applies sensitive data masking to field values for security.
    """
    fields: list[dict[str, Any]] = []
    try:
        frames = list(page.frames)
    except (AttributeError, TypeError):
        frames = [page]
    for frame in frames:
        try:
            extracted = frame.evaluate(_EXTRACT_FIELDS_SCRIPT, _frame_path(frame))
            for field in extracted:
                field["normalized_xpath"] = normalize_xpath(field.get("xpath"))
                # Mask sensitive values for security
                if "value" in field:
                    field["value"] = mask_sensitive_value(field.get("value"), field)
            fields.extend(extracted)
        except Exception as exc:
            # A frame can detach between polling and extraction.
            logger.debug(f"Failed to extract schema from frame: {exc}")
            continue
    try:
        timestamp = page.evaluate("() => Date.now()")
    except Exception:
        timestamp = int(time.time() * 1000)
    schema: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "step": step_name,
        "change_type": change_type or ("initial_load" if step_name == "initial_load" else "dynamic"),
        "url": url,
        "fields": fields,
        "timestamp": timestamp,
    }
    if change_details:
        schema["change_details"] = json.loads(json.dumps(change_details, default=str))
    return schema
