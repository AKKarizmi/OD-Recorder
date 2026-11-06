"""Session management module for maintaining browser state, cookies, and tokens."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionToken:
    """Represents an authentication or session token."""
    
    name: str
    value: str
    domain: str | None = None
    path: str = "/"
    expires: int | None = None  # Unix timestamp in seconds
    http_only: bool = False
    secure: bool = False
    same_site: str | None = None  # 'Strict', 'Lax', 'None'
    created_at: float = field(default_factory=time.time)
    
    def is_expired(self) -> bool:
        """Check if the token has expired."""
        if self.expires is None:
            return False
        return time.time() > self.expires
    
    def to_dict(self) -> dict[str, Any]:
        """Convert token to dictionary for serialization."""
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "http_only": self.http_only,
            "secure": self.secure,
            "same_site": self.same_site,
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionToken:
        """Create token from dictionary."""
        return cls(
            name=data["name"],
            value=data["value"],
            domain=data.get("domain"),
            path=data.get("path", "/"),
            expires=data.get("expires"),
            http_only=data.get("http_only", False),
            secure=data.get("secure", False),
            same_site=data.get("same_site"),
            created_at=data.get("created_at", time.time()),
        )


@dataclass
class SessionState:
    """Represents the complete state of a browser session."""
    
    session_id: str
    university: dict[str, Any]
    category: str
    base_url: str | None = None
    current_url: str | None = None
    tokens: list[SessionToken] = field(default_factory=list)
    cookies: dict[str, Any] = field(default_factory=dict)
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict)  # domain -> {key: value}
    session_storage: dict[str, dict[str, str]] = field(default_factory=dict)  # frame_id -> {key: value}
    form_data: dict[str, Any] = field(default_factory=dict)  # Field values (non-sensitive only)
    navigation_history: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    is_authenticated: bool = False
    user_agent: str | None = None
    
    def add_token(self, token: SessionToken) -> None:
        """Add or update a session token."""
        # Remove existing token with same name/domain
        self.tokens = [t for t in self.tokens 
                      if not (t.name == token.name and t.domain == token.domain)]
        self.tokens.append(token)
        self.last_activity = time.time()
    
    def get_token(self, name: str, domain: str | None = None) -> SessionToken | None:
        """Get a token by name and optionally domain."""
        for token in self.tokens:
            if token.name == name:
                if domain is None or token.domain == domain:
                    if not token.is_expired():
                        return token
        return None
    
    def remove_expired_tokens(self) -> int:
        """Remove all expired tokens. Returns count of removed tokens."""
        original_count = len(self.tokens)
        self.tokens = [t for t in self.tokens if not t.is_expired()]
        removed_count = original_count - len(self.tokens)
        if removed_count > 0:
            logger.debug(f"Removed {removed_count} expired tokens")
        return removed_count
    
    def add_cookie(self, name: str, value: Any, domain: str | None = None) -> None:
        """Add or update a cookie."""
        self.cookies[name] = {
            "value": value,
            "domain": domain,
            "timestamp": time.time()
        }
        self.last_activity = time.time()
    
    def update_form_data(
        self, field_key: str, value: Any, is_sensitive: bool = False,
        persist_sensitive: bool = False,
    ) -> None:
        """Update form data, respecting sensitivity."""
        if is_sensitive and not persist_sensitive:
            # Don't store sensitive values, just mark as filled
            self.form_data[field_key] = {"filled": True, "sensitive": True}
        else:
            self.form_data[field_key] = {
                "value": value,
                "filled": True,
                "sensitive": is_sensitive,
                "updated_at": time.time()
            }
        self.last_activity = time.time()
    
    def add_navigation(self, url: str) -> None:
        """Add URL to navigation history."""
        self.navigation_history.append(url)
        self.current_url = url
        self.last_activity = time.time()
        # Keep history manageable
        if len(self.navigation_history) > 100:
            self.navigation_history = self.navigation_history[-100:]
    
    def to_dict(self) -> dict[str, Any]:
        """Convert session state to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "university": self.university,
            "category": self.category,
            "base_url": self.base_url,
            "current_url": self.current_url,
            "tokens": [t.to_dict() for t in self.tokens],
            "cookies": self.cookies,
            "local_storage": self.local_storage,
            "session_storage": self.session_storage,
            "form_data": self.form_data,
            "navigation_history": self.navigation_history,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "is_authenticated": self.is_authenticated,
            "user_agent": self.user_agent,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """Create session state from dictionary."""
        state = cls(
            session_id=data["session_id"],
            university=data["university"],
            category=data["category"],
            base_url=data.get("base_url"),
            current_url=data.get("current_url"),
            tokens=[SessionToken.from_dict(t) for t in data.get("tokens", [])],
            cookies=data.get("cookies", {}),
            local_storage=data.get("local_storage", {}),
            session_storage=data.get("session_storage", {}),
            form_data=data.get("form_data", {}),
            navigation_history=data.get("navigation_history", []),
            created_at=data.get("created_at", time.time()),
            last_activity=data.get("last_activity", time.time()),
            is_authenticated=data.get("is_authenticated", False),
            user_agent=data.get("user_agent"),
        )
        # Clean up expired tokens on load
        state.remove_expired_tokens()
        return state


