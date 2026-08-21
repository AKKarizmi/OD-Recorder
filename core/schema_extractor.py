from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from typing import Any

from core.utils import normalize_xpath, redact_sensitive_value

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3


def generate_multi_strategy_selector(field: dict[str, Any]) -> dict[str, Any]:
    """Generate multiple selector strategies for robust element identification.
    
    Priority order: id > name > data-* > actual aria-label > placeholder > CSS class + context > XPath
    Only uses attributes actually present on the element.
    Never invents aria-label from semantic label or placeholder.
    
    Args:
        field: Field dictionary containing id, name, xpath, and other attributes
        
    Returns:
        Dictionary with selector_candidates list containing validated selectors,
        plus structural_xpath for fingerprinting (not execution)
    """
    selector_candidates = []
    attrs = field.get("attributes", {})
    tag = field.get("tag") or "*"
    tag = str(tag).lower()
    
    # Helper to validate CSS identifier
    css_identifier = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")
    css_attribute_value = re.compile(r'^[^"\\\r\n\f]+$')
    
    # Priority 1: Stable ID
    field_id = field.get("id")
    if field_id and isinstance(field_id, str) and field_id.strip():
        safe_id = field_id.strip()
        if css_identifier.fullmatch(safe_id):
            selector = f"#{safe_id}"
            selector_candidates.append({
                "type": "css",
                "value": selector,
                "strategy": "id",
                "validated": True
            })
    
    # Priority 2: Stable name attribute
    field_name = field.get("name")
    if field_name and isinstance(field_name, str) and field_name.strip():
        safe_name = field_name.strip()
        if css_attribute_value.fullmatch(safe_name):
            selector = f'{tag}[name="{safe_name}"]'
            selector_candidates.append({
                "type": "css",
                "value": selector,
                "strategy": "name",
                "validated": True
            })
    
    # Priority 3: Stable data-* attributes
    for attr_name, attr_value in sorted(attrs.items()):
        if attr_name.startswith("data-") and attr_value:
            if css_attribute_value.fullmatch(attr_value):
                selector = f'{tag}[{attr_name}="{attr_value}"]'
                selector_candidates.append({
                    "type": "css",
                    "value": selector,
                    "strategy": "data_attribute",
                    "validated": True
                })
                break  # Use first stable data-* attribute
    
    # Priority 4: Actual aria-label (only if present in attributes)
    # Issue 4: Never translate derived semantic label into aria-label selector
    actual_aria_label = attrs.get("aria-label")
    if actual_aria_label and css_attribute_value.fullmatch(actual_aria_label):
        selector = f'{tag}[aria-label="{actual_aria_label}"]'
        selector_candidates.append({
            "type": "css",
            "value": selector,
            "strategy": "aria_label",
            "validated": True
        })
    
    # Priority 5: Placeholder (only if present in attributes)
    actual_placeholder = attrs.get("placeholder")
    if actual_placeholder and css_attribute_value.fullmatch(actual_placeholder):
        selector = f'{tag}[placeholder="{actual_placeholder}"]'
        selector_candidates.append({
            "type": "css",
            "value": selector,
            "strategy": "placeholder",
            "validated": True
        })
    
    # Priority 6: Class-based with contextual scope (use only stable classes)
    css_class = attrs.get("class")
    if css_class:
        # Filter out Vue-generated or unstable classes
        stable_classes = [c for c in css_class.split() if c and not c.startswith('_') and len(c) > 2]
        if stable_classes:
            # Use up to 2 most specific classes
            selector = tag
            for cls in stable_classes[:2]:
                if css_identifier.fullmatch(cls):
                    selector += f".{cls}"
            if selector != tag:
                selector_candidates.append({
                    "type": "css",
                    "value": selector,
                    "strategy": "class",
                    "validated": False  # May not be unique
                })
    
    # Priority 7: Valid executable XPath (raw, not normalized)
    raw_xpath = field.get("xpath")
    if raw_xpath:
        selector_candidates.append({
            "type": "xpath",
            "value": raw_xpath,
            "strategy": "raw_xpath",
            "validated": True
        })
    
    # Include normalized XPath only as structural fingerprint (never for execution)
    # Issue 3: Separate executable selectors from structural fingerprints
    normalized = field.get("normalized_xpath")
    structural_xpath = None
    if normalized and normalized != raw_xpath:
        structural_xpath = normalized
    
    return {
        "candidates": selector_candidates,
        "primary": selector_candidates[0]["value"] if selector_candidates else None,
        "structural_xpath": structural_xpath
    }


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
    
    Field identity now includes form context to prevent collisions between
    fields with the same name/label in different forms on the same page.
    """
    tag = field.get("tag") or field.get("type") or ""
    field_id = str(field.get("id") or "").strip()
    field_name = str(field.get("name") or "").strip()
    
    # Build form context for identity (Issue 2)
    form_info = field.get("form") or {}
    form_context = None
    if form_info:
        form_id = form_info.get("formId")
        form_name = form_info.get("formName")
        form_action = form_info.get("formAction")
        if form_id:
            form_context = f"form_id:{form_id}"
        elif form_name:
            form_context = f"form_name:{form_name}"
        elif form_action:
            # Normalize form action to route
            form_context = f"form_action:{form_action}"
    
    locator = f"id:{field_id}" if field_id else (f"name:{field_name}" if field_name else f"xpath:{normalize_xpath(field.get('xpath'))}")
    parts = [
        str(field.get("frame_path") or "main"),
        str(tag).casefold(),
        str(field.get("inputType") or "").casefold(),
        locator,
        _normalise_text(field.get("label")),
    ]
    # Add form context to prevent collisions across forms
    if form_context:
        parts.insert(1, form_context)
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
    // Capture controls plus small, structural context. Avoid generic div/span
    // text, which is both noisy and likely to contain user-provided content.
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
        // Priority 1: aria-labelledby
        const labelledBy = element.getAttribute('aria-labelledby');
        if (labelledBy) {
            const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
            if (text) return { label: text, source: 'aria_labelledby' };
        }
        
        // Priority 2: actual aria-label
        const aria = element.getAttribute('aria-label');
        if (aria && aria.trim()) return { label: aria.trim(), source: 'aria_label' };
        
        // Priority 3: <label for="<input-id>">
        if (element.id) {
            const labelFor = document.querySelector('label[for="' + element.id.replace(/"/g, '\\"') + '"]');
            if (labelFor) {
                const text = labelFor.innerText.trim();
                if (text) return { label: text, source: 'label_for' };
            }
        }
        
        // Priority 4: wrapping ancestor <label>
        if (element.labels && element.labels.length) {
            const text = element.labels[0].innerText.trim();
            if (text) return { label: text, source: 'wrapper_label' };
        }
        
        // Priority 5: fieldset <legend>
        const fieldset = element.closest('fieldset');
        if (fieldset) {
            const legend = fieldset.querySelector('legend');
            if (legend && legend.innerText.trim()) {
                return { label: legend.innerText.trim(), source: 'fieldset_legend' };
            }
        }
        
        // Priority 6: nearby explicit label inside the same field wrapper
        const fieldWrapper = element.closest('.form-group, .field, .form-field, .input-group, [role="group"]');
        if (fieldWrapper) {
            const nearbyLabel = fieldWrapper.querySelector('label:not([for])');
            if (nearbyLabel && nearbyLabel.innerText.trim()) {
                return { label: nearbyLabel.innerText.trim(), source: 'wrapper_context' };
            }
        }
        
        // Priority 7: preceding sibling label/text
        let prevSibling = element.previousElementSibling;
        while (prevSibling) {
            if (prevSibling.matches('label')) {
                const text = prevSibling.innerText.trim();
                if (text) return { label: text, source: 'preceding_label' };
            }
            if (prevSibling.matches('h1, h2, h3, h4, h5, h6, p, span, div')) {
                const text = prevSibling.innerText.trim();
                if (text && text.length < 100) return { label: text, source: 'preceding_text' };
            }
            prevSibling = prevSibling.previousElementSibling;
        }
        
        // Priority 8: placeholder (fallback)
        const placeholder = element.getAttribute('placeholder');
        if (placeholder && placeholder.trim()) return { label: placeholder.trim(), source: 'placeholder' };
        
        // Priority 9: nearby heading / section context
        const section = element.closest('section, .section, .panel');
        if (section) {
            const heading = section.querySelector('h1, h2, h3, h4, h5, h6');
            if (heading && heading.innerText.trim()) {
                return { label: heading.innerText.trim(), source: 'section_heading' };
            }
        }
        
        // Priority 10: empty only when no useful semantic text exists
        return { label: '', source: 'none' };
    }

    function formInfo(element) {
        const form = element.form || element.closest('form');
        if (!form) return { formId: null, formName: null, formAction: null, formMethod: null };
        return {
            formId: form.id || null,
            formName: form.name || null,
            formAction: form.getAttribute('action') || null,
            formMethod: (form.getAttribute('method') || 'get').toLowerCase()
        };
    }

    function optionsFor(element) {
        if (element.tagName !== 'SELECT') return null;
        return Array.from(element.options).map(option => ({
            value: option.value,
            label: (option.label || option.text || '').trim(),
            disabled: !!option.disabled,
            selected: !!option.selected
        }));
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
            const form = formInfo(el);
            const labelInfo = labelFor(el);
            fields.push({
                type: 'control',
                tag: el.tagName.toLowerCase(),
                inputType: el.type || null,
                name: el.name || null,
                id: el.id || null,
                label: labelInfo.label,
                label_source: labelInfo.source,
                xpath: xpath(el),
                frame_path: framePath,
                visible: !isHidden(el),
                disabled: !!el.disabled,
                readonly: !!el.readOnly,
                required: !!el.required,
                // Do not persist entered values. Presence is enough to describe
                // a state transition without retaining personal data or tokens.
                value_present: typeof el.value === 'string' ? el.value.length > 0 : false,
                checked: typeof el.checked === 'boolean' ? el.checked : null,
                options: optionsFor(el),
                autocomplete: el.getAttribute('autocomplete') || null,
                placeholder: el.getAttribute('placeholder') || null,
                constraints: {
                    min: el.getAttribute('min'), max: el.getAttribute('max'),
                    minLength: el.minLength >= 0 ? el.minLength : null,
                    maxLength: el.maxLength >= 0 ? el.maxLength : null,
                    pattern: el.getAttribute('pattern'), step: el.getAttribute('step')
                },
                form: form,
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
        
        // Headings and legends provide useful form context without bulk capture
        // of page content such as application answers or profile information.
        const textSelectors = 'h1, h2, h3, h4, h5, h6, legend';
        
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
    
    // Keep hidden-field metadata but never record CSRF tokens or other values.
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
                value_present: !!el.value,
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
    
    The schema intentionally excludes entered values and hidden tokens. It
    records only metadata needed to map and replay form interactions safely.
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
                field["field_key"] = get_field_identity(field)
                field["selectors"] = generate_multi_strategy_selector(field)
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
