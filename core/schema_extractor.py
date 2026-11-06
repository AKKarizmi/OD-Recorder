from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from collections import Counter
from typing import Any

from core.utils import normalize_xpath, redact_sensitive_value

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7

LABEL_CONFIDENCE = {
    "explicit_label": 1.0,
    "aria_labelledby": 0.98,
    "aria_label": 0.96,
    "fieldset_legend": 0.94,
    "component_heading": 0.90,
    "wrapper_label": 0.88,
    "preceding_label": 0.84,
    "preceding_text": 0.72,
    "section_heading": 0.68,
    "placeholder": 0.55,
    "name": 0.40,
    "id": 0.30,
    "none": 0.0,
    "button_text": 0.92,
    "input_action_text": 0.92,
}

_GENERATED_ID_PATTERN = re.compile(
    r"^(?:(?:null|undefined|listbox-null)(?:-\d+)?|\d+)$",
    re.IGNORECASE,
)


def _xpath_literal(value: str) -> str:
    """Quote text as an XPath literal without inventing an unsafe selector."""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in parts) + ")"


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
    stable_field_id = (
        field_id.strip() if isinstance(field_id, str) and field_id.strip()
        and not _GENERATED_ID_PATTERN.fullmatch(field_id.strip()) else None
    )
    if stable_field_id:
        safe_id = field_id.strip()
        if css_identifier.fullmatch(safe_id):
            selector = f"#{safe_id}"
            selector_candidates.append({
                "type": "css",
                "value": selector,
                "strategy": "id",
                "validated": False
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
                "validated": False
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
                    "validated": False
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
            "validated": False
        })
    
    # Priority 5: label-scoped/contextual selector.  This remains executable
    # even when several controls share the same placeholder.
    label = str(field.get("semantic_label") or field.get("label") or "").strip()
    if label and tag in {"input", "textarea", "select", "button"}:
        selector_candidates.append({
            "type": "xpath",
            "value": (
                f'//label[normalize-space(.)={_xpath_literal(label)}]'
                f'//*[self::{tag}] | '
                f'//*[normalize-space(.)={_xpath_literal(label)}]'
                f'/following::*[self::{tag}][1]'
            ),
            "strategy": "semantic_context",
            "validated": False,
        })

    # Priority 6: Placeholder (only if present in attributes)
    actual_placeholder = attrs.get("placeholder")
    if actual_placeholder and css_attribute_value.fullmatch(actual_placeholder):
        selector = f'{tag}[placeholder="{actual_placeholder}"]'
        selector_candidates.append({
            "type": "css",
            "value": selector,
            "strategy": "placeholder",
            "validated": False
        })
    
    # Priority 7: Class-based with contextual scope (use only stable classes)
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

    # Priority 8: Semantic text for action controls. This is relative and is
    # preferable to an absolute DOM path when stable attributes are absent.
    if tag in {"button", "a"} and label:
        selector_candidates.append({
            "type": "xpath",
            "value": f"//{tag}[normalize-space(.)={_xpath_literal(label)}]",
            "strategy": "visible_text",
            "validated": False,
        })
    elif field.get("role") == "option" and label:
        selector_candidates.append({
            "type": "xpath",
            "value": f'//*[@role="option" and normalize-space(.)={_xpath_literal(label)}]',
            "strategy": "option_text",
            "validated": False,
        })
    
    # Priority 9: Valid executable XPath (raw, not normalized)
    raw_xpath = field.get("xpath")
    generated_id_xpath = bool(
        field_id and not stable_field_id and
        re.search(r"@id\s*=", str(raw_xpath or ""), re.IGNORECASE)
    )
    if raw_xpath and not generated_id_xpath:
        selector_candidates.append({
            "type": "xpath",
            "value": raw_xpath,
            "strategy": "raw_xpath",
            "validated": False
        })
    
    # Include normalized XPath only as structural fingerprint (never for execution)
    # Issue 3: Separate executable selectors from structural fingerprints
    normalized = field.get("normalized_xpath")
    structural_xpath = None
    if normalized and normalized != raw_xpath:
        structural_xpath = normalized
    
    return {
        "candidates": selector_candidates,
        # Filled only after the candidates are evaluated in their owning frame.
        "primary": None,
        "structural_xpath": structural_xpath,
        # Compatibility keys are populated only after uniqueness validation.
        "id": None,
        "css": None,
        "xpath": None,
        "fallbacks": [],
        "confidence": 0.0,
    }


_VALIDATE_SELECTORS_SCRIPT = r"""
(requests) => requests.map(request => {
    try {
        if (request.type === 'css') return document.querySelectorAll(request.value).length;
        if (request.type === 'xpath') {
            return document.evaluate(request.value, document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength;
        }
    } catch (error) {
        return -1;
    }
    return -1;
})
"""


