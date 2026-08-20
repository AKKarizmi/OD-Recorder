"""Utility functions for portal recorder."""

from typing import Any


def get_xpath(element: Any) -> str:
    """Generate XPath for a DOM element (JavaScript version for injection).
    
    This is the JavaScript implementation that should be used in injected scripts.
    Returns a stable XPath expression for locating the element.
    """
    # This function is meant to be used as a template for JS code
    # The actual implementation is in the injected scripts
    return """
    function getXPath(element) {
        if (!element || !element.parentNode) return '';
        if (element.id) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
        if (element === document.body) return '/html/body';
        let index = 0;
        for (const sibling of element.parentNode.childNodes) {
            if (sibling === element) {
                return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (index + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) index++;
        }
        return '';
    }
    """


def normalize_xpath(xpath: Any) -> str:
    """Remove positional XPath predicates while retaining the DOM shape.
    
    Args:
        xpath: XPath string that may contain positional predicates like [1], [2], etc.
        
    Returns:
        Normalized XPath with all numeric predicates replaced by empty brackets []
    """
    import re
    value = str(xpath or "")
    return re.sub(r"\[\d+\]", "[]", value)


def is_sensitive_field(field: dict[str, Any]) -> bool:
    """Check if a field likely contains sensitive data that should be masked.
    
    Args:
        field: Field dictionary with attributes like type, name, id, autocomplete
        
    Returns:
        True if the field appears to contain sensitive information
    """
    # Check input type
    input_type = str(field.get("inputType") or field.get("type") or "").lower()
    if input_type in ("password", "hidden"):
        return True
    
    # Check autocomplete attributes
    autocomplete_patterns = [
        "password", "current-password", "new-password",
        "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
        "transaction-currency", "transaction-amount",
        "secret", "secret-key", "api-key"
    ]
    
    for attr_name in ("autocomplete", "name", "id"):
        attr_value = str(field.get(attr_name) or "").lower()
        if any(pattern in attr_value for pattern in autocomplete_patterns):
            return True
    
    # Check common sensitive field names
    sensitive_names = ["password", "passwd", "secret", "token", "apikey", "api_key"]
    for attr_name in ("name", "id", "label"):
        attr_value = str(field.get(attr_name) or "").lower()
        if any(name in attr_value for name in sensitive_names):
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
        return "***REDACTED***"
    
    return value