class SessionManager:
    """Manages browser sessions, including cookies, tokens, and state persistence."""
    
    def __init__(self, storage_dir: str | None = None):
        """Initialize session manager.
        
        Args:
            storage_dir: Directory for persisting session data. If None, uses default.
        """
        self.storage_dir = storage_dir or os.path.join(
            os.path.dirname(__file__), "..", "recordings", "sessions", ".session_cache"
        )
        self._sessions: dict[str, SessionState] = {}
        self._ensure_storage_dir()
    
    def _ensure_storage_dir(self) -> None:
        """Ensure storage directory exists."""
        os.makedirs(self.storage_dir, exist_ok=True)
    
    def _get_session_file_path(self, session_id: str) -> str:
        """Get file path for a session."""
        return os.path.join(self.storage_dir, f"session_{session_id}.json")
    
    def create_session(
        self,
        session_id: str,
        university: dict[str, Any],
        category: str,
        base_url: str | None = None,
    ) -> SessionState:
        """Create a new session.
        
        Args:
            session_id: Unique session identifier
            university: University information dictionary
            category: Session category (e.g., 'AUTH', 'Application')
            base_url: Base URL of the portal
            
        Returns:
            Created SessionState object
        """
        session = SessionState(
            session_id=session_id,
            university=university,
            category=category,
            base_url=base_url,
        )
        self._sessions[session_id] = session
        self.persist_session(session_id)
        logger.info(f"Created session {session_id} for {university.get('University Name', 'Unknown')}")
        return session
    
    def get_session(self, session_id: str) -> SessionState | None:
        """Get a session by ID, loading from disk if needed."""
        # Check in-memory cache first
        if session_id in self._sessions:
            session = self._sessions[session_id]
            # Clean up expired tokens
            session.remove_expired_tokens()
            return session
        
        # Try to load from disk
        session_file = self._get_session_file_path(session_id)
        if os.path.exists(session_file):
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                session = SessionState.from_dict(data)
                self._sessions[session_id] = session
                logger.debug(f"Loaded session {session_id} from disk")
                return session
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning(f"Failed to load session {session_id}: {exc}")
                return None
        
        return None
    
    def update_session(self, session_id: str, **updates: Any) -> SessionState | None:
        """Update session attributes.
        
        Args:
            session_id: Session identifier
            **updates: Attributes to update
            
        Returns:
            Updated SessionState or None if session not found
        """
        session = self.get_session(session_id)
        if session is None:
            return None
        
        for key, value in updates.items():
            if hasattr(session, key):
                setattr(session, key, value)
        
        session.last_activity = time.time()
        self._sessions[session_id] = session
        self.persist_session(session_id)
        return session
    
    def persist_session(self, session_id: str) -> bool:
        """Persist session to disk.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if successful, False otherwise
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        
        try:
            session_file = self._get_session_file_path(session_id)
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(session.to_dict(), f, indent=2, ensure_ascii=False)
            logger.debug(f"Persisted session {session_id} to {session_file}")
            return True
        except OSError as exc:
            logger.error(f"Failed to persist session {session_id}: {exc}")
            return False
    
    def extract_cookies_from_playwright(self, page: Any, session_id: str) -> int:
        """Extract cookies from Playwright page and store in session.
        
        Args:
            page: Playwright page object
            session_id: Session identifier
            
        Returns:
            Number of cookies extracted
        """
        session = self.get_session(session_id)
        if session is None:
            return 0
        
        try:
            cookies = page.context.cookies()
            for cookie in cookies:
                session.add_cookie(
                    name=cookie.get("name", ""),
                    value=cookie.get("value", ""),
                    domain=cookie.get("domain"),
                )
                
                # Also create token for auth-related cookies
                name_lower = cookie.get("name", "").lower()
                if any(pattern in name_lower for pattern in ["token", "auth", "session", "csrf"]):
                    token = SessionToken(
                        name=cookie.get("name", ""),
                        value=cookie.get("value", ""),
                        domain=cookie.get("domain"),
                        path=cookie.get("path", "/"),
                        expires=cookie.get("expires"),
                        http_only=cookie.get("httpOnly", False),
                        secure=cookie.get("secure", False),
                        same_site=cookie.get("sameSite"),
                    )
                    session.add_token(token)
            
            self._sessions[session_id] = session
            self.persist_session(session_id)
            logger.debug(f"Extracted {len(cookies)} cookies from page for session {session_id}")
            return len(cookies)
        except Exception as exc:
            logger.warning(f"Failed to extract cookies: {exc}")
            return 0
    
    def inject_cookies_to_playwright(self, page: Any, session_id: str) -> int:
        """Inject stored cookies into Playwright page.
        
        Args:
            page: Playwright page object
            session_id: Session identifier
            
        Returns:
            Number of cookies injected
        """
        session = self.get_session(session_id)
        if session is None:
            return 0
        
        try:
            cookies_to_inject = []
            for name, cookie_data in session.cookies.items():
                cookie_dict = {
                    "name": name,
                    "value": cookie_data.get("value", ""),
                }
                if cookie_data.get("domain"):
                    cookie_dict["domain"] = cookie_data["domain"]
                if cookie_data.get("path"):
                    cookie_dict["path"] = cookie_data["path"]
                cookies_to_inject.append(cookie_dict)
            
            if cookies_to_inject:
                page.context.add_cookies(cookies_to_inject)
                logger.debug(f"Injected {len(cookies_to_inject)} cookies to page for session {session_id}")
                return len(cookies_to_inject)
            return 0
        except Exception as exc:
            logger.warning(f"Failed to inject cookies: {exc}")
            return 0
    
    def extract_storage_from_playwright(self, page: Any, session_id: str) -> None:
        """Extract localStorage and sessionStorage from Playwright page.
        
        Args:
            page: Playwright page object
            session_id: Session identifier
        """
        session = self.get_session(session_id)
        if session is None:
            return
        
        try:
            # Extract localStorage
            local_storage = page.evaluate("""
                () => {
                    const result = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        result[key] = localStorage.getItem(key);
                    }
                    return result;
                }
            """)
            
            # Get domain for storage key
            domain = page.url.split("/")[2] if page.url else "unknown"
            session.local_storage[domain] = local_storage
            
            # Extract sessionStorage for main frame
            try:
                session_storage = page.evaluate("""
                    () => {
                        const result = {};
                        for (let i = 0; i < sessionStorage.length; i++) {
                            const key = sessionStorage.key(i);
                            result[key] = sessionStorage.getItem(key);
                        }
                        return result;
                    }
                """)
                session.session_storage["main"] = session_storage
            except Exception:
                pass  # sessionStorage might not be available
            
            self._sessions[session_id] = session
            self.persist_session(session_id)
            logger.debug(f"Extracted storage for session {session_id}")
        except Exception as exc:
            logger.debug(f"Failed to extract storage: {exc}")
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            True if deleted, False if not found
        """
        session_file = self._get_session_file_path(session_id)
        
        # Remove from memory
        if session_id in self._sessions:
            del self._sessions[session_id]
        
        # Remove from disk
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Deleted session {session_id}")
                return True
            except OSError as exc:
                logger.error(f"Failed to delete session file: {exc}")
                return False
        
        return False
    
    def cleanup_old_sessions(self, max_age_days: int = 7) -> int:
        """Clean up sessions older than specified age.
        
        Args:
            max_age_days: Maximum age in days
            
        Returns:
            Number of sessions cleaned up
        """
        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
        cleaned = 0
        
        # Find old session files
        for filename in os.listdir(self.storage_dir):
            if not filename.startswith("session_") or not filename.endswith(".json"):
                continue
            
            session_file = os.path.join(self.storage_dir, filename)
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                created_at = data.get("created_at", 0)
                if created_at < cutoff_time:
                    session_id = data.get("session_id", filename)
                    self.delete_session(session_id)
                    cleaned += 1
            except (json.JSONDecodeError, KeyError, OSError):
                # Delete corrupted files
                try:
                    os.remove(session_file)
                    cleaned += 1
                except OSError:
                    pass
        
        logger.info(f"Cleaned up {cleaned} old sessions")
        return cleaned
    
    def get_active_sessions(self) -> list[SessionState]:
        """Get all active (non-expired) sessions.
        
        Returns:
            List of active SessionState objects
        """
        active = []
        for session_id in list(self._sessions.keys()):
            session = self.get_session(session_id)
            if session is not None:
                # Consider session active if there was activity in last 24 hours
                if time.time() - session.last_activity < 24 * 60 * 60:
                    active.append(session)
        return active


# Global session manager instance
_session_manager: SessionManager | None = None


def get_session_manager(storage_dir: str | None = None) -> SessionManager:
    """Get or create the global session manager instance.
    
    Args:
        storage_dir: Optional custom storage directory
        
    Returns:
        SessionManager instance
    """
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(storage_dir)
    return _session_manager