def _validate_selector_sets(frame: Any, records: list[dict[str, Any]]) -> None:
    """Mark selectors valid only when they resolve to exactly one element."""
    requests: list[dict[str, str]] = []
    candidate_refs: list[dict[str, Any]] = []
    for record in records:
        selectors = record.get("selectors") or {}
        for candidate in selectors.get("candidates", []):
            requests.append({"type": candidate["type"], "value": candidate["value"]})
            candidate_refs.append(candidate)
    if not requests:
        return
    try:
        counts = frame.evaluate(_VALIDATE_SELECTORS_SCRIPT, requests)
    except Exception as exc:
        logger.debug("Failed to validate selectors in frame: %s", exc)
        return
    for candidate, count in zip(candidate_refs, counts):
        candidate["match_count"] = count
        candidate["validated"] = count == 1
    for record in records:
        selectors = record.get("selectors") or {}
        validated = [candidate for candidate in selectors.get("candidates", []) if candidate.get("validated")]
        selectors["primary"] = validated[0]["value"] if validated else None
        selectors["fallbacks"] = [candidate["value"] for candidate in validated[1:]]
        strategy_confidence = {
            "id": 1.0, "name": 0.98, "data_attribute": 0.96,
            "aria_label": 0.94, "semantic_context": 0.90,
            "placeholder": 0.72, "class": 0.60, "visible_text": 0.82,
            "option_text": 0.80, "raw_xpath": 0.35,
        }
        selectors["confidence"] = (
            strategy_confidence.get(validated[0].get("strategy"), 0.5) if validated else 0.0
        )
        selectors["css"] = next((candidate["value"] for candidate in validated if candidate["type"] == "css"), None)
        selectors["xpath"] = next((candidate["value"] for candidate in validated if candidate["type"] == "xpath"), None)
        id_candidate = next((candidate for candidate in validated if candidate.get("strategy") == "id"), None)
        selectors["id"] = id_candidate["value"][1:] if id_candidate else None


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


