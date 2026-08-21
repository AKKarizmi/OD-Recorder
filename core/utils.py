"""Utility functions for portal recorder."""

from __future__ import annotations

import re
from typing import Any


# Pre-compiled regex patterns for performance
_XPATH_POSITIONAL_PREDICATE_PATTERN = re.compile(r"\[\d+\]")
_SENSITIVE_INPUT_TYPES = frozenset({"password", "hidden"})
_SENSITIVE_AUTOCOMPLETE_PATTERNS = frozenset({
    "password", "current-password", "new-password",
    "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
    "transaction-currency", "transaction-amount",
    "secret", "secret-key", "api-key"
})
_SENSITIVE_FIELD_NAMES = frozenset({"password", "passwd", "secret", "token", "apikey", "api_key"})
_REDACTED_VALUE = "***REDACTED***"


def normalize_xpath(xpath: Any) -> str:
    """Remove positional XPath predicates while retaining the DOM shape.
    
    Args:
        xpath: XPath string that may contain positional predicates like [1], [2], etc.
        
    Returns:
        Normalized XPath with all numeric predicates replaced by empty brackets []
    """
    value = str(xpath or "")
    return _XPATH_POSITIONAL_PREDICATE_PATTERN.sub("[]", value)


def is_sensitive_field(field: dict[str, Any]) -> bool:
    """Check if a field likely contains sensitive data that should be masked.
    
    Args:
        field: Field dictionary with attributes like type, name, id, autocomplete
        
    Returns:
        True if the field appears to contain sensitive information
    """
    # Check input type
    input_type = str(field.get("inputType") or field.get("type") or "").lower()
    if input_type in _SENSITIVE_INPUT_TYPES:
        return True
    
    # Check autocomplete attributes and field names/ids
    for attr_name in ("autocomplete", "name", "id"):
        attr_value = str(field.get(attr_name) or "").lower()
        if not attr_value:
            continue
        # Check autocomplete patterns
        if any(pattern in attr_value for pattern in _SENSITIVE_AUTOCOMPLETE_PATTERNS):
            return True
        # Check sensitive field names
        if any(name in attr_value for name in _SENSITIVE_FIELD_NAMES):
            return True
    
    # Check label attribute
    label_value = str(field.get("label") or "").lower()
    if any(name in label_value for name in _SENSITIVE_FIELD_NAMES):
        return True
    
    return False


def mask_sensitive_value(value: str | None, field: dict[str, Any]) -> str | None:
    """Mask sensitive field values for privacy/security.
    
    Args:
        value: The field value to potentially mask
        field: Field dictionary containing metadata
        
    Returns:
        Masked value (e.g., "***REDACTED***") if sensitive, original value otherwise
    """
    if value is None:
        return None
    
    if is_sensitive_field(field):
        return _REDACTED_VALUE
    
    return value
