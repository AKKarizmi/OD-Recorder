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
_SENSITIVE_FIELD_NAMES = frozenset({
    "password", "passwd", "secret", "token", "apikey", "api_key",
    "csrf", "authorization", "access_token", "refresh_token", "session_id",
})
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?:^|[_\s-])(?:csrf|token|auth(?:orization)?|access|refresh|session|secret|"
    r"password|passwd|api[\s_-]?key)(?:$|[_\s-])",
    re.IGNORECASE,
)
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
    
    # Check autocomplete attributes and field names/ids/labels. Security fields
    # are excluded even when their input type is an ordinary text type.
    for attr_name in ("autocomplete", "name", "id", "label"):
        attr_value = str(field.get(attr_name) or "").lower()
        if not attr_value:
            continue
        # Check autocomplete patterns
        if any(pattern in attr_value for pattern in _SENSITIVE_AUTOCOMPLETE_PATTERNS):
            return True
        # Check sensitive field names
        if any(name in attr_value for name in _SENSITIVE_FIELD_NAMES) or _SENSITIVE_FIELD_PATTERN.search(attr_value):
            return True

    attributes = field.get("attributes") or {}
    for attr_name in ("type", "autocomplete", "name", "id", "aria-label"):
        attr_value = str(attributes.get(attr_name) or "").lower()
        if attr_value and (
            any(name in attr_value for name in _SENSITIVE_FIELD_NAMES)
            or _SENSITIVE_FIELD_PATTERN.search(attr_value)
        ):
            return True
    
    return False


def safe_field_value(value: Any, field: dict[str, Any] | None) -> Any:
    """Return a field value only when it is safe to persist."""
    if value is None or is_sensitive_field(field or {}):
        return None
    return value


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


def redact_sensitive_value(value: str | None, field_name: str | None = None, 
                           input_type: str | None = None) -> tuple[bool, str | None]:
    """Redact sensitive values like CSRF tokens, passwords, etc.
    
    This function determines if a value should be redacted based on field name patterns
    and input type. It returns both whether redaction occurred and the appropriate value.
    
    Args:
        value: The attribute value to potentially redact
        field_name: The name attribute of the field
        input_type: The input type (e.g., 'hidden', 'password', 'text')
        
    Returns:
        Tuple of (value_was_redacted: bool, value_to_store: str|None)
        - If redacted: (True, None) with value_present flag to indicate value existed
        - If not sensitive: (False, original_value)
    """
    if value is None or value == "":
        return (False, None)
    
    # Check field name patterns for sensitive indicators
    sensitive_patterns = [
        "_token", "csrf", "csrf_token", "authenticity_token",
        "password", "passwd", "secret", "api_key", "apikey",
        "access_token", "refresh_token", "session_id", "authorization"
    ]
    
    field_name_lower = (field_name or "").lower()
    input_type_lower = (input_type or "").lower()
    
    # Always redact password types
    if input_type_lower in ("password", "hidden"):
        return (True, None)
    
    # Check for sensitive field names
    for pattern in sensitive_patterns:
        if pattern in field_name_lower:
            return (True, None)
    
    # Value is not sensitive
    return (False, value)