def ensure_unique_field_keys(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve accidental identities without hiding declared repeating rows."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        groups.setdefault(str(field.get("field_key") or get_field_identity(field)), []).append(field)
    for base_key, group in groups.items():
        if len(group) == 1:
            continue
        for ordinal, field in enumerate(group):
            if field.get("row_key") is not None or field.get("row_index") is not None:
                field["repeating_collection"] = True
                continue
            differentiator = (
                field.get("semantic_label") or field.get("label") or field.get("textContent") or
                next(iter(sorted((field.get("dataAttributes") or field.get("attributes") or {}).items())), None) or
                field.get("xpath") or ordinal
            )
            if isinstance(differentiator, tuple):
                differentiator = f"{differentiator[0]}:{differentiator[1]}"
            suffix = re.sub(r"[^a-z0-9_.:-]+", "_", _normalise_text(differentiator)).strip("_")[:80]
            field["field_key"] = f"{base_key}|instance:{suffix or 'element'}:{ordinal}"
            field["identity_collision_resolved"] = True
    return fields


def normalize_widget_options(widget: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate canonical widget rows while preserving state and value provenance."""
    normalized: dict[str, dict[str, Any]] = {}
    dropped_status_rows = 0
    for raw_option in widget.get("available_options") or []:
        if not isinstance(raw_option, dict):
            continue
        if raw_option.get("selectable") is False or raw_option.get("is_status"):
            dropped_status_rows += 1
            continue
        label = re.sub(r"\s+", " ", str(raw_option.get("label") or "").strip())
        if not label:
            dropped_status_rows += 1
            continue
        value = raw_option.get("value")
        key = f"value:{value}" if value is not None else f"label:{label.casefold()}"
        option = {
            "label": label,
            "value": value,
            "value_source": raw_option.get("value_source") or (
                "dom" if value is not None else "unavailable"
            ),
            "disabled": bool(raw_option.get("disabled")),
            "selected": bool(raw_option.get("selected")),
        }
        existing = normalized.get(key)
        if existing:
            existing["selected"] = existing["selected"] or option["selected"]
            existing["disabled"] = existing["disabled"] and option["disabled"]
            if existing["value"] is None and option["value"] is not None:
                existing.update(option)
        else:
            normalized[key] = option
    widget["available_options"] = list(normalized.values())
    if dropped_status_rows:
        widget["excluded_status_row_count"] = dropped_status_rows
    return widget


_EXTRACT_FIELDS_SCRIPT = r"""
(captureConfig) => {
    const framePath = typeof captureConfig === 'string' ? captureConfig : captureConfig.framePath;
    const debugCaptureValues = !!(captureConfig && captureConfig.debugCaptureValues);
    const captureSensitiveValues = !!(captureConfig && captureConfig.captureSensitiveValues);
    const activeScope = typeof captureConfig === 'object' ? captureConfig.activeScope : null;
    const preferFormScope = !!(captureConfig && captureConfig.preferFormScope);
    const LABEL_CONFIDENCE = {
        explicit_label: 1.0, aria_labelledby: 0.98, aria_label: 0.96,
        fieldset_legend: 0.94, component_heading: 0.90, wrapper_label: 0.88,
        preceding_label: 0.84, preceding_text: 0.72, section_heading: 0.68,
        placeholder: 0.55, name: 0.40, id: 0.30, none: 0.0
        , button_text: 0.92, input_action_text: 0.92
    };
    // Capture controls plus small, structural context. Avoid generic div/span
    // text, which is both noisy and likely to contain user-provided content.
    const controlSelector = 'input, textarea, select, button';

    function generatedId(value) {
        return !value || /^(?:null|undefined|listbox-null)(?:-\d+)?$/i.test(value) || /^\d+$/.test(value);
    }
    
    function xpath(element) {
        if (!element || !element.parentNode) return '';
        if (element.nodeType === 11 && element.host) return xpath(element.host) + '/shadow-root()';
        if (element.id && !generatedId(element.id)) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
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

        // Native action text is generally the most stable semantic locator for
        // attribute-free buttons and submit inputs.
        if (element.tagName === 'BUTTON' && element.innerText.trim()) {
            return { label: element.innerText.trim(), source: 'button_text' };
        }
        if (element.tagName === 'INPUT' && /^(submit|button|reset)$/i.test(element.type) && element.value) {
            return { label: element.value.trim(), source: 'input_action_text' };
        }
        
        // Priority 3: <label for="<input-id>">
        if (element.id && !generatedId(element.id)) {
            const labelFor = document.querySelector('label[for="' + element.id.replace(/"/g, '\\"') + '"]');
            if (labelFor) {
                const text = labelFor.innerText.trim();
                if (text) return { label: text, source: 'explicit_label' };
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
                return { label: nearbyLabel.innerText.trim(), source: 'wrapper_label' };
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
            formId: form.getAttribute('id') || null,
            formName: form.getAttribute('name') || null,
            formAction: form.getAttribute('action') || null,
            formMethod: (form.getAttribute('method') || 'get').toLowerCase()
        };
    }

    function scopeRoot() {
        if (activeScope?.formId) {
            const byId = document.getElementById(activeScope.formId);
            if (byId?.tagName === 'FORM') return byId;
        }
        const forms = Array.from(document.forms || []);
        const matched = forms.find(form =>
            (activeScope?.formName && form.getAttribute('name') === activeScope.formName) ||
            (activeScope?.formAction && form.getAttribute('action') === activeScope.formAction)
        );
        if (matched) return matched;
        if (!preferFormScope) return document;
        const focusedForm = document.activeElement?.form || document.activeElement?.closest?.('form');
        if (focusedForm) return focusedForm;
        const ranked = forms.map(form => {
            const descriptor = [form.id, form.getAttribute('name'), form.className].join(' ').toLowerCase();
            const controls = Array.from(form.querySelectorAll(controlSelector)).filter(control => !isHidden(control));
            const required = controls.filter(control => control.required).length;
            const penalty = /feedback|backfeed|search|newsletter/.test(descriptor) ? 1000 : 0;
            return { form, score: controls.length + required * 5 - penalty };
        }).sort((left, right) => right.score - left.score);
        return ranked.length && ranked[0].score > 0 ? ranked[0].form : document;
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

    function classificationFor(element, visible, widgetController) {
        if (widgetController) return 'widget_controller';
        if (element.type === 'hidden') return 'system_field';
        if (element.matches('button, input[type="submit"], input[type="button"], input[type="reset"]')) {
            return element.type === 'submit' || element.matches('button:not([type]), button[type="submit"]')
                ? 'submit_control' : 'navigation_control';
        }
        return 'user_field';
    }

    function captureReasonFor(element, visible, classification) {
        if (visible) return 'visible_interactive';
        if (classification === 'system_field') return 'hidden_system';
        if (classification === 'widget_controller') return 'widget_dependency';
        return 'responsive_alternative';
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

    function safeAttributes(element) {
        return Object.fromEntries(Array.from(element.attributes)
            .filter(attribute => attribute.name.toLowerCase() !== 'value' &&
                !(attribute.name.toLowerCase() === 'id' && generatedId(attribute.value)))
            .map(attribute => [attribute.name, attribute.value]));
    }

    function valueFingerprint(value) {
        // FNV-1a is diagnostic rather than cryptographic: it lets test runs
        // compare populated state without writing the source value.
        let hash = 2166136261;
        for (let index = 0; index < value.length; index++) {
            hash ^= value.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return 'fnv1a32:' + (hash >>> 0).toString(16).padStart(8, '0') + ':' + value.length;
    }

    function valueMetadata(element, semanticLabel) {
        if (typeof element.value !== 'string') return { value_present: false };
        const value = element.value;
        const descriptor = [element.type, element.name, element.id, element.autocomplete,
            element.getAttribute('aria-label'), element.getAttribute('placeholder'), semanticLabel]
            .filter(Boolean).join(' ').toLowerCase();
        const secret = /password|hidden|secret|token|csrf|api.?key|authorization|access[_ -]?token|refresh[_ -]?token|session[_ -]?id|credit.?card|cc-csc|cc-exp/.test(descriptor);
        const metadata = { value_present: value.length > 0 };
        if (value && (!secret || captureSensitiveValues)) {
            metadata.value = value;
            metadata.value_fingerprint = valueFingerprint(value);
        }
        if (value && debugCaptureValues && !secret) metadata.debug_value = value;
        return metadata;
    }

    function widgetOptionValue(row, widget) {
        const valueNodes = [
            row,
            row.closest?.('.multiselect__element, li[data-value], li[data-id], li[data-key]'),
            row.querySelector?.('[role="option"], .multiselect__option')
        ].filter((node, index, all) => node && all.indexOf(node) === index);
        for (const node of valueNodes) {
            for (const [attribute, source] of [
                ['value', 'value'], ['data-value', 'data-value'], ['data-id', 'data-id'],
                ['data-key', 'data-key'], ['aria-valuetext', 'aria-valuetext']
            ]) {
                const value = node.getAttribute(attribute);
                if (value !== null && value !== '') return { value, value_source: source };
            }
            const vueOption = node.__vue__?.option || node.parentElement?.__vue__?.option;
            if (vueOption && typeof vueOption === 'object') {
                for (const key of ['value', 'id', 'key', 'code']) {
                    if (['string', 'number', 'boolean'].includes(typeof vueOption[key])) {
                        return { value: vueOption[key], value_source: 'component_state.' + key };
                    }
                }
            }
        }
        const selected = valueNodes.some(node => node.getAttribute('aria-selected') === 'true' ||
            node.classList.contains('multiselect__option--selected'));
        const hidden = selected ? widget.querySelector('input[type="hidden"]') : null;
        if (hidden?.value) return { value: hidden.value, value_source: 'hidden_model_input' };
        return { value: null, value_source: 'unavailable' };
    }

    function renderedWidgetOptions(widget) {
        const roleRows = Array.from(widget.querySelectorAll('[role="option"]'));
        const elementRows = Array.from(widget.querySelectorAll('.multiselect__element'));
        let rows = roleRows.length ? roleRows : elementRows;
        let canonicalStrategy = roleRows.length ? 'role_option' : (elementRows.length ? 'multiselect_element' : null);
        if (!rows.length) {
            rows = Array.from(widget.querySelectorAll('.multiselect__option')).filter(node =>
                node.closest('[role="option"], .multiselect__element, li[aria-selected], li[data-value], li[data-id]')
            );
            canonicalStrategy = rows.length ? 'multiselect_option' : null;
        }
        const options = new Map();
        for (const row of rows) {
            if (row.closest('[role="status"], [aria-live]')) continue;
            const classText = [row.className, row.parentElement?.className].filter(value => typeof value === 'string').join(' ');
            if (/(?:^|[-_\s])(no[-_]?results?|no[-_]?options?|empty[-_]?message|loading)(?:$|[-_\s])/i.test(classText)) continue;
            const optionNode = row.matches('[role="option"], .multiselect__option')
                ? row : (row.querySelector('[role="option"], .multiselect__option') || row);
            if (optionNode.getAttribute('aria-disabled') === 'true' &&
                !optionNode.closest('.multiselect__element') &&
                !['value', 'data-value', 'data-id', 'data-key'].some(attribute => optionNode.hasAttribute(attribute))) {
                continue;
            }
            const label = (optionNode.innerText || optionNode.textContent || '').trim();
            if (!label) continue;
            const machine = widgetOptionValue(row, widget);
            const key = machine.value !== null
                ? 'value:' + String(machine.value)
                : 'label:' + label.replace(/\s+/g, ' ').trim().toLocaleLowerCase();
            const current = options.get(key);
            const item = {
                label: label.replace(/\s+/g, ' ').trim(), value: machine.value,
                value_source: machine.value_source,
                selectable: true,
                disabled: optionNode.getAttribute('aria-disabled') === 'true' ||
                    optionNode.classList.contains('multiselect__option--disabled'),
                selected: optionNode.getAttribute('aria-selected') === 'true' ||
                    optionNode.classList.contains('multiselect__option--selected')
            };
            if (current) {
                current.selected = current.selected || item.selected;
                current.disabled = current.disabled && item.disabled;
                if (current.value === null && item.value !== null) Object.assign(current, item);
            } else {
                options.set(key, item);
            }
        }
        return { options: Array.from(options.values()), canonicalStrategy };
    }

    function customWidgets(root) {
        if (!root || !root.querySelectorAll) return [];
        const widgets = [];
        const widgetRoots = Array.from(root.querySelectorAll('.multiselect, [data-multiselect]'));
        widgetRoots.forEach((widget, index) => {
            const controller = widget.querySelector('input.multiselect__input, input[role="combobox"], [role="combobox"], input[aria-controls]');
            if (!controller) return;
            const controllerLabel = labelFor(controller).label;
            const form = formInfo(controller);
            const stableController = controller.id && !generatedId(controller.id)
                ? 'id:' + controller.id
                : (controller.name ? 'name:' + controller.name : (controllerLabel ? 'label:' + controllerLabel : 'index:' + index));
            const formKey = form.formId || form.formName || form.formAction || 'document';
            const selectedLabels = Array.from(widget.querySelectorAll('.multiselect__single, .multiselect__tag'))
                .map(element => (element.innerText || element.textContent || '').trim())
                .filter(Boolean);
            const optionExtraction = renderedWidgetOptions(widget);
            const renderedOptions = optionExtraction.options;
            const selectedValues = Array.from(new Set(selectedLabels)).map(label => {
                const option = renderedOptions.find(item => item.selected && item.label === label) ||
                    renderedOptions.find(item => item.label === label);
                return {
                    label, value: option?.value ?? null,
                    value_source: option?.value_source || 'unavailable'
                };
            });
            const virtualized = !!widget.querySelector('[style*="translate"], [data-virtualized], .virtual-list, .virtual-scroll');
            const listVisible = !!widget.querySelector('[role="listbox"], .multiselect__content-wrapper, .multiselect__content');
            const expanded = controller.getAttribute('aria-expanded') === 'true' ||
                widget.classList.contains('multiselect--active');
            const lazyOrLoading = !!widget.querySelector('[aria-busy="true"], .multiselect__option--loading, .loading');
            const renderedAllRows = !!optionExtraction.canonicalStrategy && !virtualized && !lazyOrLoading;
            widgets.push({
                widget_key: formKey + '|multiselect|' + stableController + '|' + index,
                kind: 'multiselect',
                label: controllerLabel || null,
                controller_id: generatedId(controller.id) ? null : controller.id,
                controller_name: controller.name || null,
                controller_xpath: xpath(controller),
                form: form,
                selected_labels: Array.from(new Set(selectedLabels)),
                selected_values: selectedValues,
                semantic_value: Array.from(new Set(selectedLabels)),
                value_present: selectedLabels.length > 0,
                available_options: renderedOptions,
                options_complete: virtualized || lazyOrLoading ? false :
                    (expanded && listVisible && renderedAllRows ? true : 'unknown'),
                option_extraction_strategy: renderedOptions.length ? 'rendered_dom' : 'unknown',
                expanded: expanded,
                focused: document.activeElement === controller,
                search_query_present: !!controller.value,
                disabled: !!controller.disabled,
                required: !!controller.required,
                frame_path: framePath,
            });
        });
        return widgets;
    }

    const fields = [];
    const seen = new Set();
    
    // Collect form controls (existing functionality)
    function collectControls(root) {
        if (!root || !root.querySelectorAll) return;
        const candidates = [];
        if (root.matches?.(controlSelector)) candidates.push(root);
        root.querySelectorAll(controlSelector).forEach(el => candidates.push(el));
        candidates.forEach(el => {
            if (seen.has(el)) return;
            seen.add(el);
            const row = rowInfo(el);
            const form = formInfo(el);
            const labelInfo = labelFor(el);
            const visible = !isHidden(el);
            const widgetController = !!el.closest('.multiselect, [data-multiselect]');
            const classification = classificationFor(el, visible, widgetController);
            const nativeOptions = optionsFor(el);
            fields.push({
                type: 'control',
                tag: el.tagName.toLowerCase(),
                inputType: el.type || null,
                name: el.name || null,
                id: generatedId(el.id) ? null : el.id,
                label: labelInfo.label,
                portal_label: labelInfo.label || null,
                semantic_label: labelInfo.label || el.name || el.id || null,
                label_source: labelInfo.source,
                label_confidence: LABEL_CONFIDENCE[labelInfo.source] ?? 0.25,
                canonical_key: null,
                matcher_status: 'unresolved',
                xpath: xpath(el),
                frame_path: framePath,
                visible: visible,
                classification: classification,
                capture_reason: captureReasonFor(el, visible, classification),
                disabled: !!el.disabled,
                readonly: !!el.readOnly,
                required: !!el.required,
                ...valueMetadata(el, labelInfo.label),
                checked: /^(checkbox|radio)$/i.test(el.type) ? !!el.checked : null,
                is_submit: !!el.form && (
                    (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit').toLowerCase() === 'submit') ||
                    (el.tagName === 'INPUT' && /^(submit|image)$/i.test(el.type))
                ),
                options: nativeOptions,
                available_options: nativeOptions,
                options_complete: nativeOptions ? true : null,
                option_extraction_strategy: nativeOptions ? 'native_dom' : null,
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
                attributes: safeAttributes(el)
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
                    id: generatedId(el.id) ? null : el.id,
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

    function collectPageContent() {
        const pageRoot = document.querySelector('main, [role="main"]') || document.body || document.documentElement;
        if (!pageRoot) return { root: null, title: document.title || '', records: [], fingerprint: 'empty' };
        const records = [];
        const seenContent = new Set();
        const contentRoots = [pageRoot];
        for (let index = 0; index < contentRoots.length; index++) {
            contentRoots[index].querySelectorAll('*').forEach(element => {
                if (element.shadowRoot) contentRoots.push(element.shadowRoot);
            });
        }
        function queryAll(selector) {
            return contentRoots.flatMap(root => Array.from(root.querySelectorAll(selector)));
        }
        const excludedSelector = [
            'script', 'style', 'template', 'noscript', 'nav', 'footer',
            '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
            '[aria-hidden="true"]', '[hidden]'
        ].join(',');

        function normalized(value) {
            return String(value || '').replace(/\s+/g, ' ').trim();
        }

        function hash(value) {
            let result = 2166136261;
            for (let index = 0; index < value.length; index++) {
                result ^= value.charCodeAt(index);
                result = Math.imul(result, 16777619);
            }
            return (result >>> 0).toString(16).padStart(8, '0');
        }

        function contentSelector(element) {
            if (element.id && !generatedId(element.id)) return '#' + CSS.escape(element.id);
            for (const attribute of ['data-testid', 'data-test', 'data-field', 'itemprop']) {
                const value = element.getAttribute(attribute);
                if (value) return element.tagName.toLowerCase() + '[' + attribute + '="' + CSS.escape(value) + '"]';
            }
            return xpath(element);
        }

        function excluded(element) {
            return !element || isHidden(element) || !!element.closest(excludedSelector) ||
                !!element.closest('input, textarea, select, button');
        }

        function sectionContext(element) {
            for (let current = element.parentElement; current; current = current.parentElement) {
                if (!current.matches('section, article, main, [role="region"], .card, .panel')) continue;
                const heading = current.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]');
                const text = normalized(heading?.innerText || heading?.textContent);
                if (text) return text;
            }
            return null;
        }

        function add(element, kind, text, extra) {
            text = normalized(text);
            if (excluded(element) || (!text && !extra)) return;
            const selector = contentSelector(element);
            const payload = extra || {};
            const contentKey = framePath + '|' + kind + '|' + selector;
            const duplicateKey = kind + '|' + text + '|' + JSON.stringify(payload);
            if (seenContent.has(duplicateKey)) return;
            seenContent.add(duplicateKey);
            records.push({
                content_key: contentKey,
                kind: kind,
                text: text || null,
                tag: element.tagName.toLowerCase(),
                visible: true,
                selector: selector,
                xpath: xpath(element),
                frame_path: framePath,
                section: sectionContext(element),
                ...payload
            });
        }

        queryAll('table').forEach(table => {
            if (excluded(table)) return;
            const headers = Array.from(table.querySelectorAll('thead th')).map(cell => normalized(cell.innerText));
            const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
            const rowNodes = bodyRows.length ? bodyRows : Array.from(table.querySelectorAll('tr')).filter(row => !row.closest('thead'));
            const rows = rowNodes.map(row => Array.from(row.querySelectorAll(':scope > th, :scope > td')).map(cell => normalized(cell.innerText)));
            const caption = normalized(table.querySelector('caption')?.innerText);
            if (caption || headers.some(Boolean) || rows.some(row => row.some(Boolean))) {
                add(table, 'table', caption, { headers, rows });
            }
        });

        queryAll('dl').forEach(list => {
            if (excluded(list)) return;
            const items = [];
            let label = null;
            Array.from(list.children).forEach(child => {
                if (child.tagName === 'DT') label = normalized(child.innerText);
                if (child.tagName === 'DD') {
                    const value = normalized(child.innerText);
                    if (label || value) items.push({ label, value });
                    label = null;
                }
            });
            if (items.length) add(list, 'definition_list', null, { items });
        });

        const pairedContainers = new WeakSet();
        queryAll('[data-field], .field, .detail, .detail-item, .info-item, .profile-item').forEach(container => {
            if (excluded(container) || container.closest('table, dl')) return;
            const parts = Array.from(container.children)
                .map(child => normalized(child.innerText || child.textContent)).filter(Boolean);
            if (parts.length >= 2 && parts.length <= 4) {
                pairedContainers.add(container);
                add(container, 'label_value', parts.join(': '), {
                    label: parts[0], value: parts.slice(1).join(' ')
                });
            }
        });

        function insidePairedContainer(element) {
            for (let current = element.parentElement; current; current = current.parentElement) {
                if (pairedContainers.has(current)) return true;
            }
            return false;
        }

        queryAll('a[href]').forEach(link => {
            add(link, 'link', link.innerText || link.getAttribute('aria-label') || link.title, {
                url: link.href || null
            });
        });
        queryAll('img').forEach(image => {
            if (excluded(image)) return;
            const alt = normalized(image.alt);
            const title = normalized(image.title);
            add(image, 'image', alt || title, {
                alt: alt || null, title: title || null, src: image.currentSrc || image.src || null
            });
        });

        const semanticSelector = [
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6', '[role="heading"]',
            'p', 'label', 'li', 'figcaption', 'caption',
            '[role="alert"]', '[role="status"]', '[aria-live]',
            '.badge', '.status', '.alert', '[data-field]'
        ].join(',');
        queryAll(semanticSelector).forEach(element => {
            if (element.closest('table, dl, a[href]') || insidePairedContainer(element)) return;
            let kind = 'text';
            if (element.matches('h1,h2,h3,h4,h5,h6,[role="heading"]')) kind = 'heading';
            else if (element.matches('li')) kind = 'list_item';
            else if (element.matches('label')) kind = 'label';
            else if (element.matches('[role="alert"],.alert')) kind = 'alert';
            else if (element.matches('[role="status"],[aria-live],.status,.badge')) kind = 'status';
            add(element, kind, element.innerText || element.textContent);
        });

        // Many profile/detail views use plain div/span pairs without semantic tags.
        // Capture only leaf text elements to avoid duplicating whole container text.
        queryAll('div, span, dt, dd, strong, small').forEach(element => {
            if (element.closest('table, dl, a[href]') || element.matches(semanticSelector) || insidePairedContainer(element)) return;
            if (Array.from(element.children).some(child => normalized(child.innerText || child.textContent))) return;
            const text = normalized(element.innerText || element.textContent);
            if (text && text.length <= 2000) add(element, 'text', text);
        });

        if (document.title) {
            records.unshift({
                content_key: framePath + '|document_title', kind: 'document_title', text: normalized(document.title),
                tag: 'title', visible: true, selector: 'title', xpath: '/html/head/title',
                frame_path: framePath, section: null
            });
        }
        records.sort((left, right) => left.content_key.localeCompare(right.content_key));
        const fingerprintSource = records.map(record => record.content_key + '|' + record.text + '|' +
            JSON.stringify([record.headers, record.rows, record.items, record.url, record.src])).join('\n');
        return {
            root: contentSelector(pageRoot),
            title: normalized(document.title),
            records: records,
            fingerprint: 'fnv1a32:' + hash(fingerprintSource) + ':' + records.length
        };
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
                id: generatedId(el.id) ? null : el.id,
                value_present: !!el.value,
                ...(captureSensitiveValues && el.value ? {
                    value: el.value, value_fingerprint: valueFingerprint(el.value)
                } : {}),
                xpath: xpath(el),
                frame_path: framePath,
                visible: false,
                classification: 'system_field',
                capture_reason: 'hidden_system',
                attributes: safeAttributes(el)
            });
        });
        
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) collectHiddenInputs(el.shadowRoot); });
    }
    
    const captureRoot = scopeRoot();
    collectControls(captureRoot);
    if (captureRoot.tagName === 'FORM' && captureRoot.id) {
        document.querySelectorAll('[form]').forEach(control => {
            if (control.getAttribute('form') === captureRoot.id) collectControls(control);
        });
    }
    collectStaticContent(captureRoot);
    collectHiddenInputs(captureRoot);
    
    return {
        fields,
        widgets: customWidgets(captureRoot),
        scope: captureRoot === document ? { type: 'document' } : { type: 'form', ...formInfo(captureRoot) },
        pageContent: collectPageContent()
    };
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
                        change_details: dict[str, Any] | None = None,
                        debug_capture_values: bool = False,
                        capture_sensitive_values: bool = False,
                        active_scope: dict[str, Any] | None = None,
                        prefer_form_scope: bool = False) -> dict[str, Any]:
    """Extract controls from the page, child frames, and open shadow roots.
    
    The schema intentionally excludes entered values and hidden tokens. It
    records only metadata needed to map and replay form interactions safely.
    """
    fields: list[dict[str, Any]] = []
    widgets: list[dict[str, Any]] = []
    content_records: list[dict[str, Any]] = []
    content_roots: list[dict[str, Any]] = []
    captured_scope: dict[str, Any] = {"type": "document"}
    try:
        frames = list(page.frames)
    except (AttributeError, TypeError):
        frames = [page]
    for frame in frames:
        try:
            result = frame.evaluate(_EXTRACT_FIELDS_SCRIPT, {
                "framePath": _frame_path(frame),
                "debugCaptureValues": debug_capture_values,
                "captureSensitiveValues": capture_sensitive_values,
                "activeScope": active_scope,
                "preferFormScope": prefer_form_scope,
            })
            # Accept list results from older injected scripts during a rolling update.
            extracted = result if isinstance(result, list) else result.get("fields", [])
            frame_widgets = [] if isinstance(result, list) else result.get("widgets", [])
            frame_content = {} if isinstance(result, list) else result.get("pageContent", {})
            if isinstance(frame_content, dict):
                content_records.extend(frame_content.get("records", []) or [])
                content_roots.append({
                    "frame_path": _frame_path(frame),
                    "root": frame_content.get("root"),
                    "title": frame_content.get("title"),
                })
            if not isinstance(result, list) and result.get("scope", {}).get("type") == "form":
                captured_scope = result["scope"]
            for field in extracted:
                form = field.get("form")
                if isinstance(form, dict):
                    for key in ("formId", "formName", "formAction", "formMethod"):
                        if form.get(key) is not None and not isinstance(form[key], str):
                            form[key] = None
                field["normalized_xpath"] = normalize_xpath(field.get("xpath"))
                field["field_key"] = get_field_identity(field)
                field["selectors"] = generate_multi_strategy_selector(field)
            fields.extend(extracted)
            _validate_selector_sets(frame, extracted)
            fields_by_xpath = {field.get("xpath"): field for field in extracted if field.get("xpath")}
            for widget in frame_widgets:
                normalize_widget_options(widget)
                controller = fields_by_xpath.get(widget.get("controller_xpath"))
                if controller:
                    widget["controller_field_key"] = controller.get("field_key")
                    widget["controller_selectors"] = controller.get("selectors")
                    controller["classification"] = "widget_controller"
                    controller["capture_reason"] = "widget_dependency"
                    controller["controller_value_present"] = controller.get("value_present", False)
                    controller["value_present"] = bool(widget.get("selected_labels"))
                    controller["semantic_value"] = widget.get("semantic_value", [])
                widget_field = {
                    "type": "widget_reference",
                    "tag": "widget",
                    "inputType": "multiselect",
                    "label": widget.get("label"),
                    "portal_label": widget.get("label"),
                    "semantic_label": widget.get("label"),
                    "label_source": (controller or {}).get("label_source", "component_heading"),
                    "label_confidence": (controller or {}).get("label_confidence", 0.9),
                    "canonical_key": None,
                    "matcher_status": "unresolved",
                    "field_key": widget.get("widget_key"),
                    "widget_key": widget.get("widget_key"),
                    "classification": "user_field",
                    "capture_reason": "visible_interactive",
                    "visible": True,
                    "value_present": bool(widget.get("selected_labels")),
                    "semantic_value": widget.get("semantic_value", []),
                    "selected_values": widget.get("selected_values", []),
                    "available_options_ref": widget.get("widget_key"),
                    "controller": {
                        "field_key": (controller or {}).get("field_key"),
                        "tag": (controller or {}).get("tag"),
                        "type": (controller or {}).get("inputType"),
                        "placeholder": (controller or {}).get("placeholder"),
                    },
                    "form": widget.get("form"),
                    "frame_path": widget.get("frame_path"),
                    "selectors": (controller or {}).get("selectors", {}),
                }
                fields.append(widget_field)
            widgets.extend(frame_widgets)
        except Exception as exc:
            # A frame can detach between polling and extraction.
            logger.debug(f"Failed to extract schema from frame: {exc}")
            continue
    try:
        timestamp = page.evaluate("() => Date.now()")
    except Exception:
        timestamp = int(time.time() * 1000)
    ensure_unique_field_keys(fields)
    content_records.sort(key=lambda item: str(item.get("content_key") or ""))
    content_fingerprint_source = json.dumps(content_records, sort_keys=True, ensure_ascii=False, default=str)
    content_fingerprint = "sha256:" + hashlib.sha256(
        content_fingerprint_source.encode("utf-8")
    ).hexdigest()
    schema: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "step": step_name,
        "change_type": change_type or ("initial_load" if step_name == "initial_load" else "dynamic"),
        "url": url,
        "fields": fields,
        "widgets": widgets,
        "scope": captured_scope,
        "timestamp": timestamp,
        "page_content": {
            "url": url,
            "title": next((root.get("title") for root in content_roots if root.get("frame_path") == "main"), ""),
            "captured_at": timestamp,
            "roots": content_roots,
            "records": content_records,
            "fingerprint": content_fingerprint,
        },
    }
    if change_details:
        schema["change_details"] = json.loads(json.dumps(change_details, default=str))
    return schema
