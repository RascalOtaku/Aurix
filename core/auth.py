"""
Authentication module — multi-user password hashing, session tokens, config persistence.
Config stored in data/auth.json. Uses bcrypt directly.
"""

import json
import os
import secrets
import threading
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import bcrypt
import pyotp

logger = logging.getLogger(__name__)


from core.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402

DEFAULT_PRIVILEGES = {
    "can_use_agent": True,
    "can_use_browser": True,
    "can_use_bash": False,
    "can_use_documents": True,
    "can_use_research": True,
    "can_generate_images": True,
    "can_manage_memory": True,
    "max_messages_per_day": 0,
    "allowed_models": [],
}

# Admins get everything
ADMIN_PRIVILEGES = {k: (True if isinstance(v, bool) else (0 if isinstance(v, int) else [])) for k, v in DEFAULT_PRIVILEGES.items()}

DEFAULT_AUTH_PATH = os.path.join(
    Path(__file__).parent.parent, "data", "auth.json"
)
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# A real bcrypt hash checked when the username does not exist, so an unknown name costs the same ~bcrypt time as a
# wrong password. Without it the login response time told an attacker which usernames are real.
_DUMMY_HASH = _hash_password(secrets.token_hex(16))

# Usernames are ownership keys in the database and appear in paths and logs: keep them to a plain, bounded charset.
_USERNAME_MAX = 64
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-@")


def valid_username(username: str) -> bool:
    u = (username or "").strip().lower()
    return 0 < len(u) <= _USERNAME_MAX and not u.startswith(("-", ".")) and set(u) <= _USERNAME_OK


# Files holding password hashes, TOTP secrets and live session tokens are written owner-only.
_SECRET_FILE_MODE = 0o600

_BACKUP_HASH_PREFIX = "bcrypt:"


def _hash_backup_code(code: str) -> str:
    return _BACKUP_HASH_PREFIX + _hash_password(code.strip().lower())


def _backup_code_matches(stored, code: str) -> bool:
    """Match a presented backup code against a stored one: a bcrypt hash, or a legacy plain-text code (constant-time)."""
    stored = str(stored)
    code = (code or "").strip().lower()
    if stored.startswith(_BACKUP_HASH_PREFIX):
        try:
            return _verify_password(code, stored[len(_BACKUP_HASH_PREFIX):])
        except ValueError:
            return False
    return secrets.compare_digest(stored.lower(), code)


