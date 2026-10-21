"""Admin login: mandatory authentication for the whole UI + API.

Password: auth_password_hash in config.yaml (PBKDF2). The initial value can
come from PUS_AUTH_TOKEN / PUS_ADMIN_PASSWORD (hashed on first start); after
that the password is changed in the web UI only.

If neither is set, the instance is in *setup* mode: every API call answers
401 {"setup_required": true} and the UI shows a "create password" screen.

Browser sessions use a signed, HttpOnly, SameSite=Strict cookie. Scripts can
keep using `Authorization: Bearer <password>`. Failed logins are rate-limited
per client IP (exponential lockout).
"""
import hashlib
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

from . import config as _config
from .config import env_locked, get, save as cfg_save, set as cfg_set

MIN_PASSWORD_LEN = 8

_fail_lock = threading.Lock()
_failures: dict = {}   # ip -> (count, locked_until)


def configured() -> bool:
    return bool(get("auth_password_hash", ""))


def password_from_env() -> bool:
    return False  # env only seeds the first password; the UI owns it afterwards


def check_password(pw: str) -> bool:
    # leading/trailing whitespace from copy & paste is ignored
    pw = (pw or "").strip()
    h = get("auth_password_hash", "")
    return bool(pw) and bool(h) and check_password_hash(h, pw)


def password_version() -> str:
    """Changes whenever the password changes -> old sessions become invalid."""
    src = get("auth_password_hash", "") or ""
    return hashlib.sha256(("pus-session:" + src).encode("utf-8")).hexdigest()[:16]


def set_password(new_pw: str) -> None:
    new_pw = (new_pw or "").strip()
    if len(new_pw) < MIN_PASSWORD_LEN:
        raise _config.ConfigError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    cfg_set("auth_password_hash", generate_password_hash(new_pw))
    cfg_save()


# ------------------------------------------------------------ rate limiting
def lockout_remaining(ip: str) -> int:
    with _fail_lock:
        _, until = _failures.get(ip, (0, 0))
    return max(0, int(until - time.time()))


def record_failure(ip: str) -> None:
    with _fail_lock:
        count, _ = _failures.get(ip, (0, 0))
        count += 1
        # first 4 attempts free, then 2^n seconds (cap: 15 min)
        delay = 0 if count < 5 else min(900, 2 ** (count - 4))
        _failures[ip] = (count, time.time() + delay)


def record_success(ip: str) -> None:
    with _fail_lock:
        _failures.pop(ip, None)


def reset_rate_limits() -> None:
    with _fail_lock:
        _failures.clear()