class AuthManager:
    """Manages multi-user password + session-token auth system."""

    def __init__(self, auth_path: str = DEFAULT_AUTH_PATH):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "sessions.json")
        self._config: Dict[str, Any] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> {username, expiry}
        # Guards mutations of self._sessions and the on-disk sessions.json.
        # Validate/create/revoke run concurrently from the FastAPI threadpool.
        self._sessions_lock = threading.RLock()
        self._load()
        self._load_sessions()
        self._migrate_single_user()
        self._migrate_legacy_admin_role()
        self._migrate_plaintext_backup_codes()

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r") as f:
                    self._config = json.load(f)
                logger.info("Auth config loaded")
            else:
                self._config = {}
                logger.info("No auth config found — first-run setup required")
        except Exception as e:
            logger.error(f"Failed to load auth config: {e}")
            self._config = {}

    def _load_sessions(self):
        """Load persisted session tokens from disk, pruning expired ones."""
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r") as f:
                    data = json.load(f)
                now = time.time()
                self._sessions = {k: v for k, v in data.items() if v.get("expiry", 0) > now}
                pruned = len(data) - len(self._sessions)
                if pruned > 0:
                    self._save_sessions()
                logger.info(f"Loaded {len(self._sessions)} session(s) from disk")
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
            self._sessions = {}

    def _save_sessions(self):
        """Persist session tokens to disk (atomic, lock-guarded)."""
        try:
            with self._sessions_lock:
                snapshot = dict(self._sessions)
            _atomic_write_json(self._sessions_path, snapshot, mode=_SECRET_FILE_MODE)
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def _migrate_single_user(self):
        """Migrate old single-user format to multi-user format."""
        if "password_hash" in self._config and "users" not in self._config:
            old_user = self._config.get("username", "admin")
            old_hash = self._config["password_hash"]
            self._config = {
                "users": {
                    old_user: {
                        "password_hash": old_hash,
                        "created": time.time(),
                        "is_admin": True,
                    }
                }
            }
            self._save()
            logger.info(f"Migrated single-user auth to multi-user (admin: {old_user})")

    def _migrate_legacy_admin_role(self):
        """Normalize setup.py's old role='admin' marker to is_admin=True."""
        changed = False
        for username, user in self.users.items():
            if user.get("role") == "admin" and "is_admin" not in user:
                user["is_admin"] = True
                changed = True
                logger.info(f"Migrated legacy admin role for '{username}'")
        if changed:
            self._save()

    def _migrate_plaintext_backup_codes(self):
        """Hash backup codes that older versions stored in plain text. The codes keep working; only the file changes."""
        changed = False
        for username, user in self.users.items():
            codes = user.get("totp_backup_codes")
            if isinstance(codes, list) and any(not str(c).startswith(_BACKUP_HASH_PREFIX) for c in codes):
                user["totp_backup_codes"] = [
                    c if str(c).startswith(_BACKUP_HASH_PREFIX) else _hash_backup_code(str(c)) for c in codes
                ]
                changed = True
                logger.info("Hashed %d stored 2FA backup code(s) for '%s'", len(codes), username)
        if changed:
            self._save()

    def _save(self):
        _atomic_write_json(self.auth_path, self._config, indent=2, mode=_SECRET_FILE_MODE)

    @property
    def users(self) -> Dict[str, Any]:
        return self._config.get("users", {})

    @property
    def signup_enabled(self) -> bool:
        return self._config.get("signup_enabled", False)

    @signup_enabled.setter
    def signup_enabled(self, value: bool):
        self._config["signup_enabled"] = value
        self._save()

    @property
    def is_configured(self) -> bool:
        return len(self.users) > 0

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run admin setup. Only works if no users exist."""
        if self.is_configured:
            return False
        return self.create_user(username, password, is_admin=True)

    def create_user(self, username: str, password: str, is_admin: bool = False) -> bool:
        """Create a new user account."""
        username = username.strip().lower()
        if not valid_username(username) or username in self.users:
            return False
        if "users" not in self._config:
            self._config["users"] = {}
        self._config["users"][username] = {
            "password_hash": _hash_password(password),
            "created": time.time(),
            "is_admin": is_admin,
            "privileges": dict(ADMIN_PRIVILEGES if is_admin else DEFAULT_PRIVILEGES),
        }
        self._save()
        logger.info(f"Created user '{username}' (admin={is_admin})")
        return True

    def delete_user(self, username: str, requesting_user: str) -> bool:
        """Delete a user. Only admins can delete, and can't delete themselves.

        SECURITY: also revoke every active session token belonging to this
        user so any open browser tab they have gets kicked back to /login
        on the next request. Without this the user kept full access until
        their cookie expired naturally (TOKEN_TTL, 7 days).
        """
        username = username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if username not in self.users:
            return False
        if username == requesting_user:
            return False
        if not self.users.get(requesting_user, {}).get("is_admin"):
            return False
        del self._config["users"][username]
        self._save()
        # Purge all sessions belonging to this user. validate_token doesn't
        # cross-check `self.users`, so without this step a deleted user's
        # cookie keeps authenticating.
        revoked = 0
        with self._sessions_lock:
            to_drop = [tok for tok, sess in self._sessions.items()
                       if (sess or {}).get("username") == username]
            for tok in to_drop:
                self._sessions.pop(tok, None)
                revoked += 1
        if revoked:
            self._save_sessions()
        logger.info(f"Deleted user '{username}' (by {requesting_user}); revoked {revoked} active session(s)")
        return True

    def rename_user(self, old_username: str, new_username: str, requesting_user: str) -> bool:
        """Rename a user in auth config and active sessions. Admin only."""
        old_username = old_username.strip().lower()
        new_username = new_username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if not old_username or not valid_username(new_username):
            return False
        if old_username not in self.users:
            return False
        if new_username in self.users:
            return False
        if not self.users.get(requesting_user, {}).get("is_admin"):
            return False
        self._config.setdefault("users", {})[new_username] = self._config["users"].pop(old_username)
        self._save()

        renamed_sessions = 0
        with self._sessions_lock:
            for sess in self._sessions.values():
                if (sess or {}).get("username") == old_username:
                    sess["username"] = new_username
                    renamed_sessions += 1
        if renamed_sessions:
            self._save_sessions()
        logger.info(
            "Renamed user '%s' -> '%s' (by %s); updated %d active session(s)",
            old_username, new_username, requesting_user, renamed_sessions,
        )
        return True

    def is_admin(self, username: str) -> bool:
        return self.users.get(username, {}).get("is_admin", False)

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {"username": u, "is_admin": d.get("is_admin", False), "privileges": self.get_privileges(u)}
            for u, d in self.users.items()
        ]

    def get_privileges(self, username: str) -> Dict[str, Any]:
        """Get privileges for a user. Admins get all privileges."""
        user = self.users.get(username, {})
        if user.get("is_admin"):
            return dict(ADMIN_PRIVILEGES)
        # Merge stored privileges with defaults (in case new privileges were added)
        stored = user.get("privileges", {})
        return {**DEFAULT_PRIVILEGES, **stored}

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> bool:
        """Update privileges for a user. Can't modify admin privileges."""
        username = username.strip().lower()
        if username not in self.users:
            return False
        if self.users[username].get("is_admin"):
            return False  # admins always have full access
        # Only allow known privilege keys
        current = self.get_privileges(username)
        for k, v in privileges.items():
            if k in DEFAULT_PRIVILEGES:
                current[k] = v
        self._config["users"][username]["privileges"] = current
        self._save()
        logger.info(f"Updated privileges for '{username}': {current}")
        return True

    def change_password(self, username: str, current_password: str, new_password: str,
                        keep_token: Optional[str] = None) -> bool:
        """Change a password and sign out every other session of that user.

        A password change is what an owner does after a suspected compromise; leaving the attacker's existing session
        cookies valid for up to TOKEN_TTL defeated it. `keep_token` (the caller's own session) stays signed in.
        """
        username = username.strip().lower()
        if username not in self.users:
            return False
        if not _verify_password(current_password, self.users[username]["password_hash"]):
            return False
        self._config["users"][username]["password_hash"] = _hash_password(new_password)
        self._save()
        self.revoke_user_sessions(username, keep_token=keep_token)
        return True

    def revoke_user_sessions(self, username: str, keep_token: Optional[str] = None) -> int:
        """Drop every session token of `username` except `keep_token`. Returns how many were dropped."""
        username = (username or "").strip().lower()
        with self._sessions_lock:
            to_drop = [tok for tok, sess in self._sessions.items()
                       if (sess or {}).get("username") == username
                       and not (keep_token and secrets.compare_digest(tok, keep_token))]
            for tok in to_drop:
                self._sessions.pop(tok, None)
        if to_drop:
            self._save_sessions()
            logger.info("Revoked %d session(s) for '%s'", len(to_drop), username)
        return len(to_drop)

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        """Check if 2FA is enabled for a user."""
        user = self.users.get(username.strip().lower(), {})
        return bool(user.get("totp_enabled"))

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """Generate a new TOTP secret for a user. Returns the secret (not yet enabled)."""
        username = username.strip().lower()
        if username not in self.users:
            return None
        secret = pyotp.random_base32()
        self._config["users"][username]["totp_secret_pending"] = secret
        self._save()
        return secret

    def totp_get_provisioning_uri(self, username: str, secret: str) -> str:
        """Get the otpauth:// URI for QR code generation."""
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name="Aurix")

    @staticmethod
    def _totp_match_step(secret: str, code: str, window: int = 1) -> Optional[int]:
        """The TOTP time-step `code` is valid for (current step +/- `window`), or None."""
        code = (code or "").strip()
        if not code:
            return None
        totp = pyotp.TOTP(secret)
        now = time.time()
        step = int(totp.timecode(now))
        for offset in range(-window, window + 1):
            if secrets.compare_digest(str(totp.at(now, offset)), code):
                return step + offset
        return None

    def totp_confirm_enable(self, username: str, code: str) -> Optional[List[str]]:
        """Verify a TOTP code against the pending secret, then enable 2FA.

        Returns the new backup codes in plain text (the ONLY time they exist unhashed; show them to the user once),
        or None if the code was wrong."""
        username = username.strip().lower()
        user = self.users.get(username, {})
        secret = user.get("totp_secret_pending")
        if not secret:
            return None
        step = self._totp_match_step(secret, code)
        if step is None:
            return None
        # Enable 2FA
        self._config["users"][username]["totp_secret"] = secret
        self._config["users"][username]["totp_enabled"] = True
        self._config["users"][username]["totp_last_step"] = step  # the confirming code can't be replayed to log in
        self._config["users"][username].pop("totp_secret_pending", None)
        # Backup codes: 40 random bits each, stored only as bcrypt hashes (auth.json used to hold them in plain text,
        # so anyone who could read that file could pass 2FA).
        backup = [secrets.token_hex(5) for _ in range(8)]
        self._config["users"][username]["totp_backup_codes"] = [_hash_backup_code(c) for c in backup]
        self._save()
        logger.info(f"2FA enabled for '{username}'")
        return backup

    def totp_verify(self, username: str, code: str) -> bool:
        """Verify a TOTP code (or a one-time backup code) for login.

        Each TOTP time-step is accepted at most once: a code seen over someone's shoulder, or captured, can't be
        replayed within its ~90 s validity window. Fails closed when 2FA is on but its secret is missing."""
        username = username.strip().lower()
        user = self.users.get(username, {})
        if not user.get("totp_enabled"):
            return True  # 2FA not enabled, always pass
        secret = user.get("totp_secret")
        if not secret:
            logger.error("2FA is enabled for '%s' but no secret is stored: refusing (an admin must reset 2FA)", username)
            return False
        code = (code or "").strip()
        if not code:
            return False
        # TOTP codes are 6 digits; backup codes are 8 (legacy) or 10 hex chars. Only pay the bcrypt checks for the latter.
        backup = list(user.get("totp_backup_codes", []))
        if not (len(code) == 6 and code.isdigit()):
            matched = next((b for b in backup if _backup_code_matches(b, code)), None)
            if matched is None:
                return False
            backup.remove(matched)
            self._config["users"][username]["totp_backup_codes"] = backup
            self._save()
            logger.info(f"Backup code used for '{username}' ({len(backup)} remaining)")
            return True
        step = self._totp_match_step(secret, code)
        if step is None:
            return False
        last = user.get("totp_last_step")
        if isinstance(last, int) and step <= last:
            logger.warning("Replayed or out-of-order 2FA code refused for '%s'", username)
            return False
        self._config["users"][username]["totp_last_step"] = step
        self._save()
        return True

    def totp_disable(self, username: str, password: str) -> bool:
        """Disable 2FA for a user. Requires password confirmation."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return False
        self._config["users"][username].pop("totp_secret", None)
        self._config["users"][username].pop("totp_secret_pending", None)
        self._config["users"][username].pop("totp_backup_codes", None)
        self._config["users"][username]["totp_enabled"] = False
        self._save()
        logger.info(f"2FA disabled for '{username}'")
        return True

    # ------------------------------------------------------------------
    # Login / logout / session tokens
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> bool:
        username = username.strip().lower()
        user = self.users.get(username)
        if not user or not user.get("password_hash"):
            _verify_password(password, _DUMMY_HASH)  # equalise timing: see _DUMMY_HASH
            return False
        return _verify_password(password, user["password_hash"])

    def create_session(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and return a session token, or None."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return None
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[token] = {
                "username": username,
                "expiry": time.time() + TOKEN_TTL,
            }
        self._save_sessions()
        return token

    def create_session_trusted(self, username: str) -> Optional[str]:
        """A session for an EXISTING user, without re-checking the password. Only for a caller that has ALREADY proven
        the identity (the login route, right after verify_password + 2FA): saves a second ~0.2 s bcrypt. Never call
        this with a name taken straight from a request."""
        username = username.strip().lower()
        if username not in self.users:
            return None
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[token] = {"username": username, "expiry": time.time() + TOKEN_TTL}
        self._save_sessions()
        return token

    def validate_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        expired = False
        deleted_user = False
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return False
            if time.time() > session["expiry"]:
                self._sessions.pop(token, None)
                expired = True
            else:
                # SECURITY: if the user record has since been removed (admin
                # deleted them while their cookie was still valid), drop the
                # session so the next request kicks them out instead of
                # silently authenticating against a non-existent account.
                if session.get("username") not in self.users:
                    self._sessions.pop(token, None)
                    deleted_user = True
        if expired or deleted_user:
            self._save_sessions()
            return False
        return True

    def get_username_for_token(self, token: Optional[str]) -> Optional[str]:
        """Return the username associated with a valid token."""
        if not token:
            return None
        expired = False
        deleted_user = False
        with self._sessions_lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if time.time() > session["expiry"]:
                self._sessions.pop(token, None)
                expired = True
            else:
                _u = session["username"]
                # SECURITY: orphan check — same rationale as validate_token.
                if _u not in self.users:
                    self._sessions.pop(token, None)
                    deleted_user = True
                else:
                    return _u
        if expired or deleted_user:
            self._save_sessions()
        return None

    def revoke_token(self, token: str):
        with self._sessions_lock:
            self._sessions.pop(token, None)
        self._save_sessions()

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        username = self.get_username_for_token(token)
        authenticated = username is not None
        result = {
            "configured": self.is_configured,
            "authenticated": authenticated,
            "username": username,
            "is_admin": self.is_admin(username) if username else False,
        }
        if authenticated:
            result["privileges"] = self.get_privileges(username)
        return result
